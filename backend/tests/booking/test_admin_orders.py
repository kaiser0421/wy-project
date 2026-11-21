from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta

import pytest

from tests.booking.support import book, inventory
from tests.support import create_user, login

pytestmark = pytest.mark.integration
ADMIN_ORDERS = "/api/v1/admin/orders"
MY_ORDERS = "/api/v1/orders"
CREATED = datetime(2030, 5, 1, 12, 0, 0, 123456, UTC)


@pytest.fixture
async def actors(db, make_client):
    async with AsyncExitStack() as stack:
        sessions = {}
        for name, role in (
            ("operator", "admin"),
            ("other-admin", "admin"),
            ("buyer_one", "user"),
            ("buyerXtwo", "user"),
            ("viewer", "viewer"),
        ):
            email = f"{name}@example.com"
            await create_user(db, email, role)
            client = await stack.enter_async_context(make_client())
            sessions[name] = await login(client, email)
        yield sessions


async def insert_order(db, buyer, event, created, status="pending_payment"):
    return str(
        await db.fetchval(
            """INSERT INTO orders(user_id, event_id, quantity, status, created_at,
                                  expires_at, subtotal_twd, fee_twd, total_twd)
               VALUES ($1, $2, 2, $3, $4, $4::timestamptz + interval '10 minutes', 400, 20, 420)
               RETURNING public_id""",
            buyer,
            event,
            status,
            created,
        )
    )


async def test_admin_listing_and_existing_owner_endpoints(db, actors, make_client):
    admin, buyer, other, viewer = (
        actors[name] for name in ("operator", "buyer_one", "buyerXtwo", "viewer")
    )
    event, seats = await inventory(db, buyer.user_id, 2)
    booked = await book(buyer, event, "own-order", seat_ids=seats[:1])
    another = await book(other, event, "other-order", seat_ids=seats[1:])
    assert booked.status_code == another.status_code == 201
    own, foreign = (response.json()["public_id"] for response in (booked, another))

    response = await admin.client.get(ADMIN_ORDERS)
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "private, no-store"
    assert {item["public_id"] for item in response.json()["items"]} == {own, foreign}
    for session in (buyer, viewer):
        denied = await session.client.get(ADMIN_ORDERS)
        assert (denied.status_code, denied.json()["code"]) == (403, "forbidden")
    async with make_client() as anonymous:
        denied = await anonymous.get(ADMIN_ORDERS)
        assert (denied.status_code, denied.json()["code"]) == (401, "unauthenticated")

    assert (await admin.client.get(MY_ORDERS)).json() == {"items": [], "next_cursor": None}
    for client, params in (
        (buyer.client, {}),
        (buyer.client, {"user_id": buyer.user_id}),
        (admin.client, {"user_id": buyer.user_id}),
    ):
        assert (await client.get(MY_ORDERS, params=params)).json() == {
            "items": [booked.json()],
            "next_cursor": None,
        }
    assert (await buyer.client.get(MY_ORDERS, params={"user_id": other.user_id})).status_code == 403
    assert (await buyer.client.get(f"{MY_ORDERS}/{foreign}")).status_code == 404
    assert (await admin.client.get(f"{MY_ORDERS}/{foreign}")).json() == another.json()


async def test_admin_order_filters_and_summary(db, actors):
    admin = actors["operator"].client
    buyer, other = (actors[name].user_id for name in ("buyer_one", "buyerXtwo"))
    event, _ = await inventory(db, buyer, 1)
    different_event, _ = await inventory(db, buyer, 1)
    first = await insert_order(db, buyer, event, CREATED, "paid")
    second = await insert_order(db, other, event, CREATED + timedelta(seconds=1))
    third = await insert_order(db, buyer, different_event, CREATED + timedelta(seconds=2))

    for params, expected in (
        ({"event_id": event}, [second, first]),
        ({"status": "paid"}, [first]),
        ({"buyer_email": "BUYER_"}, [third, first]),
        ({"buyer_email": "buyer%"}, []),
        ({"created_from": CREATED.isoformat()}, [third, second, first]),
        ({"created_to": CREATED.isoformat()}, [first]),
        (
            {
                "event_id": event,
                "status": "paid",
                "buyer_email": "buyer_one@example.com",
                "created_from": CREATED.isoformat(),
                "created_to": CREATED.isoformat(),
            },
            [first],
        ),
    ):
        response = await admin.get(ADMIN_ORDERS, params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        assert [item["public_id"] for item in page["items"]] == expected
        assert page["next_cursor"] is None

    assert page["items"] == [
        {
            "public_id": first,
            "event_id": event,
            "event_title": "Concert",
            "buyer_email": "buyer_one@example.com",
            "status": "paid",
            "quantity": 2,
            "total_twd": 420,
            "created_at": "2030-05-01T12:00:00.123456Z",
            "expires_at": "2030-05-01T12:10:00.123456Z",
        }
    ]
    for params in (
        {"created_to": "2030-05-01T12:00:00"},
        {"created_from": CREATED.isoformat(), "created_to": "2030-01-01T00:00:00Z"},
        {"status": "unknown"},
        {"event_id": 0},
        {"limit": 101},
    ):
        response = await admin.get(ADMIN_ORDERS, params=params)
        assert (response.status_code, response.json()["code"]) == (422, "validation-failed")


async def test_admin_order_keyset_pagination_with_insertions(db, actors):
    admin = actors["operator"].client
    buyer = actors["buyer_one"].user_id
    event, _ = await inventory(db, buyer, 1)
    time_offsets = [0, -2, 0, -1, 0]
    ids = [
        await insert_order(db, buyer, event, CREATED + timedelta(seconds=offset))
        for offset in time_offsets
    ]
    expected = [ids[i] for i in (4, 2, 0, 3, 1)]
    response = await admin.get(ADMIN_ORDERS, params={"limit": 2})
    assert response.status_code == 200, response.text
    page = response.json()
    cursor = page["next_cursor"]
    assert cursor
    seen = [item["public_id"] for item in page["items"]]
    assert seen == expected[:2]

    newer = await insert_order(db, buyer, event, CREATED + timedelta(hours=1))
    backdated = await insert_order(db, buyer, event, CREATED - timedelta(hours=1))
    tampered = ("B" if cursor[0] == "A" else "A") + cursor[1:]
    for client, params in (
        (admin, {"cursor": tampered}),
        (admin, {"cursor": cursor, "event_id": event}),
        (actors["other-admin"].client, {"cursor": cursor}),
    ):
        denied = await client.get(ADMIN_ORDERS, params=params)
        assert (denied.status_code, denied.json()["code"]) == (409, "cursor-invalidated")

    while cursor:
        response = await admin.get(ADMIN_ORDERS, params={"cursor": cursor, "limit": 2})
        assert response.status_code == 200, response.text
        page = response.json()
        seen.extend(item["public_id"] for item in page["items"])
        cursor = page["next_cursor"]
    assert seen == expected
    fresh = (await admin.get(ADMIN_ORDERS)).json()
    assert [item["public_id"] for item in fresh["items"]] == [newer, *expected, backdated]


async def test_buyer_email_with_nul_is_rejected_as_validation_error(actors) -> None:
    admin = actors["operator"].client
    response = await admin.get("/api/v1/admin/orders", params={"buyer_email": "a\x00b"})
    assert response.status_code == 422
