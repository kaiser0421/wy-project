"""Redis consumer and per-producer monotonic calibration."""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import asyncpg
import orjson
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from ticketing.core.logging import get_logger
from ticketing.telemetry.business import OUTCOMES
from ticketing.telemetry.common import GROUP, STREAM, second
from ticketing.telemetry.histogram import BUCKETS, Counts
from ticketing.telemetry.metrics import MetricKey, write_metrics

log = get_logger(__name__)
CLAIM_IDLE_MS = 5_000
BATCH_SIZE = 128
APP_LOCK = 7103
Entry = tuple[bytes, dict[bytes, bytes]]
_BUSINESS_UPSERT = """
INSERT INTO business_1s AS b
    (ts, event_id, hold_ok, hold_conflict, hold_soldout, hold_limit,
     pay_ok, pay_fail, cancels, expiries)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
ON CONFLICT (event_id, ts) DO UPDATE SET
    hold_ok = b.hold_ok + EXCLUDED.hold_ok,
    hold_conflict = b.hold_conflict + EXCLUDED.hold_conflict,
    hold_soldout = b.hold_soldout + EXCLUDED.hold_soldout,
    hold_limit = b.hold_limit + EXCLUDED.hold_limit,
    pay_ok = b.pay_ok + EXCLUDED.pay_ok, pay_fail = b.pay_fail + EXCLUDED.pay_fail,
    cancels = b.cancels + EXCLUDED.cancels, expiries = b.expiries + EXCLUDED.expiries
"""


@dataclass(slots=True)
class Interval:
    boot: uuid.UUID
    number: int
    mono_ms: int
    wall: datetime
    received: datetime
    routes: dict[str, Counts]
    outcomes: list[tuple[int, str, int]]
    sse: int | None

    @property
    def delay(self) -> float:
        return self.received.timestamp() * 1000 - self.mono_ms


def _count(value: Any, upper: int = 2**31) -> int:
    if type(value) is not int or not 0 <= value < upper:
        raise ValueError("invalid telemetry count")
    return value


def parse(entry: Entry) -> Interval:
    entry_id, fields = entry
    raw = fields[b"payload"]
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("oversized telemetry payload")
    payload = orjson.loads(raw)
    wall = datetime.fromisoformat(fields[b"wall_ts"].decode())
    raw_routes = payload["routes"]
    raw_outcomes = payload["business"]
    if wall.tzinfo is None or len(raw_routes) > 512 or len(raw_outcomes) > 20_000:
        raise ValueError("invalid telemetry payload")
    routes = {}
    for path, (classes, hist) in raw_routes.items():
        if (
            not path
            or len(path) > 512
            or "\x00" in path
            or path == "all"
            or len(classes) != 6
            or len(hist) != BUCKETS
        ):
            raise ValueError("invalid telemetry route")
        counts = Counts([_count(c) for c in classes], [_count(c) for c in hist])
        if sum(counts.hist) != sum(counts.status_classes):
            raise ValueError("inconsistent telemetry counts")
        routes[path] = counts
    outcomes = []
    for event_id, outcome, count in raw_outcomes:
        if outcome not in OUTCOMES:
            raise ValueError("invalid business outcome")
        outcomes.append((_count(event_id, 2**63), outcome, _count(count)))
    gauge = payload.get("sse_active")
    return Interval(
        uuid.UUID(fields[b"boot_uuid"].decode()),
        _count(int(fields[b"interval_no"]), 2**63),
        _count(int(fields[b"mono_ms"]), 2**53),
        wall,
        # XADD's server-generated ID stamps arrival at Redis even if the collector is delayed.
        datetime.fromtimestamp(int(entry_id.split(b"-", 1)[0]) / 1000, UTC),
        routes,
        outcomes,
        None if gauge is None else _count(gauge),
    )


