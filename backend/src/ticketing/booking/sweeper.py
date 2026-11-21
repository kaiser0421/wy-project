"""Release overdue holds with one order per transaction to keep quota lock order local."""

from ticketing.booking.database import transaction
from ticketing.booking.orders import release_order
from ticketing.core.logging import get_logger
from ticketing.core.service import ServiceContext
from ticketing.telemetry import business

log = get_logger(__name__)
_BATCH_SIZE = 200


async def tick(ctx: ServiceContext) -> None:
    # Committed releases leave the partial expiry index. Only unreleased rows need an
    # exclusion list, so deadline ordering can use the index even when many orders tie.
    skipped: list[int] = []
    for _ in range(_BATCH_SIZE):
        if ctx.stop.is_set():
            return
        order_id: int | None = None
        try:
            async with transaction(ctx.db, ctx.settings) as conn:
                candidate = await conn.fetchrow(
                    """SELECT id FROM orders
                       WHERE status='pending_payment' AND expires_at <= (SELECT app_now())
                         AND id <> ALL($1::bigint[])
                       ORDER BY expires_at LIMIT 1 FOR UPDATE SKIP LOCKED""",
                    skipped,
                )
                if candidate is None:
                    return
                order_id = candidate["id"]
                released = await release_order(conn, order_id, "pending_payment", "expired")
        except Exception as exc:
            if order_id is None:
                raise
            skipped.append(order_id)
            log.warning("booking.expiry_failed", order_id=order_id, error=type(exc).__name__)
            continue
        if released is None:
            # A changed clock can invalidate the pre-lock deadline sample.
            skipped.append(order_id)
        else:
            business.record(released["event_id"], "expiry")
