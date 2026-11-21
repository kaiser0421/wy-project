"""Apply signed callback and polled provider events under the same order lock."""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

import asyncpg

from ticketing.booking.idempotency import Result, finish_payment
from ticketing.booking.orders import lock_order_seats, release_order
from ticketing.core.context import current_request_id
from ticketing.core.errors import ProblemError, server_busy
from ticketing.payments.models import PaidPayment, PaymentEvent
from ticketing.realtime.outbox import emit_seat_changes
from ticketing.telemetry import business

_SIGNATURE = re.compile(r"t=([0-9]{1,12}),v1=([0-9a-f]{64})")
Disposition = Literal["accepted", "refund_required", "acknowledged"]


def verify_signature(body: bytes, signature: str, secret: str) -> None:
    match = _SIGNATURE.fullmatch(signature)
    if match is not None:
        stamp, signature_hex = match.groups()
        expected = hmac.new(secret.encode(), stamp.encode() + b"." + body, hashlib.sha256)
        # Signature freshness concerns transport time. The order deadline uses the DB clock.
        if abs(int(time.time()) - int(stamp)) <= 300 and hmac.compare_digest(
            expected.hexdigest(), signature_hex
        ):
            return
    raise ProblemError(401, "invalid-payment-signature", "Invalid payment signature")


def payment_declined(public_id: UUID) -> Result:
    problem = ProblemError(
        402,
        "payment-declined",
        "Payment declined",
        "Start a payment with a new key if the order still has an active hold.",
    )
    return Result(
        402,
        problem.to_body(
            instance=f"/api/v1/orders/{public_id}/payments",
            request_id=current_request_id(),
        ),
    )


def payment_paid(public_id: UUID, paid_at: datetime) -> Result:
    model = PaidPayment(order_id=public_id, status="paid", paid_at=paid_at)
    return Result(200, model.model_dump(mode="json"))


@dataclass(frozen=True, slots=True)
class AppliedEvent:
    disposition: Disposition = "acknowledged"
    event_id: int | None = None
    outcome: str | None = None
    expired: bool = False

    def record(self) -> None:
        # Metrics are emitted after commit, including events consumed by the worker.
        if self.event_id is not None:
            if self.outcome is not None:
                business.record(self.event_id, self.outcome)
            if self.expired:
                business.record(self.event_id, "expiry")


def inconsistent_event() -> ProblemError:
    return ProblemError(422, "payment-event-mismatch", "Payment event does not match its attempt")


async def lock_payment(
    conn: asyncpg.Connection,
    order_id: int,
    attempt_id: UUID,
) -> tuple[asyncpg.Record, asyncpg.Record]:
    order = await conn.fetchrow("SELECT * FROM orders WHERE id=$1 FOR NO KEY UPDATE", order_id)
    attempt = await conn.fetchrow(
        "SELECT * FROM payment_attempts WHERE id=$1 AND order_id=$2 FOR NO KEY UPDATE",
        attempt_id,
        order_id,
    )
    if order is None or attempt is None:
        raise RuntimeError("Payment attempt lost its order.")
    return order, attempt


async def _accept_capture(
    conn: asyncpg.Connection,
    order: asyncpg.Record,
    attempt: asyncpg.Record,
) -> datetime:
    order_id = order["id"]
    paid_at: datetime | None = await conn.fetchval(
        """UPDATE orders SET status='paid', paid_at=app_now(), version=version+1
           WHERE id=$1 AND status='pending_payment' RETURNING paid_at""",
        order_id,
    )
    if paid_at is None:
        raise RuntimeError("Locked order could not be paid.")
    await lock_order_seats(conn, order_id)
    seats = await conn.fetch(
        """UPDATE seats SET status='sold',version=version+1,updated_at=app_now()
           WHERE order_id=$1 AND status='held' RETURNING seat_idx,version""",
        order_id,
    )
    if len(seats) != order["quantity"]:
        raise RuntimeError("Payment does not own all of its held seats.")
    # If this is a reordered capture from an old intent, fence off its successor for recovery.
    await conn.execute(
        """INSERT INTO provider_cancellations (attempt_id, next_try_at)
           SELECT id,app_now() FROM payment_attempts WHERE order_id=$1 AND id<>$2
             AND status IN ('creating','awaiting_customer') ON CONFLICT DO NOTHING""",
        order_id,
        attempt["id"],
    )
    await conn.execute(
        """UPDATE payment_attempts SET status='captured',updated_at=app_now() WHERE id=$1
           AND status<>'captured' AND NOT EXISTS (
               SELECT 1 FROM payment_attempts WHERE order_id=$2 AND id<>$1
                 AND status IN ('creating','awaiting_customer','captured')
           )""",
        attempt["id"],
        order_id,
    )
    await emit_seat_changes(
        conn,
        order["event_id"],
        [(s["seat_idx"], "sold", s["version"]) for s in seats],
    )
    return paid_at


