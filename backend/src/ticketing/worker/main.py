"""Background worker: hold expiry, payment reconciliation/refunds, seat imports, maintenance.

Each job runs in its own loop with its own interval so a slow import never delays hold expiry.
"""

from __future__ import annotations

import asyncio

from ticketing import maintenance
from ticketing.booking import sweeper
from ticketing.core.lifecycle import sleep_or_stop
from ticketing.core.logging import get_logger
from ticketing.core.service import ServiceContext, Tick, run_background_service
from ticketing.imports import runner as import_runner
from ticketing.payments import reconciler

log = get_logger(__name__)

JOBS: list[tuple[str, Tick, float]] = [
    ("hold-expiry", sweeper.tick, 1.0),
    ("payment-reconciler", reconciler.tick, 5.0),
    ("seat-import", import_runner.tick, 1.0),
    ("maintenance", maintenance.tick, 600.0),
]


async def _job_loop(ctx: ServiceContext, name: str, job: Tick, interval_s: float) -> None:
    while not ctx.stop.is_set():
        try:
            await job(ctx)
        except Exception as exc:
            log.error("worker.job_failed", job=name, error=type(exc).__name__)
        if await sleep_or_stop(ctx.stop, interval_s):
            return


async def tick(ctx: ServiceContext) -> None:
    """Runs all job loops until shutdown (the runner calls this once)."""
    await asyncio.gather(*(_job_loop(ctx, n, j, i) for n, j, i in JOBS))


def main() -> None:
    run_background_service("worker", tick, interval_s=0.1)


if __name__ == "__main__":
    main()
