from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import asyncpg
import httpx
import orjson
import pytest
from asgi_lifespan import LifespanManager
from starlette.types import ASGIApp, Message

from paymock.delivery import send_due
from paymock.main import create_app as create_paymock
from tests.booking.support import book, inventory
from tests.support import create_user, login
from tests.telemetry.test_logs import edge_line, service_line
from ticketing.api.app import create_asgi_app
from ticketing.core.logging import background_logging
from ticketing.core.service import ServiceContext
from ticketing.core.settings import Settings
from ticketing.payments import reconciler
from ticketing.telemetry.common import second
from ticketing.telemetry.logs import discover, ingest_segment

if TYPE_CHECKING:
    from tests.support import TestClock

pytestmark = pytest.mark.integration

BASE = "/api/v1/admin"
ROUTES = [
    ("GET", "/metrics"),
    ("GET", "/metrics/stream"),
    ("GET", "/alerts"),
    ("GET", "/alerts/stream"),
    ("POST", "/alerts/1/ack"),
    ("GET", "/logs/recent"),
    ("GET", "/logs/access"),
    ("GET", "/requests/request-1/logs"),
]


async def ingest_until(
    db: asyncpg.Connection, settings: Settings, request_id: str, event: str
) -> None:
    async with asyncio.timeout(5):
        while True:
            for segment in discover(settings):
                await ingest_segment(db, segment)
            if await db.fetchval(
                "SELECT 1 FROM service_logs "
                "WHERE request_id = $1 AND event = $2 AND service = 'api' LIMIT 1",
                request_id,
                event,
            ):
                return
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("role", ["user", "viewer"])
async def test_monitoring_routes_require_admin(
    client: httpx.AsyncClient,
    db: asyncpg.Connection,
    role: str,
) -> None:
    for method, path in ROUTES:
        response = await client.request(method, BASE + path)
        assert response.status_code == 401
        assert response.json()["code"] == "unauthenticated"
    await create_user(db, "reader@example.com", role=role)
    session = await login(client, "reader@example.com")
    for method, path in ROUTES:
        response = await client.request(method, BASE + path, headers=session.headers())
        assert response.status_code == 403
        assert response.json()["code"] == "forbidden"


async def test_admin_recent_logs_filter_by_time_service_and_level_and_cap_attrs(
    client: httpx.AsyncClient, db: asyncpg.Connection
) -> None:
    await create_user(db, "admin@example.com", role="admin")
    await login(client, "admin@example.com")
    now = datetime.now(UTC)
    metadata = orjson.dumps(
        {
            "error_type": "TimeoutError",
            "status": 503,
            "detail": "Bearer private-token",
            "path": "/private-token",
            "token": "private-token",
            "nested": {"password": "private-token"},
        }
    ).decode()
    oversized = orjson.dumps({"error_type": "E" * 4096, "status": {"token": "private"}}).decode()
    await db.executemany(
        "INSERT INTO service_logs (ts, service, level, event, request_id, attrs) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        [
            (now + timedelta(minutes=2), "api", "critical", "future", None, None),
            (now - timedelta(seconds=2), "api", "info", "ordinary", None, None),
            (now - timedelta(seconds=3), "api", "warning", "overload", "request-1", metadata),
            (now - timedelta(seconds=4), "worker", "error", "retry", None, oversized),
            (now - timedelta(minutes=2), "api", "error", "timeout", "request-2", None),
            (now - timedelta(minutes=3), "api", "critical", "failed", None, None),
            (now - timedelta(minutes=20), "api", "error", "older", None, None),
            (now - timedelta(minutes=61), "api", "error", "too-old", None, None),
        ],
    )
    path = BASE + "/logs/recent"
    response = await client.get(path)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    rows = response.json()["items"]
    assert [row["event"] for row in rows] == ["overload", "retry", "timeout", "failed"]
    assert rows[0] == {
        "ts": (now - timedelta(seconds=3)).isoformat().replace("+00:00", "Z"),
        "service": "api",
        "level": "warning",
        "event": "overload",
        "request_id": "request-1",
        "attrs": {"error_type": "TimeoutError", "status": 503},
    }
    assert rows[1]["attrs"] == {}

    for params, expected in (
        ({"since_minutes": 1}, ["overload", "retry"]),
        ({"since_minutes": 60, "level": "error"}, ["retry", "timeout", "older"]),
        ({"service": "api", "level": "error", "limit": 1}, ["timeout"]),
        ({"service": "paymock"}, []),
    ):
        response = await client.get(path, params=params)
        assert response.status_code == 200, response.text
        assert [row["event"] for row in response.json()["items"]] == expected

    for key, value in (
        ("service", "nginx"),
        ("level", "info"),
        ("since_minutes", 0),
        ("since_minutes", 61),
        ("limit", 0),
        ("limit", 201),
    ):
        response = await client.get(path, params={key: value})
        assert (response.status_code, response.json()["code"]) == (422, "validation-failed")


