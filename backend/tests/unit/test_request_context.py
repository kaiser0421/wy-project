"""Request-id binding, propagation to outbound calls, and problem+json on every error path."""

from __future__ import annotations

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from structlog.testing import capture_logs

from ticketing.api.middleware import RequestContextMiddleware
from ticketing.api.problems import install_problem_handlers
from ticketing.core.context import request_id_var
from ticketing.core.errors import ProblemError
from ticketing.core.http import create_http_client


def _app(outbound: list[httpx.Request]) -> RequestContextMiddleware:
    app = FastAPI()
    install_problem_handlers(app)

    @app.get("/ok")
    async def ok() -> dict[str, str | None]:
        return {"rid": request_id_var.get()}

    @app.get("/problem")
    async def problem() -> None:
        raise ProblemError(409, "seat-unavailable", "Seat unavailable", retryable=False)

    @app.get("/db-timeout")
    async def db_timeout() -> None:
        raise asyncpg.QueryCanceledError("canceling statement due to statement timeout")

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret-internal-detail")

    @app.post("/private/{item_id}")
    async def private(item_id: str) -> None:
        raise RuntimeError(item_id)

    @app.get("/healthz/live")
    async def live() -> None:
        pass

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        return StreamingResponse(iter([": keep-alive\n\n"]), media_type="text/event-stream")

    @app.get("/outbound")
    async def call_outbound() -> dict[str, str]:
        def handler(request: httpx.Request) -> httpx.Response:
            outbound.append(request)
            return httpx.Response(200)

        client = create_http_client("http://provider")
        client._transport = httpx.MockTransport(handler)
        async with client:
            await client.get("/v1/ping")
        return {"status": "sent"}

    return RequestContextMiddleware(app)


@pytest.fixture
def outbound() -> list[httpx.Request]:
    return []


@pytest.fixture
async def app_client(outbound: list[httpx.Request]) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=_app(outbound), raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://t")


async def test_incoming_request_id_is_bound_and_echoed(app_client: httpx.AsyncClient) -> None:
    rid = "abcdef0123456789abcdef0123456789"
    response = await app_client.get("/ok", headers={"X-Request-ID": rid})
    assert response.headers["x-request-id"] == rid
    assert response.json() == {"rid": rid}


async def test_invalid_request_id_is_replaced(app_client: httpx.AsyncClient) -> None:
    response = await app_client.get("/ok", headers={"X-Request-ID": "bad id\n<script>"})
    rid = response.headers["x-request-id"]
    assert rid != "bad id\n<script>"
    assert len(rid) == 32


async def test_problem_error_is_rfc9457(app_client: httpx.AsyncClient) -> None:
    response = await app_client.get("/problem", headers={"X-Request-ID": "r" * 32})
    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == "seat-unavailable"
    assert body["type"].endswith("/seat-unavailable")
    assert body["request_id"] == "r" * 32
    assert body["instance"] == "/problem"
    assert body["retryable"] is False


async def test_unhandled_error_keeps_request_id(app_client: httpx.AsyncClient) -> None:
    with capture_logs() as logs:
        response = await app_client.get("/boom", headers={"X-Request-ID": "e" * 32})
    assert logs, "the unhandled error must be logged"
    assert "secret-internal-detail" not in repr(logs)  # message never reaches persistent logs
    assert any(entry.get("error_type") == "RuntimeError" for entry in logs)
    assert response.status_code == 500
    assert response.headers["x-request-id"] == "e" * 32
    body = response.json()
    assert body["code"] == "internal-error"
    assert body["request_id"] == "e" * 32
    assert "secret-internal-detail" not in response.text  # no internal details leak


async def test_request_completion_records_only_safe_fields(app_client: httpx.AsyncClient) -> None:
    with capture_logs() as logs:
        response = await app_client.post(
            "/private/private-path?token=private-query",
            content="private-body",
            headers={
                "Authorization": "Bearer private-token",
                "Cookie": "sid=private-cookie",
                "X-CSRF-Token": "private-csrf",
            },
        )
    assert response.status_code == 500
    (entry,) = [item for item in logs if item["event"] == "request.completed"]
    assert entry == {
        "event": "request.completed",
        "log_level": "info",
        "method": "POST",
        "route": "/private/{item_id}",
        "status": 500,
        "duration_ms": entry["duration_ms"],
    }
    assert entry["duration_ms"] >= 0
    assert "private-" not in repr(logs)


async def test_probes_and_sse_keep_alives_do_not_log_completions(
    app_client: httpx.AsyncClient,
) -> None:
    with capture_logs() as logs:
        for path in ("/healthz/live", "/stream"):
            assert (await app_client.get(path)).status_code == 200
    assert not logs


async def test_database_back_pressure_is_retryable_503(app_client: httpx.AsyncClient) -> None:
    response = await app_client.get("/db-timeout")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    body = response.json()
    assert body["code"] == "server-busy"
    assert body["retryable"] is True
    assert "statement timeout" not in response.text


async def test_not_found_is_problem_json(app_client: httpx.AsyncClient) -> None:
    response = await app_client.get("/nope")
    assert response.status_code == 404
    assert response.json()["code"] == "not-found"


async def test_outbound_calls_carry_request_id(
    app_client: httpx.AsyncClient, outbound: list[httpx.Request]
) -> None:
    rid = "0" * 31 + "1"
    await app_client.get("/outbound", headers={"X-Request-ID": rid})
    assert outbound
    assert outbound[0].headers["x-request-id"] == rid
    assert outbound[0].headers.get("x-parent-op")
