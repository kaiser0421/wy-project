import asyncio
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import orjson
import pytest
from asgi_lifespan import LifespanManager

from paymock.delivery import signature
from paymock.main import create_app
from tests.booking.support import assert_invariants, book, cancel, inventory, race, ticket_users
from ticketing.booking import sweeper
from ticketing.booking.idempotency import Claim
from ticketing.core.service import ServiceContext
from ticketing.payments import reconciler
from ticketing.payments.provider import Provider, ProviderUnavailable

pytestmark = pytest.mark.integration


@contextmanager
def provider_transport(app, http):
    # Starlette stores state attributes in a separate dictionary. Restore the
    # original client explicitly so the application's lifespan still owns it.
    state = app.fastapi.state
    original = state.http
    state.http = http
    try:
        yield
    finally:
        state.http = original


@pytest.fixture
def settings_overrides():
    return {
        "db_pool_acquire_timeout_s": 10,
        "db_statement_timeout_ms": 15_000,
        "db_idle_in_tx_timeout_ms": 20_000,
    }


@pytest.fixture
async def provider_app(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("PAYMENT_WEBHOOK_SECRET", settings.payment_webhook_secret.get_secret_value())
    monkeypatch.setenv("PAYMOCK_STORE_PATH", str(tmp_path / "provider.sqlite3"))
    monkeypatch.setenv("PAYMOCK_DROP_CALLBACKS", "1")
    app = create_app()
    async with LifespanManager(app):
        yield app


@pytest.fixture
async def world(db, settings, asgi_app, running_app, provider_app, monkeypatch):
    def provider_client(_=None):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=provider_app), base_url="http://paymock"
        )

    def api_client():
        # Use the started application itself for independent ASGI request state.
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi_app), base_url="http://api")

    monkeypatch.setattr(reconciler, "create_http_client", provider_client)
    state = asgi_app.fastapi.state
    async with provider_client() as provider:
        with provider_transport(asgi_app, provider):
            buyers = await ticket_users(db, settings, api_client, 2)
            try:
                ctx = ServiceContext(settings, state.db, state.redis, asyncio.Event())
                yield buyers[0], buyers[1], provider, ctx
            finally:
                await asyncio.gather(*(buyer.client.aclose() for buyer in buyers))


async def start(buyer, order, key):
    return await buyer.client.post(
        f"/api/v1/orders/{order}/payments",
        headers={**buyer.headers(), "Idempotency-Key": key},
    )


async def confirm(provider, checkout):
    path = urlsplit(checkout["checkout_url"]).path
    response = await provider.post(f"{path}/confirm")
    assert response.status_code == 200, response.text
    return response.json()["event"]


async def callback(buyer, settings, event, *, replace=None, age=0):
    body = orjson.dumps(event)
    sig = signature(
        settings.payment_webhook_secret.get_secret_value(), body, int(time.time()) - age
    )
    return await buyer.client.post(
        "/api/v1/payments/callbacks",
        content=body if replace is None else orjson.dumps(replace),
        headers={"Mockpay-Signature": sig, "Content-Type": "application/json"},
    )


async def place(db, buyer):
    event, seats = await inventory(db, buyer.user_id, 1)
    response = await book(buyer, event, "order-key", seat_ids=seats)
    assert response.status_code == 201, response.text
    return event, seats, response.json()


