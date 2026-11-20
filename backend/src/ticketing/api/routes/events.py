"""Event and zone HTTP endpoints."""

from typing import Annotated, Any

from fastapi import Depends, Header, Path, Query, Request, Response

from ticketing.api.security import Authenticated, Principal, Roles, SecureRouter, current_principal
from ticketing.core.db import acquire
from ticketing.core.errors import PROBLEM_CONTENT_TYPE, ProblemError
from ticketing.events.access import EventActor, event_transaction, get_event, live_writer
from ticketing.events.models import EventCreate, EventOut, EventPage, EventPatch, ZoneOut
from ticketing.events.pagination import EventQuery, list_events
from ticketing.events.service import (
    create_event,
    delete_event,
    etag,
    expected_version,
    patch_event,
    publish_event,
    representation,
)

router = SecureRouter(prefix="/api/v1/events", tags=["events"])
WRITE = Roles("admin", "user")
READ = Authenticated()
Current = Annotated[Principal, Depends(current_principal)]
EventId = Annotated[int, Path(ge=1, le=2**63 - 1, description="Event ID", examples=[42])]
Match = Annotated[
    str | None, Header(description='Current strong ETag, such as "v1"', examples=['"v1"'])
]


def problems(
    *, write: bool = False, codes: dict[str, str] | None = None
) -> dict[int | str, dict[str, Any]]:
    errors = {"401": "unauthenticated", "422": "validation-failed", "503": "server-busy"}
    if write:
        errors["403"] = "forbidden, csrf-failed, origin-not-allowed"
    if codes:
        errors.update(codes)
    return {
        int(status): {
            "description": f"Problem code(s) `{code}`",
            "content": {PROBLEM_CONTENT_TYPE: {}},
        }
        for status, code in errors.items()
    }


_NOT_FOUND = {"404": "not-found"}
_CONDITIONAL = {
    **_NOT_FOUND,
    "412": "precondition-failed (includes current and ETag)",
    "428": "precondition-required (includes current and ETag)",
}
_ETAG = {"ETag": {"description": "Strong event version", "schema": {"type": "string"}}}


@router.post(
    "",
    policy=WRITE,
    status_code=201,
    response_model=EventOut,
    summary="Create a draft event",
    description=(
        "Users and administrators create a draft with its zones in one transaction. Each "
        "successful POST creates a new event and returns its Location and ETag."
    ),
    responses={
        **problems(write=True),
        201: {
            "headers": {
                **_ETAG,
                "Location": {"schema": {"type": "string", "format": "uri-reference"}},
            }
        },
    },
)
async def create(
    body: EventCreate, request: Request, response: Response, principal: Current
) -> EventOut:
    state = request.app.state
    actor = EventActor(principal.user_id, principal.role)
    if any(
        "\x00" in value
        for value in (
            body.title,
            body.description,
            body.category,
            body.venue,
            *(z.name for z in body.zones),
        )
    ):
        raise ProblemError(422, "validation-failed", "Text fields cannot contain NUL")
    async with (
        acquire(state.db, state.settings.db_pool_acquire_timeout_s) as conn,
        event_transaction(conn),
    ):
        await live_writer(conn, actor, principal.session_id)
        event = await create_event(conn, actor.user_id, body)
    response.headers["ETag"] = etag(event.version)
    response.headers["Location"] = f"/api/v1/events/{event.id}"
    return event


@router.get(
    "",
    policy=READ,
    response_model=EventPage,
    summary="List events using a snapshot",
    description=(
        "Viewers see published events. Users also see their own drafts, and administrators see "
        "all events. The first page fixes membership and order for ten minutes. Later pages use "
        "current values, represent deletions with placeholders and reject cursors if access is "
        "lost. Starting a new snapshot consumes one of 20 active slots."
    ),
    responses=problems(
        codes={
            "409": "cursor-invalidated",
            "410": "cursor-expired",
            "422": "validation-failed, refine-filters",
            "429": "snapshot-limit",
        }
    ),
)
async def listing(
    request: Request, principal: Current, query: Annotated[EventQuery, Query()]
) -> EventPage:
    state = request.app.state
    async with (
        acquire(state.db, state.settings.db_pool_acquire_timeout_s) as conn,
        event_transaction(
            conn,
            isolation="repeatable_read" if query.cursor else "read_committed",
        ),
    ):
        return await list_events(
            conn,
            EventActor(principal.user_id, principal.role),
            query,
            state.settings.secret_key.get_secret_value(),
        )


