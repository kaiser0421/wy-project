"""Container healthcheck for background services: is the service's heartbeat fresh?

Usage: ticketing-healthcheck <service>   (exit 0 healthy, 1 unhealthy)
"""

from __future__ import annotations

import asyncio
import sys
import time

from ticketing.core.heartbeat import heartbeat_key
from ticketing.core.redis import create_redis
from ticketing.core.settings import get_settings

MAX_AGE_MS = 5_000


async def _check(service: str) -> bool:
    redis = create_redis(get_settings())
    try:
        value = await redis.get(heartbeat_key(service))
    finally:
        await redis.aclose()
    if value is None:
        return False
    text = value.decode() if isinstance(value, bytes) else str(value)
    _boot, _, stamp = text.partition(":")
    return int(time.time() * 1000) - int(stamp) <= MAX_AGE_MS


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: ticketing-healthcheck <service>", file=sys.stderr)
        sys.exit(2)
    try:
        healthy = asyncio.run(_check(sys.argv[1]))
    except Exception:
        healthy = False
    sys.exit(0 if healthy else 1)


if __name__ == "__main__":
    main()
