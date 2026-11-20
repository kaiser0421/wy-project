"""Graceful shutdown for long-running background services.

Background entrypoints run `run_service(loop_fn)`: SIGTERM/SIGINT set a stop event that
the loop observes between iterations, so leases are released and buffers flushed before exit.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Awaitable, Callable

from ticketing.core.logging import get_logger

log = get_logger(__name__)


def install_stop_event() -> asyncio.Event:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    return stop


async def sleep_or_stop(stop: asyncio.Event, seconds: float) -> bool:
    """Sleep up to `seconds`; return True if a stop was requested."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True


async def run_service(name: str, body: Callable[[asyncio.Event], Awaitable[None]]) -> None:
    stop = install_stop_event()
    log.info("service.start", component=name)
    try:
        await body(stop)
    finally:
        log.info("service.stop", component=name)
