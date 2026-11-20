from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx
import orjson
import pytest
from redis.asyncio import Redis
from starlette.types import ASGIApp

from ticketing.core.settings import Settings
from ticketing.telemetry import business
from ticketing.telemetry.app_metrics import Consumer, ingest_intervals, parse_batch
from ticketing.telemetry.collector import sample_connections
from ticketing.telemetry.common import GROUP, STREAM, second
from ticketing.telemetry.histogram import Counts

pytestmark = pytest.mark.integration


def fields(boot: uuid.UUID, number: int, mono: int, wall: datetime) -> dict[str, bytes | str | int]:
    counts = Counts()
    counts.observe(200, 4)
    return {
        "boot_uuid": boot.hex,
        "interval_no": number,
        "mono_ms": mono,
        "wall_ts": wall.isoformat(),
        "payload": orjson.dumps(
            {
                "routes": {"/api/v1/events/{id}/orders": [counts.status_classes, counts.hist]},
                "business": [[27, "hold_ok", 2]],
                "sse_active": number,
            }
        ),
    }


async def test_minimum_delay_dedupe_wall_skew_completeness_and_gauge(
    db: asyncpg.Connection,
    db_url: str,
    settings: Settings,
) -> None:
    now = second(datetime.now(UTC))
    first, last = now - timedelta(seconds=3), now - timedelta(seconds=2)
    boot, missing = uuid.uuid4(), uuid.uuid4()
    await db.execute(
        """INSERT INTO telemetry_producers (boot_uuid, service, started_at, heartbeat_at)
           VALUES ($1, 'api', $2, $3), ($4, 'api', $2, $3)""",
        boot,
        first,
        now,
        missing,
    )
    redis = Redis.from_url(settings.redis_url)
    other = await asyncpg.connect(db_url)
    try:
        await redis.delete(STREAM)
        # The first emission reaches Redis 2.2 seconds late. The second establishes the offset
        # before either is written to PostgreSQL. Both producer wall stamps are 20 seconds slow.
        await redis.xadd(
            STREAM,
            fields(boot, 2, 11_000, last - timedelta(seconds=20)),
            id=f"{int(last.timestamp() * 1000)}-0",
        )
        await redis.xadd(
            STREAM,
            fields(boot, 1, 10_000, first - timedelta(seconds=20)),
            id=f"{int(first.timestamp() * 1000) + 2200}-0",
        )
        entries = await Consumer("first").read(redis)
        await asyncio.gather(
            ingest_intervals(db, parse_batch(entries)),
            ingest_intervals(other, parse_batch(entries)),
        )
        rows = await db.fetch(
            """SELECT ts, req_total, completeness FROM metrics_1s
               WHERE scope = 'all' AND source = 'app' ORDER BY ts"""
        )
        assert [tuple(row) for row in rows] == [(first, 1, 0.5), (last, 1, 0.5)]
        assert await db.fetchval("SELECT sum(hold_ok) FROM business_1s") == 4
        assert await db.fetchval("SELECT count(*) FROM telemetry_ingest") == 2
        producer = await db.fetchrow(
            "SELECT wall_skew_ms, offset_err_ms, latest_sse FROM telemetry_producers "
            "WHERE boot_uuid = $1",
            boot,
        )
        assert tuple(producer) == (20_000, 2200, 2)
        # Claim the original unacknowledged deliveries after a simulated restart.
        await redis.xclaim(
            STREAM,
            GROUP,
            "first",
            0,
            [entry[0] for entry in entries],
            idle=10_000,
        )
        replay = await Consumer("replacement").read(redis)
        assert {entry[0] for entry in replay} == {entry[0] for entry in entries}
        await ingest_intervals(db, parse_batch(replay))
        assert await db.fetchval("SELECT sum(hold_ok) FROM business_1s") == 4
        await sample_connections(db, 30, sampled_at=now)
        gauge = await db.fetchval(
            "SELECT conns_sse FROM metrics_1s WHERE source = 'app' ORDER BY ts DESC LIMIT 1"
        )
        assert gauge == 2
        assert (
            await db.fetchval(
                "SELECT clock_skew_ms FROM metrics_1s WHERE source = 'app' ORDER BY ts DESC LIMIT 1"
            )
            == 20_000
        )
        await sample_connections(db, 30, sampled_at=now + timedelta(seconds=3))
        assert (
            await db.fetchval(
                "SELECT conns_sse FROM metrics_1s WHERE source = 'app' ORDER BY ts DESC LIMIT 1"
            )
            == 0
        )
        assert await redis.xack(STREAM, GROUP, *(entry[0] for entry in replay)) == 2
    finally:
        await redis.delete(STREAM)
        await redis.aclose()
        await other.close()


async def test_api_lifespan_flushes_requests_business_and_sse_gauge(
    client: httpx.AsyncClient,
    asgi_app: ASGIApp,
    settings: Settings,
) -> None:
    asgi_app.fastapi.state.sse_active = 7
    business.record(813, "hold_ok")
    assert (await client.get("/healthz/live")).status_code == 200

    redis = Redis.from_url(settings.redis_url)
    try:
        async with asyncio.timeout(8):
            while True:
                intervals = parse_batch(await redis.xrange(STREAM))
                if any(item.outcomes == [(813, "hold_ok", 1)] for item in intervals) and any(
                    "/healthz/live" in item.routes for item in intervals
                ):
                    break
                await asyncio.sleep(0.1)
        assert any(item.sse == 7 for item in intervals)
        assert (
            sum(
                item.routes["/healthz/live"].status_classes[2]
                for item in intervals
                if "/healthz/live" in item.routes
            )
            == 1
        )
    finally:
        await redis.aclose()
