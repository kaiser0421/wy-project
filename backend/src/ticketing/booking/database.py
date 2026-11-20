"""Connection and transaction boundaries shared by order writers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import asyncpg

from ticketing.auth.sessions import lock_live_session
from ticketing.core.db import acquire
from ticketing.core.errors import ProblemError, server_busy
from ticketing.core.settings import Settings


@asynccontextmanager
async def connection(pool: asyncpg.Pool, settings: Settings) -> AsyncIterator[asyncpg.Connection]:
    try:
        async with acquire(pool, settings.db_pool_acquire_timeout_s) as conn:
            yield conn
    except (
        asyncpg.LockNotAvailableError,
        asyncpg.QueryCanceledError,
        asyncpg.DeadlockDetectedError,
        TimeoutError,
    ) as exc:
        raise server_busy() from exc


@asynccontextmanager
async def transaction(pool: asyncpg.Pool, settings: Settings) -> AsyncIterator[asyncpg.Connection]:
    async with connection(pool, settings) as conn, conn.transaction(isolation="read_committed"):
        yield conn


async def fence(conn: asyncpg.Connection, session_id: UUID, user_id: int) -> None:
    if not await lock_live_session(conn, session_id, user_id):
        raise ProblemError(401, "unauthenticated", "Authentication required", "Your session ended.")
