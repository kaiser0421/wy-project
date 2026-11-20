"""Outbox relay: assigns per-event sequence numbers and delivers seat events to Redis Streams.

P0 stub: heartbeat only. Implemented in T4.1.
"""

from __future__ import annotations

from ticketing.core.service import ServiceContext, run_background_service


async def tick(_ctx: ServiceContext) -> None:
    return None


def main() -> None:
    run_background_service("relay", tick, interval_s=0.05)


if __name__ == "__main__":
    main()
