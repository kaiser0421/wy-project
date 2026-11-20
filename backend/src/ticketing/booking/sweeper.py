"""Release overdue holds with one order per transaction to keep quota lock order local."""

from ticketing.booking.database import connection, transaction
from ticketing.booking.orders import release_order
from ticketing.core.errors import ProblemError
from ticketing.core.service import ServiceContext
from ticketing.telemetry import business


async def tick(ctx: ServiceContext) -> None:
    if ctx.stop.is_set():
        return
    async with connection(ctx.db, ctx.settings) as conn:
        candidates = await conn.fetch(
            """SELECT id FROM orders
               WHERE status='pending_payment' AND expires_at <= (SELECT app_now())
               ORDER BY expires_at, id LIMIT 200"""
        )
    for candidate in candidates:
        if ctx.stop.is_set():
            break
        try:
            async with transaction(ctx.db, ctx.settings) as conn:
                released = await release_order(conn, candidate["id"], "pending_payment", "expired")
        except ProblemError as exc:
            if not exc.retryable:
                raise
            continue
        if released is not None:
            business.record(released["event_id"], "expiry")
