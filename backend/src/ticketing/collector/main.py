"""Telemetry collector entrypoint (logic in ticketing.telemetry.collector)."""

from __future__ import annotations

from ticketing.core.service import run_background_service
from ticketing.telemetry.collector import tick


def main() -> None:
    run_background_service("collector", tick, interval_s=0.5)


if __name__ == "__main__":
    main()
