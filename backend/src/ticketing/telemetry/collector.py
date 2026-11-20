"""Telemetry collector: log ingestion, metric aggregation, alerts.

Owned by workstream D. Called repeatedly by the background service runner.
"""

from __future__ import annotations

from ticketing.core.service import ServiceContext


async def tick(ctx: ServiceContext) -> None:
    """Run one bounded iteration. Must not raise for expected conditions."""
    return None