async def refresh_completeness(conn: asyncpg.Connection, buckets: list[datetime]) -> None:
    await conn.execute(
        """WITH coverage AS (
               SELECT ts,
                 (SELECT count(DISTINCT boot_uuid) FROM telemetry_ingest
                   WHERE bucket_sec = ts) AS reporting,
                 (SELECT count(*) FROM telemetry_producers
                   WHERE started_at < ts + interval '1 second'
                     AND greatest(heartbeat_at, last_seen_at) >= ts - interval '5 seconds'
                 ) AS expected
               FROM unnest($1::timestamptz[]) AS seconds(ts)
           )
           INSERT INTO metrics_1s (ts, source, completeness, late_or_missing)
           SELECT ts, 'app', CASE WHEN expected = 0 THEN NULL
                            ELSE least(1.0, reporting::real / expected) END,
                  greatest(0, expected - reporting)
           FROM coverage ORDER BY ts
           ON CONFLICT (source, scope, ts) DO UPDATE SET
                completeness = EXCLUDED.completeness, late_or_missing = EXCLUDED.late_or_missing""",
        buckets,
    )


async def _new_intervals(
    conn: asyncpg.Connection, intervals: list[Interval], cutoff: datetime
) -> list[Interval]:
    """Filter duplicate stream deliveries before they can affect the minimum-delay sample."""
    pending: dict[tuple[uuid.UUID, int], Interval] = {}
    for item in intervals:
        if item.received >= cutoff:
            pending.setdefault((item.boot, item.number), item)
    if not pending:
        return []
    rows = await conn.fetch(
        """SELECT i.boot_uuid, i.interval_no FROM telemetry_ingest i
           JOIN unnest($1::uuid[], $2::bigint[]) AS pending(boot_uuid, interval_no)
           USING (boot_uuid, interval_no)""",
        [boot for boot, _ in pending],
        [number for _, number in pending],
    )
    for row in rows:
        pending.pop((row["boot_uuid"], row["interval_no"]))
    return list(pending.values())


async def _ingest_producer(
    conn: asyncpg.Connection,
    pending: list[Interval],
    cutoff: datetime,
    upper: datetime,
    metrics: dict[MetricKey, Counts],
    outcomes: dict[tuple[datetime, int], list[int]],
) -> set[datetime]:
    boot = pending[0].boot
    newest = max(pending, key=lambda item: item.number)
    latest_receive = max(item.received for item in pending)
    calibration_since = latest_receive - timedelta(seconds=60)
    samples = [item.delay for item in pending if item.received >= calibration_since]
    stats = await conn.fetchrow(
        """SELECT min(delay_sample_ms) AS low, max(delay_sample_ms) AS high
           FROM telemetry_ingest WHERE boot_uuid = $1 AND received_at >= $2""",
        boot,
        calibration_since,
    )
    offset = min(*samples, stats["low"]) if stats["low"] is not None else min(samples)
    # A previously established minimum helps late claims even after the sample window moved on.
    previous = await conn.fetchrow(
        "SELECT last_interval_no, last_seen_at, offset_ms FROM telemetry_producers "
        "WHERE boot_uuid = $1",
        boot,
    )
    if (
        previous
        and previous["last_seen_at"]
        and previous["last_seen_at"] > latest_receive
        and previous["offset_ms"] is not None
    ):
        offset = min(offset, previous["offset_ms"])

    buckets: set[datetime] = set()
    for item in pending:
        bucket = second(datetime.fromtimestamp((offset + item.mono_ms) / 1000, UTC))
        if not cutoff <= bucket < upper:
            continue
        inserted = await conn.fetchval(
            """INSERT INTO telemetry_ingest
                 (boot_uuid, interval_no, bucket_sec, received_at, delay_sample_ms)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT DO NOTHING RETURNING true""",
            boot,
            item.number,
            bucket,
            item.received,
            item.delay,
        )
        if not inserted:
            continue
        buckets.add(bucket)
        total = metrics.setdefault((bucket, "app", "all"), Counts())
        for route, counts in item.routes.items():
            metrics.setdefault((bucket, "app", route), Counts()).add(counts)
            total.add(counts)
        for event_id, outcome, count in item.outcomes:
            key = (bucket, event_id)
            outcomes.setdefault(key, [0] * len(OUTCOMES))[OUTCOMES.index(outcome)] += count

    if buckets:
        spread = max(*samples, stats["high"]) if stats["high"] is not None else max(samples)
        await conn.execute(
            """INSERT INTO telemetry_producers
                 (boot_uuid, service, started_at, last_interval_no, last_seen_at, offset_ms,
                  offset_err_ms, wall_skew_ms, latest_sse)
               VALUES ($1, 'api', $2, $3, $4, $5, $6, $7, $8)
               ON CONFLICT (boot_uuid) DO UPDATE SET
                 last_interval_no = EXCLUDED.last_interval_no,
                 last_seen_at = EXCLUDED.last_seen_at,
                 offset_ms = EXCLUDED.offset_ms, offset_err_ms = EXCLUDED.offset_err_ms,
                 wall_skew_ms = EXCLUDED.wall_skew_ms, latest_sse = EXCLUDED.latest_sse
               WHERE telemetry_producers.last_interval_no IS NULL
                  OR telemetry_producers.last_interval_no < EXCLUDED.last_interval_no""",
            boot,
            min(buckets),
            newest.number,
            newest.received,
            offset,
            spread - offset,
            offset + newest.mono_ms - newest.wall.timestamp() * 1000,
            newest.sse,
        )
    return buckets


