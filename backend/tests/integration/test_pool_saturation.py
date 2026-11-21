"""Pool saturation sheds requests before they can queue unboundedly in authentication."""

import asyncio

import asyncpg
import httpx
import pytest
from starlette.types import ASGIApp

from tests.support import ORIGIN, PASSWORD, create_user, login

pytestmark = pytest.mark.integration


@pytest.fixture
def settings_overrides() -> dict[str, object]:
    return {"db_pool_min": 1, "db_pool_max": 1, "db_pool_acquire_timeout_s": 0.05}


async def test_saturated_pool_returns_retryable_problem(
    db: asyncpg.Connection, client: httpx.AsyncClient, asgi_app: ASGIApp
) -> None:
    email = "pool@example.com"
    await create_user(db, email)
    session = await login(client, email)
    pool = asgi_app.fastapi.state.db
    async with pool.acquire(), asyncio.timeout(1):
        responses = await asyncio.gather(
            client.get("/api/v1/auth/me"),
            client.get("/api/v1/events/1/seats"),
            client.get("/api/v1/events/1/seat-stream"),
            client.post(
                "/api/v1/events/1/orders",
                headers={**session.headers(), "Idempotency-Key": "pool-saturation"},
                json={"seat_ids": [1]},
            ),
            client.post(
                "/api/v1/auth/login", headers=ORIGIN, json={"email": email, "password": PASSWORD}
            ),
            client.post(
                "/api/v1/auth/register",
                headers=ORIGIN,
                json={"email": "new-pool@example.com", "password": PASSWORD},
            ),
        )
        for response in responses:
            assert response.status_code == 503, response.text
            assert response.headers["content-type"] == "application/problem+json"
            assert response.headers["retry-after"] == "1"
            assert response.json()["code"] == "server-busy"
            assert response.json()["retryable"] is True
    assert (await client.get("/api/v1/auth/me")).status_code == 200