async def test_payment_keys_and_callback_replay(db, settings, world):
    buyer, other, provider, _ = world
    _, _, order = await place(db, buyer)
    public = order["public_id"]
    assert (await start(other, public, "payment-key")).status_code == 404
    pending = await race(start(buyer, public, f"payment-{n}") for n in range(2))
    assert [result.status_code for result in pending] == [201, 201]
    assert pending[0].json() == pending[1].json()
    checkout = pending[0].json()
    assert await db.fetchval("SELECT provider_captured_at FROM payment_attempts") is None
    replay = await start(buyer, public, "payment-0")
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json() == checkout
    assert await db.fetchval("SELECT count(*) FROM payment_attempts") == 1

    capture = await confirm(provider, checkout)
    doubled = {**capture, "event_id": str(uuid4())}
    a, b = await race(callback(buyer, settings, ev) for ev in (capture, doubled))
    assert a.json() == b.json() == {"disposition": "accepted"}
    again = await callback(buyer, settings, capture)
    assert again.status_code == 200
    assert again.json() == a.json()
    declined = {
        **capture,
        "event_id": str(uuid4()),
        "event_type": "declined",
        "capture_id": None,
        "captured_at": None,
    }
    assert (await callback(buyer, settings, declined)).status_code == 200
    attempts = await buyer.client.get(f"/api/v1/orders/{public}/payments")
    (attempt,) = attempts.json()
    assert attempt["status"] == "captured"
    assert attempts.status_code == 200
    assert await db.fetchval("SELECT count(*) FROM payment_captures") == 1
    assert await db.fetchval("SELECT count(*) FROM refunds") == 0
    assert await db.fetchval("SELECT count(*) FROM outbox") == 2
    paid_at = await db.fetchval("SELECT paid_at FROM orders")
    schema = (await buyer.client.get("/api/openapi.json")).json()
    payment_responses = schema["paths"]["/api/v1/orders/{order_id}/payments"]["post"]["responses"]
    assert payment_responses["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PaidPayment"
    }
    for key in ("payment-0", "payment-1"):
        paid = await start(buyer, public, key)
        assert paid.status_code == 200
        assert paid.headers["Idempotent-Replayed"] == "true"
        assert paid.json().keys() == {"order_id", "status", "paid_at"}
        assert paid.json()["order_id"] == public
        assert paid.json()["status"] == "paid"
        assert datetime.fromisoformat(paid.json()["paid_at"]) == paid_at
    assert await db.fetchval(
        "SELECT provider_captured_at FROM payment_attempts"
    ) == datetime.fromisoformat(capture["captured_at"])

    extra = {
        **capture,
        "event_id": str(uuid4()),
        "capture_id": str(uuid4()),
        "captured_at": (
            datetime.fromisoformat(capture["captured_at"]) + timedelta(seconds=1)
        ).isoformat(),
    }
    tracked = await callback(buyer, settings, extra)
    assert tracked.json() == {"disposition": "refund_required"}
    assert await db.fetchval("SELECT reason FROM refunds") == "duplicate_capture"
    assert await db.fetchval(
        "SELECT provider_captured_at FROM payment_attempts"
    ) == datetime.fromisoformat(capture["captured_at"])
    await assert_invariants(db)


async def test_signature_and_event_validation(db, settings, world):
    buyer, _, provider, _ = world
    _, _, order = await place(db, buyer)
    payment = await start(buyer, order["public_id"], "payment-key")
    capture = await confirm(provider, payment.json())
    altered = {**capture, "amount_twd": capture["amount_twd"] + 1}
    tamper = await callback(buyer, settings, capture, replace=altered)
    assert tamper.status_code == 401
    assert tamper.json()["code"] == "invalid-payment-signature"
    assert (await callback(buyer, settings, capture, age=301)).status_code == 401
    assert (await callback(buyer, settings, altered)).json()["code"] == "payment-event-mismatch"
    assert (await callback(buyer, settings, {**capture, "currency": "USD"})).status_code == 422
    assert await db.fetchval("SELECT count(*) FROM payment_callbacks") == 0
    assert await db.fetchval("SELECT status FROM orders") == "pending_payment"
    await assert_invariants(db)


@pytest.mark.parametrize("sweep_first", [False, True])
async def test_late_capture_preserves_quota(db, settings, test_clock, world, sweep_first):
    buyer, other, provider, ctx = world
    event_id, seats, order = await place(db, buyer)
    payment = await start(buyer, order["public_id"], "payment-key")
    capture = await confirm(provider, payment.json())
    await test_clock.advance(timedelta(seconds=settings.hold_ttl_seconds))
    if sweep_first:
        await sweeper.tick(ctx)
        next_hold = await book(other, event_id, "new-buyer", seat_ids=seats)
        assert next_hold.status_code == 201
        applied = await callback(buyer, settings, capture)
    else:
        applied, _ = await race([callback(buyer, settings, capture), sweeper.tick(ctx)])
        next_hold = await book(other, event_id, "new-buyer", seat_ids=seats)
        assert next_hold.status_code == 201
    assert applied.json() == {"disposition": "refund_required"}
    await reconciler.tick(ctx)
    assert await db.fetchval("SELECT status FROM refunds") == "succeeded"
    assert (
        await db.fetchval(
            "SELECT reserved FROM user_event_quota WHERE user_id=$1",
            buyer.user_id,
        )
        == 0
    )
    assert (
        await db.fetchval(
            "SELECT reserved FROM user_event_quota WHERE user_id=$1",
            other.user_id,
        )
        == 1
    )
    await assert_invariants(db)


