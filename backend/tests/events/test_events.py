import asyncio
from datetime import timedelta

import asyncpg
import pytest

from tests.events.helpers import BODY, EVENTS, seed_event, seed_seat, seed_zone
from tests.support import create_user, login

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("method", "suffix", "body", "published", "outsider", "viewer"),
    [
        ("POST", "", BODY, False, 201, 403),
        ("GET", "", None, False, 404, 404),
        ("GET", "", None, True, 200, 200),
        ("GET", "/zones", None, False, 404, 404),
        ("GET", "/zones", None, True, 200, 200),
        ("PATCH", "", {"title": "Edited"}, False, 404, 403),
        ("PATCH", "", {"title": "Edited"}, True, 403, 403),
        ("DELETE", "", None, False, 404, 403),
        ("DELETE", "", None, True, 403, 403),
        ("POST", "/publish", None, False, 404, 403),
        ("POST", "/publish", None, True, 403, 403),
    ],
)
async def test_event_access(db, make_client, method, suffix, body, published, outsider, viewer):
    owner = await create_user(db, "owner@example.com")
    event = await seed_event(db, owner, published=published)
    await seed_seat(db, event, await seed_zone(db, event))
    url = EVENTS if body == BODY else f"{EVENTS}/{event}{suffix}"
    async with make_client() as client:
        denied = await client.request(method, url, json=body)
        assert denied.status_code == 401
    for role, expected in (("user", outsider), ("viewer", viewer), ("admin", None)):
        email = f"{role}@example.com"
        await create_user(db, email, role)
        async with make_client() as client:
            session = await login(client, email)
            response = await client.request(
                method,
                url,
                json=body,
                headers={**session.headers(), "If-Match": '"v1"'},
            )
            if expected is None:
                assert response.status_code in {200, 201, 204}, response.text
            else:
                assert response.status_code == expected, response.text
                assert await db.fetchval("SELECT version FROM events WHERE id = $1", event) == 1


async def test_lifecycle_and_concurrent_patch(db, make_client):
    await create_user(db, "owner@example.com")
    async with make_client() as first, make_client() as second:
        one = await login(first, "owner@example.com")
        two = await login(second, "owner@example.com")
        created = await first.post(EVENTS, json=BODY, headers=one.headers())
        assert created.status_code == 201, created.text
        path = created.headers["location"]
        version = created.headers["etag"]
        assert version == '"v1"'
        assert (
            await first.patch(path, json={"title": "x"}, headers=one.headers())
        ).status_code == 428
        no_seats = await first.post(
            f"{path}/publish", headers={**one.headers(), "If-Match": version}
        )
        assert no_seats.json()["code"] == "no-seats"
        left, right = await asyncio.gather(
            first.patch(
                path, json={"title": "First"}, headers={**one.headers(), "If-Match": version}
            ),
            second.patch(
                path,
                json={"zones": [{"code": "A", "name": "Main", "price_twd": 900}]},
                headers={**two.headers(), "If-Match": version},
            ),
        )
        good, stale = sorted((left, right), key=lambda result: result.status_code)
        assert (good.status_code, stale.status_code) == (200, 412)
        assert stale.json()["current"] == good.json()
        assert stale.headers["etag"] == good.headers["etag"] == '"v2"'
        event_id = good.json()["id"]
        zone = await db.fetchval("SELECT id FROM zones WHERE event_id = $1", event_id)
        await seed_seat(db, event_id, zone)
        published = await first.post(
            f"{path}/publish",
            headers={**one.headers(), "If-Match": good.headers["etag"]},
        )
        assert published.status_code == 200, published.text
        headers = {**one.headers(), "If-Match": published.headers["etag"]}
        replay = await first.post(f"{path}/publish", headers=headers)
        assert replay.json() == published.json()
        frozen = await first.patch(path, json={"zones": []}, headers=headers)
        assert frozen.json()["code"] == "field-frozen-after-publish"
        assert (await first.get(f"{path}/zones")).json()[0]["available"] == 1
        await db.execute(
            "INSERT INTO orders (event_id, user_id, quantity) VALUES ($1, $2, 1)",
            event_id,
            one.user_id,
        )
        assert (await first.delete(path, headers=headers)).json()["code"] == "event-has-orders"


@pytest.fixture
def settings_overrides():
    return {"db_statement_timeout_ms": 8000, "db_lock_timeout_ms": 5000}


async def test_delete_rechecks_orders_after_waiting_for_event_lock(db, db_url, client):
    owner = await create_user(db, "owner@example.com")
    event = await seed_event(db, owner, published=True)
    session = await login(client, "owner@example.com")
    booking = await asyncpg.connect(db_url)
    try:
        async with booking.transaction():
            # The order FK takes a key-share lock on the same event the DELETE locks.
            await booking.execute(
                "INSERT INTO orders(event_id, user_id, quantity) VALUES ($1, $2, 1)",
                event,
                owner,
            )
            deleting = asyncio.create_task(
                client.delete(f"{EVENTS}/{event}", headers=session.headers())
            )
            async with asyncio.timeout(3):
                while not await db.fetchval(  # noqa: ASYNC110
                    """SELECT EXISTS (SELECT FROM pg_stat_activity
                       WHERE $1 = ANY(pg_blocking_pids(pid)))""",
                    booking.get_server_pid(),
                ):
                    # PostgreSQL holds the barrier in a separate process from the test.
                    await asyncio.sleep(0.005)
        result = await deleting
        assert result.status_code == 409, result.text
        assert result.json()["code"] == "event-has-orders"
        assert await db.fetchval("SELECT count(*) FROM events") == 1
        assert await db.fetchval("SELECT count(*) FROM orders") == 1
    finally:
        await booking.close()


