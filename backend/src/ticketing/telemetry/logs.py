"""File-spool ingestion. The opened inode, not its current name, owns the cursor."""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import stat
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

import asyncpg
import orjson

from ticketing.core.logging import get_logger
from ticketing.core.settings import Settings
from ticketing.telemetry.common import is_stream, second
from ticketing.telemetry.histogram import Counts
from ticketing.telemetry.metrics import MetricKey, write_metrics

log = get_logger(__name__)
MAX_ROWS = 2_000
MAX_LINE = 256 * 1024
MAX_READ = 4 * 1024 * 1024
ANCHOR_BYTES = 64
EDGE_GRACE = timedelta(seconds=2)
EDGE_COLUMNS = (
    "ts",
    "request_id",
    "method",
    "path",
    "status",
    "req_ms",
    "upstream_ms",
    "upstream",
    "bytes",
    "load_run",
    "user_agent",
)
SERVICE_COLUMNS = (
    "ts",
    "request_id",
    "service",
    "boot_uuid",
    "op_id",
    "parent_op_id",
    "local_seq",
    "level",
    "event",
    "attrs",
)


@dataclass(frozen=True, slots=True)
class Segment:
    path: Path
    device: int
    inode: int
    edge: bool


@dataclass(slots=True)
class Batch:
    rows: list[tuple[Any, ...]]
    offset: int
    anchor: bytes
    discarding: bool
    rejected: int


def discover(settings: Settings) -> list[Segment]:
    segments: list[Segment] = []
    seen: set[tuple[int, int]] = set()
    for directory, edge in ((settings.nginx_log_dir, True), (settings.log_dir, False)):
        if not directory:
            continue
        paths = Path(directory).glob("nginx-access.json*" if edge else "*.json*")
        # Natural mtime order preserves the edge watermark when renamed segments are drained.
        candidates: list[tuple[int, Segment]] = []
        for path in paths:
            if path.suffix in {".gz", ".zst", ".bz2", ".xz", ".zip"}:
                continue
            try:
                info = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            key = (info.st_dev, info.st_ino)
            if stat.S_ISREG(info.st_mode) and key not in seen:
                seen.add(key)
                candidates.append((info.st_mtime_ns, Segment(path, *key, edge)))
        segments.extend(segment for _, segment in sorted(candidates, key=lambda item: item[0]))
    return segments


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and "\x00" not in value and value != "-" else None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    parsed = int(value) if isinstance(value, (int, str)) else None
    return parsed if parsed is not None and 0 <= parsed < 2**63 else None


def _seconds_ms(value: Any) -> float | None:
    if value is None or value == "-":
        return None
    number = float(value) * 1000
    return number if math.isfinite(number) and 0 <= number <= 2**31 else None


def _parse_edge(data: dict[str, Any]) -> tuple[Any, ...]:
    ts = datetime.fromtimestamp(float(data["ts"]), UTC)
    path = (_text(data.get("uri")) or "").partition("?")[0]
    status = _integer(data.get("status"))
    elapsed = _seconds_ms(data.get("req_ms"))
    if not path or status is None or not 100 <= status <= 599 or elapsed is None:
        raise ValueError("invalid access record")
    # nginx lists retries in order. Store the final timed upstream attempt in the scalar field.
    raw_upstream = _text(data.get("upstream_ms")) or "-"
    upstream_times = [
        _seconds_ms(value)
        for value in raw_upstream.replace(":", ",").split(",")
        if value.strip() not in {"", "-"}
    ]
    return (
        ts,
        _text(data.get("request_id")),
        _text(data.get("method")),
        path,
        status,
        elapsed,
        next((value for value in reversed(upstream_times) if value is not None), None),
        _text(data.get("upstream")),
        _integer(data.get("bytes")),
        _text(data.get("load_run")),
        _text(data.get("ua")),
    )


def _parse_service(data: dict[str, Any]) -> tuple[Any, ...]:
    ts = datetime.fromisoformat(data["ts"])
    service = _text(data.get("service"))
    if ts.tzinfo is None or not service:
        raise ValueError("invalid service record")
    boot_uuid = _text(data.get("boot_uuid"))
    fields = {
        "ts",
        "request_id",
        "service",
        "boot_uuid",
        "op_id",
        "parent_op_id",
        "local_seq",
        "level",
        "event",
        "attrs",
    }
    nested = data.get("attrs")
    attrs = dict(nested) if isinstance(nested, dict) else {}
    attrs.update((key, value) for key, value in data.items() if key not in fields)
    packed = orjson.dumps(attrs)
    if b"\\u0000" in packed:
        raise ValueError("invalid JSON text for PostgreSQL")
    return (
        ts,
        _text(data.get("request_id")),
        service,
        uuid.UUID(boot_uuid) if boot_uuid else None,
        _text(data.get("op_id")),
        _text(data.get("parent_op_id")),
        _integer(data.get("local_seq")),
        _text(data.get("level")),
        _text(data.get("event")),
        packed.decode(),
    )


