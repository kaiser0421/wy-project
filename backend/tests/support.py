"""Test helpers shared across suites."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import asyncpg
import httpx
from argon2 import PasswordHasher

SQL_DIR = Path(__file__).parent / "sql"
ORIGIN = {"Origin": "http://localhost:8080"}
PASSWORD = "Correct-Horse-1"
_hasher = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)


@dataclass(slots=True)
class TestClock:
    """Controls app_now() in a cloned test database."""

    db: asyncpg.Connection

    async def set(self, when: datetime) -> None:
        await self.db.execute("UPDATE test_clock SET now_override = $1 WHERE id = 1", when)

    async def advance(self, delta: timedelta) -> datetime:
        now: datetime = await self.db.fetchval("SELECT app_now()")
        await self.set(now + delta)
        return now + delta

    async def release(self) -> None:
        await self.db.execute("UPDATE test_clock SET now_override = NULL WHERE id = 1")


async def install_test_clock(db: asyncpg.Connection) -> TestClock:
    await db.execute((SQL_DIR / "test_clock.sql").read_text())
    return TestClock(db)


async def create_user(
    db: asyncpg.Connection,
    email: str,
    role: str = "user",
    password: str = PASSWORD,
    active: bool = True,
) -> int:
    user_id: int = await db.fetchval(
        "INSERT INTO users (email, password_hash, role, is_active) "
        "VALUES ($1, $2, $3::user_role, $4) RETURNING id",
        email,
        _hasher.hash(password),
        role,
        active,
    )
    return user_id


@dataclass(slots=True)
class Session:
    client: httpx.AsyncClient
    user_id: int
    csrf: str

    def headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf, **ORIGIN}


async def login(client: httpx.AsyncClient, email: str, password: str = PASSWORD) -> Session:
    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}, headers=ORIGIN
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return Session(client=client, user_id=body["user"]["id"], csrf=body["csrf_token"])