async def test_lost_create_response_and_paid_cancel(db, settings, test_clock, world, monkeypatch):
    buyer, _, provider, ctx = world
    event_id, seats, order = await place(db, buyer)
    public = order["public_id"]
    original_create = Provider.create

    async def lose_response(self, *args):
        await original_create(self, *args)
        raise ProviderUnavailable

    with monkeypatch.context() as patch:
        patch.setattr(Provider, "create", lose_response)
        assert (await start(buyer, public, "payment-key")).status_code == 503
    attempt = await db.fetchval("SELECT id FROM payment_attempts")
    assert (
        await db.fetchval("SELECT status FROM idempotency_keys WHERE key='payment-key'")
        == "in_progress"
    )
    lock = Claim(buyer.user_id, "payment-key", "create-payment", b"").lock_id
    async with db.transaction():
        await db.fetchval("SELECT pg_advisory_xact_lock($1)", lock)
        contended = await start(buyer, uuid4(), "payment-key")
        assert contended.status_code == 409
        assert contended.json()["code"] == "idempotency-in-progress"
        assert contended.headers["Retry-After"] == "1"
    changed = await start(buyer, uuid4(), "payment-key")
    assert changed.status_code == 422
    assert changed.json()["code"] == "idempotency-key-reused"
    provider_intent = (await provider.get(f"/v1/intents/{attempt}")).json()

    checkout = await start(buyer, public, "payment-key")
    assert checkout.status_code == 201
    assert checkout.headers["Idempotent-Replayed"] == "true"
    assert checkout.json()["attempt_id"] == str(attempt)
    assert checkout.json()["checkout_url"].endswith(provider_intent["intent_id"])
    assert await db.fetchval("SELECT count(*) FROM payment_attempts") == 1

    capture = await confirm(provider, checkout.json())
    await test_clock.advance(timedelta(seconds=16))
    await reconciler.tick(ctx)
    assert await db.fetchval("SELECT status FROM orders") == "paid"
    assert await db.fetchval(
        "SELECT provider_captured_at FROM payment_attempts"
    ) == datetime.fromisoformat(capture["captured_at"])
    requested = await cancel(buyer, public, "refund-key")
    assert requested.status_code == 200
    assert requested.json()["refund_status"] == "pending"
    assert await db.fetchval("SELECT status FROM seats") == "sold"
    await assert_invariants(db)

    original_refund = Provider.refund

    async def lose_refund_response(self, *args):
        await original_refund(self, *args)
        raise ProviderUnavailable

    with monkeypatch.context() as patch:
        patch.setattr(Provider, "refund", lose_refund_response)
        await reconciler.tick(ctx)
    assert await db.fetchval("SELECT status FROM orders") == "refund_pending"
    assert await db.fetchval("SELECT status FROM refunds") == "failed_retryable"
    await test_clock.advance(timedelta(seconds=31))
    await race([reconciler.tick(ctx), reconciler.tick(ctx)])
    assert await db.fetchval("SELECT attempts FROM refunds") == 2
    assert await db.fetchval("SELECT status FROM orders") == "refunded"
    assert (await book(buyer, event_id, "after-refund", seat_ids=seats)).status_code == 201
    await assert_invariants(db)


async def test_definitive_decline_and_provider_cancellation(db, asgi_app, world):
    buyer, _, provider, ctx = world
    _, _, order = await place(db, buyer)
    public = order["public_id"]

    async def decline(request):
        return httpx.Response(402, request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(decline),
        base_url="http://paymock",
    ) as rejecting:
        with provider_transport(asgi_app, rejecting):
            failure = await start(buyer, public, "payment-key")
    assert failure.status_code == 402
    assert failure.json()["code"] == "payment-declined"
    replay = await start(buyer, public, "payment-key")
    assert replay.status_code == 402
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json()["code"] == failure.json()["code"]
    next_attempt = await start(buyer, public, "another-key")
    assert next_attempt.status_code == 201
    assert (await cancel(buyer, public, "cancel-key")).status_code == 200
    await reconciler.tick(ctx)
    attempt = next_attempt.json()["attempt_id"]
    assert (await provider.get(f"/v1/intents/{attempt}")).json()["status"] == "cancelled"
    assert await db.fetchval("SELECT status FROM provider_cancellations") == "succeeded"
    assert await db.fetchval("SELECT count(*) FROM refunds") == 0
    await assert_invariants(db)


async def test_declined_checkout_replays_bound_attempt(db, settings, world):
    buyer, _, _, _ = world
    _, _, order = await place(db, buyer)
    public = order["public_id"]
    checkout = await start(buyer, public, "payment-key")
    assert checkout.status_code == 201
    declined = {
        "event_id": str(uuid4()),
        "event_type": "declined",
        "merchant_payment_id": checkout.json()["attempt_id"],
        "amount_twd": order["total_twd"],
        "currency": "TWD",
    }
    assert (await callback(buyer, settings, declined)).status_code == 200

    other_checkout = await start(buyer, public, "another-key")
    assert other_checkout.status_code == 201
    assert other_checkout.json()["attempt_id"] != checkout.json()["attempt_id"]
    replay = await start(buyer, public, "payment-key")
    assert replay.status_code == 402
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json()["code"] == "payment-declined"
    assert "checkout_url" not in replay.json()
    assert await db.fetchval("SELECT count(*) FROM payment_attempts") == 2
    assert (
        await db.fetchval("SELECT count(*) FROM payment_attempts WHERE status='awaiting_customer'")
        == 1
    )
    await assert_invariants(db)


