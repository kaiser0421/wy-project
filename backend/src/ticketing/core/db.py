"""asyncpg connection pool with per-connection safety timeouts."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from ticketing.core.errors import server_busy
from ticketing.core.settings import Settings


async def create_pool(
    settings: Settings,
    *,
    min_size: int | None = None,
    max_size: int | None = None,
    application_name: str | None = None,
) -> asyncpg.Pool:
    server_settings = {
        "application_name": application_name or settings.service_name,
        "statement_timeout": str(settings.db_statement_timeout_ms),
        "lock_timeout": str(settings.db_lock_timeout_ms),
        "idle_in_transaction_session_timeout": str(settings.db_idle_in_tx_timeout_ms),
        "timezone": "UTC",
    }
    pool: asyncpg.Pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=min_size if min_size is not None else settings.db_pool_min,
        max_size=max_size if max_size is not None else settings.db_pool_max,
        server_settings=server_settings,
        command_timeout=settings.db_statement_timeout_ms / 1000 + 1,
    )
    return pool


@asynccontextmanager
async def acquire(pool: asyncpg.Pool, timeout_s: float) -> AsyncIterator[asyncpg.Connection]:
    """Acquire a connection or fail fast with 503 server-busy (load shedding).

    A client-side command timeout (`command_timeout`) while the connection is held means the
    database is not answering in time: that is overload, reported as the same retryable 503.
    A connection that cannot be reset in time on release is discarded instead of failing a
    request that has already finished its work.
    """
    try:
        conn = await pool.acquire(timeout=timeout_s)
    except TimeoutError as exc:
        raise server_busy("Database connection pool exhausted.") from exc
    try:
        yield conn
    except TimeoutError as exc:
        raise server_busy("The database did not answer in time.") from exc
    finally:
        try:
            await pool.release(conn, timeout=timeout_s)
        except TimeoutError:
            conn.terminate()
