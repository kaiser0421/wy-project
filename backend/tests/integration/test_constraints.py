"""Behavioural tests for the schema-level safety nets (not just their presence in the catalog)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from tests.support import TestClock

pytestmark = pytest.mark.integration


async def _event_with_seat(db: asyncpg.Connection) -> dict[str, int]:
    user_id = await db.fetchval(
        "INSERT INTO users (email, password_hash) VALUES ('buyer@example.com', 'x') RETURNING id"
    )
    start = datetime(2026, 12, 1, tzinfo=UTC)
    event_id = await db.fetchval(
        """INSERT INTO events (owner_id, title, category, venue, sales_start_at, sales_end_at)
           VALUES ($1, 'Concert', 'music', 'Arena', $2, $3) RETURNING id""",
        user_id,
        start,
        start + timedelta(days=1),
    )
    zone_id = await db.fetchval(
        "INSERT INTO zones (event_id, code, name, price_twd) VALUES ($1, 'A', 'Zone A', 2000) "
        "RETURNING id",
        event_id,
    )
    other_zone = await db.fetchval(
        "INSERT INTO zones (event_id, code, name, price_twd) VALUES ($1, 'B', 'Zone B', 1000) "
        "RETURNING id",
        event_id,
    )
    seat_id = await db.fetchval(
        """INSERT INTO seats
             (event_id, zone_id, row_label, seat_number, norm_key, sort_key, seat_idx)
           VALUES ($1, $2, '1', '1', 'A|1|1', 0, 0) RETURNING id""",
        event_id,
        zone_id,
    )
    return {
        "user": user_id,
        "event": event_id,
        "zone": zone_id,
        "other_zone": other_zone,
        "seat": seat_id,
    }


async def _order(db: asyncpg.Connection, ids: dict[str, int]) -> int:
    order_id: int = await db.fetchval(
        "INSERT INTO orders (user_id, event_id, quantity) VALUES ($1, $2, 1) RETURNING id",
        ids["user"],
        ids["event"],
    )
    return order_id


async def _claim(
    db: asyncpg.Connection, ids: dict[str, int], order_id: int, zone_key: str = "zone"
) -> None:
    await db.execute(
        """INSERT INTO order_items (order_id, seat_id, event_id, zone_id, unit_price_twd)
           VALUES ($1, $2, $3, $4, 2000)""",
        order_id,
        ids["seat"],
        ids["event"],
        ids[zone_key],
    )


async def test_second_active_claim_on_a_seat_is_rejected(db: asyncpg.Connection) -> None:
    ids = await _event_with_seat(db)
    first, second = await _order(db, ids), await _order(db, ids)
    await _claim(db, ids, first)

    with pytest.raises(asyncpg.UniqueViolationError) as info:
        await _claim(db, ids, second)
    assert info.value.constraint_name == "order_items_one_active_claim"

    # After the first claim is released, the seat can be claimed again.
    await db.execute("UPDATE order_items SET released_at = now() WHERE order_id = $1", first)
    await _claim(db, ids, second)


async def test_order_item_zone_must_match_the_seat(db: asyncpg.Connection) -> None:
    ids = await _event_with_seat(db)
    order_id = await _order(db, ids)
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await _claim(db, ids, order_id, zone_key="other_zone")


async def test_order_amounts_are_all_or_nothing_and_balanced(db: asyncpg.Connection) -> None:
    ids = await _event_with_seat(db)
    order_id = await _order(db, ids)
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute("UPDATE orders SET total_twd = 100 WHERE id = $1", order_id)
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "UPDATE orders SET subtotal_twd = 2000, fee_twd = 100, total_twd = 2101 WHERE id = $1",
            order_id,
        )
    await db.execute(
        "UPDATE orders SET subtotal_twd = 2000, fee_twd = 100, total_twd = 2100 WHERE id = $1",
        order_id,
    )


async def test_capture_must_belong_to_the_attempts_order(db: asyncpg.Connection) -> None:
    ids = await _event_with_seat(db)
    order_a, order_b = await _order(db, ids), await _order(db, ids)
    attempt = await db.fetchval(
        """INSERT INTO payment_attempts (order_id, amount_twd, status, expires_at)
           VALUES ($1, 2100, 'awaiting_customer', now() + interval '10 minutes') RETURNING id""",
        order_a,
    )
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await db.execute(
            """INSERT INTO payment_captures
                 (provider_capture_id, attempt_id, order_id, amount_twd, disposition, captured_at)
               VALUES ('cap_1', $1, $2, 2100, 'accepted', now())""",
            attempt,
            order_b,
        )


async def test_quota_cannot_exceed_four(db: asyncpg.Connection) -> None:
    ids = await _event_with_seat(db)
    with pytest.raises(asyncpg.CheckViolationError):
        await db.execute(
            "INSERT INTO user_event_quota (user_id, event_id, reserved) VALUES ($1, $2, 5)",
            ids["user"],
            ids["event"],
        )


async def test_test_clock_controls_app_now(db: asyncpg.Connection, test_clock: TestClock) -> None:
    frozen = datetime(2030, 1, 1, tzinfo=UTC)
    await test_clock.set(frozen)
    assert await db.fetchval("SELECT app_now()") == frozen
    moved = await test_clock.advance(timedelta(minutes=11))
    assert moved == frozen + timedelta(minutes=11)
    await test_clock.release()
    assert await db.fetchval("SELECT app_now()") < frozen