@router.get(
    "/{event_id}",
    policy=READ,
    response_model=EventOut,
    summary="Get an event",
    description=(
        "All roles can read published events. Drafts are visible to the owner and administrators. "
        "The idempotent read returns event fields and zones from one snapshot with a strong ETag."
    ),
    responses={**problems(codes=_NOT_FOUND), 200: {"headers": _ETAG}},
)
async def get(
    event_id: EventId, request: Request, response: Response, principal: Current
) -> EventOut:
    state = request.app.state
    async with (
        acquire(state.db, state.settings.db_pool_acquire_timeout_s) as conn,
        event_transaction(conn, isolation="repeatable_read", readonly=True),
    ):
        row = await get_event(
            conn, event_id, EventActor(principal.user_id, principal.role), access="view"
        )
        event = await representation(conn, row)
    response.headers["ETag"] = etag(event.version)
    return event


@router.patch(
    "/{event_id}",
    policy=WRITE,
    response_model=EventOut,
    summary="Edit an event",
    description=(
        "Only the owner or an administrator may edit. If-Match guards concurrent edits and "
        "advances the ETag on success. Sales dates and zones are frozen after publication. A "
        "draft zone replacement retains seats in zones with the same code."
    ),
    responses={
        **problems(
            write=True,
            codes={
                **_CONDITIONAL,
                "409": "field-frozen-after-publish, import-running, zone-has-seats",
            },
        ),
        200: {"headers": _ETAG},
    },
)
async def patch(
    event_id: EventId,
    body: EventPatch,
    request: Request,
    response: Response,
    principal: Current,
    if_match: Match = None,
) -> EventOut:
    state = request.app.state
    actor = EventActor(principal.user_id, principal.role)
    async with (
        acquire(state.db, state.settings.db_pool_acquire_timeout_s) as conn,
        event_transaction(conn),
    ):
        await live_writer(conn, actor, principal.session_id)
        row = await get_event(conn, event_id, actor, access="lock")
        event = await patch_event(conn, row, body, expected_version(if_match))
    response.headers["ETag"] = etag(event.version)
    return event


@router.delete(
    "/{event_id}",
    policy=WRITE,
    status_code=204,
    response_model=None,
    summary="Delete an event",
    description=(
        "The owner or an administrator can delete an event with no orders. The event lock "
        "serializes deletion with other event writes. A repeat after successful deletion returns "
        "404."
    ),
    responses=problems(write=True, codes={**_NOT_FOUND, "409": "event-has-orders"}),
)
async def delete(event_id: EventId, request: Request, principal: Current) -> Response:
    state = request.app.state
    actor = EventActor(principal.user_id, principal.role)
    async with (
        acquire(state.db, state.settings.db_pool_acquire_timeout_s) as conn,
        event_transaction(conn),
    ):
        await live_writer(conn, actor, principal.session_id)
        await get_event(conn, event_id, actor, access="lock")
        await delete_event(conn, event_id)
    return Response(status_code=204)


@router.post(
    "/{event_id}/publish",
    policy=WRITE,
    response_model=EventOut,
    summary="Publish an event",
    description=(
        "The owner or an administrator may publish with the current If-Match once seats exist and "
        "no import is active. The event lock serializes publication with import admission. "
        "Repeating with the current ETag after publication returns the same version."
    ),
    responses={
        **problems(write=True, codes={**_CONDITIONAL, "409": "no-seats, import-running"}),
        200: {"headers": _ETAG},
    },
)
async def publish(
    event_id: EventId,
    request: Request,
    response: Response,
    principal: Current,
    if_match: Match = None,
) -> EventOut:
    state = request.app.state
    actor = EventActor(principal.user_id, principal.role)
    async with (
        acquire(state.db, state.settings.db_pool_acquire_timeout_s) as conn,
        event_transaction(conn),
    ):
        await live_writer(conn, actor, principal.session_id)
        row = await get_event(conn, event_id, actor, access="lock")
        event = await publish_event(conn, row, expected_version(if_match))
    response.headers["ETag"] = etag(event.version)
    return event


@router.get(
    "/{event_id}/zones",
    policy=READ,
    response_model=list[ZoneOut],
    summary="List event zones",
    description=(
        "All roles may read published event zones. Only the owner and administrators may read "
        "draft zones. An idempotent read returns counts from one database snapshot."
    ),
    responses=problems(codes=_NOT_FOUND),
)
async def zones(event_id: EventId, request: Request, principal: Current) -> list[ZoneOut]:
    state = request.app.state
    async with (
        acquire(state.db, state.settings.db_pool_acquire_timeout_s) as conn,
        event_transaction(conn, isolation="repeatable_read", readonly=True),
    ):
        await get_event(
            conn, event_id, EventActor(principal.user_id, principal.role), access="view"
        )
        rows = await conn.fetch(
            """SELECT z.id, z.code, z.name, z.price_twd,
                      count(s.id) FILTER (WHERE s.status = 'available') AS available
               FROM zones z LEFT JOIN seats s ON s.event_id = z.event_id AND s.zone_id = z.id
               WHERE z.event_id = $1 GROUP BY z.id ORDER BY z.code""",
            event_id,
        )
    return [ZoneOut.model_validate(dict(row)) for row in rows]
