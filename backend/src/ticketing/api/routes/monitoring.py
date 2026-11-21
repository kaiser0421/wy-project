"""Administrator monitoring and request trace lookup."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

import asyncpg
import orjson
from fastapi import Depends, Path, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import AwareDatetime, BaseModel, Field, JsonValue

from ticketing.api.security import (
    Principal,
    Roles,
    SecureRouter,
    current_principal,
    resolve_principal,
)
from ticketing.auth.sessions import lock_live_session
from ticketing.core.context import current_request_id
from ticketing.core.db import acquire
from ticketing.core.errors import PROBLEM_CONTENT_TYPE, ProblemError

router = SecureRouter(prefix="/api/v1/admin", tags=["monitoring"])
ADMIN = Roles("admin")
Source = Literal["edge", "app"]
AlertState = Literal["pending", "firing", "resolved"]
LogService = Literal["api", "worker", "relay", "collector", "paymock", "seed"]
LogLevel = Literal["warning", "error", "critical"]
EXAMPLE_TS = "2026-11-20T12:00:00Z"
EXAMPLE_ID = "657f152f5d414b80b54903dde00693a8"
TRACE_LIMIT = 1000
LINK_LIMIT = 100
REQUEST_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_REQUEST_ID = re.compile(REQUEST_ID_PATTERN)
_ERROR_TYPE = re.compile(r"[A-Za-z_][A-Za-z_0-9]{0,79}")


def _problem_responses(*, missing: bool = False, write: bool = False) -> dict[int, dict[str, Any]]:
    codes = {
        401: ["unauthenticated"],
        403: ["forbidden"],
        422: ["validation-failed"],
        503: ["server-busy"],
        500: ["internal-error"],
    }
    if write:
        codes[403] += ["csrf-failed", "origin-not-allowed"]
    if missing:
        codes[404] = ["not-found"]
    return {
        status: {
            "description": ", ".join(names),
            "content": {
                PROBLEM_CONTENT_TYPE: {
                    "examples": {
                        code: {
                            "value": {
                                "type": f"https://ticketing.example/problems/{code}",
                                "title": code.replace("-", " ").capitalize(),
                                "status": status,
                                "code": code,
                                "retryable": status == 503,
                            }
                        }
                        for code in names
                    }
                }
            },
        }
        for status, names in codes.items()
    }


class Metric(BaseModel):
    ts: datetime = Field(description="Start of the UTC second.", examples=[EXAMPLE_TS])
    source: Source = Field(description="Nginx edge or API producer.", examples=["edge"])
    scope: str = Field(description="All requests or a route template.", examples=["all"])
    req_total: int = Field(
        description="Requests completed during the second (RPS).", examples=[250]
    )
    status_classes: list[int] | None = Field(
        description=(
            "Indices 1 through 5 count HTTP status classes. Index 0 counts unclassified responses."
        ),
        examples=[[0, 0, 245, 1, 3, 1]],
    )
    err_4xx: int = Field(description="Number of 4xx responses.", examples=[3])
    err_5xx: int = Field(description="Number of 5xx responses.", examples=[1])
    error_rate: float | None = Field(
        description="Fraction of requests with 4xx or 5xx.", examples=[0.016]
    )
    p50_ms: float | None = Field(description="Median latency in milliseconds.", examples=[12.0])
    p95_ms: float | None = Field(
        description="95th percentile latency in milliseconds.", examples=[32.0]
    )
    p99_ms: float | None = Field(
        description="99th percentile latency in milliseconds.", examples=[80.0]
    )
    conns_active: int | None = Field(description="Nginx active HTTP connections.", examples=[120])
    conns_sse: int | None = Field(
        description="Sum of recent API producer SSE gauges.", examples=[55]
    )
    clock_skew_ms: float | None = Field(
        description="Largest absolute wall-clock difference among recent API producers.",
        examples=[20_000],
    )
    completeness: float | None = Field(
        description="Fraction of expected API producers that reported. Null when unknown.",
        examples=[0.5],
    )
    late_or_missing: int = Field(
        description="Expected API producers missing from this second.", examples=[1]
    )


class MetricSeries(BaseModel):
    items: list[Metric] = Field(description="Samples in chronological order.", examples=[[]])


class BusinessMetric(BaseModel):
    ts: datetime = Field(description="Start of the UTC second.", examples=[EXAMPLE_TS])
    event_id: int = Field(description="Event identifier.", examples=[12])
    hold_ok: int = Field(description="Successful seat holds.", examples=[40])
    hold_conflict: int = Field(description="Seat hold conflicts.", examples=[5])
    hold_soldout: int = Field(
        description="Seat holds rejected for insufficient inventory.", examples=[1]
    )
    hold_limit: int = Field(description="Seat holds rejected at the purchase limit.", examples=[2])
    pay_ok: int = Field(description="Successful payments.", examples=[36])
    pay_fail: int = Field(description="Failed payments.", examples=[3])
    cancels: int = Field(description="Orders cancelled.", examples=[1])
    expiries: int = Field(description="Orders expired.", examples=[2])


class MetricFrame(BaseModel):
    ts: datetime = Field(description="UTC second this frame represents.", examples=[EXAMPLE_TS])
    edge: Metric | None = Field(description="Nginx metrics for this second.", examples=[None])
    app: Metric | None = Field(description="API metrics for this second.", examples=[None])
    business: list[BusinessMetric] = Field(description="Business outcomes by event.", examples=[[]])
    complete: bool = Field(
        description="Whether all expected API producers reported.", examples=[True]
    )


class Alert(BaseModel):
    id: int = Field(description="Occurrence identifier.", examples=[17])
    rule_id: str = Field(description="Name of the alert rule.", examples=["SERVICE_DOWN"])
    scope: str = Field(description="Service, event ID, or global scope.", examples=["collector"])
    state: AlertState = Field(description="Current state of the occurrence.", examples=["firing"])
    value: float | None = Field(description="Measurement that triggered the rule.", examples=[0.04])
    threshold: float | None = Field(description="Rule threshold in the same unit.", examples=[0.02])
    ts: datetime = Field(
        description="Time the occurrence entered pending state.", examples=[EXAMPLE_TS]
    )
    updated_at: datetime = Field(
        description="Time of the last state transition.", examples=[EXAMPLE_TS]
    )
    fired_at: datetime | None = Field(
        description="Time the occurrence entered firing state.", examples=[EXAMPLE_TS]
    )
    ack_by: int | None = Field(description="ID of the first admin to acknowledge.", examples=[4])
    ack_at: datetime | None = Field(
        description="Time of the first acknowledgement.", examples=[EXAMPLE_TS]
    )


class AlertList(BaseModel):
    items: list[Alert] = Field(description="Occurrences, newest first.", examples=[[]])


class AccessLog(BaseModel):
    ts: datetime = Field(description="Nginx completion timestamp.", examples=[EXAMPLE_TS])
    request_id: str | None = Field(
        description="Request correlation identifier.", examples=[EXAMPLE_ID]
    )
    method: str | None = Field(description="HTTP method.", examples=["POST"])
    path: str | None = Field(description="Request path.", examples=["/api/v1/events/12/orders"])
    status: int | None = Field(description="HTTP response status.", examples=[201])
    req_ms: float | None = Field(description="End-to-end request duration in ms.", examples=[15.4])
    upstream_ms: float | None = Field(
        description="Final timed upstream attempt in ms.", examples=[14.0]
    )
    upstream: str | None = Field(description="Nginx upstream address list.", examples=["api:8000"])
    bytes: int | None = Field(description="Total bytes sent.", examples=[450])
    load_run: str | None = Field(description="Load test run label.", examples=["sale-1"])
    user_agent: str | None = Field(description="Client user-agent string.", examples=["curl/8"])
    ingest_lag_ms: float = Field(
        description="Collector time minus log timestamp, in ms.", examples=[438.5]
    )


class ServiceLog(BaseModel):
    ts: datetime = Field(description="Service wall-clock timestamp.", examples=[EXAMPLE_TS])
    request_id: str | None = Field(
        description="Request correlation identifier.", examples=[EXAMPLE_ID]
    )
    service: str = Field(description="Service name.", examples=["worker"])
    boot_uuid: str | None = Field(description="Writer process boot UUID.", examples=[EXAMPLE_ID])
    op_id: str | None = Field(
        description="Local operation identifier.", examples=["a58d388602d3bcaf"]
    )
    parent_op_id: str | None = Field(description="Parent operation identifier.", examples=[None])
    local_seq: int | None = Field(description="Process-local log sequence.", examples=[140])
    level: str | None = Field(description="Logging severity.", examples=["info"])
    event: str | None = Field(description="Structured event name.", examples=["payment.recorded"])
    attrs: dict[str, JsonValue] | None = Field(
        description="Event attributes.", examples=[{"order_id": 19}]
    )
    ingest_lag_ms: float = Field(
        description="Collector time minus writer timestamp, in ms.", examples=[512.0]
    )


class RequestLogs(BaseModel):
    request_id: str = Field(description="Request being traced.", examples=[EXAMPLE_ID])
    access_log: AccessLog | None = Field(
        description="Latest matching nginx record.", examples=[None]
    )
    service_logs: list[ServiceLog] = Field(
        description="Trace entries by timestamp and sequence.", examples=[[]]
    )
    linked_request_ids: list[str] = Field(
        description="Directly related request identifiers.", examples=[[]]
    )
    ingest_lag_ms: float | None = Field(
        description="Maximum ingestion lag across returned log entries.", examples=[512.0]
    )
    truncated: bool = Field(
        description="Whether the trace exceeded the entry or link limit.", examples=[False]
    )


class RecentLog(BaseModel):
    ts: datetime = Field(description="Service wall-clock timestamp.", examples=[EXAMPLE_TS])
    service: str = Field(description="Service name.", examples=["worker"])
    level: LogLevel = Field(description="Logging severity.", examples=["warning"])
    event: str | None = Field(
        description="Structured event name.", examples=["request.db_overload"]
    )
    request_id: str | None = Field(
        description="Request correlation identifier.", examples=[EXAMPLE_ID]
    )
    attrs: dict[str, str | int] = Field(
        description="Bounded diagnostic metadata: exception class and HTTP status when present.",
        examples=[{"error_type": "TimeoutError", "status": 503}],
    )


class RecentLogs(BaseModel):
    items: list[RecentLog] = Field(description="Service log entries, newest first.", examples=[[]])


_METRICS_HISTORY = """
SELECT ts, source, scope, req_total, err_4xx, err_5xx, status_classes,
       p50_ms, p95_ms, p99_ms, conns_active, conns_sse, clock_skew_ms, completeness,
       late_or_missing,
       err_5xx::double precision / nullif(req_total, 0) AS error_rate
