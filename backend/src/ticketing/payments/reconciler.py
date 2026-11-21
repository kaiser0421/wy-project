"""Recover provider calls and drain durable refund and cancellation work."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

import asyncpg

from ticketing.booking.database import transaction
from ticketing.booking.idempotency import finish_payment
from ticketing.booking.orders import release_order
from ticketing.core.errors import ProblemError
from ticketing.core.http import create_http_client
from ticketing.core.logging import get_logger
from ticketing.core.service import ServiceContext
from ticketing.payments.attempts import Attempt, finalize_attempt
from ticketing.payments.callbacks import AppliedEvent, apply_event, lock_payment, payment_declined
from ticketing.payments.provider import Provider, ProviderUnavailable

log = get_logger(__name__)
# Per tick, each phase may handle up to this many rows. Every open checkout is re-polled about
# every 15 s, so polling gets the larger budget (a Scenario A run leaves hundreds open).
_BATCH_SIZE = 50
_POLL_BATCH_SIZE = 200
# Rows are claimed only when they can start: a phase claims `width` rows, processes them, then
# claims again. Widths 4 + 1 + 1 keep the reconciler at <= 6 of the worker's DB connections
# (DB_POOL_MAX, default 10), leaving room for the sweeper, imports and maintenance.
_POLL_WIDTH = 4
_QUEUE_WIDTH = 1
# Less than the 30-second durable lease so a stalled phase cannot outlive its claim.
_PHASE_TIMEOUT_S = 12.0


async def _due_attempts(ctx: ServiceContext, limit: int) -> list[asyncpg.Record]:
    async with transaction(ctx.db, ctx.settings) as conn:
        rows: list[asyncpg.Record] = await conn.fetch(
            """WITH due AS (
                 SELECT p.id FROM payment_attempts p
                 WHERE COALESCE(p.last_polled_at, p.updated_at) <=
                       (SELECT app_now()-interval '15 seconds')
                   AND (status IN ('creating','awaiting_customer')
                        OR (status='captured' AND checkout_url IS NULL))
                   AND (status<>'captured' OR EXISTS (
                     SELECT 1 FROM idempotency_keys k WHERE k.operation='create-payment'
                       AND k.status='in_progress' AND k.resource_ref=p.id::text
                   ))
                 ORDER BY COALESCE(p.last_polled_at,p.updated_at),p.id
                 LIMIT $1 FOR NO KEY UPDATE SKIP LOCKED
               )
               UPDATE payment_attempts p SET last_polled_at=app_now() FROM due
               WHERE p.id=due.id RETURNING p.*""",
            limit,
        )
    return rows


async def _recover_attempt(ctx: ServiceContext, provider: Provider, row: asyncpg.Record) -> None:
    if ctx.stop.is_set():
        return
    attempt = Attempt.from_row(row)
    try:
        if row["status"] == "creating":
            intent = await provider.create(attempt.id, attempt.amount_twd, attempt.expires_at)
        else:
            intent = await provider.poll(attempt.id)
        await finalize_attempt(ctx.db, ctx.settings, attempt, intent)
    except (ProviderUnavailable, ProblemError) as exc:
        log.warning("payment.attempt_retry", error=type(exc).__name__, attempt_id=str(attempt.id))


async def _claim_refunds(ctx: ServiceContext, owner: str, limit: int) -> list[asyncpg.Record]:
    async with transaction(ctx.db, ctx.settings) as conn:
        rows: list[asyncpg.Record] = await conn.fetch(
            """WITH due AS (
                 SELECT id FROM refunds
                 WHERE status <> 'succeeded' AND next_try_at <= (SELECT app_now())
                   AND (lease_until IS NULL OR lease_until <= app_now())
                 ORDER BY next_try_at,id LIMIT $2 FOR NO KEY UPDATE SKIP LOCKED
               )
               UPDATE refunds r SET attempts=r.attempts+1,
                 lease_owner=$1, lease_until=app_now()+interval '30 seconds'
               FROM due, payment_captures c
               WHERE r.id=due.id AND r.provider_capture_id=c.provider_capture_id
               RETURNING r.id,r.attempts,r.reason,c.provider_capture_id,c.amount_twd,c.order_id""",
            owner,
            limit,
        )
    return rows


def _retry_delay(row: asyncpg.Record) -> int:
    attempts: int = row["attempts"]
    return min(300, 1 << min(attempts, 9))


async def _refund(
    ctx: ServiceContext,
    provider: Provider,
    owner: str,
    row: asyncpg.Record,
) -> None:
    if ctx.stop.is_set():
        return
    succeeded = True
    try:
        await provider.refund(row["id"], row["provider_capture_id"], row["amount_twd"])
    except ProviderUnavailable:
        succeeded = False
        log.warning("payment.refund_retry", refund_id=str(row["id"]))
    async with transaction(ctx.db, ctx.settings) as conn:
        # Cancellation and refund confirmation both lock order -> refund -> seats.
        if succeeded and row["reason"] == "order_cancelled":
            await conn.fetchval(
                "SELECT id FROM orders WHERE id=$1 FOR NO KEY UPDATE", row["order_id"]
            )
        changed = await conn.fetchval(
            """UPDATE refunds SET status=$3,lease_owner=NULL,lease_until=NULL,
                 updated_at=app_now(),next_try_at=app_now()+make_interval(secs=>$4)
               WHERE id=$1 AND lease_owner=$2 AND status <> 'succeeded' RETURNING id""",
            row["id"],
            owner,
            "succeeded" if succeeded else "failed_retryable",
            _retry_delay(row),
        )
        if changed is not None and succeeded and row["reason"] == "order_cancelled":
            released = await release_order(conn, row["order_id"], "refund_pending", "refunded")
            if released is None:
                raise RuntimeError("Refunded order could not release its seats.")


async def _claim_cancellations(ctx: ServiceContext, owner: str, limit: int) -> list[asyncpg.Record]:
    async with transaction(ctx.db, ctx.settings) as conn:
        rows: list[asyncpg.Record] = await conn.fetch(
            """WITH due AS (
                 SELECT attempt_id FROM provider_cancellations
                 WHERE status <> 'succeeded' AND next_try_at <= (SELECT app_now())
                   AND (lease_until IS NULL OR lease_until <= app_now())
                 ORDER BY next_try_at,attempt_id LIMIT $2 FOR NO KEY UPDATE SKIP LOCKED
               )
               UPDATE provider_cancellations c SET attempts=c.attempts+1,
                 lease_owner=$1, lease_until=app_now()+interval '30 seconds'
               FROM due, payment_attempts p WHERE c.attempt_id=due.attempt_id AND p.id=c.attempt_id
               RETURNING c.attempt_id,c.attempts,p.order_id,p.amount_twd,p.expires_at""",
            owner,
            limit,
        )
    return rows


async def _close_intent(
    conn: asyncpg.Connection,
    row: asyncpg.Record,
    attempt_id: UUID,
) -> None:
    order, _ = await lock_payment(conn, row["order_id"], attempt_id)
    changed = await conn.fetchval(
        """UPDATE payment_attempts SET status='abandoned',updated_at=app_now()
           WHERE id=$1 AND status IN ('creating','awaiting_customer')
             AND NOT EXISTS (SELECT 1 FROM payment_captures WHERE attempt_id=$1)
           RETURNING id""",
        attempt_id,
    )
    if changed is not None:
        await finish_payment(conn, attempt_id, payment_declined(order["public_id"]))


async def _cancel_provider(
    ctx: ServiceContext,
    provider: Provider,
    owner: str,
    row: asyncpg.Record,
) -> None:
    if ctx.stop.is_set():
        return
    attempt_id = row["attempt_id"]
    applied = AppliedEvent()
    try:
        intent = await provider.cancel(attempt_id)
        async with transaction(ctx.db, ctx.settings) as conn:
            if intent is not None and intent.event is not None:
                applied = await apply_event(conn, intent.event)
            else:
                await _close_intent(conn, row, attempt_id)
            await conn.execute(
                """UPDATE provider_cancellations SET status='succeeded',
                     lease_owner=NULL,lease_until=NULL
                   WHERE attempt_id=$1 AND lease_owner=$2 AND status <> 'succeeded'""",
                attempt_id,
                owner,
            )
    except Exception as exc:
        log.warning("payment.cancel_failed", error=type(exc).__name__, attempt_id=str(attempt_id))
        if isinstance(exc, ProviderUnavailable) or (
            isinstance(exc, ProblemError) and exc.retryable
        ):
            await _retry_cancellation(ctx, owner, row)
        return
    applied.record()


async def _retry_cancellation(ctx: ServiceContext, owner: str, row: asyncpg.Record) -> None:
    try:
        async with transaction(ctx.db, ctx.settings) as conn:
            await conn.execute(
                """UPDATE provider_cancellations SET status='failed_retryable',
                     lease_owner=NULL,lease_until=NULL,next_try_at=app_now()+make_interval(secs=>$3)
                   WHERE attempt_id=$1 AND lease_owner=$2 AND status <> 'succeeded'""",
                row["attempt_id"],
                owner,
                _retry_delay(row),
            )
    except Exception as exc:
        # This transaction can also hit contention. The existing lease bounds the recovery wait.
        log.warning(
            "payment.cancel_reschedule_failed",
            error=type(exc).__name__,
            attempt_id=str(row["attempt_id"]),
        )


async def _clean_completed_keys(ctx: ServiceContext) -> None:
    async with transaction(ctx.db, ctx.settings) as conn:
        await conn.execute(
            """WITH old AS (
                 SELECT user_id,key FROM idempotency_keys
                 WHERE status='completed' AND updated_at<(SELECT app_now()-interval '24 hours')
                 ORDER BY updated_at LIMIT 200 FOR UPDATE SKIP LOCKED
               )
               DELETE FROM idempotency_keys k USING old
               WHERE k.user_id=old.user_id AND k.key=old.key""",
        )


async def _unclaim_attempts(ctx: ServiceContext, rows: list[asyncpg.Record]) -> None:
    """Make claimed-but-unpolled attempts due again (NULL falls back to updated_at).

    Only rows still carrying this claim's stamp are reset, so a later poll is never undone.
    """
    async with transaction(ctx.db, ctx.settings) as conn:
        await conn.execute(
            """UPDATE payment_attempts p SET last_polled_at=NULL
               FROM unnest($1::uuid[], $2::timestamptz[]) AS c(id, stamp)
               WHERE p.id=c.id AND p.last_polled_at=c.stamp""",
            [row["id"] for row in rows],
            [row["last_polled_at"] for row in rows],
        )


async def _unclaim_refunds(ctx: ServiceContext, owner: str, rows: list[asyncpg.Record]) -> None:
    async with transaction(ctx.db, ctx.settings) as conn:
        await conn.execute(
            """UPDATE refunds SET lease_owner=NULL, lease_until=NULL
               WHERE id = ANY($1::uuid[]) AND lease_owner=$2""",
            [row["id"] for row in rows],
            owner,
        )


async def _unclaim_cancellations(
    ctx: ServiceContext, owner: str, rows: list[asyncpg.Record]
) -> None:
    async with transaction(ctx.db, ctx.settings) as conn:
        await conn.execute(
            """UPDATE provider_cancellations SET lease_owner=NULL, lease_until=NULL
               WHERE attempt_id = ANY($1::uuid[]) AND lease_owner=$2""",
            [row["attempt_id"] for row in rows],
            owner,
        )


async def _run_phase(
    ctx: ServiceContext,
    name: str,
    claim: Callable[[int], Awaitable[list[asyncpg.Record]]],
    process: Callable[[asyncpg.Record], Awaitable[None]],
    budget: int,
    width: int,
    unclaim: Callable[[list[asyncpg.Record]], Awaitable[None]],
) -> None:
    """Claim small batches as they can start, until the queue, the budget or the time runs out.

    A claim marks rows as handled (poll time or lease), so claiming only what runs right away
    means a phase timeout never leaves claimed-but-untouched rows behind.
    """
    if ctx.stop.is_set():
        return

    unfinished: list[asyncpg.Record] = []

    async def run_row(row: asyncpg.Record) -> None:
        if ctx.stop.is_set():
            return  # never started: stays in `unfinished` and is handed back below
        try:
            await process(row)
        except Exception as exc:
            log.warning("payment.reconcile_row_failed", phase=name, error=type(exc).__name__)
        unfinished.remove(row)

    handled = 0
    try:
        async with asyncio.timeout(_PHASE_TIMEOUT_S):
            while handled < budget and not ctx.stop.is_set():
                rows = await claim(min(width, budget - handled))
                if not rows:
                    return
                handled += len(rows)
                unfinished.extend(rows)
                async with asyncio.TaskGroup() as work:
                    for row in rows:
                        work.create_task(run_row(row))
    except Exception as exc:
        log.warning("payment.reconcile_phase_failed", phase=name, error=type(exc).__name__)
    finally:
        # A timeout or shutdown interrupted these rows: hand them back so they are due again
        # instead of looking freshly polled or staying leased for 30 s.
        if unfinished:
            try:
                await asyncio.shield(unclaim(list(unfinished)))
            except Exception as exc:
                log.warning(
                    "payment.reconcile_unclaim_failed", phase=name, error=type(exc).__name__
                )


async def tick(ctx: ServiceContext) -> None:
    if ctx.stop.is_set():
        return
    owner = str(uuid4())
    async with create_http_client(ctx.settings.payment_provider_url) as http:
        provider = Provider(http, ctx.settings)
        async with asyncio.TaskGroup() as phases:
            phases.create_task(
                _run_phase(
                    ctx,
                    "poll",
                    lambda limit: _due_attempts(ctx, limit),
                    lambda row: _recover_attempt(ctx, provider, row),
                    _POLL_BATCH_SIZE,
                    _POLL_WIDTH,
                    lambda rows: _unclaim_attempts(ctx, rows),
                )
            )
            phases.create_task(
                _run_phase(
                    ctx,
                    "cancel",
                    lambda limit: _claim_cancellations(ctx, owner, limit),
                    lambda row: _cancel_provider(ctx, provider, owner, row),
                    _BATCH_SIZE,
                    _QUEUE_WIDTH,
                    lambda rows: _unclaim_cancellations(ctx, owner, rows),
                )
            )
            phases.create_task(
                _run_phase(
                    ctx,
                    "refund",
                    lambda limit: _claim_refunds(ctx, owner, limit),
                    lambda row: _refund(ctx, provider, owner, row),
                    _BATCH_SIZE,
                    _QUEUE_WIDTH,
                    lambda rows: _unclaim_refunds(ctx, owner, rows),
                )
            )
    if not ctx.stop.is_set():
        await _clean_completed_keys(ctx)
