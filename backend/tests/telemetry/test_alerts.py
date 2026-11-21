from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from tests.support import TestClock as Clock
from tests.support import create_user
from ticketing.telemetry.alerts import RULES, evaluate, observe_collector
from ticketing.telemetry.collector import retention
from ticketing.telemetry.common import second

pytestmark = pytest.mark.integration


async def event_fixture(db: asyncpg.Connection) -> tuple[int, int]:
    user = await create_user(db, "seller@example.com")
    event = await db.fetchval(
        """INSERT INTO events (owner_id, title, category, venue, sales_start_at, sales_end_at)
           VALUES ($1, 'Concert', 'Music', 'Hall', now(), now() + interval '1 hour')
           RETURNING id""",
        user,
    )
    return user, event


@pytest.mark.parametrize("rule", RULES)
async def test_rule_fires_and_resolves(
    rule: str, db: asyncpg.Connection, test_clock: Clock
) -> None:
    now = second(datetime.now(UTC))
    await test_clock.set(now)
    missing: set[str] = set()
    expected_scope = "all"
    if rule in {"EDGE_5XX", "EDGE_P99"}:
        await db.execute(
            """INSERT INTO metrics_1s (ts, source, req_total, err_5xx, p99_ms)
               SELECT ts, 'edge', 10, $2, $3 FROM
               generate_series($1::timestamptz - interval '32 seconds',
                               $1::timestamptz - interval '3 seconds', interval '1 second') ts""",
            now,
            1 if rule == "EDGE_5XX" else 0,
            1500 if rule == "EDGE_P99" else 10,
        )
    elif rule in {"PURCHASE_FUNNEL_COLLAPSE", "PAYMENT_CALLBACK_LAG", "HOLD_EXPIRY_BACKLOG"}:
        user, event = await event_fixture(db)
        if rule == "PURCHASE_FUNNEL_COLLAPSE":
            expected_scope = str(event)
            zone = await db.fetchval(
                """INSERT INTO zones (event_id, code, name, price_twd)
                   VALUES ($1, 'A', 'Main', 200) RETURNING id""",
                event,
            )
            await db.execute(
                """INSERT INTO seats (event_id, zone_id, row_label, seat_number, norm_key,
                                      sort_key, seat_idx)
                   VALUES ($1, $2, 'A', '1', 'A:A:1', 1, 1)""",
                event,
                zone,
            )
            await db.execute(
                """INSERT INTO business_1s (ts, event_id, hold_soldout)
                   SELECT ts, $2, 100 FROM generate_series(
                      $1::timestamptz - interval '3 seconds',
                      $1::timestamptz + interval '29 seconds', interval '1 second') ts""",
                now,
                event,
            )
            await evaluate(db, missing)
            assert (
                await db.fetchval("SELECT state FROM alert_events WHERE rule_id = $1", rule)
                == "pending"
            )
            await test_clock.advance(timedelta(seconds=30))
        else:
            order = await db.fetchval(
                """INSERT INTO orders (event_id, user_id, quantity, expires_at)
                   VALUES ($1, $2, 1, $3) RETURNING id""",
                event,
                user,
                now + timedelta(seconds=-45 if rule == "HOLD_EXPIRY_BACKLOG" else 60),
            )
            if rule == "PAYMENT_CALLBACK_LAG":
                await db.execute(
                    """INSERT INTO payment_attempts
                         (order_id, amount_twd, status, expires_at, last_polled_at)
                       VALUES ($1, 200, 'awaiting_customer', $2, $3)""",
                    order,
                    now + timedelta(seconds=60),
                    now - timedelta(seconds=60),
                )
                await evaluate(db, missing)
                assert not await db.fetchval("SELECT id FROM alert_events WHERE rule_id = $1", rule)
                attempt = await db.fetchval("SELECT id FROM payment_attempts")
                await db.execute(
                    """INSERT INTO payment_captures (provider_capture_id, attempt_id, order_id,
                            amount_twd, disposition, captured_at, recorded_at)
                       VALUES ('cap-lag', $1, $2, 200, 'accepted', $3, $4)""",
                    attempt,
                    order,
                    now - timedelta(seconds=45),
                    now,
                )
    else:
        expected_scope = "relay"
        missing.add("relay")
        await evaluate(db, missing)
        await test_clock.advance(timedelta(seconds=5, microseconds=1))

    changes = await evaluate(db, missing)
    assert any(row["rule_id"] == rule and row["state"] == "firing" for row in changes)
    row = await db.fetchrow("SELECT * FROM alert_events WHERE rule_id = $1", rule)
    assert (row["state"], row["scope"]) == ("firing", expected_scope)

    await db.execute("DELETE FROM metrics_1s")
    await db.execute("DELETE FROM business_1s")
    await db.execute("DELETE FROM payment_captures")
    await db.execute("UPDATE orders SET status = 'expired' WHERE status = 'pending_payment'")
    await evaluate(db, set())
    assert (
        await db.fetchval("SELECT state FROM alert_events WHERE id = $1", row["id"]) == "resolved"
    )


async def test_parallel_watchdogs_share_pending_timer_and_cooldown(
    db: asyncpg.Connection,
    db_url: str,
    test_clock: Clock,
) -> None:
    await test_clock.set(second(datetime.now(UTC)))
    other = await asyncpg.connect(db_url)
    try:
        await asyncio.gather(
            observe_collector(db, missing=True), observe_collector(other, missing=True)
        )
        assert await db.fetchval("SELECT count(*) FROM alert_events") == 1
        await test_clock.advance(timedelta(seconds=6))
        transitions = await asyncio.gather(
            observe_collector(db, missing=True),
            observe_collector(other, missing=True),
        )
        assert sum(len(rows) for rows in transitions) == 1
        assert await db.fetchval("SELECT state FROM alert_events") == "firing"
        await observe_collector(db, missing=False)
        await observe_collector(db, missing=True)
        assert await db.fetchval("SELECT count(*) FROM alert_events") == 1
        await test_clock.advance(timedelta(minutes=5))
        await observe_collector(db, missing=True)
        assert await db.fetchval("SELECT count(*) FROM alert_events") == 2
    finally:
        await other.close()


async def test_outbox_lag_is_an_independent_service_scope(db: asyncpg.Connection) -> None:
    await db.execute(
        """INSERT INTO outbox (event_id, kind, changes, created_at)
           VALUES (1, 'seat_changes', '[]', app_now() - interval '6 seconds')"""
    )
    await evaluate(db, set())
    assert (
        await db.fetchval(
            "SELECT state FROM alert_events WHERE rule_id = 'SERVICE_DOWN' AND scope = 'outbox'"
        )
        == "firing"
    )
    await db.execute("UPDATE outbox SET seq = 1")
    await evaluate(db, set())
    assert await db.fetchval("SELECT state FROM alert_events") == "resolved"


async def test_retention_checks_real_partition_bounds_and_preserves_unrelated_tables(
    db: asyncpg.Connection,
) -> None:
    await db.execute(
        """CREATE TABLE public.monitoring_retention_fixture PARTITION OF access_logs
           FOR VALUES FROM ('2000-01-01T00:00:00Z') TO ('2000-01-02T00:00:00Z');
           CREATE TABLE public.access_logs_p20000101 (id int)"""
    )
    assert await retention(db) >= 1
    assert await db.fetchval("SELECT to_regclass('monitoring_retention_fixture')") is None
    assert await db.fetchval("SELECT to_regclass('access_logs_p20000101')") is not None
    assert await retention(db) == 0
