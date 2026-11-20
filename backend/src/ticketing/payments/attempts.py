"""Create attempts without holding database locks across provider calls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import asyncpg

from ticketing.booking.database import fence, transaction
from ticketing.booking.idempotency import Claim, Result, finish_payment
from ticketing.booking.orders import visible_order
from ticketing.core.errors import ProblemError, server_busy
from ticketing.core.settings import Settings
from ticketing.payments.callbacks import (
    AppliedEvent,
    apply_event,
    inconsistent_event,
    lock_payment,
    payment_declined,
)
from ticketing.payments.models import Checkout, PaymentAttempt
from ticketing.payments.provider import Intent, Provider, ProviderUnavailable


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
    decision_at = await conn.fetchval("SELECT app_now()")
    if order["status"] != "pending_payment" or decision_at >= order["expires_at"]:
        raise ProblemError(409, "order-not-payable", "Order no longer accepts payment")
    row = await conn.fetchrow(
        """SELECT * FROM payment_attempts WHERE order_id=$1
           AND status IN ('creating','awaiting_customer','captured')""",
        order["id"],
    )
    if row is None:
        row = await conn.fetchrow(
            """INSERT INTO payment_attempts
                 (order_id,amount_twd,status,expires_at,created_at,updated_at)
               VALUES ($1,$2,'creating',$3,app_now(),app_now()) RETURNING *""",
            order["id"],
            order["total_twd"],
            order["expires_at"],
        )
    attempt = Attempt.from_row(row)
    if row["checkout_url"] is not None:
        result = checkout(attempt, row["checkout_url"])
        await claim.save(conn, result, resource_ref=attempt.id)
        return result
    await claim.save(conn, resource_ref=attempt.id)
    return attempt


async def finalize_attempt(
    pool: asyncpg.Pool,
    settings: Settings,
    attempt: Attempt,
    intent: Intent,
    claim: Claim | None = None,
) -> Result:
    """Finalize the durable checkout response for every key bound to this intent."""
    if intent.amount_twd != attempt.amount_twd or intent.expires_at != attempt.expires_at:
        raise inconsistent_event()
    applied = AppliedEvent()
    async with transaction(pool, settings) as conn:
        # Event insertion precedes order locking on both the API and reconciler paths.
        if intent.event is not None:
            applied = await apply_event(conn, intent.event)
        order, row = await lock_payment(conn, attempt.order_id, attempt.id)
        url = row["checkout_url"]
        if url is not None:
            result = checkout(attempt, url)
        elif intent.intent_id is not None and (
            (row["status"] == "creating" and order["status"] == "pending_payment")
            or row["status"] == "captured"
        ):
            url = f"{settings.payment_checkout_base_url.rstrip('/')}/{intent.intent_id}"
            updated = await conn.fetchval(
                """UPDATE payment_attempts SET provider_intent_id=$2,checkout_url=$3,
                       status=CASE WHEN status='creating' THEN 'awaiting_customer' ELSE status END,
                       updated_at=app_now()
                   WHERE id=$1 AND status IN ('creating','captured') AND checkout_url IS NULL
                   RETURNING id""",
                attempt.id,
                str(intent.intent_id),
                url,
            )
            if updated is None:
                raise RuntimeError("Locked attempt could not be finalized.")
            result = checkout(attempt, url)
        else:
            result = payment_declined(order["public_id"])
        await finish_payment(conn, attempt.id, result)
        if claim is not None:
            # A callback or another API request can complete this key before txn2.
            stored = await claim.completed_result(conn)
            if stored is None:
                raise RuntimeError("Payment key could not be completed.")
            result = stored
    applied.record()
    return result


async def create_payment(
    pool: asyncpg.Pool,
    settings: Settings,
    provider: Provider,
    session_id: UUID,
    public_id: UUID,
    claim: Claim,
) -> Result:
    async with transaction(pool, settings) as conn:
        await fence(conn, session_id, claim.user_id)
        prior = await claim.check(conn)
        if prior is not None:
            return prior
        work = await _begin(conn, public_id, claim)
    if isinstance(work, Result):
        return work
    try:
        intent = await provider.create(work.id, work.amount_twd, work.expires_at)
        return await finalize_attempt(pool, settings, work, intent, claim)
    except ProviderUnavailable as exc:
        raise server_busy("Payment provider outcome is pending. Retry using the same key.") from exc


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
