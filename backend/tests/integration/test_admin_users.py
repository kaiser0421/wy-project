"""Admin user management: revocation on role change/disable, If-Match, last-admin guard."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import asyncpg
import httpx
import pytest

from tests.support import create_user, login

pytestmark = pytest.mark.integration
USERS = "/api/v1/admin/users"


async def _etag(admin: httpx.AsyncClient, user_id: int) -> str:
    response = await admin.get(f"{USERS}/{user_id}")
    assert response.status_code == 200, response.text
    return response.headers["etag"]


async def test_role_change_revokes_target_sessions_and_is_audited(
    make_client: Callable[[], httpx.AsyncClient], db: asyncpg.Connection
) -> None:
    await create_user(db, "admin@example.com", "admin")
    target_id = await create_user(db, "target@example.com", "user")
    async with make_client() as admin_client, make_client() as target_client:
        admin = await login(admin_client, "admin@example.com")
        await login(target_client, "target@example.com")
        etag = await _etag(admin_client, target_id)
        response = await admin_client.patch(
            f"{USERS}/{target_id}",
            json={"role": "viewer"},
            headers={"If-Match": etag, **admin.headers()},
        )
        assert response.status_code == 200, response.text
        assert response.json()["role"] == "viewer"
        assert response.headers["etag"] != etag
        # The target's existing cookie stops working on the very next request.
        assert (await target_client.get("/api/v1/auth/me")).status_code == 401
    audit = await db.fetchrow("SELECT action, before, after FROM audit_log")
    assert audit["action"] == "user.update"


async def test_disable_revokes_sessions(
    make_client: Callable[[], httpx.AsyncClient], db: asyncpg.Connection
) -> None:
    await create_user(db, "admin@example.com", "admin")
    target_id = await create_user(db, "victim@example.com")
    async with make_client() as admin_client, make_client() as target_client:
        admin = await login(admin_client, "admin@example.com")
        await login(target_client, "victim@example.com")
        etag = await _etag(admin_client, target_id)
        response = await admin_client.patch(
            f"{USERS}/{target_id}",
            json={"is_active": False},
            headers={"If-Match": etag, **admin.headers()},
        )
        assert response.status_code == 200
        assert (await target_client.get("/api/v1/auth/me")).status_code == 401
    reasons = await db.fetch("SELECT revoked_reason FROM sessions WHERE user_id = $1", target_id)
    assert [r["revoked_reason"] for r in reasons] == ["disabled"]


async def test_if_match_is_required_and_checked(
    client: httpx.AsyncClient, db: asyncpg.Connection
) -> None:
    await create_user(db, "admin@example.com", "admin")
    target_id = await create_user(db, "t@example.com")
    admin = await login(client, "admin@example.com")
    missing = await client.patch(
        f"{USERS}/{target_id}", json={"role": "viewer"}, headers=admin.headers()
    )
    assert missing.status_code == 428
    stale = await client.patch(
        f"{USERS}/{target_id}",
        json={"role": "viewer"},
        headers={"If-Match": '"v999"', **admin.headers()},
    )
    assert stale.status_code == 412
    assert stale.json()["current"]["role"] == "user"


async def test_admin_cannot_demote_or_disable_self(
    client: httpx.AsyncClient, db: asyncpg.Connection
) -> None:
    admin_id = await create_user(db, "admin@example.com", "admin")
    await create_user(db, "admin2@example.com", "admin")
    admin = await login(client, "admin@example.com")
    etag = await _etag(client, admin_id)
    response = await client.patch(
        f"{USERS}/{admin_id}", json={"role": "user"}, headers={"If-Match": etag, **admin.headers()}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "self-modification"


async def test_concurrent_cross_demotion_keeps_one_admin(
    make_client: Callable[[], httpx.AsyncClient], db: asyncpg.Connection
) -> None:
    a_id = await create_user(db, "a@example.com", "admin")
    b_id = await create_user(db, "b@example.com", "admin")
    async with make_client() as ca, make_client() as cb:
        a = await login(ca, "a@example.com")
        b = await login(cb, "b@example.com")
        etag_b, etag_a = await _etag(ca, b_id), await _etag(cb, a_id)
        results = await asyncio.gather(
            ca.patch(
                f"{USERS}/{b_id}",
                json={"role": "user"},
                headers={"If-Match": etag_b, **a.headers()},
            ),
            cb.patch(
                f"{USERS}/{a_id}",
                json={"role": "user"},
                headers={"If-Match": etag_a, **b.headers()},
            ),
        )
    statuses = sorted(r.status_code for r in results)
    assert statuses[0] == 200
    assert statuses[1] in {401, 403, 409}  # the loser lost its admin role or its session
    remaining = await db.fetchval("SELECT count(*) FROM users WHERE role = 'admin' AND is_active")
    assert remaining == 1


async def test_non_admin_cannot_list_users(
    client: httpx.AsyncClient, db: asyncpg.Connection
) -> None:
    await create_user(db, "plain@example.com", "user")
    await login(client, "plain@example.com")
    response = await client.get(USERS)
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


async def test_list_users_pages_by_id(client: httpx.AsyncClient, db: asyncpg.Connection) -> None:
    await create_user(db, "admin@example.com", "admin")
    for i in range(4):
        await create_user(db, f"p{i}@example.com")
    await login(client, "admin@example.com")
    first = (await client.get(USERS, params={"limit": 3})).json()
    second = (await client.get(USERS, params={"limit": 3, "cursor": first["next_cursor"]})).json()
    ids = [u["id"] for u in first["items"] + second["items"]]
    assert len(ids) == 5 == len(set(ids))
    assert second["next_cursor"] is None