FROM metrics_1s WHERE ts >= $1 AND ts < $2 AND ($3::text IS NULL OR source = $3)
  AND scope = $4
ORDER BY ts, source
"""
_LATEST_METRICS = """
SELECT ts, source, scope, req_total, err_4xx, err_5xx, status_classes,
       p50_ms, p95_ms, p99_ms, conns_active, conns_sse, clock_skew_ms, completeness,
       late_or_missing,
       err_5xx::double precision / nullif(req_total, 0) AS error_rate
FROM metrics_1s WHERE ts = $1 AND scope = 'all'
"""
_LATEST_EDGE = """
SELECT ts, source, scope, req_total, err_4xx, err_5xx, status_classes,
       p50_ms, p95_ms, p99_ms, conns_active, conns_sse, clock_skew_ms, completeness,
       late_or_missing,
       err_5xx::double precision / nullif(req_total, 0) AS error_rate
FROM metrics_1s
WHERE source = 'edge' AND scope = 'all' AND ts <= $1 AND ts > $1 - interval '10 seconds'
ORDER BY ts DESC LIMIT 1
"""

_ALERT_LIST = """
SELECT * FROM alert_events
WHERE ($1::text IS NULL OR state = $1) AND ($2::bigint IS NULL OR id < $2)
ORDER BY id DESC LIMIT $3
"""
_STREAM_ALERTS = """
SELECT * FROM alert_events
WHERE state <> 'resolved' OR updated_at >= clock_timestamp() - interval '5 minutes'
ORDER BY (state = 'resolved'), id DESC LIMIT 250
"""
_RECENT_LOGS = """
SELECT ts, service, level, event, request_id, attrs
FROM service_logs
WHERE ts >= $1 AND ts < $2 AND level IN ('warning', 'error', 'critical')
  AND ($3::text IS NULL OR service = $3) AND ($4::text IS NULL OR level = $4)
