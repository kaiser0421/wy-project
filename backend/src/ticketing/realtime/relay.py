"""Outbox relay: assigns per-event sequence numbers and archives seat events.

Owned by workstream C. Called repeatedly by the background service runner.
"""

from __future__ import annotations

from ticketing.core.service import ServiceContext


async def tick(ctx: ServiceContext) -> None:
    """Run one bounded iteration. Must not raise for expected conditions."""
    return None
