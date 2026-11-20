"""Outbox relay entrypoint (logic in ticketing.realtime.relay)."""

from __future__ import annotations

from ticketing.core.service import run_background_service
from ticketing.realtime.relay import tick


def main() -> None:
    run_background_service("relay", tick, interval_s=0.05)


if __name__ == "__main__":
    main()
