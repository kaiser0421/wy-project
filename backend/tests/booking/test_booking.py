import asyncio
from datetime import timedelta
from uuid import UUID

import pytest
from redis.asyncio import Redis

from tests.booking.naive import broken_strategy
from tests.booking.support import assert_invariants, book, cancel, inventory, race, ticket_users
from tests.support import create_user, login
from ticketing.booking import sweeper, test_hooks
from ticketing.booking.idempotency import Claim, request_hash
from ticketing.core.db import create_pool
from ticketing.core.service import ServiceContext

pytestmark = pytest.mark.integration


@pytest.fixture
def settings_overrides():
    # The races measure seat and quota behavior for all entrants through a ten-connection pool.
    return {
        "db_pool_acquire_timeout_s": 45,
        "db_statement_timeout_ms": 20_000,
        "db_lock_timeout_ms": 3_000,
        "db_idle_in_tx_timeout_ms": 40_000,
    }


@pytest.fixture
async def buyers(db, settings, make_client):
    all_sessions = []

    async def create(count):
        sessions = await ticket_users(db, settings, make_client, count)
        all_sessions.extend(sessions)
        return sessions

    yield create
    await asyncio.gather(*(session.client.aclose() for session in all_sessions))


@pytest.mark.parametrize(("users", "patterns"), [(200, ((0,),)), (150, ((0, 1), (1, 2), (2, 0)))])
async def test_specific_seat_races(db, buyers, users, patterns):
    sessions = await buyers(users)
    event, seats = await inventory(db, sessions[0].user_id, 3)
    responses = await race(
        book(
            session,
            event,
            f"specific-race-{n}",
            seat_ids=[seats[i] for i in patterns[n % len(patterns)]],
        )
        for n, session in enumerate(sessions)
    )
    assert sum(r.status_code == 201 for r in responses) == 1
    assert {r.json()["code"] for r in responses if r.status_code == 409} <= {
        "seat-contended",
        "seat-unavailable",
    }
    assert {r.status_code for r in responses} == {201, 409}
    await assert_invariants(db)


async def test_zone_converges(db, buyers):
    sessions = await buyers(500)
    event, _ = await inventory(db, sessions[0].user_id, 300)

    async def allocate(session):
        for _ in range(3):
            response = await book(session, event, "zone-race", zone_code="A", quantity=2)
            if response.status_code == 201:
                return True
            assert response.status_code == 409, response.text
            if response.json()["code"] == "insufficient-seats":
                break
            assert response.json()["code"] == "allocation-contended", response.text
        return False

    results = await race(allocate(session) for session in sessions)
    for session, succeeded in zip(sessions, results, strict=True):
        if not succeeded and not await allocate(session):
            break
    assert await db.fetchval("SELECT count(*) FROM orders") == 150
    assert await db.fetchval("SELECT count(*) FROM seats WHERE status='held'") == 300
    await assert_invariants(db)


async def test_quota_race(db, buyers):
    sessions = await buyers(100)
    event, _ = await inventory(db, sessions[0].user_id, 600)
    for session in sessions:
        results = await race(
            book(session, event, f"quota-key-{n}", zone_code="A", quantity=3) for n in range(2)
        )
        assert sorted(r.status_code for r in results) == [201, 409]
        assert next(r for r in results if r.status_code == 409).json()["code"] == (
            "purchase-limit-exceeded"
        )
    await assert_invariants(db)


@pytest.mark.parametrize("strategy", ["normal", "indexed", "naive"])
async def test_break_it(db, buyers, monkeypatch, strategy):
    if strategy != "normal":
        monkeypatch.setenv("BOOKING_STRATEGY", "naive")
        monkeypatch.setattr(test_hooks, "override", broken_strategy())
    if strategy == "naive":
        await db.execute("DROP INDEX order_items_one_active_claim")
    first, second = await buyers(2)
    event, seats = await inventory(db, first.user_id, 3)
    for n, seat in enumerate(seats):
        responses = await race(
            book(session, event, f"break-it-{n}", seat_ids=[seat]) for session in (first, second)
        )
        assert sorted(r.status_code for r in responses) == (
            [201, 201] if strategy == "naive" else [201, 409]
        )
    if strategy == "naive":
        with pytest.raises(AssertionError, match="seat active claims") as detected:
            await assert_invariants(db)
        print(detected.value)
    else:
        await assert_invariants(db)


async def test_idempotency_contention_and_canonical_replay(db, buyers):
    (buyer,) = await buyers(1)
    event, seats = await inventory(db, buyer.user_id, 2)
    claim = Claim(
        buyer.user_id, "hold-key", "create-order", request_hash("create-order", event, {})
    )
    async with db.transaction():
        await db.execute("SELECT pg_advisory_xact_lock($1::bigint)", claim.lock_id)
        busy = await book(buyer, event, "hold-key", seat_ids=seats)
        assert busy.status_code == 409
        assert busy.json()["code"] == "idempotency-in-progress"
        assert busy.headers["Retry-After"] == "1"
    initial = await book(buyer, event, "hold-key", seat_ids=seats)
    assert initial.status_code == 201, initial.text
    assert initial.json()["fee_twd"] == 281
    again = await book(buyer, event, "hold-key", seat_ids=seats[::-1])
    assert again.status_code == 201
    assert again.headers["Idempotent-Replayed"] == "true"
    assert again.headers["Location"] == initial.headers["Location"]
    assert again.json() == initial.json()
    changed = await book(buyer, event, "hold-key", seat_ids=seats[:1])
    assert changed.status_code == 422
    assert changed.json()["code"] == "idempotency-key-reused"
    await assert_invariants(db)


