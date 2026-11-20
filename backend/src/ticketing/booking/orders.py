"""Order reads and the only code path that releases held or sold seats."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

import asyncpg

from ticketing.booking.models import Order, OrderItem, OrderPage
from ticketing.core.errors import ProblemError
from ticketing.realtime.outbox import emit_seat_changes

_ITEMS_SQL = """
SELECT i.order_id, i.seat_id, z.code AS zone_code, s.row_label, s.seat_number, i.unit_price_twd
FROM order_items i JOIN seats s ON s.id=i.seat_id JOIN zones z ON z.id=i.zone_id
WHERE i.order_id=ANY($1::bigint[]) ORDER BY i.seat_id
"""


async def order_models(conn: asyncpg.Connection, rows: list[asyncpg.Record]) -> list[Order]:
    items: dict[int, list[OrderItem]] = {row["id"]: [] for row in rows}
    if items:
        for item in await conn.fetch(_ITEMS_SQL, list(items)):
            items[item["order_id"]].append(OrderItem.model_validate(dict(item)))
    return [
        Order.model_validate(
            {
                **dict(row),
                "items": items[row["id"]],
                "refund_status": {"refund_pending": "pending", "refunded": "succeeded"}.get(
                    row["status"], "none"
                ),
            }
        )
        for row in rows
    ]


async def order_page(
    conn: asyncpg.Connection,
    owner_id: int,
    cursor: int | None,
    limit: int,
) -> OrderPage:
    rows = await conn.fetch(
        """SELECT * FROM orders WHERE user_id=$1 AND ($2::bigint IS NULL OR id < $2)
           ORDER BY id DESC LIMIT $3""",
        owner_id,
        cursor,
        limit + 1,
    )
    return OrderPage(
        items=await order_models(conn, rows[:limit]),
        next_cursor=str(rows[limit - 1]["id"]) if len(rows) > limit else None,
    )


async def visible_order(
    conn: asyncpg.Connection,
    public_id: UUID,
    owner_id: int | None,
    *,
    lock: bool = False,
) -> asyncpg.Record:
    query = "SELECT * FROM orders WHERE public_id=$1 AND ($2::bigint IS NULL OR user_id=$2)"
    if lock:
        query += " FOR NO KEY UPDATE"
    row = await conn.fetchrow(query, public_id, owner_id)
    if row is None:
        raise ProblemError(404, "not-found", "Not found", "Order not found.")
    return row


async def lock_order_seats(conn: asyncpg.Connection, order_id: int) -> None:
    await conn.fetch(
        "SELECT id FROM seats WHERE order_id=$1 ORDER BY id FOR NO KEY UPDATE", order_id
    )


async def release_order(
    conn: asyncpg.Connection,
    order_id: int,
    expected: Literal["pending_payment", "refund_pending"],
    new_status: Literal["expired", "cancelled", "refunded"],
) -> asyncpg.Record | None:
    """Release an order once. Lock before sampling the expiry clock even if the caller did not."""
    await conn.fetchval("SELECT id FROM orders WHERE id=$1 FOR NO KEY UPDATE", order_id)
    order = await conn.fetchrow(
        """UPDATE orders SET status=$3::order_status, closed_at=app_now(), version=version+1
           WHERE id=$1 AND status=$2 AND ($3::order_status <> 'expired' OR expires_at <= app_now())
           RETURNING *""",
        order_id,
        expected,
        new_status,
    )
    if order is None:
        return None
    await lock_order_seats(conn, order_id)
    seats = await conn.fetch(
        """UPDATE seats SET status='available', order_id=NULL, version=version+1,
                  updated_at=app_now()
           WHERE order_id=$1 AND status=$2 RETURNING id, seat_idx, version""",
        order_id,
        "sold" if expected == "refund_pending" else "held",
    )
    items = await conn.fetch(
        """UPDATE order_items SET released_at=app_now()
           WHERE order_id=$1 AND released_at IS NULL RETURNING seat_id""",
        order_id,
    )
    quantity = order["quantity"]
    if len(seats) != quantity or {seat["id"] for seat in seats} != {
        item["seat_id"] for item in items
    }:
        raise RuntimeError("Order release violates seat ownership invariants.")
    quota = await conn.fetchval(
        """UPDATE user_event_quota SET reserved=reserved-$3
           WHERE user_id=$1 AND event_id=$2 AND reserved >= $3 RETURNING reserved""",
        order["user_id"],
        order["event_id"],
        quantity,
    )
    if quota is None:
        raise RuntimeError("Order release violates quota invariants.")
    if expected == "pending_payment":
        await conn.execute(
            """INSERT INTO provider_cancellations (attempt_id, next_try_at)
               SELECT id, app_now() FROM payment_attempts
               WHERE order_id=$1 AND status IN ('creating','awaiting_customer')
               ON CONFLICT DO NOTHING""",
            order_id,
        )
    await emit_seat_changes(
        conn,
        order["event_id"],
        [(seat["seat_idx"], "available", seat["version"]) for seat in seats],
    )
    return order


async def cancel_order(conn: asyncpg.Connection, order: asyncpg.Record) -> asyncpg.Record:
    order_id = order["id"]
    if order["status"] == "pending_payment":
        result = await release_order(conn, order_id, "pending_payment", "cancelled")
    elif order["status"] == "paid":
        result = await conn.fetchrow(
            """UPDATE orders SET status='refund_pending', version=version+1
               WHERE id=$1 AND status='paid' RETURNING *""",
            order_id,
        )
        refund = await conn.fetchval(
            """INSERT INTO refunds (provider_capture_id, reason, next_try_at)
               SELECT provider_capture_id, 'order_cancelled', app_now() FROM payment_captures
               WHERE order_id=$1 AND disposition='accepted' RETURNING id""",
            order_id,
        )
        if refund is None:
            raise RuntimeError("Paid order has no accepted capture.")
    else:
        raise ProblemError(409, "order-not-cancellable", "Order cannot be cancelled")
    if result is None:
        raise RuntimeError("Locked order changed during cancellation.")
    return result