async def _capture(
    conn: asyncpg.Connection,
    order: asyncpg.Record,
    attempt: asyncpg.Record,
    event: PaymentEvent,
) -> AppliedEvent:
    previous = await conn.fetchrow(
        "SELECT attempt_id, disposition FROM payment_captures WHERE provider_capture_id=$1",
        event.capture_id,
    )
    if previous is not None:
        if previous["attempt_id"] != attempt["id"]:
            raise inconsistent_event()
        return AppliedEvent(previous["disposition"])

    pending = order["status"] == "pending_payment"
    decision_at = await conn.fetchval("SELECT app_now()")
    accepted = pending and decision_at < order["expires_at"]
    disposition: Disposition = "accepted" if accepted else "refund_required"
    inserted = await conn.fetchval(
        """INSERT INTO payment_captures
             (provider_capture_id,attempt_id,order_id,amount_twd,disposition,captured_at)
           VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (provider_capture_id) DO NOTHING
           RETURNING provider_capture_id""",
        event.capture_id,
        attempt["id"],
        order["id"],
        event.amount_twd,
        disposition,
        event.captured_at,
    )
    if inserted is None:
        # Identical attempts already serialize on this order. A cross-order ID collision is invalid.
        raise inconsistent_event()

    # Preserve the provider's first capture timestamp even when the charge needs a refund.
    await conn.execute(
        """UPDATE payment_attempts SET provider_captured_at=$2
           WHERE id=$1 AND provider_captured_at IS NULL""",
        attempt["id"],
        event.captured_at,
    )
    if accepted:
        paid_at = await _accept_capture(conn, order, attempt)
        await finish_payment(conn, attempt["id"], payment_paid(order["public_id"], paid_at))
        return AppliedEvent(disposition, order["event_id"], "pay_ok")
    if pending:
        released = await release_order(conn, order["id"], "pending_payment", "expired")
        if released is None:
            # The decision clock moved backwards during the transaction. Retry the decision.
            raise server_busy()
    reason = (
        "duplicate_capture"
        if order["status"] in ("paid", "refund_pending", "refunded")
        else "late_capture"
    )
    await conn.execute(
        "INSERT INTO refunds (provider_capture_id, reason, next_try_at) VALUES ($1,$2,app_now())",
        event.capture_id,
        reason,
    )
    terminal = await conn.fetchval(
        """UPDATE payment_attempts SET status='abandoned',updated_at=app_now()
           WHERE id=$1 AND status IN ('creating','awaiting_customer') RETURNING id""",
        attempt["id"],
    )
    if terminal is not None:
        await finish_payment(conn, attempt["id"], payment_declined(order["public_id"]))
    return AppliedEvent(disposition, order["event_id"], "pay_fail", expired=pending)


async def apply_event(conn: asyncpg.Connection, event: PaymentEvent) -> AppliedEvent:
    identity = await conn.fetchrow(
        "SELECT order_id, amount_twd FROM payment_attempts WHERE id=$1",
        event.merchant_payment_id,
    )
    if identity is None:
        raise ProblemError(404, "not-found", "Not found", "Payment attempt not found.")
    if identity["amount_twd"] != event.amount_twd:
        raise inconsistent_event()
    payload = event.model_dump_json()
    inserted = await conn.fetchval(
        """INSERT INTO payment_callbacks
             (provider_event_id,attempt_id,event_type,payload,request_id,received_at)
           VALUES ($1,$2,$3,$4,$5,app_now())
           ON CONFLICT DO NOTHING RETURNING provider_event_id""",
        event.event_id,
        event.merchant_payment_id,
        event.event_type,
        payload,
        current_request_id(),
    )
    if inserted is None:
        matches = await conn.fetchval(
            "SELECT payload=$2::jsonb FROM payment_callbacks WHERE provider_event_id=$1",
            event.event_id,
            payload,
        )
        if not matches:
            raise inconsistent_event()
        # The conflicting INSERT waits for commit. Read capture disposition under the order lock.
        if event.capture_id is not None:
            await lock_payment(conn, identity["order_id"], event.merchant_payment_id)
            previous = await conn.fetchval(
                "SELECT disposition FROM payment_captures WHERE provider_capture_id=$1",
                event.capture_id,
            )
            if previous is None:
                raise RuntimeError("Recorded capture event has no capture.")
            return AppliedEvent(previous)
        return AppliedEvent()

    order, attempt = await lock_payment(conn, identity["order_id"], event.merchant_payment_id)
    if event.event_type == "captured":
        return await _capture(conn, order, attempt, event)
    updated = await conn.fetchval(
        """UPDATE payment_attempts SET status=$2,updated_at=app_now()
           WHERE id=$1 AND status IN ('creating','awaiting_customer')
             AND NOT EXISTS (SELECT 1 FROM payment_captures WHERE attempt_id=$1)
           RETURNING id""",
        attempt["id"],
        "failed" if event.event_type == "declined" else "abandoned",
    )
    if updated is not None:
        await finish_payment(conn, attempt["id"], payment_declined(order["public_id"]))
        return AppliedEvent(event_id=order["event_id"], outcome="pay_fail")
    return AppliedEvent()