async def ingest_intervals(conn: asyncpg.Connection, intervals: list[Interval]) -> None:
    """Dedupe and all aggregates share one commit, before the consumer acknowledges Redis."""
    if not intervals:
        return
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", APP_LOCK)
        now: datetime = await conn.fetchval("SELECT clock_timestamp()")
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff, upper = day - timedelta(days=14), day + timedelta(days=4)
        per_boot: dict[uuid.UUID, list[Interval]] = defaultdict(list)
        for item in await _new_intervals(conn, intervals, cutoff):
            per_boot[item.boot].append(item)
        metrics: dict[MetricKey, Counts] = {}
        outcomes: dict[tuple[datetime, int], list[int]] = {}
        buckets: set[datetime] = set()
        for pending in per_boot.values():
            buckets.update(await _ingest_producer(conn, pending, cutoff, upper, metrics, outcomes))
        await write_metrics(conn, metrics)
        await conn.executemany(
            _BUSINESS_UPSERT,
            [(bucket, event, *counts) for (bucket, event), counts in sorted(outcomes.items())],
        )
        if buckets:
            await refresh_completeness(conn, sorted(buckets))


class Consumer:
    """Periodically scan pending entries so a restart younger than the idle time is safe."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.cursor = b"0-0"
        self.ready = False

    async def read(self, redis: Redis) -> list[Entry]:
        if not self.ready:
            try:
                await redis.xgroup_create(STREAM, GROUP, id="0-0", mkstream=True)
            except ResponseError as exc:
                if not str(exc).startswith("BUSYGROUP"):
                    raise
            self.ready = True
        try:
            claimed = await redis.xautoclaim(
                STREAM, GROUP, self.name, CLAIM_IDLE_MS, self.cursor, count=BATCH_SIZE
            )
            self.cursor, entries = claimed[:2]
            new = await redis.xreadgroup(GROUP, self.name, {STREAM: ">"}, count=BATCH_SIZE)
        except ResponseError as exc:
            if str(exc).startswith("NOGROUP"):
                self.ready = False
                self.cursor = b"0-0"
                return []
            raise
        return [
            *entries,
            *(entry for _, chunk in cast(list[tuple[bytes, list[Entry]]], new) for entry in chunk),
        ]


def parse_batch(entries: list[Entry]) -> list[Interval]:
    intervals = []
    rejected = 0
    for entry in entries:
        try:
            intervals.append(parse(entry))
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError, OSError):
            rejected += 1
    if rejected:
        log.warning("telemetry.intervals_skipped", count=rejected)
    return intervals