async def test_admin_request_trace_returns_linked_entries_in_timestamp_sequence_order(
    client: httpx.AsyncClient,
    db: asyncpg.Connection,
    tmp_path: Path,
) -> None:
    await create_user(db, "admin@example.com", role="admin")
    await login(client, "admin@example.com")
    nginx = tmp_path / "nginx"
    nginx.mkdir()
    settings = Settings(nginx_log_dir=str(nginx), log_dir=str(tmp_path))
    ts = datetime.now(UTC)
    (nginx / "nginx-access.json").write_bytes(
        edge_line(ts, "/api/v1/events/1/orders", 200, "0.100", request_id="request-1")
    )
    (tmp_path / "api-start.json").write_bytes(service_line(ts, 1, request_id="request-1"))
    (tmp_path / "paymock-start.json").write_bytes(
        service_line(ts, 3, "paymock", request_id="request-1", links=["request-linked"])
    )
    (tmp_path / "worker-start.json").write_bytes(
        service_line(ts, 2, "worker", request_id="request-linked", origin_request_id="request-1")
    )
    for segment in discover(settings):
        await ingest_segment(db, segment)

    path = BASE + "/requests/request-1/logs"
    response = await client.get(path)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    trace = response.json()
    assert trace["linked_request_ids"] == ["request-linked"]
    assert trace["access_log"]["req_ms"] == 100
    assert trace["ingest_lag_ms"] >= 0
    assert [row["service"] for row in trace["service_logs"]] == ["api", "worker", "paymock"]
    primary = await client.get(path, params={"include_linked": "false"})
    assert [row["service"] for row in primary.json()["service_logs"]] == ["api", "paymock"]
    missing = await client.get(BASE + "/requests/missing/logs")
    assert (missing.status_code, missing.json()["code"]) == (404, "not-found")
    assert missing.headers["cache-control"] == "private, no-store"


async def test_admin_access_logs_filter_persisted_rows(
    client: httpx.AsyncClient, db: asyncpg.Connection, tmp_path: Path
) -> None:
    await create_user(db, "admin@example.com", role="admin")
    await login(client, "admin@example.com")
    now = datetime.now(UTC)
    path = tmp_path / "nginx-access.json"
    path.write_bytes(
        b"".join(
            edge_line(
                now + timedelta(seconds=offset), uri, status, "0.010", request_id=rid, method=method
            )
            for offset, uri, status, rid, method in [
                (5, "/api/example", 200, "future", "GET"),
                (-1, "/api/example?secret=private", 201, "recent-1", "POST"),
                (-2, "/api/example/redirect", 307, "recent-2", "GET"),
                (-3, "/api/example", 403, "recent-3", "GET"),
                (-4, "/api/_literal", 503, "recent-4", "GET"),
                (-5, "/api/Xliteral", 500, "recent-5", "GET"),
                (-120, "/healthz/live", 200, "older-1", "GET"),
                (-1200, "/api/example", 204, "older-2", "GET"),
                (-3660, "/api/example", 200, "too-old", "GET"),
            ]
        )
    )
    (segment,) = discover(Settings(nginx_log_dir=str(tmp_path), log_dir=None))
    assert await ingest_segment(db, segment) == 9
    endpoint = BASE + "/logs/access"
    for params, expected in (
        ({}, ["recent-1", "recent-2", "recent-3", "recent-4", "recent-5", "older-1"]),
        ({"since_minutes": 1, "status_class": "2xx", "method": "post"}, ["recent-1"]),
        ({"status_class": "3xx", "path_prefix": "/api/example"}, ["recent-2"]),
        ({"status_class": "4xx", "request_id": "recent-3"}, ["recent-3"]),
        ({"status_class": "5xx", "path_prefix": "/api/_"}, ["recent-4"]),
        ({"status_class": "5xx", "request_id": "recent-3"}, []),
        (
            {"since_minutes": 60, "limit": 200, "status_class": "2xx"},
            ["recent-1", "older-1", "older-2"],
        ),
        ({"limit": 1}, ["recent-1"]),
    ):
        response = await client.get(endpoint, params=params)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "private, no-store"
        assert [row["request_id"] for row in response.json()["items"]] == expected
        assert "private" not in response.text
    for key, value in (
        ("since_minutes", 0),
        ("since_minutes", 61),
        ("limit", 0),
        ("limit", 201),
        ("status_class", "1xx"),
        ("method", "GET POST"),
        ("path_prefix", "\x00"),
        ("request_id", "bad id"),
    ):
        response = await client.get(endpoint, params={key: value})
        assert (response.status_code, response.json()["code"]) == (422, "validation-failed")


