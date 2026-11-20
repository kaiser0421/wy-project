"""Redis client factory. Redis never decides seat ownership: delivery and rate limits only."""

from __future__ import annotations

from redis.asyncio import Redis

from ticketing.core.settings import Settings


def create_redis(settings: Settings) -> Redis:
    client: Redis = Redis.from_url(
        settings.redis_url,
        socket_timeout=2.0,
        socket_connect_timeout=2.0,
        health_check_interval=15,
        decode_responses=False,
    )
    return client
