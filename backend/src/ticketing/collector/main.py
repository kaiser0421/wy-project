"""Telemetry collector: metric aggregation, batched log ingestion, alert evaluation.

P0 stub: heartbeat only. Implemented in P7.
"""

from __future__ import annotations

from ticketing.core.service import ServiceContext, run_background_service


async def tick(_ctx: ServiceContext) -> None:
    return None


def main() -> None:
    run_background_service("collector", tick, interval_s=1.0)


if __name__ == "__main__":
    main()