async def test_admin_trace_ingests_actual_api_completion(
    db: asyncpg.Connection, settings: Settings, tmp_path: Path
) -> None:
    edge = tmp_path / "nginx"
    edge.mkdir()
    settings = settings.model_copy(update={"log_dir": str(tmp_path), "nginx_log_dir": str(edge)})
    request_id = "trace-request-1"
    admin_id = await create_user(db, "admin@example.com", role="admin")
    app = create_asgi_app(settings)
    async with (
        LifespanManager(app) as manager,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=manager.app), base_url="http://testserver"
        ) as client,
    ):
        await login(client, "admin@example.com")
        path = f"{BASE}/users/{admin_id}"
        response = await client.get(path, headers={"X-Request-ID": request_id})
        assert response.status_code == 200
        assert response.headers["X-Request-ID"] == request_id
        (edge / "nginx-access.json").write_bytes(
            edge_line(datetime.now(UTC), path, 200, "0.010", request_id=request_id)
        )
        await ingest_until(db, settings, request_id, "request.completed")
        response = await client.get(f"{BASE}/requests/{request_id}/logs")
    assert response.status_code == 200, response.text
    trace = response.json()
    assert trace["access_log"]["request_id"] == request_id
    (entry,) = trace["service_logs"]
    assert (entry["service"], entry["level"], entry["event"]) == (
        "api",
        "info",
        "request.completed",
    )
    assert entry["request_id"] == request_id
    assert entry["op_id"]
    assert entry["attrs"] == {
        "method": "GET",
        "route": "/api/v1/admin/users/{user_id}",
        "status": 200,
        "duration_ms": entry["attrs"]["duration_ms"],
        "user_id": admin_id,
    }
    assert entry["attrs"]["duration_ms"] >= 0