async def test_open_response_after_hold_expires(db, test_clock, world, monkeypatch):
    buyer, _, _, _ = world
    await test_clock.advance(timedelta())
    _, _, order = await place(db, buyer)
    public = order["public_id"]
    original_create = Provider.create
    responses = asyncio.Barrier(3)
    resume = asyncio.Event()

    async def delay_response(self, *args):
        intent = await original_create(self, *args)
        assert intent.status == "open"
        await responses.wait()
        await resume.wait()
        return intent

    monkeypatch.setattr(Provider, "create", delay_response)
    async with asyncio.timeout(10), asyncio.TaskGroup() as work:
        requests = [work.create_task(start(buyer, public, f"payment-{n}")) for n in range(2)]
        await responses.wait()
        assert await db.fetchval("SELECT status FROM orders") == "pending_payment"
        await test_clock.set(datetime.fromisoformat(order["expires_at"]))
        resume.set()

    for request in requests:
        assert request.result().status_code == 402
        assert request.result().json()["code"] == "payment-declined"
    assert await db.fetchval("SELECT status FROM orders") == "expired"
    assert await db.fetchval("SELECT checkout_url FROM payment_attempts") is None
    assert (
        await db.fetchval(
            "SELECT count(*) FROM idempotency_keys WHERE operation='create-payment' "
            "AND status='completed' AND response_status=402"
        )
        == 2
    )
    assert await db.fetchval("SELECT count(*) FROM outbox") == 2
    assert await db.fetchval("SELECT count(*) FROM provider_cancellations") == 1
    await assert_invariants(db)


async def test_reconciler_polls_when_callback_url_changes(db, test_clock, world, monkeypatch):
    buyer, _, provider, ctx = world
    _, _, order = await place(db, buyer)
    public = order["public_id"]
    original_create = Provider.create

    async def lose_response(self, *args):
        await original_create(self, *args)
        raise ProviderUnavailable

    with monkeypatch.context() as patch:
        patch.setattr(Provider, "create", lose_response)
        assert (await start(buyer, public, "payment-key")).status_code == 503
    attempt_id = await db.fetchval("SELECT id FROM payment_attempts")
    original = (await provider.get(f"/v1/intents/{attempt_id}")).json()
    changed_url = "http://api-new/api/v1/payments/callbacks"
    ctx = replace(
        ctx, settings=ctx.settings.model_copy(update={"payment_callback_url": changed_url})
    )
    conflict = await provider.post(
        "/v1/intents",
        json={
            "merchant_payment_id": str(attempt_id),
            "amount_twd": original["amount_twd"],
            "currency": "TWD",
            "expires_at": original["expires_at"],
            "callback_url": changed_url,
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "intent-key-reused"

    original_poll = Provider.poll
    for field, change in (("amount_twd", 1), ("expires_at", timedelta(seconds=1))):

        async def mismatched_intent(self, merchant_id, field=field, change=change):
            intent = await original_poll(self, merchant_id)
            return intent.model_copy(update={field: getattr(intent, field) + change})

        with monkeypatch.context() as patch:
            patch.setattr(Provider, "poll", mismatched_intent)
            await test_clock.advance(timedelta(seconds=16))
            await reconciler.tick(ctx)
        assert await db.fetchval("SELECT status FROM payment_attempts") == "creating"
        assert await db.fetchval("SELECT status FROM idempotency_keys WHERE key='payment-key'") == (
            "in_progress"
        )

    await test_clock.advance(timedelta(seconds=16))
    await race([reconciler.tick(ctx), reconciler.tick(ctx)])
    assert await db.fetchval("SELECT status FROM payment_attempts") == "awaiting_customer"
    assert await db.fetchval("SELECT count(*) FROM payment_attempts") == 1
    assert (
        await db.fetchval("SELECT response_status FROM idempotency_keys WHERE key='payment-key'")
        == 201
    )
    recovered = await start(buyer, public, "payment-key")
    assert recovered.status_code == 201
    assert recovered.headers["Idempotent-Replayed"] == "true"
    assert recovered.json()["checkout_url"].endswith(original["intent_id"])
    await assert_invariants(db)
