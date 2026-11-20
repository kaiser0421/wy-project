from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import orjson
import pytest
from redis.asyncio import Redis

from ticketing.core.service import ServiceContext
from ticketing.core.settings import Settings
from ticketing.telemetry.collector import Collector, sample_connections
from ticketing.telemetry.common import second
from ticketing.telemetry.logs import discover, ingest_segment

pytestmark = pytest.mark.integration


def service_line(ts: datetime, seq: int, service: str = "api", **fields: object) -> bytes:
    return (
        orjson.dumps(
            {
                "ts": ts.isoformat(),
                "service": service,
                "level": "info",
                "event": "request.completed",
                "boot_uuid": "a44abee445c948448100a8d1ebb9c7ed",
                "local_seq": seq,
                **fields,
            }
        )
        + b"\n"
    )


def edge_line(ts: datetime, path: str, status: int, elapsed: str, **fields: object) -> bytes:
    return (
        orjson.dumps(
            {
                "ts": str(ts.timestamp()),
                "uri": path,
                "status": status,
                "req_ms": elapsed,
                "upstream_ms": "0.010, 0.030",
                "method": "GET",
                **fields,
            }
        )
        + b"\n"
    )


async def test_checkpoint_rolls_back_then_handles_parallel_replay_rotation_and_truncate(
    db: asyncpg.Connection,
    db_url: str,
    tmp_path: Path,
) -> None:
    settings = Settings(log_dir=str(tmp_path), nginx_log_dir=str(tmp_path / "edge"))
    path = tmp_path / "api-boot.json"
    now = datetime.now(UTC)
    first, second_line = service_line(now, 1), service_line(now, 2)
    path.write_bytes(first + second_line[:-1])
    (segment,) = discover(settings)
    tx = db.transaction()
    await tx.start()
    try:
        assert await ingest_segment(db, segment) == 1
    finally:
        await tx.rollback()
    assert await db.fetchval("SELECT count(*) FROM log_checkpoint") == 0
    assert await db.fetchval("SELECT count(*) FROM service_logs") == 0

    parallel = await asyncpg.connect(db_url)
    try:
        await asyncio.gather(ingest_segment(db, segment), ingest_segment(parallel, segment))
    finally:
        await parallel.close()
    assert await db.fetchval('SELECT "offset" FROM log_checkpoint') == len(first)

    with path.open("ab") as handle:
        handle.write(b"\n")
    path.rename(tmp_path / "api-boot.json.1")
    path.write_bytes(service_line(now, 3))
    for candidate in discover(settings):
        await ingest_segment(db, candidate)

    # The new segment is truncated and regrows past its last offset before the next scan.
    path.write_bytes(service_line(now, 4, message="longer replacement"))
    for candidate in discover(settings):
        await ingest_segment(db, candidate)
    assert [
        r[0] for r in await db.fetch("SELECT local_seq FROM service_logs ORDER BY local_seq")
    ] == [
        1,
        2,
        3,
        4,
    ]
    assert await db.fetchval("SELECT count(*) FROM log_checkpoint") == 2


async def test_edge_event_time_grace_sse_exclusion_and_connection_sample(
    db: asyncpg.Connection,
    tmp_path: Path,
) -> None:
    settings = Settings(nginx_log_dir=str(tmp_path))
    path = tmp_path / "nginx-access.json"
    later = second(datetime.now(UTC)) - timedelta(seconds=5)
    earlier = later - timedelta(seconds=2)
    path.write_bytes(
        edge_line(earlier, "/one", 200, "0.020")
        + edge_line(later, "/two", 503, "1.256")
        + edge_line(earlier, "/late?test=1", 404, "0.040")
        + edge_line(earlier - timedelta(seconds=1), "/too-late", 500, "0.100")
        + edge_line(later, "/api/v1/events/7/seat-stream", 200, "30")
    )
    (segment,) = discover(settings)
    assert await ingest_segment(db, segment) == 5
    assert await ingest_segment(db, segment) == 0
    rows = await db.fetch("SELECT * FROM metrics_1s WHERE source = 'edge' ORDER BY ts")
    assert len(rows) == 2
    assert (rows[0]["req_total"], rows[0]["err_4xx"], rows[0]["p99_ms"]) == (2, 1, 40)
    assert (rows[1]["req_total"], rows[1]["err_5xx"], rows[1]["p99_ms"]) == (1, 1, 1280)
    assert sum(rows[0]["hist"]) == 2
    timing = await db.fetchval("SELECT upstream_ms FROM access_logs WHERE path = '/late'")
    assert timing == 30
    with path.open("ab") as handle:
        handle.write(edge_line(earlier, "/second-batch", 503, "0.080"))
    assert await ingest_segment(db, segment) == 1
    amended = await db.fetchrow(
        "SELECT req_total, err_5xx, p99_ms, hist FROM metrics_1s WHERE ts = $1", earlier
    )
    assert (amended["req_total"], amended["err_5xx"], amended["p99_ms"]) == (3, 1, 80)
    assert sum(amended["hist"]) == 3
    await sample_connections(db, 123)
    assert await db.fetchval("SELECT count(*) FROM metrics_1s WHERE conns_active = 123") == 1


async def test_collector_reaches_active_log_past_many_rotated_segments(
    db: asyncpg.Connection, settings: Settings, tmp_path: Path
) -> None:
    settings = settings.model_copy(update={"nginx_log_dir": str(tmp_path)})
    for rotation in range(35):
        (tmp_path / f"nginx-access.json.{rotation}").touch()
    now = datetime.now(UTC)
    (tmp_path / "nginx-access.json").write_bytes(edge_line(now, "/active", 200, "0.010"))

    redis = Redis.from_url(settings.redis_url)
    try:
        async with asyncpg.create_pool(settings.database_url, min_size=1, max_size=1) as pool:
            collector = Collector(ServiceContext(settings, pool, redis, asyncio.Event()))
            async with asyncio.timeout(5):
                while not await db.fetchval("SELECT count(*) FROM access_logs"):
                    await collector._logs()
            await collector._logs()
    finally:
        await redis.aclose()
    assert await db.fetchval("SELECT count(*) FROM access_logs WHERE path = '/active'") == 1
