"""Bounded collector jobs with independent failure handling."""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

import asyncpg
import httpx

from ticketing.core.context import BOOT_UUID
from ticketing.core.db import acquire
from ticketing.core.logging import get_logger
from ticketing.core.service import ServiceContext
from ticketing.telemetry import alerts
from ticketing.telemetry.app_metrics import (
    Consumer,
    ingest_intervals,
    parse_batch,
    refresh_completeness,
)
from ticketing.telemetry.common import GROUP, STREAM, second
from ticketing.telemetry.logs import discover, ingest_segment

log = get_logger(__name__)
NGINX_STATUS_URL = os.environ.get("NGINX_STATUS_URL", "http://nginx:8080/nginx_status")
_ACTIVE = re.compile(r"^Active connections:\s*(\d+)\s*$", re.MULTILINE)


async def _nginx_active() -> int | None:
    try:
        async with asyncio.timeout(0.4), httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(NGINX_STATUS_URL)
            response.raise_for_status()
        match = _ACTIVE.search(response.text)
        value = int(match[1]) if match else None
        return value if value is not None and value < 2**31 else None
    except (TimeoutError, httpx.HTTPError, ValueError):
        return None


async def sample_connections(
    conn: asyncpg.Connection, active: int | None, *, sampled_at: datetime | None = None
) -> None:
    async with conn.transaction():
        now: datetime = sampled_at or await conn.fetchval("SELECT clock_timestamp()")
        bucket = second(now) - timedelta(seconds=1)
        await conn.execute(
            """INSERT INTO metrics_1s (ts, source, conns_active) VALUES ($1, 'edge', $2)
               ON CONFLICT (source, scope, ts) DO UPDATE
               SET conns_active = EXCLUDED.conns_active""",
            bucket,
            active,
        )
        await refresh_completeness(conn, [bucket - timedelta(seconds=i) for i in range(3, -1, -1)])
        await conn.execute(
            """WITH gauges AS (
                   SELECT
                       CASE WHEN count(latest_sse) > 0 THEN coalesce(
                           sum(latest_sse) FILTER (
                               WHERE last_seen_at > $2::timestamptz - interval '3 seconds'
                           ), 0) END AS sse,
                       max(abs(wall_skew_ms)) FILTER (
                           WHERE last_seen_at > $2::timestamptz - interval '3 seconds'
                       ) AS skew
                   FROM telemetry_producers
               )
               UPDATE metrics_1s SET conns_sse = gauges.sse, clock_skew_ms = gauges.skew
               FROM gauges WHERE ts = $1 AND source = 'app' AND scope = 'all'""",
            bucket,
            now,
        )


async def retention(conn: asyncpg.Connection) -> int:
    """Remove expired daily data and bound the lifetime of replay and liveness metadata."""
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(7106)")
        dropped: int = await conn.fetchval("SELECT drop_expired_monitoring_partitions()")
        await conn.execute(
            """DELETE FROM telemetry_ingest WHERE received_at < date_trunc('day', now(), 'UTC')
               - interval '14 days'"""
        )
        await conn.execute(
            """DELETE FROM telemetry_producers
               WHERE greatest(started_at, heartbeat_at, last_seen_at)
                 < now() - interval '15 days'"""
        )
        return dropped


class Collector:
    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx
        self.consumer = Consumer(BOOT_UUID)
        self.edge_cursor = 0
        self.log_cursor = 0
        self.next_second = 0.0
        self.next_retention = 0.0

    async def _guard(self, job: Callable[[], Awaitable[None]]) -> None:
        if self.ctx.stop.is_set():
            return
        try:
            await job()
        except Exception as exc:
            log.warning(
                "telemetry.collector_job_failed", job=job.__name__, error_type=type(exc).__name__
            )

    async def run(self) -> None:
        now = time.monotonic()
        jobs = [self._logs, self._intervals]
        if now >= self.next_second:
            self.next_second = int(now) + 1
            jobs.extend((self._connections, self._alerts))
        await asyncio.gather(*(self._guard(job) for job in jobs))
        if now >= self.next_retention:
            await self._guard(self._retention)

    async def _logs(self) -> None:
        ctx = self.ctx
        segments = await asyncio.to_thread(discover, ctx.settings)
        edge = [segment for segment in segments if segment.edge]
        services = [segment for segment in segments if not segment.edge]
        timeout_s = ctx.settings.db_pool_acquire_timeout_s
        async with acquire(ctx.db, timeout_s) as conn:
            scan_started = time.monotonic()
            deadline = scan_started + (0.3 if services else 0.4)
            for _ in range(min(len(edge), 32)):
                if ctx.stop.is_set() or time.monotonic() >= deadline:
                    break
                segment = edge[self.edge_cursor % len(edge)]
                self.edge_cursor += 1
                for _ in range(3):
                    if ctx.stop.is_set() or time.monotonic() >= deadline:
                        break
                    if await ingest_segment(conn, segment) == 0:
                        break

            deadline = scan_started + 0.4
            for _ in range(min(len(services), 128)):
                if ctx.stop.is_set() or time.monotonic() >= deadline:
                    break
                index = self.log_cursor % len(services)
                self.log_cursor += 1
                await ingest_segment(conn, services[index])

    async def _intervals(self) -> None:
        ctx = self.ctx
        async with asyncio.timeout(0.4):
            entries = await self.consumer.read(ctx.redis)
        if not entries or ctx.stop.is_set():
            return
        async with acquire(ctx.db, ctx.settings.db_pool_acquire_timeout_s) as conn:
            await ingest_intervals(conn, parse_batch(entries))
        async with asyncio.timeout(0.4):
            await ctx.redis.xack(STREAM, GROUP, *(entry_id for entry_id, _ in entries))

    async def _connections(self) -> None:
        active = await _nginx_active()
        ctx = self.ctx
        async with acquire(ctx.db, ctx.settings.db_pool_acquire_timeout_s) as conn:
            await sample_connections(conn, active)

    async def _alerts(self) -> None:
        ctx = self.ctx
        missing = await alerts.missing_heartbeats(ctx.redis, alerts.SERVICE_NAMES)
        async with acquire(ctx.db, ctx.settings.db_pool_acquire_timeout_s) as conn:
            changed = await alerts.evaluate(conn, missing)
        await alerts.publish_alerts(ctx.redis, changed)

    async def _retention(self) -> None:
        self.next_retention = time.monotonic() + 60
        ctx = self.ctx
        async with acquire(ctx.db, ctx.settings.db_pool_acquire_timeout_s) as conn:
            dropped = await retention(conn)
        self.next_retention = time.monotonic() + 3600
        if dropped:
            log.info("telemetry.partitions_dropped", count=dropped)


_collector: Collector | None = None


async def tick(ctx: ServiceContext) -> None:
    """Run one bounded iteration as called by the shared service runner."""
    global _collector
    if ctx.stop.is_set():
        return
    if _collector is None or _collector.ctx is not ctx:
        _collector = Collector(ctx)
    await _collector.run()
