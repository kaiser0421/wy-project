"""Background worker: import jobs, hold-expiry sweeper, payment reconciler, refunds.

P0 stub: heartbeat only. Job pools are added in later milestones (T2.5, T3.4, T5.4).
"""

from __future__ import annotations

from ticketing.core.service import ServiceContext, run_background_service


async def tick(_ctx: ServiceContext) -> None:
    return None


def main() -> None:
    run_background_service("worker", tick, interval_s=1.0)


if __name__ == "__main__":
    main()
