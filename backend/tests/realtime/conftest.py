from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from ticketing.core.db import create_pool
from ticketing.core.redis import create_redis
from ticketing.core.service import ServiceContext
from ticketing.core.settings import Settings


@pytest.fixture
async def relay_context(settings: Settings) -> AsyncIterator[ServiceContext]:
    pool = await create_pool(settings, min_size=1, max_size=4)
    redis = create_redis(settings)
    try:
        yield ServiceContext(settings, pool, redis, asyncio.Event())
    finally:
        await redis.aclose()
        await pool.close()
