"""Allow tests to inject a seat strategy without shipping its SQL in the service image."""

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import asyncpg

from ticketing.booking.models import SpecificSeats, ZoneSeats
from ticketing.core.settings import Settings

Allocation = Callable[
    [asyncpg.Connection, int, SpecificSeats | ZoneSeats], Awaitable[list[asyncpg.Record]]
]
Hold = Callable[[asyncpg.Connection, int, list[int]], Awaitable[list[asyncpg.Record]]]


@dataclass(frozen=True, slots=True)
class SeatStrategy:
    allocate: Allocation
    mark_held: Hold


override: SeatStrategy | None = None


def naive_strategy(settings: Settings) -> SeatStrategy | None:
    if settings.enable_test_hooks and os.getenv("BOOKING_STRATEGY") == "naive":
        return override
    return None