async def test_snapshot_preserves_membership_through_concurrent_mutations(db, db_url, make_client):
    owner = await create_user(db, "owner@example.com")
    await create_user(db, "viewer@example.com", "viewer")
    members = [await seed_event(db, owner, published=True) for _ in range(9)]
    draft = await seed_event(db, owner)
    async with make_client() as client, make_client() as other:
        await login(client, "viewer@example.com")
        await login(other, "owner@example.com")
        params = {"sort": "sales_start_at", "limit": 2, "q": "summer"}
        first = await client.get(EVENTS, params=params)
        assert first.status_code == 200, first.text
        page = first.json()
        seen = page["items"][:]
        assert (await other.get(EVENTS, params={**params, "cursor": page["next_cursor"]})).json()[
            "code"
        ] == "cursor-invalidated"

        async def mutate(sql, *args):
            conn = await asyncpg.connect(db_url)
            try:
                await conn.execute(sql, *args)
            finally:
                await conn.close()

        inserted, *_ = await asyncio.gather(
            seed_event(db, owner, published=True),
            mutate(
                "UPDATE events SET title = 'Moved', "
                "sales_start_at = sales_start_at - interval '1 day' WHERE id = $1",
                members[4],
            ),
            mutate("DELETE FROM events WHERE id = $1", members[5]),
            mutate(
                "UPDATE events SET status = 'published', published_at = app_now() WHERE id = $1",
                draft,
            ),
        )
        while page["next_cursor"] is not None:
            following = await client.get(EVENTS, params={**params, "cursor": page["next_cursor"]})
            assert following.status_code == 200, following.text
            page = following.json()
            seen.extend(page["items"])
        assert [item["id"] for item in seen] == members
        assert seen[4]["title"] == "Moved"
        assert seen[5] == {"id": members[5], "unavailable": True}
        assert {inserted, draft}.isdisjoint({item["id"] for item in seen[2:]})


async def test_cursor_expiry_and_lost_access(db, test_clock, make_client):
    owner = await create_user(db, "owner@example.com")
    admin_id = await create_user(db, "admin@example.com", "admin")
    await seed_event(db, owner)
    await seed_event(db, owner, published=True)
    async with make_client() as client:
        assert (await client.get(EVENTS)).status_code == 401
        await login(client, "admin@example.com")
        first = await client.get(EVENTS, params={"limit": 1})
        assert first.status_code == 200, first.text
        token = first.json()["next_cursor"]
        await db.execute("UPDATE users SET role = 'viewer' WHERE id = $1", admin_id)
        denied = await client.get(EVENTS, params={"cursor": token, "limit": 1})
        assert denied.status_code == 409
        # Restore the role so the next check isolates expiry (role is bound into the cursor).
        await db.execute("UPDATE users SET role = 'admin' WHERE id = $1", admin_id)
        await test_clock.advance(timedelta(minutes=11))
        expired = await client.get(EVENTS, params={"cursor": token, "limit": 1})
        assert expired.status_code == 410


async def test_snapshot_cap_rolls_back_materialized_rows(db, make_client):
    owner = await create_user(db, "owner@example.com")
    await db.execute(
        """INSERT INTO events (owner_id, title, category, venue, sales_start_at, sales_end_at)
           SELECT $1, 'Event', 'music', 'Arena', app_now(), app_now() + interval '1 hour'
           FROM generate_series(1, 50001)""",
        owner,
    )
    async with make_client() as client:
        await login(client, "owner@example.com")
        response = await client.get(EVENTS)
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "refine-filters"
    assert await db.fetchval("SELECT count(*) FROM list_snapshots") == 0
    assert await db.fetchval("SELECT count(*) FROM list_snapshot_rows") == 0


async def test_list_visibility_and_single_page_snapshot_cleanup(db, make_client):
    owner = await create_user(db, "owner@example.com")
    other = await create_user(db, "other@example.com")
    await create_user(db, "admin@example.com", "admin")
    await create_user(db, "viewer@example.com", "viewer")
    owned_draft = await seed_event(db, owner)
    other_draft = await seed_event(db, other)
    published = await seed_event(db, other, published=True)

    for email, expected in (
        ("owner@example.com", [owned_draft, published]),
        ("admin@example.com", [owned_draft, other_draft, published]),
        ("viewer@example.com", [published]),
    ):
        async with make_client() as client:
            await login(client, email)
            response = await client.get(EVENTS)
            assert response.status_code == 200, response.text
            assert [item["id"] for item in response.json()["items"]] == expected
            assert response.json()["next_cursor"] is None
    assert await db.fetchval("SELECT count(*) FROM list_snapshots") == 0
    assert await db.fetchval("SELECT count(*) FROM list_snapshot_rows") == 0