def read_batch(
    segment: Segment, offset: int, anchor: bytes | None, discarding: bool
) -> Batch | None:
    """Leave a partial normal record unread until its writer appends a newline."""
    try:
        handle = segment.path.open("rb")
    except FileNotFoundError:
        return None
    with handle:
        info = os.fstat(handle.fileno())
        if (info.st_dev, info.st_ino) != (segment.device, segment.inode):
            return None
        if info.st_size < offset or (anchor is not None and _anchor(handle, offset) != anchor):
            offset, discarding = 0, False
        start = offset
        handle.seek(offset)
        rows: list[tuple[Any, ...]] = []
        rejected = 0
        deadline = time.monotonic() + 0.25
        for _ in range(MAX_ROWS):
            if offset - start >= MAX_READ or time.monotonic() >= deadline:
                break
            line = handle.readline(MAX_LINE + 1)
            if not line:
                break
            complete = line.endswith(b"\n")
            if not (complete or discarding or len(line) > MAX_LINE):
                break
            offset = handle.tell()
            if discarding or len(line) > MAX_LINE:
                rejected += int(not discarding)
                discarding = not complete
                continue
            try:
                data = orjson.loads(line)
                if not isinstance(data, dict):
                    raise ValueError("invalid record")
                rows.append(_parse_edge(data) if segment.edge else _parse_service(data))
            except (ValueError, TypeError, KeyError, OverflowError, OSError):
                rejected += 1
        if offset == start == 0 and anchor is None:
            return None
        return Batch(rows, offset, _anchor(handle, offset), discarding, rejected)


def _anchor(handle: BinaryIO, offset: int) -> bytes:
    # Hash bytes already acknowledged at each end of the consumed prefix. A
    # truncate-and-regrow can keep the same inode and exceed the previous offset.
    digest = hashlib.blake2b(digest_size=16)
    handle.seek(0)
    digest.update(handle.read(min(offset, ANCHOR_BYTES)))
    start = max(0, offset - ANCHOR_BYTES)
    handle.seek(start)
    digest.update(handle.read(offset - start))
    return digest.digest()


async def _rollup_edge(conn: asyncpg.Connection, rows: list[tuple[Any, ...]]) -> None:
    if not rows:
        return
    await conn.execute(
        """INSERT INTO telemetry_edge_watermark (latest) VALUES ($1)
           ON CONFLICT DO NOTHING""",
        rows[0][0],
    )
    watermark = await conn.fetchval(
        "SELECT latest FROM telemetry_edge_watermark WHERE singleton FOR UPDATE"
    )
    aggregates: dict[MetricKey, Counts] = {}
    for ts, _, _, path, status, elapsed, *_ in rows:
        bucket = second(ts)
        if bucket < second(watermark) - EDGE_GRACE:
            continue
        watermark = max(watermark, ts)
        if not is_stream(path):
            counts = aggregates.setdefault((bucket, "edge", "all"), Counts())
            counts.observe(status, elapsed)
    await write_metrics(conn, aggregates)
    await conn.execute("UPDATE telemetry_edge_watermark SET latest = $1 WHERE singleton", watermark)


async def ingest_segment(conn: asyncpg.Connection, segment: Segment) -> int:
    """Serialize readers of one inode and commit the cursor with both durable outputs."""
    async with conn.transaction():
        locked = await conn.fetchval(
            "SELECT pg_try_advisory_xact_lock(hashtextextended($1, 7102))",
            f"{segment.device}:{segment.inode}",
        )
        if not locked:
            return 0
        checkpoint = await conn.fetchrow(
            """SELECT device, path, "offset", anchor, discarding FROM log_checkpoint
               WHERE inode = $2 AND
                 (device = $1 OR (device = 0 AND path = $3))
               ORDER BY device DESC LIMIT 1 FOR UPDATE""",
            segment.device,
            segment.inode,
            str(segment.path),
        )
        batch = await asyncio.to_thread(
            read_batch,
            segment,
            checkpoint["offset"] if checkpoint else 0,
            checkpoint["anchor"] if checkpoint else None,
            checkpoint["discarding"] if checkpoint else False,
        )
        if batch is None:
            return 0
        if (
            checkpoint
            and not batch.rows
            and (batch.offset, batch.anchor, batch.discarding, str(segment.path), segment.device)
            == (
                checkpoint["offset"],
                checkpoint["anchor"],
                checkpoint["discarding"],
                checkpoint["path"],
                checkpoint["device"],
            )
        ):
            return 0
        now: datetime = await conn.fetchval("SELECT clock_timestamp()")
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff, upper = day - timedelta(days=7), day + timedelta(days=4)
        rows = [row for row in batch.rows if cutoff <= row[0] < upper]
        if batch.rejected or len(rows) != len(batch.rows):
            log.warning(
                "telemetry.log_records_skipped",
                count=batch.rejected + len(batch.rows) - len(rows),
                edge=segment.edge,
            )
        if rows:
            await conn.copy_records_to_table(
                "access_logs" if segment.edge else "service_logs",
                records=[(*row, now) for row in rows],
                columns=(*(EDGE_COLUMNS if segment.edge else SERVICE_COLUMNS), "ingested_at"),
            )
        if segment.edge:
            await _rollup_edge(conn, rows)
        if checkpoint and checkpoint["device"] != segment.device:
            await conn.execute(
                "DELETE FROM log_checkpoint WHERE device = 0 AND inode = $1", segment.inode
            )
        await conn.execute(
            """INSERT INTO log_checkpoint (device, path, inode, "offset", anchor, discarding)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT (device, inode) DO UPDATE SET path = EXCLUDED.path,
                 "offset" = EXCLUDED."offset", anchor = EXCLUDED.anchor,
                 discarding = EXCLUDED.discarding, updated_at = clock_timestamp()""",
            segment.device,
            str(segment.path),
            segment.inode,
            batch.offset,
            batch.anchor,
            batch.discarding,
        )
        return len(rows)
