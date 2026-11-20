"""Seat snapshots and the process-local single-flight cache."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Literal

import asyncpg
from pydantic import BaseModel, Field

from ticketing.core.db import acquire
from ticketing.core.errors import ProblemError

SeatStatus = Literal["available", "held", "sold"]
SnapshotFormat = Literal["full", "compact"]
STATUS_CHAR = {"available": "A", "held": "H", "sold": "S"}
_CACHE_TTL_S = 1.0
_CACHE_EVENTS = 32
_CACHE_SEATS = 100_000


class Zone(BaseModel):
    code: str = Field(description="Zone code referenced by seats", examples=["A"])
    name: str = Field(description="Zone display name", examples=["Main floor"])
    price_twd: int = Field(description="Price per seat in whole Taiwan dollars", examples=[2000])


class SeatLocation(BaseModel):
    idx: int = Field(description="Seat index used by stream deltas", examples=[0])
    id: int = Field(description="Seat ID used when booking", examples=[84])
    zone: str = Field(description="Zone code", examples=["A"])
    row: str = Field(description="Seat row label", examples=["C"])
    number: str = Field(description="Seat number as displayed", examples=["12"])


class Seat(SeatLocation):
    status: SeatStatus = Field(description="Current seat status", examples=["available"])
    version: int = Field(description="Monotonic seat state version", examples=[2])


class SnapshotHeader(BaseModel):
    event_id: int = Field(description="Event ID", examples=[1])
    layout_ver: int = Field(description="Version of the seat index mapping", examples=[2])
    last_seq: int = Field(description="Resume the stream after this sequence", examples=[31])
    zones: list[Zone] = Field(description="Zones sorted by code", examples=[[]])


class FullSnapshot(SnapshotHeader):
    seats: list[Seat] = Field(description="Seats sorted by index with their state", examples=[[]])


class CompactSnapshot(SnapshotHeader):
    seats: list[SeatLocation] = Field(
        description="Seat locations sorted by index. Array offsets match status and versions.",
        examples=[[]],
    )
    status: str = Field(
        description="One character per seat in the seats array. A available, H held, S sold.",
        examples=["AHS"],
    )
    versions: list[int] = Field(description="One version per seat in array order", examples=[[2]])


def event_not_found() -> ProblemError:
    return ProblemError(404, "not-found", "Not found", "Event not found.")


async def require_visible(
    pool: asyncpg.Pool, acquire_timeout: float, event_id: int, user_id: int, admin: bool
) -> None:
    """Authorize each request against PostgreSQL, independently of cache contents."""
    async with acquire(pool, acquire_timeout) as conn:
        found = await conn.fetchval(
            """
            SELECT 1 FROM events
            WHERE id = $1 AND (status = 'published' OR owner_id = $2 OR $3::bool)
            """,
            event_id,
            user_id,
            admin,
        )
    if found is None:
        raise event_not_found()


@dataclass(slots=True)
class CachedSnapshot:
    snapshot: FullSnapshot
    expires_at: float
    bodies: dict[SnapshotFormat, bytes] = field(default_factory=dict)

    def body(self, format_: SnapshotFormat) -> bytes:
        if format_ not in self.bodies:
            snapshot = self.snapshot
            model: FullSnapshot | CompactSnapshot = snapshot
            if format_ == "compact":
                model = CompactSnapshot(
                    event_id=snapshot.event_id,
                    layout_ver=snapshot.layout_ver,
                    last_seq=snapshot.last_seq,
                    zones=snapshot.zones,
                    seats=[
                        SeatLocation(idx=s.idx, id=s.id, zone=s.zone, row=s.row, number=s.number)
                        for s in snapshot.seats
                    ],
                    status="".join(STATUS_CHAR[s.status] for s in snapshot.seats),
                    versions=[s.version for s in snapshot.seats],
                )
            self.bodies[format_] = model.model_dump_json().encode()
        return self.bodies[format_]


class SnapshotCache:
    def __init__(self, pool: asyncpg.Pool, timeout: float) -> None:
        self._pool = pool
        self._timeout = timeout
        self._cache: OrderedDict[int, CachedSnapshot] = OrderedDict()
        self._pending: dict[int, asyncio.Task[CachedSnapshot]] = {}

    async def get(self, event_id: int, min_layout_ver: int) -> CachedSnapshot:
        cached = self._cache.get(event_id)
        now = asyncio.get_running_loop().time()
        if (
            cached is not None
            and cached.expires_at > now
            and cached.snapshot.layout_ver >= min_layout_ver
        ):
            self._cache.move_to_end(event_id)
            return cached
        pending = self._pending.get(event_id)
        if pending is not None:
            cached = await asyncio.shield(pending)
            if cached.snapshot.layout_ver >= min_layout_ver:
                return cached
            # The shared read may have started before the layout that prompted this request.
        return await self._join_refresh(event_id)

    async def _join_refresh(self, event_id: int) -> CachedSnapshot:
        task = self._pending.get(event_id)
        if task is None:
            task = asyncio.create_task(self._refresh(event_id))
            self._pending[event_id] = task
            task.add_done_callback(self._retrieve_error)
        # A disconnected request must not cancel the shared refresh.
        return await asyncio.shield(task)

    @staticmethod
    def _retrieve_error(task: asyncio.Task[CachedSnapshot]) -> None:
        if not task.cancelled():
            task.exception()

    async def _refresh(self, event_id: int) -> CachedSnapshot:
        started = asyncio.get_running_loop().time()
        try:
            snapshot = await self._read(event_id)
            cached = CachedSnapshot(snapshot, started + _CACHE_TTL_S)
            # Expired layouts can be large. Drop them on the next refresh.
            now = asyncio.get_running_loop().time()
            for key, entry in tuple(self._cache.items()):
                if entry.expires_at <= now:
                    del self._cache[key]
            self._cache[event_id] = cached
            self._cache.move_to_end(event_id)
            total_seats = sum(len(entry.snapshot.seats) for entry in self._cache.values())
            while len(self._cache) > _CACHE_EVENTS or total_seats > _CACHE_SEATS:
                _, oldest = self._cache.popitem(last=False)
                total_seats -= len(oldest.snapshot.seats)
            return cached
        finally:
            del self._pending[event_id]

    async def _read(self, event_id: int) -> FullSnapshot:
        # Seat versions and assigned head must describe the very same MVCC view.
        async with (
            acquire(self._pool, self._timeout) as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            event = await conn.fetchrow(
                """
                SELECT e.layout_ver, coalesce(s.last_assigned_seq, 0) AS last_seq
                FROM events e LEFT JOIN event_stream_state s ON s.event_id = e.id
                WHERE e.id = $1
                """,
                event_id,
            )
            if event is None:
                raise event_not_found()
            zones = await conn.fetch(
                "SELECT code, name, price_twd FROM zones WHERE event_id = $1 ORDER BY code",
                event_id,
            )
            seats = await conn.fetch(
                """
                SELECT s.seat_idx AS idx, s.id, z.code AS zone, s.row_label AS row,
                       s.seat_number AS number, s.status, s.version
                FROM seats s JOIN zones z ON (z.event_id, z.id) = (s.event_id, s.zone_id)
                WHERE s.event_id = $1 ORDER BY s.seat_idx
                """,
                event_id,
            )
        return FullSnapshot(
            event_id=event_id,
            layout_ver=event["layout_ver"],
            last_seq=event["last_seq"],
            zones=[Zone(**dict(z)) for z in zones],
            seats=[Seat(**dict(s)) for s in seats],
        )

    async def close(self) -> None:
        pending = tuple(self._pending.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._cache.clear()