async def test_admin_request_trace_returns_paymock_processing_and_delivery(
    db: asyncpg.Connection,
    settings: Settings,
    test_clock: TestClock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs = tmp_path / "logs"
    edge = tmp_path / "nginx"
    edge.mkdir()
    settings = settings.model_copy(update={"log_dir": str(logs), "nginx_log_dir": str(edge)})
    secret = settings.payment_webhook_secret.get_secret_value()
    provider_token = settings.payment_provider_token.get_secret_value()
    monkeypatch.setenv("LOG_DIR", str(logs))
    monkeypatch.setenv("PAYMOCK_STORE_PATH", str(tmp_path / "paymock.sqlite3"))
    monkeypatch.setenv("PAYMENT_WEBHOOK_SECRET", secret)
    monkeypatch.setenv("PAYMENT_PROVIDER_TOKEN", provider_token)
    monkeypatch.setenv("PAYMOCK_CALLBACK_URL", "http://api/api/v1/payments/callbacks")
    monkeypatch.setenv("PAYMOCK_DROP_CALLBACKS", "1")
    monkeypatch.setenv("PAYMOCK_DELAY_CALLBACK_S", "0")
    merchant_request = "merchant-request-1"
    checkout_request = "checkout-request-1"
    paymock = create_paymock()
    api = create_asgi_app(settings)
    admin_id = await create_user(db, "admin@example.com", role="admin")
    event_id, seats = await inventory(db, admin_id, 1)

    async with (
        LifespanManager(api) as manager,
        LifespanManager(paymock),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=manager.app), base_url="http://api"
        ) as client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=paymock), base_url="http://paymock"
        ) as provider,
    ):
        session = await login(client, "admin@example.com")
        booking = await book(session, event_id, "trace-booking", seat_ids=seats)
        assert booking.status_code == 201, booking.text
        order_id = booking.json()["public_id"]
        state = api.fastapi.state  # type: ignore[attr-defined]
        http = state.http
        monkeypatch.setattr(http, "_transport", httpx.ASGITransport(app=paymock))
        payment_path = f"/api/v1/orders/{order_id}/payments"
        created = await client.post(
            payment_path,
            headers={
                **session.headers(),
                "Idempotency-Key": "trace-payment",
                "X-Request-ID": merchant_request,
            },
        )
        assert created.status_code == 201, created.text
        monkeypatch.setattr(
            reconciler,
            "create_http_client",
            lambda base: httpx.AsyncClient(
                base_url=base,
                transport=httpx.ASGITransport(app=paymock),
                event_hooks=http.event_hooks,
            ),
        )
        await test_clock.advance(timedelta(seconds=16))
        async with background_logging("worker", "INFO", str(logs)):
            await reconciler.tick(ServiceContext(settings, state.db, state.redis, asyncio.Event()))

        checkout = urlsplit(created.json()["checkout_url"]).path
        token = checkout.rsplit("/", 1)[-1]
        confirmed = await provider.post(
            f"{checkout}/confirm?test=private-query",
            headers={"X-Request-ID": checkout_request},
        )
        assert confirmed.status_code == 200
        await send_due(client, paymock.state.store, secret, False, paymock.state.service_log)
        (edge / "nginx-access.json").write_bytes(
            edge_line(
                datetime.now(UTC),
                payment_path,
                201,
                "0.010",
                request_id=merchant_request,
                method="POST",
            )
        )
        await ingest_until(db, settings, checkout_request, "request.completed")
        merchant_trace = await client.get(f"{BASE}/requests/{merchant_request}/logs")
        checkout_trace = await client.get(f"{BASE}/requests/{checkout_request}/logs")

    assert merchant_trace.status_code == 200, merchant_trace.text
    trace = merchant_trace.json()
    assert trace["access_log"]["status"] == 201
    assert trace["linked_request_ids"] == [checkout_request]
    entries = trace["service_logs"]
    assert {(row["service"], row["event"], row["request_id"]) for row in entries} == {
        ("api", "request.completed", merchant_request),
        ("api", "payment.intent_created", merchant_request),
        ("paymock", "request.completed", merchant_request),
        ("worker", "payment.reconcile_started", merchant_request),
        ("api", "request.completed", checkout_request),
        ("api", "payment.callback_applied", checkout_request),
        ("paymock", "request.completed", checkout_request),
        ("paymock", "callback.delivery", checkout_request),
    }
    assert checkout_trace.status_code == 200, checkout_trace.text
    assert checkout_trace.json()["linked_request_ids"] == [merchant_request]
    assert any(
        row["service"] == "paymock" and row["attrs"].get("method") == "GET" for row in entries
    )
    assert any(row["attrs"].get("path") == "/pay/{intent_id}/confirm" for row in entries)
    assert any(
        row["event"] == "callback.delivery" and row["attrs"]["status"] == 200 for row in entries
    )
    for segment in discover(settings):
        raw = segment.path.read_text()
        assert all(value not in raw for value in (token, "private-query", provider_token))


async def test_concurrent_ack_keeps_first_admin_and_single_audit_entry(
    make_client: Callable[[], httpx.AsyncClient],
    db: asyncpg.Connection,
) -> None:
    await create_user(db, "one@example.com", role="admin")
    await create_user(db, "two@example.com", role="admin")
    incident = await db.fetchval(
        """INSERT INTO alert_events (rule_id, state) VALUES ('EDGE_5XX', 'firing') RETURNING id"""
    )
    async with make_client() as first, make_client() as second:
        one, two = await asyncio.gather(
            login(first, "one@example.com"), login(second, "two@example.com")
        )
        path = f"{BASE}/alerts/{incident}/ack"
        ack = await asyncio.gather(
            first.post(path, headers=one.headers()), second.post(path, headers=two.headers())
        )
        assert [response.status_code for response in ack] == [200, 200]
        assert ack[0].json() == ack[1].json()
        assert ack[0].json()["ack_by"] in {one.user_id, two.user_id}
        alerts = await first.get(BASE + "/alerts", params={"state": "firing"})
        assert alerts.json()["items"] == [ack[0].json()]
    assert await db.fetchval("SELECT count(*) FROM audit_log WHERE action = 'alert.ack'") == 1


