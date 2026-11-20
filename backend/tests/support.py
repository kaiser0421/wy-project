"""Test helpers shared across suites."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import asyncpg

SQL_DIR = Path(__file__).parent / "sql"


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
