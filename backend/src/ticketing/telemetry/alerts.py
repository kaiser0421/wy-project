"""Persisted monitoring rules and the independent API collector watchdog."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

import asyncpg
import orjson
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.datastructures import State

from ticketing.core.db import acquire
from ticketing.core.logging import get_logger
from ticketing.telemetry.common import ALERT_CHANNEL

log = get_logger(__name__)
RULES = (
    "EDGE_5XX",
    "EDGE_P99",
    "PURCHASE_FUNNEL_COLLAPSE",
    "PAYMENT_CALLBACK_LAG",
    "HOLD_EXPIRY_BACKLOG",
    "SERVICE_DOWN",
)
ALERT_LOCK = 7104
SERVICE_NAMES = ("worker", "relay", "collector")
FUNNEL = "PURCHASE_FUNNEL_COLLAPSE"
HOLD_BACKLOG = """
SELECT extract(epoch FROM $1::timestamptz - expires_at)::double precision
FROM orders WHERE status = 'pending_payment'
  AND expires_at < $1::timestamptz - interval '30 seconds'
ORDER BY expires_at LIMIT 1
"""
OUTBOX_LAG = """
SELECT extract(epoch FROM $1::timestamptz - created_at)::double precision
FROM outbox WHERE seq IS NULL AND created_at < $1::timestamptz - interval '5 seconds'
ORDER BY created_at LIMIT 1
"""
CALLBACK_LAG = """
SELECT extract(epoch FROM $1::timestamptz - a.provider_captured_at)::double precision
FROM payment_attempts a JOIN orders o ON o.id = a.order_id
WHERE a.status = 'awaiting_customer' AND o.status = 'pending_payment'
  AND a.provider_captured_at < $1::timestamptz - interval '30 seconds'
  AND NOT EXISTS (SELECT 1 FROM payment_captures c WHERE c.attempt_id = a.id)
ORDER BY a.provider_captured_at LIMIT 1
"""
EDGE = """
SELECT coalesce(sum(req_total), 0) AS requests, coalesce(sum(err_5xx), 0) AS failures,
       count(*) FILTER (WHERE p99_ms > 1000) AS slow_seconds, min(p99_ms) AS minimum_p99
FROM metrics_1s
WHERE source = 'edge' AND scope = 'all'
  AND ts >= $1::timestamptz - interval '30 seconds' AND ts < $1
"""
FUNNEL_SQL = """
WITH funnel AS (
    SELECT event_id, sum(hold_ok) AS successes, sum(hold_conflict + hold_limit) AS contention,
           sum(hold_conflict + hold_limit + hold_soldout) AS failures
    FROM business_1s WHERE ts >= $1::timestamptz - interval '30 seconds' AND ts < $1
    GROUP BY event_id
)
SELECT f.event_id, f.successes::double precision / (f.successes + f.failures) AS rate
FROM funnel f
WHERE f.failures > 0 AND f.contention * 2 < f.failures
  AND f.successes * 5 < f.successes + f.failures
  AND EXISTS (SELECT 1 FROM seats s WHERE s.event_id = f.event_id AND s.status = 'available')
