"""Container healthcheck for background services: is the service's heartbeat fresh?

Usage: ticketing-healthcheck <service>   (exit 0 healthy, 1 unhealthy)

Deliberately lightweight (no settings model, no asyncio): it runs every few seconds inside each
container, so it must start fast even on a busy host.
"""

from __future__ import annotations

import os
import sys
import time

from redis import Redis

MAX_AGE_MS = 5_000


def heartbeat_is_fresh(service: str, redis_url: str) -> bool:
    client = Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
    try:
        value = client.get(f"hb:{service}")
    finally:
        client.close()
    if not isinstance(value, bytes):
        return False
    _boot, _, stamp = value.decode().partition(":")
    return int(time.time() * 1000) - int(stamp) <= MAX_AGE_MS


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: ticketing-healthcheck <service>", file=sys.stderr)
        sys.exit(2)
    try:
        healthy = heartbeat_is_fresh(
            sys.argv[1], os.environ.get("REDIS_URL", "redis://redis:6379/0")
        )
    except Exception:
        healthy = False
    sys.exit(0 if healthy else 1)


if __name__ == "__main__":
    main()
