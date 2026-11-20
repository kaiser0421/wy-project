"""Two independent revocation paths for local SSE connections."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

import asyncpg
import orjson
from redis.asyncio import Redis
from redis.exceptions import RedisError

from ticketing.auth.sessions import REVOKE_CHANNEL
from ticketing.core.db import acquire
from ticketing.core.logging import get_logger

log = get_logger(__name__)

_LIVE_SESSIONS = """
WITH clock AS MATERIALIZED (SELECT app_now() AS sampled_at)
SELECT s.id
FROM sessions s JOIN users u ON u.id = s.user_id CROSS JOIN clock
WHERE s.id = ANY($1::uuid[]) AND s.revoked_at IS NULL AND u.is_active
  AND s.abs_expires_at > clock.sampled_at AND s.idle_expires_at > clock.sampled_at
"""


class Revocations:
    def __init__(self, pool: asyncpg.Pool, redis: Redis) -> None:
        self.pool = pool
        self.redis = redis
        self.sessions: dict[UUID, set[asyncio.Task[None]]] = {}
        self.active = 0
        self._tasks = (
            asyncio.create_task(self._listen()),
            asyncio.create_task(self._recheck()),
        )

    @contextmanager
    def watch(self, session_id: UUID, writer: asyncio.Task[None]) -> Iterator[None]:
        writers = self.sessions.setdefault(session_id, set())
        writers.add(writer)
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1
            writers.remove(writer)
            if not writers:
                del self.sessions[session_id]

    def revoke(self, session_id: UUID) -> None:
        for writer in self.sessions.get(session_id, ()):
            writer.cancel()

    async def _listen(self) -> None:
        while True:
            try:
                async with self.redis.pubsub() as pubsub:
                    await pubsub.subscribe(REVOKE_CHANNEL)
                    while True:
                        notice = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
                        if notice is not None:
                            self._apply_notice(notice["data"])
            except (RedisError, OSError, TimeoutError) as exc:
                log.warning("seat_stream.revoke_listener_failed", error=type(exc).__name__)
                await asyncio.sleep(0.25)

    def _apply_notice(self, raw: bytes) -> None:
        try:
            payload = orjson.loads(raw)
        except (ValueError, TypeError):
            return
        if isinstance(payload, list):
            for value in payload:
                if isinstance(value, str):
                    try:
                        sid = UUID(value)
                    except ValueError:
                        continue
                    self.revoke(sid)

    async def _live_sessions(self, session_ids: list[UUID]) -> set[UUID]:
        async with acquire(self.pool, 0.2) as conn, asyncio.timeout(0.3):
            rows = await conn.fetch(_LIVE_SESSIONS, session_ids)
        return {row["id"] for row in rows}

    async def _recheck(self) -> None:
        """Check distinct IDs independently of traffic and fail closed after two failures."""
        loop = asyncio.get_running_loop()
        failures = 0
        while True:
            started = loop.time()
            ids = list(self.sessions)
            if ids:
                try:
                    live = await self._live_sessions(ids)
                except Exception as exc:
                    failures += 1
                    log.warning(
                        "seat_stream.recheck_failed", error=type(exc).__name__, failures=failures
                    )
                    if failures >= 2:
                        for sid in self.sessions:
                            self.revoke(sid)
                else:
                    failures = 0
                    for sid in set(ids) - live:
                        self.revoke(sid)
            else:
                failures = 0
            await asyncio.sleep(max(0, started + 1.0 - loop.time()))

    async def close(self) -> None:
        for sid in self.sessions:
            self.revoke(sid)
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
