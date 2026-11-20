import asyncio

import pytest
from redis.asyncio import Redis

from ticketing.core.db import create_pool
from ticketing.core.service import ServiceContext


@pytest.fixture
def settings_overrides(tmp_path, request):
    return getattr(request, "param", {}) | {
        "import_dir": str(tmp_path / "imports"),
        "export_dir": str(tmp_path / "exports"),
    }


@pytest.fixture
async def worker(settings):
    pool = await create_pool(settings, min_size=1, max_size=4)
    redis = Redis.from_url(settings.redis_url)
    try:
        yield ServiceContext(settings, pool, redis, asyncio.Event())
    finally:
        await redis.aclose()
        await pool.close()
