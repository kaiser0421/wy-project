"""Partition maintenance: keep range partitions created ahead of time."""

from __future__ import annotations

from datetime import timedelta

from ticketing.core.service import ServiceContext

DAILY = ("seat_events", "metrics_1s", "business_1s", "access_logs", "service_logs")


async def tick(ctx: ServiceContext) -> None:
    async with ctx.db.acquire() as conn:
        for table in DAILY:
            await conn.execute(
                "SELECT ensure_range_partitions($1, $2, 1, 3)", table, timedelta(days=1)
            )
        await conn.execute(
            "SELECT ensure_range_partitions($1, $2, 1, 6)",
            "list_snapshot_rows",
            timedelta(hours=1),
        )
