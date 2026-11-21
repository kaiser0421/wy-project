"""Housekeeping: keep range partitions created ahead of time and prune delivered outbox rows."""

from __future__ import annotations

from datetime import timedelta

from ticketing.core.db import acquire
from ticketing.core.service import ServiceContext

# Assigned outbox rows are archived in seat_events by the relay; SSE replay reads at most a few
# seconds back and turns any missing range into a resync, so an hour is a wide safety margin.
OUTBOX_RETENTION = timedelta(hours=1)
_PRUNE_BATCH = 5_000

_PRUNE_OUTBOX = """
DELETE FROM outbox WHERE id IN (
  SELECT id FROM outbox
  WHERE seq IS NOT NULL AND assigned_at < clock_timestamp() - $1::interval
  ORDER BY id LIMIT $2
)
"""

DAILY = ("seat_events", "metrics_1s", "business_1s", "access_logs", "service_logs")


async def tick(ctx: ServiceContext) -> None:
    async with acquire(ctx.db, ctx.settings.db_pool_acquire_timeout_s) as conn:
        for table in DAILY:
            await conn.execute(
                "SELECT ensure_range_partitions($1, $2, 1, 3)", table, timedelta(days=1)
            )
        await conn.execute(
            "SELECT ensure_range_partitions($1, $2, 1, 6)",
            "list_snapshot_rows",
            timedelta(hours=1),
        )
        while not ctx.stop.is_set():
            status = await conn.execute(_PRUNE_OUTBOX, OUTBOX_RETENTION, _PRUNE_BATCH)
            if int(status.split()[-1]) < _PRUNE_BATCH:
                break
