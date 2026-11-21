"""Create attempts without holding database locks across provider calls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import asyncpg

from ticketing.booking.database import fence, transaction
from ticketing.booking.idempotency import Claim, Result, finish_payment
from ticketing.booking.orders import release_order, visible_order
from ticketing.core.context import current_request_id
from ticketing.core.errors import ProblemError, server_busy
from ticketing.core.logging import get_logger
from ticketing.core.settings import Settings
from ticketing.payments.callbacks import (
    AppliedEvent,
    apply_event,
    inconsistent_event,
    lock_payment,
    payment_declined,
    payment_paid,
)
from ticketing.payments.models import Checkout, PaymentAttempt
from ticketing.payments.provider import Intent, Provider, ProviderUnavailable

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Attempt:
    id: UUID
    order_id: int
    amount_twd: int
    expires_at: datetime

    @classmethod
    def from_row(cls, row: asyncpg.Record) -> Attempt:
        return cls(row["id"], row["order_id"], row["amount_twd"], row["expires_at"])


def checkout(attempt: Attempt, url: str) -> Result:
    model = Checkout(attempt_id=attempt.id, checkout_url=url, expires_at=attempt.expires_at)
    return Result(201, model.model_dump(mode="json"))


async def _begin(
    conn: asyncpg.Connection,
    public_id: UUID,
    claim: Claim,
) -> Attempt | Result:
    order = await visible_order(conn, public_id, claim.user_id, lock=True)
    if order["status"] != "pending_payment":
        raise ProblemError(409, "order-not-payable", "Order no longer accepts payment")
    row = await conn.fetchrow(
        """SELECT * FROM payment_attempts WHERE order_id=$1
           AND status IN ('creating','awaiting_customer','captured') FOR NO KEY UPDATE""",
        order["id"],
    )
    decision_at = await conn.fetchval("SELECT app_now()")
    if decision_at >= order["expires_at"]:
        raise ProblemError(409, "order-not-payable", "Order no longer accepts payment")
    if row is None:
        row = await conn.fetchrow(
            """INSERT INTO payment_attempts
                 (order_id,amount_twd,status,expires_at,created_at,updated_at,origin_request_id)
               VALUES ($1,$2,'creating',$3,app_now(),app_now(),$4) RETURNING *""",
            order["id"],
            order["total_twd"],
            order["expires_at"],
            current_request_id(),
        )
    attempt = Attempt.from_row(row)
    result = _current_result(order, row)
    await claim.save(conn, result, resource_ref=attempt.id)
    return result if result is not None else attempt


def _current_result(order: asyncpg.Record, row: asyncpg.Record) -> Result | None:
    if order["status"] == "paid":
        return payment_paid(order["public_id"], order["paid_at"])
    if order["status"] != "pending_payment" or row["status"] not in (
        "creating",
        "awaiting_customer",
    ):
        return payment_declined(order["public_id"])
    if row["checkout_url"] is not None:
        return checkout(Attempt.from_row(row), row["checkout_url"])
    if row["status"] != "creating":
        raise RuntimeError("Active payment attempt has no checkout.")
    return None


async def _expire_hold(
    conn: asyncpg.Connection, order: asyncpg.Record
) -> tuple[asyncpg.Record, AppliedEvent]:
    """Provider calls and lock waits can outlast the hold. Sample only under both locks."""
    if order["status"] == "pending_payment":
        decision_at = await conn.fetchval("SELECT app_now()")
        if decision_at >= order["expires_at"]:
            released = await release_order(conn, order["id"], "pending_payment", "expired")
            if released is None:
                raise server_busy()
            return released, AppliedEvent(event_id=order["event_id"], expired=True)
    return order, AppliedEvent()


async def finalize_attempt(
    pool: asyncpg.Pool,
    settings: Settings,
    attempt: Attempt,
    intent: Intent,
) -> Result:
    """Apply provider observations and resolve the checkout against current merchant state."""
    if (intent.merchant_payment_id, intent.amount_twd, intent.expires_at) != (
        attempt.id,
        attempt.amount_twd,
        attempt.expires_at,
    ):
        raise inconsistent_event()
    applied = AppliedEvent()
    intent_created = False
    async with transaction(pool, settings) as conn:
        # Event insertion precedes order locking on both the API and reconciler paths.
        if intent.event is not None:
            applied = await apply_event(conn, intent.event)
        order, row = await lock_payment(conn, attempt.order_id, attempt.id)
        order, expiry = await _expire_hold(conn, order)
        result = _current_result(order, row)
        if result is None:
            if intent.status != "open" or intent.intent_id is None:
                raise inconsistent_event()
            url = f"{settings.payment_checkout_base_url.rstrip('/')}/{intent.intent_id}"
            updated = await conn.fetchval(
                """UPDATE payment_attempts SET provider_intent_id=$2,checkout_url=$3,
                       status='awaiting_customer',updated_at=app_now()
                   WHERE id=$1 AND status='creating' AND checkout_url IS NULL RETURNING id""",
                attempt.id,
                str(intent.intent_id),
                url,
            )
            if updated is None:
                raise RuntimeError("Locked attempt could not be finalized.")
            intent_created = True
            result = checkout(attempt, url)
        await finish_payment(conn, attempt.id, result)
    applied.record()
    expiry.record()
    if intent_created:
        log.info("payment.intent_created", attempt_id=str(attempt.id), order_id=attempt.order_id)
    return result


async def create_payment(
    pool: asyncpg.Pool,
    settings: Settings,
    provider: Provider,
    session_id: UUID,
    public_id: UUID,
    claim: Claim,
) -> Result:
    expiry = AppliedEvent()
    async with transaction(pool, settings) as conn:
        await fence(conn, session_id, claim.user_id)
        attempt_id = await claim.payment_ref(conn)
        if attempt_id is None:
            work = await _begin(conn, public_id, claim)
        else:
            visible = await visible_order(conn, public_id, claim.user_id)
            order, row = await lock_payment(conn, visible["id"], attempt_id)
            order, expiry = await _expire_hold(conn, order)
            current = _current_result(order, row)
            if current is not None:
                await finish_payment(conn, attempt_id, current)
                work = current
            else:
                work = Attempt.from_row(row)
    expiry.record()
    if isinstance(work, Result):
        result = work
    else:
        try:
            intent = await provider.create(work.id, work.amount_twd, work.expires_at)
            result = await finalize_attempt(pool, settings, work, intent)
        except ProviderUnavailable as exc:
            raise server_busy(
                "Payment provider outcome is pending. Retry using the same key."
            ) from exc
    result.replayed = attempt_id is not None
    return result


async def list_attempts(
    conn: asyncpg.Connection,
    public_id: UUID,
    owner_id: int | None,
) -> list[PaymentAttempt]:
    order = await visible_order(conn, public_id, owner_id)
    rows = await conn.fetch(
        """SELECT id AS attempt_id,status,amount_twd,checkout_url,expires_at
           FROM payment_attempts WHERE order_id=$1 ORDER BY created_at DESC,id DESC""",
        order["id"],
    )
    return [PaymentAttempt.model_validate(dict(row)) for row in rows]
