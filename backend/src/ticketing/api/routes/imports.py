"""Seat import upload, progress and error download."""

import asyncio
import csv
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import Path as PathParam
from fastapi import Request, Response
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError
from starlette.background import BackgroundTask
from starlette.responses import FileResponse, StreamingResponse

from ticketing.api.routes.events import WRITE, Current, EventId, problems
from ticketing.api.security import SecureRouter, resolve_principal
from ticketing.core.db import acquire
from ticketing.core.errors import ProblemError
from ticketing.events.access import EventActor, event_transaction, get_event, live_writer
from ticketing.imports.jobs import admit, job_out, read_job, safe_cell
from ticketing.imports.models import JobOut
from ticketing.imports.upload import store_upload

router = SecureRouter(prefix="/api/v1", tags=["imports"])
JobId = Annotated[
    UUID,
    PathParam(description="Seat import job ID", examples=["ddfb26f6-fe06-4aae-b557-6c268323f99a"]),
]
_CSV = ("line", "item_index", "zone_code", "row_label", "seat_number", "reason")
_ACTIVE = {"queued", "running"}


@router.post(
    "/events/{event_id}/seat-imports",
    policy=WRITE,
    status_code=202,
    response_model=JobOut,
    summary="Upload a seat layout",
    description=(
        "The owner or an administrator may upload CSV or JSON to a draft. The file is streamed to "
        "disk before admitting one active job per event. Identical bytes replay the original job "
        "with 200 regardless of mode or publication state."
    ),
    responses={
        **problems(
            write=True,
            codes={
                "400": "invalid-multipart",
                "404": "not-found",
                "409": "event-not-draft, import-running",
                "413": "payload-too-large",
                "415": "unsupported-media-type",
            },
        ),
        200: {"model": JobOut, "description": "Existing job for identical file bytes"},
        202: {
            "headers": {
                "Location": {
                    "description": "URL of the newly queued import job",
                    "schema": {"type": "string", "format": "uri-reference"},
                }
            }
        },
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {
                            "file": {
                                "type": "string",
                                "format": "binary",
                                "description": "UTF-8 .csv or .json seat layout",
                                "examples": ["seats.csv"],
                            },
                            "mode": {
                                "type": "string",
                                "enum": ["all_or_nothing", "skip_invalid"],
                                "default": "all_or_nothing",
                                "description": (
                                    "Whether to save valid rows when other rows contain errors"
                                ),
                                "examples": ["skip_invalid"],
                            },
                        },
                    }
                }
            },
        }
    },
)
async def upload(
    event_id: EventId, request: Request, response: Response, principal: Current
) -> JobOut:
    state = request.app.state
    actor = EventActor(principal.user_id, principal.role)
    timeout = state.settings.db_pool_acquire_timeout_s
    async with acquire(state.db, timeout) as conn:
        await get_event(conn, event_id, actor, access="manage")
    stored = await store_upload(
        request.stream(),
        request.headers.get("content-type", ""),
        state.settings.import_dir,
        state.settings.import_max_bytes,
    )
    placed = False
    try:
        async with acquire(state.db, timeout) as conn, event_transaction(conn):
            await live_writer(conn, actor, principal.session_id)
            event = await get_event(conn, event_id, actor, access="lock")
            # Keep the file if an interrupted COMMIT could have made the job durable.
            row, placed = await admit(conn, event, actor, stored)
    except ProblemError:
        if not placed:
            stored.path.unlink(missing_ok=True)
        raise
    if not placed:
        stored.path.unlink(missing_ok=True)
        response.status_code = 200
    response.headers["Location"] = f"/api/v1/seat-imports/{row['id']}"
    return job_out(row, state.settings.import_max_rows)


@router.get(
    "/seat-imports/{job_id}",
    policy=WRITE,
    response_model=JobOut,
    summary="Get seat import progress",
    description=(
        "Only the parent event owner or an administrator may read the job. Poll this idempotent "
        "endpoint for progress and terminal results."
    ),
    responses=problems(codes={"403": "forbidden", "404": "not-found"}),
)
async def status(job_id: JobId, request: Request, principal: Current) -> JobOut:
    state = request.app.state
    async with acquire(state.db, state.settings.db_pool_acquire_timeout_s) as conn:
        row = await read_job(conn, EventActor(principal.user_id, principal.role), job_id)
    return job_out(row, state.settings.import_max_rows)


