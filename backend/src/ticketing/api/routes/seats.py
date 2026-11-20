"""Authenticated seat snapshots and resumable Server-Sent Events."""

from __future__ import annotations

from typing import Annotated

import asyncpg
from fastapi import Depends, Header, Path, Query, Request, Response

from ticketing.api.security import Authenticated, Principal, SecureRouter, current_principal
from ticketing.core.errors import PROBLEM_CONTENT_TYPE, ProblemError, server_busy
from ticketing.realtime.snapshot import (
    CompactSnapshot,
    FullSnapshot,
    SnapshotCache,
    SnapshotFormat,
    require_visible,
)
from ticketing.realtime.stream import SeatStream

router = SecureRouter(prefix="/api/v1/events", tags=["seats"])
_MAX_BIGINT = 2**63 - 1
EventId = Annotated[int, Path(ge=1, le=_MAX_BIGINT, description="Event ID", examples=[1])]
Caller = Annotated[Principal, Depends(current_principal)]

_ERRORS = {
    status: {
        "description": description,
        "content": {PROBLEM_CONTENT_TYPE: {"example": {"status": status, "code": code}}},
    }
    for status, code, description in (
        (401, "unauthenticated", "`unauthenticated` — a valid session is required."),
        (404, "not-found", "`not-found` — the event is absent or hidden."),
        (
            422,
            "validation-failed",
            "`validation-failed` — invalid path, query or header parameters.",
        ),
        (500, "internal-error", "`internal-error` — unexpected server failure."),
        (503, "server-busy", "`server-busy` — database capacity or timeout. Retry shortly."),
    )
}
_VISIBILITY = (
    "Any authenticated role may read a published event. Drafts are visible to the owner "
    "and administrators. Missing and hidden events return 404."
)
_DB_UNAVAILABLE = (
    TimeoutError,
    asyncpg.PostgresConnectionError,
    asyncpg.QueryCanceledError,
    asyncpg.CannotConnectNowError,
    asyncpg.TooManyConnectionsError,
    OSError,
)


@router.get(
    "/{event_id}/seats",
    policy=Authenticated(),
    summary="Get a consistent seat snapshot",
    description=(
        f"{_VISIBILITY}\n\n"
        "Read-only and safe to retry. Layout, seat state and the assigned stream sequence come "
        "from one repeatable-read database snapshot. Concurrent requests on the same API process "
        "share a refresh. A cached result is at most one second old and is bypassed when its "
        "layout version is below `min_layout_ver`. The response carries the database version "
        "even if it is below the requested minimum. Resume the stream using `last_seq`."
    ),
    response_model=FullSnapshot | CompactSnapshot,
    responses=_ERRORS,
    openapi_extra={"security": [{"sessionCookie": []}]},
)
async def get_seats(
    event_id: EventId,
    request: Request,
    principal: Caller,
    format_: Annotated[
        SnapshotFormat,
        Query(
            alias="format",
            description="Full seat objects or compact status and version arrays",
            examples=["compact"],
        ),
    ] = "full",
    min_layout_ver: Annotated[
        int,
        Query(ge=0, description="Bypass cached layouts below this version", examples=[2]),
    ] = 0,
) -> Response:
    state = request.app.state
    cache: SnapshotCache = state.seat_snapshots
    try:
        await require_visible(
            state.db,
            state.settings.db_pool_acquire_timeout_s,
            event_id,
            principal.user_id,
            principal.is_admin,
        )
        snapshot = await cache.get(event_id, min_layout_ver)
    except _DB_UNAVAILABLE as exc:
        raise server_busy() from exc
    return Response(
        snapshot.body(format_),
        media_type="application/json",
        headers={
            "Cache-Control": "private, no-store",
        },
    )


@router.get(
    "/{event_id}/seat-stream",
    policy=Authenticated(),
    summary="Subscribe to a resumable seat update stream",
    description=(
        f"{_VISIBILITY}\n\n"
        "The read-only SSE stream accepts the last **applied** sequence in `Last-Event-ID`. "
        "Start with a snapshot and send its `last_seq`. Omitting the header tails the live head. "
        "Registration captures a live head and replays through it before following new frames. "
        "Reconnects may receive overlapping intervals. Apply only newer seat versions.\n\n"
        "- `delta`, with `id: <to>` and payload "
        '`{"from": 10, "to": 12, "changes": [[0, "held", 2]], "ts": "2026-11-21T00:00:00Z"}`. '
        "The inclusive range covers all coalesced outbox rows and `ts` is their earliest outbox "
        "creation time. If `from > last_applied + 1`, reconnect from the last applied sequence.\n"
        '- `layout`, with `id: <seq>`: `{"layout_ver": 3}`. Fetch a snapshot with '
        "`min_layout_ver=3` before applying deltas. Resume at that snapshot's sequence.\n"
        '- `progress`, every second: `{"head": 12, "oldest_unassigned_age_ms": 0}`. '
        "If progress advances beyond the applied cursor for more than three seconds or the oldest "
        "unassigned change exceeds two seconds, show the state as stale.\n"
        '- `resync`, followed by EOF: `{"reason": "replay-unavailable"}`. '
        "Fetch a new snapshot and reconnect. A stalled socket may close before the frame arrives.\n"
        "- A heartbeat comment is sent every 15 seconds. Revoked sessions are disconnected. "
        "Bytes already handed to the transport can still arrive."
    ),
    status_code=200,
    response_class=SeatStream,
    response_model=None,
    responses={
        **_ERRORS,
        200: {
            "description": "Server-Sent Events with the frame formats described above.",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
    },
    openapi_extra={"security": [{"sessionCookie": []}]},
)
async def seat_stream(
    event_id: EventId,
    request: Request,
    principal: Caller,
    last_event_id: Annotated[
        str | None,
        Header(
            pattern=r"^[0-9]{1,19}$",
            description="Last applied sequence, or last_seq from a new snapshot",
            examples=["31"],
        ),
    ] = None,
) -> SeatStream:
    resume = int(last_event_id) if last_event_id is not None else None
    if resume is not None and resume > _MAX_BIGINT:
        raise ProblemError(422, "validation-failed", "Last-Event-ID is out of range")
    state = request.app.state
    try:
        await require_visible(
            state.db,
            state.settings.db_pool_acquire_timeout_s,
            event_id,
            principal.user_id,
            principal.is_admin,
        )
    except _DB_UNAVAILABLE as exc:
        raise server_busy() from exc
    return SeatStream(
        state.seat_readers, state.seat_revocations, event_id, principal.session_id, resume
    )
