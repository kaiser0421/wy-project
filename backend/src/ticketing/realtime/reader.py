"""One PostgreSQL reader and bounded encoded replay ring per locally watched event."""

from __future__ import annotations

import asyncio
from bisect import bisect_left
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import asyncpg

from ticketing.core.db import acquire
from ticketing.core.errors import ProblemError, server_busy
from ticketing.core.logging import get_logger
from ticketing.realtime.client_model import SequenceGap
from ticketing.realtime.frames import Frame, coalesce, encode

log = get_logger(__name__)

POLL_S = 0.1
PROGRESS_S = 1.0
RING_S = 5.0
RING_BYTES = 2 * 1024 * 1024
POLL_ROWS = 1000
REPLAY_ROWS = 256
MAX_REPLAY_ROWS = 5000
MAX_REPLAY_TASKS = 8
MAX_EVENT_READERS = 64

_DB_FAILURES = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError, ProblemError)

_PROGRESS = """
SELECT coalesce(s.last_assigned_seq, 0) AS head,
       greatest(0, coalesce(
           (extract(epoch FROM clock_timestamp() - oldest.created_at) * 1000)::bigint, 0
       )) AS oldest_unassigned_age_ms
FROM events e
LEFT JOIN event_stream_state s ON s.event_id = e.id
LEFT JOIN LATERAL (
    SELECT created_at FROM outbox WHERE event_id = e.id AND seq IS NULL
    ORDER BY created_at LIMIT 1
) oldest ON true
WHERE e.id = $1
"""
_POLL = """
SELECT seq, kind, changes, created_at FROM outbox
WHERE event_id = $1 AND seq > $2 ORDER BY seq LIMIT $3
"""
_REPLAY = """
SELECT seq, kind, changes, created_at FROM outbox
WHERE event_id = $1 AND seq > $2 AND seq <= $3 ORDER BY seq LIMIT $4
"""


@dataclass(frozen=True, slots=True)
class RingEntry:
    at: float
    frame: Frame


def _end(entry: RingEntry) -> int:
    return entry.frame.end


class Ring:
    def __init__(self) -> None:
        self.entries: deque[RingEntry] = deque()
        self.size = 0

    def append(self, frame: Frame, now: float) -> None:
        self.entries.append(RingEntry(now, frame))
        self.size += len(frame.body)
        self.prune(now)

    def prune(self, now: float) -> None:
        while self.entries and (self.entries[0].at <= now - RING_S or self.size > RING_BYTES):
            self.size -= len(self.entries.popleft().frame.body)

    def after(self, cursor: int) -> Frame | None:
        idx = bisect_left(self.entries, cursor + 1, key=_end)
        if idx < len(self.entries):
            frame = self.entries[idx].frame
            if frame.start == cursor + 1:
                return frame
        return None


class Replays:
    def __init__(self, pool: asyncpg.Pool, timeout: float) -> None:
        self.pool = pool
        self.timeout = timeout
        self._tasks: dict[tuple[int, int, int], asyncio.Task[list[Frame]]] = {}

    async def read(self, event_id: int, cursor: int, ceiling: int) -> list[Frame]:
        """Share bounded reads across subscribers and release connections before network writes."""
        if ceiling - cursor > MAX_REPLAY_ROWS:
            raise SequenceGap("Replay limit exceeded")
        key = (event_id, cursor, min(ceiling, cursor + REPLAY_ROWS))
        task = self._tasks.get(key)
        if task is None:
            if len(self._tasks) >= MAX_REPLAY_TASKS:
                raise server_busy("Seat stream replay capacity exhausted.")
            task = asyncio.create_task(self._read(*key))
            self._tasks[key] = task
            task.add_done_callback(lambda done: self._finished(key, done))
        return await asyncio.shield(task)

    def _finished(self, key: tuple[int, int, int], task: asyncio.Task[list[Frame]]) -> None:
        del self._tasks[key]
        # The last waiter may have disconnected while the shielded read was running.
        if not task.cancelled():
            task.exception()

    async def _read(self, event_id: int, cursor: int, end: int) -> list[Frame]:
        async with acquire(self.pool, self.timeout) as conn:
            rows = await conn.fetch(_REPLAY, event_id, cursor, end, REPLAY_ROWS)
        if len(rows) != end - cursor:
            raise SequenceGap("Replay rows are missing")
        return coalesce(rows, cursor)

    async def close(self) -> None:
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class EventReader:
    def __init__(self, pool: asyncpg.Pool, timeout: float, event_id: int, replays: Replays) -> None:
        self.pool = pool
        self.timeout = timeout
        self.event_id = event_id
        self.head: int | None = None
        self.progress: bytes | None = None
        self.ring = Ring()
        self.changed = asyncio.Event()
        self.ready = asyncio.Event()
        self.subscribers = 0
        self.broken = False
        self.replays = replays
        self.task = asyncio.create_task(self.run())

    def notify(self) -> None:
        changed = self.changed
        self.changed = asyncio.Event()
        changed.set()

    async def assigned_head(self) -> int:
        async with acquire(self.pool, self.timeout) as conn:
            head: int = await conn.fetchval(
                """
                SELECT coalesce((SELECT last_assigned_seq FROM event_stream_state
                                 WHERE event_id = $1), 0)
                """,
                self.event_id,
            )
        return head

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        next_progress = 0.0
        assigned = 0
        while True:
            started = loop.time()
            self.ring.prune(started)
            try:
                if started >= next_progress:
                    async with acquire(self.pool, self.timeout) as conn:
                        progress = await conn.fetchrow(_PROGRESS, self.event_id)
                    if progress is None:
                        raise SequenceGap("Event no longer exists")
                    assigned = progress["head"]
                    self.progress = encode("progress", dict(progress))
                    next_progress = started + PROGRESS_S
                    self.notify()
                    if self.head is None:
                        self.head = assigned
                        self.ready.set()
                assert self.head is not None
                async with acquire(self.pool, self.timeout) as conn:
                    rows = await conn.fetch(_POLL, self.event_id, self.head, POLL_ROWS)
                frames = coalesce(rows, self.head)
                if frames:
                    now = loop.time()
                    for frame in frames:
                        self.ring.append(frame, now)
                    self.head = frames[-1].end
                    self.notify()
                if len(rows) < POLL_ROWS and self.head < assigned:
                    raise SequenceGap("Assigned rows are missing")
            except _DB_FAILURES as exc:
                log.warning("seat_stream.poll_failed", error=type(exc).__name__)
            except Exception as exc:
                log.warning("seat_stream.reader_stopped", error=type(exc).__name__)
                self.broken = True
                self.ready.set()
                self.notify()
                return
            await asyncio.sleep(max(0, started + POLL_S - loop.time()))


class Readers:
    def __init__(self, pool: asyncpg.Pool, timeout: float) -> None:
        self.pool = pool
        self.timeout = timeout
        self.events: dict[int, EventReader] = {}
        self.replays = Replays(pool, timeout)

    @contextmanager
    def subscribe(self, event_id: int) -> Iterator[EventReader]:
        reader = self.events.get(event_id)
        if reader is None:
            if len(self.events) >= MAX_EVENT_READERS:
                raise server_busy("Seat stream event reader capacity exhausted.")
            reader = self.events[event_id] = EventReader(
                self.pool, self.timeout, event_id, self.replays
            )
        reader.subscribers += 1
        try:
            yield reader
        finally:
            reader.subscribers -= 1
            if not reader.subscribers:
                reader.task.cancel()
                self.events.pop(event_id, None)

    async def close(self) -> None:
        tasks = [reader.task for reader in self.events.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, self.replays.close(), return_exceptions=True)