async def test_cancel_expire_and_page(db, buyers, settings, test_clock):
    await test_clock.advance(timedelta())
    owner, other = await buyers(2)
    event, seats = await inventory(db, owner.user_id, 3)
    first = await book(owner, event, "first-key", seat_ids=seats[:1])
    second = await book(owner, event, "second-key", seat_ids=seats[1:])
    assert first.status_code == second.status_code == 201
    first_id = first.json()["public_id"]
    assert first.json()["fee_twd"] == 141
    assert (await other.client.get(first.headers["Location"])).status_code == 404
    assert (await cancel(other, first_id, "cancel-key")).status_code == 404
    page = (await owner.client.get("/api/v1/orders?limit=1")).json()
    assert page["items"] == [second.json()]
    next_page = (
        await owner.client.get("/api/v1/orders", params={"limit": 1, "cursor": page["next_cursor"]})
    ).json()
    assert next_page["items"] == [first.json()]
    assert next_page["next_cursor"] is None

    cancelled = await cancel(owner, first_id, "cancel-key")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    replay = await cancel(owner, first_id, "cancel-key")
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json() == cancelled.json()
    assert (await cancel(owner, first_id, "other-key")).json()["code"] == "order-not-cancellable"

    await test_clock.advance(timedelta(seconds=settings.hold_ttl_seconds))
    assert await db.fetchval(
        "SELECT expires_at <= app_now() FROM orders WHERE public_id=$1",
        UUID(second.json()["public_id"]),
    )
    pool = await create_pool(settings)
    redis = Redis.from_url(settings.redis_url)
    try:
        ctx = ServiceContext(settings, pool, redis, asyncio.Event())
        await race([sweeper.tick(ctx), sweeper.tick(ctx)])
    finally:
        await pool.close()
        await redis.aclose()
    assert (
        await db.fetchval(
            "SELECT status FROM orders WHERE public_id=$1", UUID(second.json()["public_id"])
        )
        == "expired"
    )
    assert await db.fetchval("SELECT count(*) FROM outbox") == 4
    await assert_invariants(db)


async def test_visibility_sales_and_validation(db, client, buyers, test_clock):
    (buyer,) = await buyers(1)
    event, seats = await inventory(db, buyer.user_id, 1)
    await create_user(db, "viewer@example.com", "viewer")
    viewer = await login(client, "viewer@example.com")
    assert (await book(viewer, event, "view-key", seat_ids=seats)).status_code == 403
    missing = await buyer.client.post(
        f"/api/v1/events/{event}/orders", headers=buyer.headers(), json={"seat_ids": seats}
    )
    assert missing.status_code == 422
    assert (await book(buyer, event, "short", seat_ids=seats)).status_code == 422
    assert (await book(buyer, event, "dup-seat", seat_ids=seats * 2)).status_code == 422
    assert (await book(buyer, event + 1, "not-here", seat_ids=seats)).status_code == 404
    await test_clock.advance(timedelta(days=2))
    failed = await book(buyer, event, "too-late", seat_ids=seats)
    assert failed.status_code == 409
    assert failed.json()["code"] == "event-not-on-sale"
    assert await db.fetchval("SELECT count(*) FROM idempotency_keys") == 0
    await assert_invariants(db)


async def test_admin_order_access_and_cancellation_replay(db, buyers, make_client):
    (buyer,) = await buyers(1)
    event, seats = await inventory(db, buyer.user_id, 1)
    await create_user(db, "operator@example.com", "admin")
    async with make_client() as client:
        admin = await login(client, "operator@example.com")
        held = await book(buyer, event, "owner-key", seat_ids=seats)
        assert held.status_code == 201
        order_id = held.json()["public_id"]
        path = held.headers["Location"]
        assert (await client.get(path)).json() == held.json()
        assert (await client.get("/api/v1/orders")).json()["items"] == []
        filtered = await client.get("/api/v1/orders", params={"user_id": buyer.user_id})
        assert filtered.json()["items"] == [held.json()]
        denied = await buyer.client.get("/api/v1/orders", params={"user_id": admin.user_id})
        assert denied.status_code == 403
        cancelled = await cancel(admin, order_id, "operator-cancel")
        assert cancelled.status_code == 200
        assert (await cancel(admin, order_id, "operator-cancel")).json() == cancelled.json()

        # A fresh session after a demotion has user permissions, including during replay.
        await db.execute("UPDATE users SET role='user' WHERE id=$1", admin.user_id)
        demoted = await login(client, "operator@example.com")
        assert (await cancel(demoted, order_id, "operator-cancel")).status_code == 404
    await assert_invariants(db)


async def test_allocation_distinguishes_locked_inventory_from_sold_out(db, buyers):
    first, second = await buyers(2)
    event, seats = await inventory(db, first.user_id, 1)
    async with db.transaction():
        await db.fetchval("SELECT id FROM seats WHERE id=$1 FOR NO KEY UPDATE", seats[0])
        busy = await book(first, event, "allocation-key", zone_code="A", quantity=1)
        assert busy.status_code == 409
        assert busy.json()["code"] == "allocation-contended"
        assert await db.fetchval("SELECT count(*) FROM orders") == 0
        assert await db.fetchval("SELECT count(*) FROM user_event_quota") == 0
    assert (
        await book(first, event, "allocation-key", zone_code="A", quantity=1)
    ).status_code == 201
    sold_out = await book(second, event, "allocation-key", zone_code="A", quantity=1)
    assert sold_out.status_code == 409
    assert sold_out.json()["code"] == "insufficient-seats"
    await assert_invariants(db)