"""
AlertKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class Signal:
    value: float
    threshold: float
    wait_s: int = 0


async def missing_heartbeats(redis: Redis, services: tuple[str, ...]) -> set[str] | None:
    try:
        async with asyncio.timeout(0.4):
            values = await redis.mget([f"hb:{service}" for service in services])
        return {service for service, value in zip(services, values, strict=True) if value is None}
    except (TimeoutError, RedisError, OSError):
        return None


async def publish_alerts(redis: Redis, alerts: list[asyncpg.Record]) -> None:
    for row in alerts:
        try:
            async with asyncio.timeout(0.3):
                await redis.publish(ALERT_CHANNEL, orjson.dumps(dict(row)))
        except (TimeoutError, RedisError, OSError) as exc:
            log.warning("telemetry.alert_notification_failed", error_type=type(exc).__name__)
            return


async def _transition(
    conn: asyncpg.Connection,
    key: AlertKey,
    signal: Signal | None,
    now: datetime,
) -> asyncpg.Record | None:
    rule, scope = key
    active = await conn.fetchrow(
        """SELECT id, state, ts FROM alert_events
           WHERE rule_id = $1 AND scope = $2 AND state <> 'resolved' FOR UPDATE""",
        rule,
        scope,
    )
    if active is None:
        if signal is None:
            return None
        return await conn.fetchrow(
            """INSERT INTO alert_events (rule_id, scope, state, value, threshold, ts, updated_at)
               SELECT $1, $2, 'pending', $3, $4, $5, $5
               WHERE NOT EXISTS (
                   SELECT 1 FROM alert_events WHERE rule_id = $1 AND scope = $2
                     AND state = 'resolved' AND fired_at IS NOT NULL
                     AND updated_at > $5::timestamptz - interval '5 minutes'
               )
               ON CONFLICT (rule_id, scope) WHERE state <> 'resolved' DO NOTHING
               RETURNING *""",
            rule,
            scope,
            signal.value,
            signal.threshold,
            now,
        )
    if signal is None:
        return await conn.fetchrow(
            """UPDATE alert_events SET state = 'resolved', updated_at = $2
               WHERE id = $1 AND state <> 'resolved' RETURNING *""",
            active["id"],
            now,
        )
    if active["state"] == "pending" and now - active["ts"] >= timedelta(seconds=signal.wait_s):
        return await conn.fetchrow(
            """UPDATE alert_events SET state = 'firing', fired_at = $2, updated_at = $2,
                   value = $3, threshold = $4
               WHERE id = $1 AND state = 'pending' RETURNING *""",
            active["id"],
            now,
            signal.value,
            signal.threshold,
        )
    return None


async def _apply(
    conn: asyncpg.Connection,
    signals: dict[AlertKey, Signal | None],
    now: datetime,
) -> list[asyncpg.Record]:
    changed = []
    for key, signal in sorted(signals.items()):
        row = await _transition(conn, key, signal, now)
        if row is not None:
            changed.append(row)
            if signal is not None and signal.wait_s == 0 and row["state"] == "pending":
                firing = await _transition(conn, key, signal, now)
                if firing is not None:
                    changed.append(firing)
    return changed


async def evaluate(conn: asyncpg.Connection, missing: set[str] | None) -> list[asyncpg.Record]:
    """Apply six rules using the DB deadline clock and finalized edge seconds.

    Funnel checks a rolling 30-second event-wide rate for 30 sustained seconds. It
    requires at least one currently available seat and fewer than half of failures
    attributable to conflicts or limits. Payment lag requires a trusted provider
    capture timestamp while the attempt is still awaiting_customer with no capture row.
    """
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", ALERT_LOCK)
        now: datetime = await conn.fetchval("SELECT app_now()")
        end = now.replace(microsecond=0) - timedelta(seconds=2)
        app_end = now.replace(microsecond=0)
        edge = await conn.fetchrow(EDGE, end)
        requests, failures = edge["requests"], edge["failures"]
        hold = await conn.fetchval(HOLD_BACKLOG, now)
        payment = await conn.fetchval(CALLBACK_LAG, now)
        outbox = await conn.fetchval(OUTBOX_LAG, now)
        signals: dict[AlertKey, Signal | None] = {
            ("EDGE_5XX", "all"): (
                Signal(failures / requests, 0.02)
                if requests >= 100 and failures * 50 > requests
                else None
            ),
            ("EDGE_P99", "all"): (
                Signal(edge["minimum_p99"], 1000) if edge["slow_seconds"] == 30 else None
            ),
            ("HOLD_EXPIRY_BACKLOG", "all"): Signal(hold, 30) if hold is not None else None,
            ("PAYMENT_CALLBACK_LAG", "all"): Signal(payment, 30) if payment is not None else None,
            ("SERVICE_DOWN", "outbox"): Signal(outbox, 5) if outbox is not None else None,
        }

        scopes = await conn.fetch(
            "SELECT scope FROM alert_events WHERE rule_id = $1 AND state <> 'resolved'", FUNNEL
        )
        signals.update({(FUNNEL, row["scope"]): None for row in scopes})
        for row in await conn.fetch(FUNNEL_SQL, app_end):
            signals[(FUNNEL, str(row["event_id"]))] = Signal(row["rate"], 0.2, wait_s=30)
        if missing is not None:
            signals.update(
                {
                    ("SERVICE_DOWN", service): Signal(0, 5, wait_s=5)
                    if service in missing
                    else None
                    for service in SERVICE_NAMES
                }
            )
        return await _apply(conn, signals, now)


async def observe_collector(conn: asyncpg.Connection, *, missing: bool) -> list[asyncpg.Record]:
    async with conn.transaction():
        # Collector and API replicas share the lock and persisted pending timer.
        await conn.execute("SELECT pg_advisory_xact_lock($1)", ALERT_LOCK)
        now = await conn.fetchval("SELECT app_now()")
        return await _apply(
            conn, {("SERVICE_DOWN", "collector"): Signal(0, 5, wait_s=5) if missing else None}, now
        )


async def collector_watchdog(state: State) -> None:
    redis = cast(Redis, state.redis)
    while True:
        try:
            missing = await missing_heartbeats(redis, ("collector",))
            if missing is not None:
                async with acquire(state.db, state.settings.db_pool_acquire_timeout_s) as conn:
                    changed = await observe_collector(conn, missing="collector" in missing)
                await publish_alerts(redis, changed)
        except Exception as exc:
            log.warning("telemetry.watchdog_failed", error_type=type(exc).__name__)
        await asyncio.sleep(1)
