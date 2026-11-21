"""A database that stops answering in time sheds the request as 503, never 500."""

import asyncpg
import pytest

from ticketing.core.db import acquire, create_pool
from ticketing.core.errors import ProblemError
from ticketing.core.settings import Settings

pytestmark = pytest.mark.integration


@pytest.fixture
def settings_overrides() -> dict[str, object]:
    # Client-side command timeout = statement timeout + 1 s; keep the test fast.
    return {"db_statement_timeout_ms": 200}


async def _stall(pool: asyncpg.Pool) -> None:
    async with acquire(pool, 1.0) as conn:
        # Stall past the client-side command timeout (1.2 s) without tripping the server-side
        # statement_timeout, which would raise QueryCanceledError instead.
        await conn.execute("SET statement_timeout = 0")
        await conn.execute("SELECT pg_sleep(3)")


async def test_command_timeout_while_holding_a_connection_is_server_busy(
    settings: Settings,
) -> None:
    pool = await create_pool(settings, min_size=1, max_size=1)
    try:
        with pytest.raises(ProblemError) as caught:
            await _stall(pool)
        assert caught.value.status == 503
        assert caught.value.code == "server-busy"
        # The timed-out connection is discarded and the pool keeps serving.
        async with acquire(pool, 2.0) as conn:
            assert await conn.fetchval("SELECT 1") == 1
    finally:
        await pool.close()
