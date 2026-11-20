"""Service heartbeats in Redis, watched by API processes (RELAY_DOWN / MONITOR_DOWN alerts)."""

from __future__ import annotations

import time

from redis.asyncio import Redis

from ticketing.core.context import BOOT_UUID

HEARTBEAT_TTL_S = 3


def heartbeat_key(service: str) -> str:
    return f"hb:{service}"


async def beat(redis: Redis, service: str) -> None:
    await redis.set(
        heartbeat_key(service),
        f"{BOOT_UUID}:{int(time.time() * 1000)}".encode(),
        ex=HEARTBEAT_TTL_S,
    )
