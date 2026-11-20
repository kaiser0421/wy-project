"""Session persistence: authentication lookup, creation, revocation and activity refresh.

Every authenticated request performs one indexed lookup on the primary; there is deliberately no
cache, so logout, disable and role changes take effect on the very next request.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass
from datetime import timedelta

import asyncpg
import orjson
from redis.asyncio import Redis

from ticketing.auth.tokens import hash_token, new_session_token
from ticketing.core.logging import get_logger
from ticketing.core.settings import Settings

log = get_logger(__name__)
REVOKE_CHANNEL = "auth:revoke"


@dataclass(frozen=True, slots=True)
class SessionPrincipal:
    session_id: uuid.UUID
    user_id: int
    email: str
    role: str
    needs_refresh: bool


_AUTH_SQL = """
SELECT s.id, s.user_id, u.email, u.role::text AS role,
       s.last_seen_at <= now() - interval '60 seconds' AS needs_refresh
FROM sessions s
JOIN users u ON u.id = s.user_id
WHERE s.token_hash = $1
  AND s.revoked_at IS NULL
  AND s.abs_expires_at > now()
  AND s.idle_expires_at > now()
  AND u.is_active
"""


async def authenticate(pool: asyncpg.Pool, token: str) -> SessionPrincipal | None:
    row = await pool.fetchrow(_AUTH_SQL, hash_token(token))
    if row is None:
        return None
    return SessionPrincipal(
        session_id=row["id"],
        user_id=row["user_id"],
        email=row["email"],
        role=row["role"],
        needs_refresh=row["needs_refresh"],
    )


async def create_session(
    conn: asyncpg.Connection,
    user_id: int,
    settings: Settings,
    ip: str | None,
    user_agent: str | None,
) -> tuple[uuid.UUID, str]:
    token = new_session_token()
    session_id: uuid.UUID = await conn.fetchval(
        """
        INSERT INTO sessions (token_hash, user_id, csrf_secret, idle_expires_at, abs_expires_at,
                              ip, user_agent)
        VALUES ($1, $2, $3, now() + $4::interval, now() + $5::interval, $6::inet, $7)
        RETURNING id
        """,
        hash_token(token),
        user_id,
        f"v{settings.csrf_key_version}".encode(),
        timedelta(minutes=settings.session_idle_minutes),
        timedelta(hours=settings.session_absolute_hours),
        ip,
        (user_agent or "")[:512],
    )
    return session_id, token


async def revoke_session(conn: asyncpg.Connection, session_id: uuid.UUID, reason: str) -> bool:
    status: str = await conn.execute(
        "UPDATE sessions SET revoked_at = now(), revoked_reason = $2 "
        "WHERE id = $1 AND revoked_at IS NULL",
        session_id,
        reason,
    )
    return status.endswith(" 1")


async def revoke_user_sessions(
    conn: asyncpg.Connection, user_id: int, reason: str
) -> list[uuid.UUID]:
    rows = await conn.fetch(
        "UPDATE sessions SET revoked_at = now(), revoked_reason = $2 "
        "WHERE user_id = $1 AND revoked_at IS NULL RETURNING id",
        user_id,
        reason,
    )
    return [row["id"] for row in rows]


async def lock_live_session(conn: asyncpg.Connection, session_id: uuid.UUID, user_id: int) -> bool:
    """Session fence for sensitive writes, run inside the caller's transaction.

    Lock order is always users -> sessions (the same order as disable/role change, which take
    users FOR UPDATE and then revoke sessions), so the two can never deadlock. A concurrent
    revocation either commits first (we see it and refuse) or waits for this transaction.
    """
    active = await conn.fetchval("SELECT is_active FROM users WHERE id = $1 FOR SHARE", user_id)
    if not active:
        return False
    found = await conn.fetchval(
        """
        SELECT 1 FROM sessions
        WHERE id = $1 AND user_id = $2 AND revoked_at IS NULL
          AND abs_expires_at > now() AND idle_expires_at > now()
        FOR SHARE
        """,
        session_id,
        user_id,
    )
    return found is not None


async def publish_revocations(redis: Redis, session_ids: list[uuid.UUID]) -> None:
    """Best-effort fast path for closing open streams; the 1 s SSE recheck is the backstop."""
    if not session_ids:
        return
    payload = orjson.dumps([str(s) for s in session_ids])
    try:
        async with asyncio.timeout(0.5):
            await redis.publish(REVOKE_CHANNEL, payload)
    except Exception as exc:
        log.warning("auth.revoke_publish_failed", error=type(exc).__name__, count=len(session_ids))


class LastSeenRefresher:
    """Coalesces idle-timeout refreshes off the request path.

    Requests only enqueue session ids; a background task flushes them every few seconds with one
    conditional UPDATE that uses database time and never revives a revoked or expired session.
    """

    def __init__(self, pool: asyncpg.Pool, settings: Settings, interval_s: float = 5.0) -> None:
        self._pool = pool
        self._idle = timedelta(minutes=settings.session_idle_minutes)
        self._interval = interval_s
        self._pending: set[uuid.UUID] = set()
        self._task: asyncio.Task[None] | None = None
        self.failed_flushes = 0

    def touch(self, session_id: uuid.UUID) -> None:
        if len(self._pending) < 50_000:
            self._pending.add(session_id)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self.flush()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self.flush()

    async def flush(self) -> None:
        if not self._pending:
            return
        batch, self._pending = list(self._pending), set()
        try:
            await self._pool.execute(
                """
                UPDATE sessions
                SET last_seen_at = now(),
                    idle_expires_at = LEAST(abs_expires_at, now() + $2::interval)
                WHERE id = ANY($1::uuid[])
                  AND revoked_at IS NULL
                  AND abs_expires_at > now()
                  AND idle_expires_at > now()
                  AND last_seen_at <= now() - interval '60 seconds'
                """,
                batch,
                self._idle,
            )
        except Exception as exc:
            self.failed_flushes += 1
            log.warning("auth.refresh_failed", error=type(exc).__name__, count=len(batch))