async def _updates(
    request: Request,
    job_id: UUID,
    initial: JobOut,
    signals: PubSub | None,
) -> AsyncIterator[str]:
    previous = initial
    yield f"event: progress\ndata: {previous.model_dump_json()}\n\n"
    while previous.status in _ACTIVE:
        if signals is None:
            await asyncio.sleep(1)
        else:
            try:
                # Pub/Sub wakes the stream quickly. Polling also covers lost notifications.
                async with asyncio.timeout(1):
                    await signals.get_message(ignore_subscribe_messages=True, timeout=1)
            except TimeoutError:
                pass
            except RedisError:
                signals = None
        principal = await resolve_principal(request)
        if principal is None or principal.role not in {"admin", "user"}:
            return
        try:
            current = await status(job_id, request, principal)
        except ProblemError:
            return
        if current != previous:
            yield f"event: progress\ndata: {current.model_dump_json()}\n\n"
        else:
            yield ": keep-alive\n\n"
        previous = current


async def _progress_stream(
    request: Request,
    job_id: UUID,
    initial: JobOut,
) -> AsyncIterator[str]:
    if initial.status not in _ACTIVE:
        yield f"event: progress\ndata: {initial.model_dump_json()}\n\n"
        return
    async with request.app.state.redis.pubsub() as subscription:
        signals: PubSub | None = subscription
        try:
            async with asyncio.timeout(0.25):
                await subscription.subscribe(f"seat-import:{job_id}")
        except (RedisError, TimeoutError):
            signals = None
        async for frame in _updates(request, job_id, initial, signals):
            yield frame


@router.get(
    "/seat-imports/{job_id}/stream",
    policy=WRITE,
    response_model=None,
    response_class=StreamingResponse,
    summary="Stream seat import progress",
    description=(
        "The parent event owner or an administrator may subscribe. Each connection sends the "
        "current job, then committed changes. It closes on completion or loss of access."
    ),
    responses={
        **problems(codes={"403": "forbidden", "404": "not-found"}),
        200: {
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
            "description": "Progress events contain the same job object as the status endpoint",
        },
    },
)
async def progress(job_id: JobId, request: Request, principal: Current) -> StreamingResponse:
    initial = await status(job_id, request, principal)
    return StreamingResponse(
        _progress_stream(request, job_id, initial),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/seat-imports/{job_id}/errors.csv",
    policy=WRITE,
    response_model=None,
    response_class=FileResponse,
    summary="Download seat import errors",
    description=(
        "Only the parent event owner or an administrator may download row errors. This idempotent "
        "read returns the current attempt's committed errors in physical line order. Formula- "
        "leading text is escaped."
    ),
    responses={
        **problems(codes={"403": "forbidden", "404": "not-found"}),
        200: {
            "content": {"text/csv": {"schema": {"type": "string", "format": "binary"}}},
            "description": "CSV attachment",
        },
    },
)
async def errors(job_id: JobId, request: Request, principal: Current) -> FileResponse:
    state = request.app.state
    path: Path | None = None
    try:
        async with (
            acquire(state.db, state.settings.db_pool_acquire_timeout_s) as conn,
            event_transaction(conn, isolation="repeatable_read", readonly=True),
        ):
            job = await read_job(conn, EventActor(principal.user_id, principal.role), job_id)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="", suffix=".csv", delete=False
            ) as output:
                path = Path(output.name)
                writer = csv.writer(output)
                writer.writerow(_CSV)
                async for row in conn.cursor(
                    """SELECT line, item_index, zone_code, row_label, seat_number, reason
                       FROM import_errors WHERE job_id = $1 AND attempt_no = $2
                       ORDER BY line, item_index""",
                    job_id,
                    job["attempt_no"],
                    prefetch=1000,
                ):
                    writer.writerow(safe_cell(row[key]) for key in _CSV)
        return FileResponse(
            path,
            filename=f"{job_id}-errors.csv",
            headers={"Cache-Control": "private, no-store"},
            media_type="text/csv",
            background=BackgroundTask(path.unlink, missing_ok=True),
        )
    except BaseException:
        if path is not None:
            path.unlink(missing_ok=True)
        raise
