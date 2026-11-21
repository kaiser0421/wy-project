"""Claim quota and seats and persist one order in a short READ COMMITTED transaction."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

import asyncpg

from ticketing.booking import test_hooks
from ticketing.booking.database import connection, fence, transaction
from ticketing.booking.idempotency import Claim, Result
from ticketing.booking.models import SpecificSeats, ZoneSeats
from ticketing.booking.orders import order_models
from ticketing.core.errors import ProblemError
from ticketing.core.logging import get_logger
from ticketing.core.settings import Settings
from ticketing.realtime.outbox import emit_seat_changes
from ticketing.telemetry import business

log = get_logger(__name__)


class ShortAllocation(Exception):
    def __init__(self, zone_id: int, quantity: int) -> None:
        self.zone_id = zone_id
        self.quantity = quantity


def fee_for(subtotal: int) -> int:
    return (subtotal * 5 + 50) // 100


def _unavailable() -> ProblemError:
    return ProblemError(409, "seat-unavailable", "Requested seats are unavailable")


def _on_sale(event: asyncpg.Record, decision_at: datetime) -> None:
    if not event["sales_start_at"] <= decision_at < event["sales_end_at"]:
        raise ProblemError(409, "event-not-on-sale", "Event is outside its sales window")


async def _allocate(
    conn: asyncpg.Connection,
    event_id: int,
    selection: SpecificSeats | ZoneSeats,
) -> list[asyncpg.Record]:
    seats: list[asyncpg.Record]
    if isinstance(selection, SpecificSeats):
        try:
            seats = await conn.fetch(
                """SELECT id, status='available' AS available FROM seats
                   WHERE event_id=$1 AND id=ANY($2::bigint[])
                   ORDER BY id FOR NO KEY UPDATE NOWAIT""",
                event_id,
                selection.seat_ids,
            )
        except asyncpg.LockNotAvailableError as exc:
            raise ProblemError(
                409, "seat-contended", "Requested seats are being booked", retryable=True
            ) from exc
        if len(seats) != len(selection.seat_ids) or not all(s["available"] for s in seats):
            raise _unavailable()
        return seats

    zone_id = await conn.fetchval(
        "SELECT id FROM zones WHERE event_id=$1 AND code=$2",
        event_id,
        selection.zone_code,
    )
    if zone_id is None:
        raise ProblemError(404, "not-found", "Not found", "Zone not found.")
    seats = await conn.fetch(
        """SELECT id FROM seats WHERE event_id=$1 AND zone_id=$2 AND status='available'
           ORDER BY sort_key,id LIMIT $3 FOR NO KEY UPDATE SKIP LOCKED""",
        event_id,
        zone_id,
        selection.quantity,
    )
    if len(seats) != selection.quantity:
        raise ShortAllocation(zone_id, selection.quantity)
    return seats


async def _mark_held(
    conn: asyncpg.Connection,
    order_id: int,
    seat_ids: list[int],
) -> list[asyncpg.Record]:
    seats: list[asyncpg.Record] = await conn.fetch(
        """UPDATE seats SET status='held', order_id=$1, version=version+1, updated_at=app_now()
           WHERE id=ANY($2::bigint[]) AND status='available' RETURNING seat_idx, version""",
        order_id,
        seat_ids,
    )
    return seats


async def _add_items(conn: asyncpg.Connection, order_id: int, seat_ids: list[int]) -> int:
    try:
        prices = await conn.fetch(
            """INSERT INTO order_items (order_id, seat_id, event_id, zone_id, unit_price_twd)
               SELECT $1, s.id, s.event_id, s.zone_id, z.price_twd
               FROM seats s JOIN zones z ON z.id=s.zone_id WHERE s.id=ANY($2::bigint[])
               RETURNING unit_price_twd""",
            order_id,
            seat_ids,
        )
    except asyncpg.UniqueViolationError as exc:
        if exc.constraint_name != "order_items_one_active_claim":
            raise
        raise _unavailable() from exc
    if len(prices) != len(seat_ids):
        raise RuntimeError("Held seats are missing order items.")
    return sum(row["unit_price_twd"] for row in prices)


async def _reserve_quota(
    conn: asyncpg.Connection,
    user_id: int,
    event_id: int,
    quantity: int,
    limit: int,
) -> None:
    await conn.execute(
        "INSERT INTO user_event_quota (user_id,event_id) VALUES ($1,$2) ON CONFLICT DO NOTHING",
        user_id,
        event_id,
    )
    reserved = await conn.fetchval(
        """UPDATE user_event_quota SET reserved=reserved+$3
           WHERE user_id=$1 AND event_id=$2 AND reserved <= $4::int - $3 RETURNING reserved""",
        user_id,
        event_id,
        quantity,
        limit,
    )
    if reserved is None:
        raise ProblemError(409, "purchase-limit-exceeded", "Ticket limit exceeded")


async def _book(
    conn: asyncpg.Connection,
    settings: Settings,
    event_id: int,
    user_id: int,
    selection: SpecificSeats | ZoneSeats,
) -> Result:
    event = await conn.fetchrow(
        """SELECT sales_start_at, sales_end_at, app_now() AS observed_at FROM events
           WHERE id=$1 AND status='published'""",
        event_id,
    )
    if event is None:
        raise ProblemError(404, "not-found", "Not found", "Published event not found.")
    _on_sale(event, event["observed_at"])
    quantity = (
        len(selection.seat_ids) if isinstance(selection, SpecificSeats) else selection.quantity
    )
    try:
        await _reserve_quota(conn, user_id, event_id, quantity, settings.max_tickets_per_user_event)
        # Acquire the event FK lock before locking seats so event deletion uses the same order.
        order_id = await conn.fetchval(
            "INSERT INTO orders (user_id,event_id,quantity) VALUES ($1,$2,$3) RETURNING id",
            user_id,
            event_id,
            quantity,
        )
    except asyncpg.ForeignKeyViolationError as exc:
        if exc.constraint_name not in {"orders_event_id_fkey", "user_event_quota_event_id_fkey"}:
            raise
        raise ProblemError(404, "not-found", "Not found", "Published event not found.") from exc

    strategy = test_hooks.naive_strategy(settings) if isinstance(selection, SpecificSeats) else None
    allocate = strategy.allocate if strategy else _allocate
    mark_held = strategy.mark_held if strategy else _mark_held
    seats = await allocate(conn, event_id, selection)
    decision_at = await conn.fetchval("SELECT app_now()")
    _on_sale(event, decision_at)
    expires = decision_at + timedelta(seconds=settings.hold_ttl_seconds)
    seat_ids = [row["id"] for row in seats]
    held = await mark_held(conn, order_id, seat_ids)
    if len(held) != quantity:
        raise _unavailable()
    subtotal = await _add_items(conn, order_id, seat_ids)
    fee = fee_for(subtotal)
    order = await conn.fetchrow(
        """UPDATE orders SET subtotal_twd=$2, fee_twd=$3, total_twd=$4, expires_at=$5
           WHERE id=$1 AND status='pending_payment'
           RETURNING id, public_id, user_id, event_id, status::text AS status,
                     subtotal_twd, fee_twd, total_twd, expires_at""",
        order_id,
        subtotal,
        fee,
        subtotal + fee,
        expires,
    )
    if order is None:
        raise RuntimeError("New order header could not be finalized.")
    await emit_seat_changes(
        conn,
        event_id,
        [(row["seat_idx"], "held", row["version"]) for row in held],
    )
    (model,) = await order_models(conn, [order])
    return Result(
        201, model.model_dump(mode="json"), {"Location": f"/api/v1/orders/{model.public_id}"}
    )


async def _classify_shortage(
    pool: asyncpg.Pool,
    settings: Settings,
    shortage: ShortAllocation,
) -> ProblemError:
    # Count in a fresh snapshot after releasing any rows we got from SKIP LOCKED.
    async with connection(pool, settings) as conn:
        available = await conn.fetchval(
            """SELECT count(*) FROM (
                   SELECT 1 FROM seats WHERE zone_id=$1 AND status='available' LIMIT $2
               ) eligible""",
            shortage.zone_id,
            shortage.quantity,
        )
    if available < shortage.quantity:
        return ProblemError(409, "insufficient-seats", "Zone has too few available seats")
    return ProblemError(
        409, "allocation-contended", "Zone seats are being allocated", retryable=True
    )


async def create_order(
    pool: asyncpg.Pool,
    settings: Settings,
    session_id: UUID,
    claim: Claim,
    event_id: int,
    selection: SpecificSeats | ZoneSeats,
) -> Result:
    try:
        try:
            async with transaction(pool, settings) as conn:
                await fence(conn, session_id, claim.user_id)
                prior = await claim.check(conn)
                if prior is not None:
                    return prior
                result = await _book(conn, settings, event_id, claim.user_id, selection)
                await claim.save(conn, result, resource_ref=result.body["public_id"])
        except ShortAllocation as shortage:
            raise await _classify_shortage(pool, settings, shortage) from shortage
    except ProblemError as exc:
        outcome = {
            "seat-unavailable": "hold_conflict",
            "seat-contended": "hold_conflict",
            "allocation-contended": "hold_conflict",
            "insufficient-seats": "hold_soldout",
            "purchase-limit-exceeded": "hold_limit",
        }.get(exc.code)
        if outcome:
            business.record(event_id, outcome)
        raise
    business.record(event_id, "hold_ok")
    log.info("order.hold_created", event_id=event_id, order_id=result.body["public_id"])
    return result
