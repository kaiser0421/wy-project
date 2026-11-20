"""Sales report generation isolated from the request and booking pool."""

import asyncio
import shutil
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from threading import Event
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from anyio import CancelScope, to_thread
from fastapi import FastAPI, Query, Request
from starlette.background import BackgroundTask
from starlette.responses import FileResponse
from starlette.types import Receive, Scope, Send

from ticketing.api.routes.events import READ, Current, EventId, problems
from ticketing.api.security import SecureRouter
from ticketing.core.errors import ProblemError
from ticketing.core.logging import get_logger
from ticketing.events.access import EventActor, can_view
from ticketing.reports.sales import (
    MIME,
    ExportCancelled,
    generate,
    prune_stale_exports,
    report_busy,
)

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sales-report")
_slot = asyncio.Semaphore(1)
log = get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        await to_thread.run_sync(prune_stale_exports, Path(app.state.settings.export_dir))
    except OSError as exc:
        log.warning("report.cleanup_failed", error=type(exc).__name__)
    yield


router = SecureRouter(prefix="/api/v1/events", tags=["reports"], lifespan=_lifespan)


def _clean(path: Path) -> None:
    shutil.rmtree(path.parent, ignore_errors=True)


class ReportResponse(FileResponse):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        except BaseException:
            # FileResponse runs its background task on success. A failed send also owns cleanup.
            if self.background is not None:
                with CancelScope(shield=True):
                    await self.background()
            raise


async def _authorize(request: Request, actor: EventActor, event_id: int) -> None:
    """Reject forbidden or unknown events before taking an export slot.

    The same rule is enforced again inside the workbook snapshot (reports.sales._event).
    """
    row = await request.app.state.db.fetchrow(
        "SELECT owner_id, status::text AS status FROM events WHERE id = $1", event_id
    )
    if row is None or (
        actor.role != "viewer" and not can_view(actor, row["owner_id"], row["status"])
    ):
        raise ProblemError(404, "not-found", "Event not found")
    if actor.role == "user" and row["owner_id"] != actor.user_id:
        raise ProblemError(403, "forbidden", "Users may only export reports for their own events")


async def _export(request: Request, actor: EventActor, event_id: int, tz: ZoneInfo) -> Path:
    if _slot.locked():
        raise report_busy()
    await _slot.acquire()
    stop = Event()
    loop = asyncio.get_running_loop()
    work = loop.run_in_executor(
        _executor, partial(generate, request.app.state.settings, actor, event_id, tz, stop)
    )

    async def watch_disconnect() -> None:
        while not stop.is_set():
            if (await request.receive())["type"] == "http.disconnect":
                stop.set()
                break

    watcher = asyncio.create_task(watch_disconnect())
    claimed = False

    def finish(result: asyncio.Future[Path]) -> None:
        if not claimed and not result.cancelled() and result.exception() is None:
            loop.run_in_executor(None, _clean, result.result())
        _slot.release()

    try:
        path = await asyncio.shield(work)
        if stop.is_set():
            raise ExportCancelled
        claimed = True
        return path
    except ExportCancelled as exc:
        raise ProblemError(499, "client-disconnected", "The client disconnected") from exc
    finally:
        stop.set()
        watcher.cancel()
        work.add_done_callback(finish)


@router.get(
    "/{event_id}/reports/sales.xlsx",
    policy=READ,
    response_model=None,
    response_class=FileResponse,
    summary="Download the event sales report",
    description=(
        "Administrators and viewers may export any event. Users may export only their own. Viewer "
        "reports pseudonymize buyers per event. The idempotent read generates a new workbook from "
        "one database snapshot. A concurrent export returns 429."
    ),
    responses={
        **problems(
            codes={
                "403": "forbidden",
                "404": "not-found",
                "422": "validation-failed, invalid-timezone, report-row-limit",
                "429": "report-busy",
                "499": "client-disconnected",
                "507": "export-space-exhausted",
            }
        ),
        200: {
            "content": {MIME: {"schema": {"type": "string", "format": "binary"}}},
            "description": "XLSX attachment with Summary, Zones and Orders sheets",
        },
    },
)
async def sales(
    event_id: EventId,
    request: Request,
    principal: Current,
    tz: Annotated[
        str,
        Query(
            max_length=100,
            description="IANA timezone for all worksheet times",
            examples=["Asia/Taipei"],
        ),
    ] = "Asia/Taipei",
) -> FileResponse:
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ProblemError(422, "invalid-timezone", "Select a valid IANA timezone") from exc
    actor = EventActor(principal.user_id, principal.role)
    await _authorize(request, actor, event_id)
    path = await _export(request, actor, event_id, zone)
    return ReportResponse(
        path,
        media_type=MIME,
        filename=f"event-{event_id}-sales.xlsx",
        background=BackgroundTask(_clean, path),
    )