async def first_frame(app: ASGIApp, path: str, cookie: str) -> tuple[dict[str, str], bytes]:
    disconnected = asyncio.Event()
    headers: dict[str, str] = {}
    frame = b""

    async def receive() -> Message:
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        nonlocal frame
        if message["type"] == "http.response.start":
            assert message["status"] == 200
            headers.update((key.decode(), value.decode()) for key, value in message["headers"])
        else:
            frame = message["body"]
            disconnected.set()

    async with asyncio.timeout(5):
        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "method": "GET",
                "scheme": "http",
                "path": path,
                "root_path": "",
                "query_string": b"",
                "headers": [(b"cookie", cookie.encode())],
                "server": ("testserver", 80),
                "client": ("127.0.0.1", 20000),
            },
            receive,
            send,
        )
    return headers, frame


async def test_admin_metric_history_streams_and_openapi(
    client: httpx.AsyncClient,
    db: asyncpg.Connection,
    running_app: ASGIApp,
) -> None:
    await create_user(db, "admin@example.com", role="admin")
    await login(client, "admin@example.com")
    start = second(datetime.now(UTC)) - timedelta(seconds=3)
    for source in ("edge", "app"):
        await db.execute(
            """INSERT INTO metrics_1s (ts, source, req_total, err_4xx, err_5xx, completeness)
               SELECT ts, $2, 10, 1, 1, 0.5 FROM generate_series(
                   $1::timestamptz, $1::timestamptz + interval '60 seconds', interval '1 second'
               ) AS ticks(ts)""",
            start,
            source,
        )
    await db.execute(
        """INSERT INTO business_1s (ts, event_id, hold_ok)
           SELECT ts, 98, 8 FROM metrics_1s WHERE source = 'app';
           INSERT INTO alert_events (rule_id, scope, state)
           VALUES ('SERVICE_DOWN', 'relay', 'firing')"""
    )
    response = await client.get(
        BASE + "/metrics",
        params={
            "from": start.isoformat(),
            "to": (start + timedelta(seconds=1)).isoformat(),
            "source": "edge",
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    (item,) = response.json()["items"]
    assert item["req_total"] == 10
    assert item["error_rate"] == 0.1  # 5xx only; 4xx are client/business outcomes

    cookie = f"sid={client.cookies['sid']}"
    headers, frame = await first_frame(running_app, BASE + "/metrics/stream", cookie)
    assert headers["content-type"].startswith("text/event-stream")
    assert headers["cache-control"] == "private, no-store"
    data = orjson.loads(frame.split(b"\n")[1].removeprefix(b"data: "))
    assert data["edge"]["req_total"] == data["app"]["req_total"] == 10
    assert data["business"][0]["hold_ok"] == 8
    assert data["complete"] is False
    _, frame = await first_frame(running_app, BASE + "/alerts/stream", cookie)
    data = orjson.loads(frame.split(b"\n")[1].removeprefix(b"data: "))
    assert data["items"][0]["scope"] == "relay"

    schema = (await client.get("/api/openapi.json")).json()
    for method, path in ROUTES:
        documented = path.replace("/alerts/1/ack", "/alerts/{id}/ack").replace(
            "/requests/request-1/logs", "/requests/{request_id}/logs"
        )
        operation = schema["paths"][BASE + documented][method.lower()]
        assert operation["summary"]
        assert operation["description"]
        for status in ("401", "403", "503"):
            assert operation["responses"][status]["content"]["application/problem+json"]["examples"]
    assert "MetricFrame" in schema["components"]["schemas"]


async def test_metric_error_rate_excludes_4xx(
    client: httpx.AsyncClient,
    db: asyncpg.Connection,
) -> None:
    await create_user(db, "admin@example.com", role="admin")
    await login(client, "admin@example.com")
    start = second(datetime.now(UTC)) - timedelta(seconds=10)
    await db.execute(
        """INSERT INTO metrics_1s (ts, source, req_total, err_4xx, err_5xx) VALUES
           ($1::timestamptz, 'edge', 10, 0, 1),
           ($1::timestamptz + interval '1 second', 'edge', 10, 4, 1),
           ($1::timestamptz + interval '2 seconds', 'edge', 10, 4, 0)""",
        start,
    )
    response = await client.get(
        BASE + "/metrics",
        params={
            "from": start.isoformat(),
            "to": (start + timedelta(seconds=3)).isoformat(),
            "source": "edge",
        },
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["err_4xx"] for item in items] == [0, 4, 4]
    assert [item["error_rate"] for item in items] == [0.1, 0.1, 0.0]
