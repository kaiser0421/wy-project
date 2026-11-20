"""Resource visibility and the shared event write gate."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

import asyncpg

from ticketing.auth.sessions import lock_live_session
from ticketing.core.errors import ProblemError, server_busy


@dataclass(frozen=True, slots=True)
class EventActor:
    user_id: int
    role: str


def event_missing() -> ProblemError:
    return ProblemError(404, "not-found", "Event not found")


def can_view(actor: EventActor, owner_id: int, status: str) -> bool:
    return (
        actor.role == "admin"
        or status == "published"
        or (actor.role == "user" and owner_id == actor.user_id)
    )


def require_owner(actor: EventActor, owner_id: int, status: str) -> None:
    if not can_view(actor, owner_id, status):
        raise event_missing()
    if actor.role != "admin" and (actor.role != "user" or owner_id != actor.user_id):
        raise ProblemError(403, "forbidden", "Only the event owner or an administrator may do this")


async def live_writer(conn: asyncpg.Connection, actor: EventActor, session_id: UUID) -> None:
    if not await lock_live_session(conn, session_id, actor.user_id):
        raise ProblemError(401, "unauthenticated", "Your session ended")


@asynccontextmanager
async def event_transaction(
    conn: asyncpg.Connection, *, isolation: str = "read_committed", readonly: bool = False
) -> AsyncIterator[None]:
    """Bound lock contention and let the caller retry an aborted event transaction."""
    try:
        async with conn.transaction(isolation=isolation, readonly=readonly):
            yield
    except (
        asyncpg.LockNotAvailableError,
        asyncpg.QueryCanceledError,
        asyncpg.SerializationError,
        asyncpg.DeadlockDetectedError,
        TimeoutError,
    ) as exc:
        raise server_busy() from exc


async def get_event(
    conn: asyncpg.Connection,
    event_id: int,
    actor: EventActor,
    *,
    access: Literal["view", "manage", "lock"],
) -> asyncpg.Record:
    if access == "lock":
        row = await conn.fetchrow("SELECT * FROM events WHERE id = $1 FOR UPDATE", event_id)
    else:
        row = await conn.fetchrow("SELECT * FROM events WHERE id = $1", event_id)
    if row is None or not can_view(actor, row["owner_id"], row["status"]):
        raise event_missing()
    if access != "view":
        require_owner(actor, row["owner_id"], row["status"])
    return row
