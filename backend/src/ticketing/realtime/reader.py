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
from ticketing.core.errors import ProblemError
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
            if frame.start <= cursor + 1:
                return frame
        return None


class EventReader:
    def __init__(self, pool: asyncpg.Pool, timeout: float, event_id: int) -> None:
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
        self._replays: dict[tuple[int, int], asyncio.Task[list[Frame]]] = {}
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

    async def replay(self, cursor: int, ceiling: int) -> list[Frame]:
        """Bound each replay read and release its connection before any network write."""
        if ceiling - cursor > MAX_REPLAY_ROWS:
            raise SequenceGap("Replay limit exceeded")
        key = (cursor, min(ceiling, cursor + REPLAY_ROWS))
        task = self._replays.get(key)
        if task is None:
            task = asyncio.create_task(self._read_replay(key))
            self._replays[key] = task
            task.add_done_callback(self._retrieve_replay_error)
        return await asyncio.shield(task)

    @staticmethod
    def _retrieve_replay_error(task: asyncio.Task[list[Frame]]) -> None:
        if not task.cancelled():
            task.exception()

    async def _read_replay(self, key: tuple[int, int]) -> list[Frame]:
        cursor, end = key
        try:
            async with acquire(self.pool, self.timeout) as conn:
                rows = await conn.fetch(_REPLAY, self.event_id, cursor, end, REPLAY_ROWS)
            if len(rows) != end - cursor:
                raise SequenceGap("Replay rows are missing")
            return coalesce(rows, cursor)
        finally:
            del self._replays[key]

    def stop(self) -> tuple[asyncio.Task[list[Frame] | None], ...]:
        tasks = (self.task, *self._replays.values())
        for task in tasks:
            task.cancel()
        return tasks

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

    @contextmanager
    def subscribe(self, event_id: int) -> Iterator[EventReader]:
        reader = self.events.get(event_id)
        if reader is None:
            reader = self.events[event_id] = EventReader(self.pool, self.timeout, event_id)
        reader.subscribers += 1
        try:
            yield reader
        finally:
            reader.subscribers -= 1
            if not reader.subscribers:
                reader.stop()
                self.events.pop(event_id, None)

    async def close(self) -> None:
        tasks = [task for reader in self.events.values() for task in reader.stop()]
        await asyncio.gather(*tasks, return_exceptions=True)
