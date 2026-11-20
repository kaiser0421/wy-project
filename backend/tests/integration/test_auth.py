"""Authentication: sign-in, immediate credential invalidation, CSRF/Origin, rate limits."""

from __future__ import annotations

from collections.abc import Callable

import asyncpg
import httpx
import pytest

from tests.support import ORIGIN, PASSWORD, create_user, login

pytestmark = pytest.mark.integration

LOGIN = "/api/v1/auth/login"
ME = "/api/v1/auth/me"
LOGOUT = "/api/v1/auth/logout"


async def test_register_then_login_and_me(client: httpx.AsyncClient) -> None:
    created = await client.post(
        "/api/v1/auth/register",
        headers=ORIGIN,
        json={"email": "New@Example.com", "password": "long-enough-1"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["role"] == "user"
    assert created.json()["email"] == "new@example.com"

    session = await login(client, "new@example.com", "long-enough-1")
    me = await client.get(ME)
    assert me.status_code == 200
    assert me.json()["user"]["email"] == "new@example.com"
    assert me.json()["csrf_token"] == session.csrf  # stable for the session (survives reload)
    assert me.headers["cache-control"] == "no-store"


async def test_register_cannot_choose_role_or_reuse_email(
    client: httpx.AsyncClient, db: asyncpg.Connection
) -> None:
    payload = {"email": "a@example.com", "password": "long-enough-1", "role": "admin"}
    response = await client.post("/api/v1/auth/register", headers=ORIGIN, json=payload)
    assert response.status_code == 422  # extra fields are rejected (mass assignment)
    await create_user(db, "taken@example.com")
    dup = await client.post(
        "/api/v1/auth/register",
        headers=ORIGIN,
        json={"email": "taken@example.com", "password": "long-enough-1"},
    )
    assert dup.status_code == 409
    assert dup.json()["code"] == "email-taken"


async def test_session_cookie_is_httponly_lax_and_not_secure_on_http(
    client: httpx.AsyncClient, db: asyncpg.Connection
) -> None:
    await create_user(db, "c@example.com")
    response = await client.post(
        LOGIN, headers=ORIGIN, json={"email": "c@example.com", "password": PASSWORD}
    )
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("sid=")
    assert "HttpOnly" in cookie
    assert "samesite=lax" in cookie.lower()
    assert "Secure" not in cookie


async def test_wrong_password_and_unknown_email_look_the_same(
    client: httpx.AsyncClient, db: asyncpg.Connection
) -> None:
    await create_user(db, "u@example.com")
    wrong = await client.post(
        LOGIN, headers=ORIGIN, json={"email": "u@example.com", "password": "nope"}
    )
    unknown = await client.post(
        LOGIN, headers=ORIGIN, json={"email": "ghost@example.com", "password": "nope"}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["code"] == unknown.json()["code"] == "invalid-credentials"


async def test_logout_invalidates_the_cookie_immediately(
    make_client: Callable[[], httpx.AsyncClient], db: asyncpg.Connection
) -> None:
    await create_user(db, "out@example.com")
    async with make_client() as client:
        session = await login(client, "out@example.com")
        stolen_cookie = client.cookies.get("sid")
        assert (await client.post(LOGOUT, headers=session.headers())).status_code == 204
        assert (await client.get(ME)).status_code == 401
    async with make_client() as attacker:  # a copied cookie is dead too
        attacker.cookies.set("sid", stolen_cookie or "")
        assert (await attacker.get(ME)).status_code == 401


async def test_disabled_user_sessions_stop_working(
    client: httpx.AsyncClient, db: asyncpg.Connection
) -> None:
    user_id = await create_user(db, "dis@example.com")
    await login(client, "dis@example.com")
    await db.execute("UPDATE users SET is_active = false WHERE id = $1", user_id)
    assert (await client.get(ME)).status_code == 401


async def test_disabled_user_cannot_login(
    client: httpx.AsyncClient, db: asyncpg.Connection
) -> None:
    await create_user(db, "gone@example.com", active=False)
    response = await client.post(
        LOGIN, headers=ORIGIN, json={"email": "gone@example.com", "password": PASSWORD}
    )
    assert response.status_code == 401


async def test_unsafe_request_requires_csrf_token(
    client: httpx.AsyncClient, db: asyncpg.Connection
) -> None:
    await create_user(db, "csrf@example.com")
    await login(client, "csrf@example.com")
    missing = await client.post(LOGOUT, headers=ORIGIN)
    assert missing.status_code == 403
    assert missing.json()["code"] == "csrf-failed"
    forged = await client.post(LOGOUT, headers={"X-CSRF-Token": "v1.deadbeef", **ORIGIN})
    assert forged.status_code == 403
    assert (await client.get(ME)).status_code == 200  # session still valid


async def test_csrf_token_is_bound_to_its_session(
    make_client: Callable[[], httpx.AsyncClient], db: asyncpg.Connection
) -> None:
    await create_user(db, "one@example.com")
    await create_user(db, "two@example.com")
    async with make_client() as a, make_client() as b:
        session_a = await login(a, "one@example.com")
        await login(b, "two@example.com")
        response = await b.post(LOGOUT, headers=session_a.headers())
        assert response.status_code == 403


async def test_login_requires_allowed_origin(
    client: httpx.AsyncClient, db: asyncpg.Connection
) -> None:
    await create_user(db, "o@example.com")
    body = {"email": "o@example.com", "password": PASSWORD}
    for headers in ({}, {"Origin": "https://evil.example"}, {"Origin": "null"}):
        response = await client.post(LOGIN, json=body, headers=headers)
        assert response.status_code == 403, headers
        assert response.json()["code"] == "origin-not-allowed"
    referer = await client.post(
        LOGIN, json=body, headers={"Referer": "http://localhost:8080/login"}
    )
    assert referer.status_code == 200


async def test_csrf_token_is_also_sent_after_reload(
    make_client: Callable[[], httpx.AsyncClient], db: asyncpg.Connection
) -> None:
    await create_user(db, "reload@example.com")
    async with make_client() as client:
        await login(client, "reload@example.com")
        token = (await client.get(ME)).json()["csrf_token"]
        response = await client.post(LOGOUT, headers={"X-CSRF-Token": token, **ORIGIN})
        assert response.status_code == 204


class TestRateLimits:
    @pytest.fixture
    def settings_overrides(self) -> dict[str, object]:
        return {"rate_limit_login_per_email": 3, "rate_limit_register_per_ip": 2}

    async def test_login_is_limited_per_email(
        self, client: httpx.AsyncClient, db: asyncpg.Connection
    ) -> None:
        await create_user(db, "rl@example.com")
        body = {"email": "rl@example.com", "password": "wrong"}
        codes = [
            (await client.post(LOGIN, json=body, headers=ORIGIN)).status_code for _ in range(4)
        ]
        assert codes == [401, 401, 401, 429]
        limited = await client.post(LOGIN, json=body, headers=ORIGIN)
        assert int(limited.headers["retry-after"]) >= 1
        assert limited.json()["retryable"] is True

    async def test_registration_is_limited_per_ip(self, client: httpx.AsyncClient) -> None:
        codes = []
        for i in range(3):
            response = await client.post(
                "/api/v1/auth/register",
                headers=ORIGIN,
                json={"email": f"r{i}@example.com", "password": "long-enough-1"},
            )
            codes.append(response.status_code)
        assert codes == [201, 201, 429]


class TestRedisDown:
    @pytest.fixture
    def settings_overrides(self) -> dict[str, object]:
        return {"redis_url": "redis://127.0.0.1:1/0"}

    async def test_login_fails_closed(
        self, client: httpx.AsyncClient, db: asyncpg.Connection
    ) -> None:
        await create_user(db, "down@example.com")
        response = await client.post(
            LOGIN, headers=ORIGIN, json={"email": "down@example.com", "password": PASSWORD}
        )
        assert response.status_code == 503
        assert response.json()["code"] == "server-busy"
        assert "retry-after" in response.headers