ORDER BY ts DESC LIMIT $5
"""
_ACCESS_LOG = """
SELECT *, extract(epoch FROM ingested_at - ts)::double precision * 1000 AS ingest_lag_ms
FROM access_logs WHERE request_id = $1 ORDER BY ts DESC LIMIT 1
"""
_SERVICE_LOGS = """
SELECT *, extract(epoch FROM ingested_at - ts)::double precision * 1000 AS ingest_lag_ms
FROM service_logs WHERE request_id = ANY($1::text[])
ORDER BY ts, local_seq NULLS LAST, service, boot_uuid, op_id LIMIT $2
"""
_REVERSE_LINKS = """
SELECT DISTINCT request_id FROM service_logs
WHERE request_id IS NOT NULL AND request_id <> $1
  AND (attrs->'links' ? $1 OR attrs->>'origin_request_id' = $1)
ORDER BY request_id LIMIT $2
"""


@router.get(
    "/metrics",
    policy=ADMIN,
    response_model=MetricSeries,
    summary="Read per-second metrics",
    description=(
        "Admins can read a UTC interval with an inclusive start and exclusive end, up to one hour "
        "per call. Each read is a snapshot. Late edge logs may revise recent samples."
    ),
    responses=_problem_responses(),
)
async def metrics(
    request: Request,
    response: Response,
    from_ts: Annotated[
        AwareDatetime,
        Query(alias="from", description="Inclusive UTC start.", examples=[EXAMPLE_TS]),
    ],
    to_ts: Annotated[
        AwareDatetime,
        Query(alias="to", description="Exclusive UTC end.", examples=["2026-11-20T12:05:00Z"]),
    ],
    source: Annotated[
        Source | None, Query(description="Select one metrics source.", examples=["edge"])
    ] = None,
    scope: Annotated[
        str,
        Query(
            max_length=512,
            pattern=r"^[^\x00]+$",
            description="Metrics scope or route template.",
            examples=["all"],
        ),
    ] = "all",
) -> MetricSeries:
    if not timedelta(0) < to_ts - from_ts <= timedelta(hours=1):
        raise ProblemError(
            422, "validation-failed", "Invalid metrics interval", "Select up to one hour."
        )
    state = request.app.state
    async with acquire(state.db, state.settings.db_pool_acquire_timeout_s) as conn:
        rows = await conn.fetch(_METRICS_HISTORY, from_ts, to_ts, source, scope)
    response.headers["Cache-Control"] = "private, no-store"
    return MetricSeries(items=[Metric.model_validate(dict(row)) for row in rows])


class EventStream(StreamingResponse):
    media_type = "text/event-stream"


def _stream_docs(event: str, model: type[BaseModel]) -> dict[int, dict[str, Any]]:
    return {
        **_problem_responses(),
        200: {
            "description": f"SSE frames with event {event} and a JSON data object.",
            "content": {
                "text/event-stream": {
                    "schema": {"type": "string"},
                    "x-event-data-schema": {"$ref": f"#/components/schemas/{model.__name__}"},
                },
            },
        },
    }


async def _stream(
    request: Request, event: str, load: Callable[[Request], Awaitable[BaseModel]]
) -> AsyncIterator[str]:
    previous: str | None = None
    loop = asyncio.get_running_loop()
    deadline = loop.time()
    while not getattr(request.app.state, "draining", False):
        try:
            principal = await resolve_principal(request)
            if principal is None or not principal.is_admin:
                return
            data = (await load(request)).model_dump_json()
        except ProblemError as problem:
            body = problem.to_body(instance=request.url.path, request_id=current_request_id())
            yield f"event: problem\ndata: {orjson.dumps(body).decode()}\n\n"
            return
        if event == "metrics" or data != previous:
            yield f"event: {event}\ndata: {data}\n\n"
            previous = data
        else:
            yield ": keepalive\n\n"
        # Slow clients skip snapshots to keep the next frame on the live cadence.
        deadline += max(1, int(loop.time() - deadline) + 1)
        await asyncio.sleep(max(0.0, deadline - loop.time()))


def _event_stream(
    request: Request, event: str, load: Callable[[Request], Awaitable[BaseModel]]
) -> EventStream:
    return EventStream(
        _stream(request, event, load),
        headers={"Cache-Control": "private, no-store", "X-Accel-Buffering": "no"},
    )


async def _latest_metrics(request: Request) -> MetricFrame:
    state = request.app.state
    async with (
        acquire(state.db, state.settings.db_pool_acquire_timeout_s) as conn,
        conn.transaction(isolation="repeatable_read", readonly=True),
    ):
        ts = await conn.fetchval(
            "SELECT date_trunc('second', clock_timestamp()) - interval '1 second'"
        )
        rows = list(await conn.fetch(_LATEST_METRICS, ts))
        if not any(row["source"] == "edge" for row in rows):
            # Edge seconds finalize after nginx log ingestion plus a grace period, so the newest
            # second usually has no edge row yet; show the latest finalized edge second instead.
            edge = await conn.fetchrow(_LATEST_EDGE, ts)
            if edge is not None:
                rows.append(edge)
        business = await conn.fetch("SELECT * FROM business_1s WHERE ts = $1 ORDER BY event_id", ts)
    by_source = {row["source"]: Metric.model_validate(dict(row)) for row in rows}
    app = by_source.get("app")
    return MetricFrame(
        ts=ts,
        edge=by_source.get("edge"),
        app=app,
        business=[BusinessMetric.model_validate(dict(row)) for row in business],
        complete=app is not None and app.completeness == 1.0,
    )


@router.get(
    "/metrics/stream",
    policy=ADMIN,
    response_model=MetricFrame,
    response_class=EventStream,
    summary="Stream metrics once per second",
    description=(
        "Admins receive the latest completed second with edge, app and per-event business metrics. "
        "Each frame is a consistent database snapshot. Reconnecting resumes at the latest second. "
        "The stream checks the session every second."
    ),
    responses=_stream_docs("metrics", MetricFrame),
)
async def metrics_stream(request: Request) -> EventStream:
    return _event_stream(request, "metrics", _latest_metrics)


@router.get(
    "/alerts",
    policy=ADMIN,
    response_model=AlertList,
    summary="List alert occurrences",
    description=(
        "Admins can read a page of alerts by state. Reads are repeatable for the same database "
        "state. Use before_id to page through older occurrences while new alerts arrive."
    ),
    responses=_problem_responses(),
)
async def list_alerts(
    request: Request,
    state_filter: Annotated[
        AlertState | None, Query(alias="state", description="Alert state.", examples=["firing"])
    ] = None,
    before_id: Annotated[
        int | None, Query(gt=0, le=2**63 - 1, description="Exclusive occurrence ID.", examples=[47])
    ] = None,
    limit: Annotated[
        int, Query(ge=1, le=250, description="Maximum occurrences.", examples=[100])
    ] = 100,
) -> AlertList:
    state = request.app.state
    async with acquire(state.db, state.settings.db_pool_acquire_timeout_s) as conn:
        rows = await conn.fetch(_ALERT_LIST, state_filter, before_id, limit)
    return AlertList(items=[Alert.model_validate(dict(row)) for row in rows])


async def _latest_alerts(request: Request) -> AlertList:
    state = request.app.state
    async with acquire(state.db, state.settings.db_pool_acquire_timeout_s) as conn:
        rows = await conn.fetch(_STREAM_ALERTS)
    return AlertList(items=[Alert.model_validate(dict(row)) for row in rows])


@router.get(
    "/alerts/stream",
    policy=ADMIN,
    response_model=AlertList,
    response_class=EventStream,
    summary="Stream alert snapshots",
    description=(
        "Admins receive the latest active and recently resolved alert occurrences, up to 250. "
        "The stream reads durable state each second and works across replicas and Redis outages. "
        "A reconnect starts with the current snapshot. The session is checked every second."
    ),
    responses=_stream_docs("alerts", AlertList),
)
async def alerts_stream(request: Request) -> EventStream:
    return _event_stream(request, "alerts", _latest_alerts)


@router.post(
    "/alerts/{id}/ack",
    policy=ADMIN,
    response_model=Alert,
    summary="Acknowledge an alert",
    description=(
        "Admins with a valid CSRF token can acknowledge an alert. Concurrent or repeated "
        "acknowledgements return the first admin and timestamp. Acknowledgement records receipt."
    ),
    responses=_problem_responses(missing=True, write=True),
)
async def ack_alert(
    id: Annotated[int, Path(gt=0, le=2**63 - 1, description="Alert occurrence ID.", examples=[17])],
    request: Request,
    principal: Annotated[Principal, Depends(current_principal)],
) -> Alert:
    state = request.app.state
    async with (
        acquire(state.db, state.settings.db_pool_acquire_timeout_s) as conn,
        conn.transaction(),
    ):
        if not await lock_live_session(conn, principal.session_id, principal.user_id):
            raise ProblemError(
                401, "unauthenticated", "Authentication required", "Your session ended."
            )
        role = await conn.fetchval("SELECT role FROM users WHERE id = $1", principal.user_id)
        if role != "admin":
            raise ProblemError(403, "forbidden", "Forbidden", "Admin access is required.")
        row = await conn.fetchrow(
            """UPDATE alert_events SET ack_by = $2, ack_at = app_now()
               WHERE id = $1 AND ack_at IS NULL RETURNING *""",
            id,
            principal.user_id,
        )
        if row is not None:
            await conn.execute(
                """INSERT INTO audit_log (actor_id, action, target_type, target_id, request_id)
                   VALUES ($1, 'alert.ack', 'alert', $2, $3)""",
                principal.user_id,
                str(id),
                current_request_id(),
            )
        else:
            row = await conn.fetchrow("SELECT * FROM alert_events WHERE id = $1", id)
        if row is None:
            raise ProblemError(404, "not-found", "Not found", "Alert occurrence not found.")
    return Alert.model_validate(dict(row))


def _recent_log(row: asyncpg.Record) -> RecentLog:
    # The trace receives producer-redacted attrs. Keep only bounded structured metadata here:
    # raw paths, free-form details, and nested payloads can contain credentials or user input.
    raw = orjson.loads(row["attrs"]) if row["attrs"] else None
    attrs: dict[str, str | int] = {}
    if isinstance(raw, dict):
        error_type = raw.get("error_type")
        if isinstance(error_type, str) and _ERROR_TYPE.fullmatch(error_type):
            attrs["error_type"] = error_type
        status = raw.get("status")
        if type(status) is int and 100 <= status <= 599:
            attrs["status"] = status
    return RecentLog.model_validate({**dict(row), "attrs": attrs})


@router.get(
    "/logs/recent",
    policy=ADMIN,
    response_model=RecentLogs,
    summary="List recent service warnings and errors",
    description=(
        "Admins can read warning, error, and critical entries in a rolling window of up to one "
        "hour, newest first. Filter by service or exact severity. Only bounded diagnostic "
        "attributes are returned."
    ),
    responses=_problem_responses(),
)
async def recent_logs(
    request: Request,
    response: Response,
    service: Annotated[LogService | None, Query(description="Service to include.")] = None,
    level: Annotated[LogLevel | None, Query(description="Exact severity to include.")] = None,
    since_minutes: Annotated[int, Query(ge=1, le=60, description="Window in minutes.")] = 15,
    limit: Annotated[int, Query(ge=1, le=200, description="Maximum entries.")] = 50,
) -> RecentLogs:
    now = datetime.now(UTC)
    state = request.app.state
    async with acquire(state.db, state.settings.db_pool_acquire_timeout_s) as conn:
        rows = await conn.fetch(
            _RECENT_LOGS, now - timedelta(minutes=since_minutes), now, service, level, limit
        )
    response.headers["Cache-Control"] = "private, no-store"
    return RecentLogs(items=[_recent_log(row) for row in rows])


def _service_log(row: asyncpg.Record) -> ServiceLog:
    values = dict(row)
    values["boot_uuid"] = str(row["boot_uuid"]) if row["boot_uuid"] else None
    values["attrs"] = orjson.loads(row["attrs"]) if row["attrs"] else None
    return ServiceLog.model_validate(values)


def _outgoing_links(logs: list[ServiceLog]) -> set[str]:
    result: set[str] = set()
    for entry in logs:
        attrs = entry.attrs or {}
        origin = attrs.get("origin_request_id")
        links = attrs.get("links")
        if isinstance(origin, str) and _REQUEST_ID.fullmatch(origin):
            result.add(origin)
        if isinstance(links, list):
            result.update(
                link for link in links if isinstance(link, str) and _REQUEST_ID.fullmatch(link)
            )
    return result


@router.get(
    "/requests/{request_id}/logs",
    policy=ADMIN,
    response_model=RequestLogs,
    summary="Trace a request across services",
    description=(
        "Admins can read a consistent snapshot of the access log and up to 1,000 service entries, "
        "ordered by timestamp then process sequence. Including linked requests follows one hop in "
        "both directions. Later reads may include entries still being ingested."
    ),
    responses=_problem_responses(missing=True),
)
async def request_logs(
    request_id: Annotated[
        str,
        Path(
            pattern=REQUEST_ID_PATTERN,
            description="Request correlation ID.",
            examples=[EXAMPLE_ID],
        ),
    ],
    request: Request,
    response: Response,
    include_linked: Annotated[
        bool,
        Query(description="Include service logs for directly linked requests.", examples=[True]),
    ] = True,
) -> RequestLogs:
    state = request.app.state
    async with (
        acquire(state.db, state.settings.db_pool_acquire_timeout_s) as conn,
        conn.transaction(isolation="repeatable_read", readonly=True),
    ):
        access = await conn.fetchrow(_ACCESS_LOG, request_id)
        primary = await conn.fetch(_SERVICE_LOGS, [request_id], TRACE_LIMIT + 1)
        reverse = await conn.fetch(_REVERSE_LINKS, request_id, LINK_LIMIT + 1)
        linked = _outgoing_links([_service_log(row) for row in primary])
        linked.update(row["request_id"] for row in reverse)
        linked.discard(request_id)
        linked_ids = sorted(linked)[:LINK_LIMIT]
        rows = (
            await conn.fetch(_SERVICE_LOGS, [request_id, *linked_ids], TRACE_LIMIT + 1)
            if include_linked and linked_ids
            else primary
        )
    if access is None and not rows:
        raise ProblemError(404, "not-found", "Not found", "No logs found for this request.")
    access_log = AccessLog.model_validate(dict(access)) if access else None
    service_logs = [_service_log(row) for row in rows[:TRACE_LIMIT]]
    lags = [entry.ingest_lag_ms for entry in service_logs]
    if access_log:
        lags.append(access_log.ingest_lag_ms)
    response.headers["Cache-Control"] = "private, no-store"
    return RequestLogs(
        request_id=request_id,
        access_log=access_log,
        service_logs=service_logs,
        linked_request_ids=linked_ids,
        ingest_lag_ms=max(lags, default=None),
        truncated=len(rows) > TRACE_LIMIT or len(linked) > LINK_LIMIT,
    )
