from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import httpx
import orjson
import pytest
from starlette.types import ASGIApp, Message

from tests.support import create_user, login
from tests.telemetry.test_logs import edge_line, service_line
from ticketing.core.settings import Settings
from ticketing.telemetry.common import second
from ticketing.telemetry.logs import discover, ingest_segment

pytestmark = pytest.mark.integration

BASE = "/api/v1/admin"
ROUTES = [
    ("GET", "/metrics"),
    ("GET", "/metrics/stream"),
    ("GET", "/alerts"),
    ("GET", "/alerts/stream"),
    ("POST", "/alerts/1/ack"),
    ("GET", "/requests/request-1/logs"),
]


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
    trace = response.json()
    assert trace["linked_request_ids"] == ["request-linked"]
    assert trace["access_log"]["req_ms"] == 100
    assert trace["ingest_lag_ms"] >= 0
    assert [row["service"] for row in trace["service_logs"]] == ["api", "worker", "paymock"]
    primary = await client.get(path, params={"include_linked": "false"})
    assert [row["service"] for row in primary.json()["service_logs"]] == ["api", "paymock"]
    missing = await client.get(BASE + "/requests/missing/logs")
    assert (missing.status_code, missing.json()["code"]) == (404, "not-found")


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
    (item,) = response.json()["items"]
    assert item["req_total"] == 10
    assert item["error_rate"] == 0.1  # 5xx only; 4xx are client/business outcomes

    cookie = f"sid={client.cookies['sid']}"
    headers, frame = await first_frame(running_app, BASE + "/metrics/stream", cookie)
    assert headers["content-type"].startswith("text/event-stream")
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
