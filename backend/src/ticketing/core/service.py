"""Shared runner for background services (worker, relay, collector).

The heartbeat runs in its own task so a Redis outage never stops PostgreSQL-only work such as
hold expiry; each job handles its own Redis dependencies.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import asyncpg
from redis.asyncio import Redis

from ticketing.core.db import create_pool
from ticketing.core.heartbeat import beat
from ticketing.core.lifecycle import run_service, sleep_or_stop
from ticketing.core.logging import configure_logging, get_logger
from ticketing.core.redis import create_redis
from ticketing.core.settings import Settings, get_settings

log = get_logger(__name__)
HEARTBEAT_INTERVAL_S = 1.0


@dataclass(slots=True)
class ServiceContext:
    settings: Settings
    db: asyncpg.Pool
    redis: Redis
    stop: asyncio.Event


Tick = Callable[[ServiceContext], Awaitable[None]]


async def _heartbeat_loop(redis: Redis, name: str, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            async with asyncio.timeout(HEARTBEAT_INTERVAL_S):
                await beat(redis, name)
        except Exception as exc:
            log.warning("service.heartbeat_failed", component=name, error=type(exc).__name__)
        if await sleep_or_stop(stop, HEARTBEAT_INTERVAL_S):
            return


async def _run(name: str, tick: Tick, interval_s: float) -> None:
    settings = get_settings()
    # The worker runs several job loops on one pool; DB_POOL_MAX (default 10) leaves room for an
    # import, the sweeper and the bounded reconciler at the same time.
    db = await create_pool(
        settings, min_size=1, max_size=settings.db_pool_max, application_name=name
    )
    redis = create_redis(settings)

    async def body(stop: asyncio.Event) -> None:
        ctx = ServiceContext(settings=settings, db=db, redis=redis, stop=stop)
        heartbeat = asyncio.create_task(_heartbeat_loop(redis, name, stop))
        try:
            while not stop.is_set():
                try:
                    await tick(ctx)
                except Exception as exc:
                    log.error("service.tick_failed", component=name, error=type(exc).__name__)
                if await sleep_or_stop(stop, interval_s):
                    break
        finally:
            stop.set()
            await heartbeat

    try:
        await run_service(name, body)
    finally:
        await redis.aclose()
        await db.close()


def run_background_service(name: str, tick: Tick, interval_s: float = 1.0) -> None:
    settings = get_settings()
    configure_logging(name, settings.log_level, settings.log_dir)
    asyncio.run(_run(name, tick, interval_s))
