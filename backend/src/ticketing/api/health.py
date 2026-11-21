"""Liveness and readiness probes."""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ticketing.api.security import Public, SecureRouter
from ticketing.core.db import acquire
from ticketing.core.schema import SCHEMA_HEAD

router = SecureRouter(prefix="/healthz", tags=["health"])


class LiveResult(BaseModel):
    status: Literal["ok"] = Field(default="ok", description="API is running.", examples=["ok"])


class ReadyResult(BaseModel):
    status: Literal["ready", "not-ready"] = Field(
        description="Machine-readable readiness result.", examples=["ready"]
    )
    checks: dict[str, str | bool | None] = Field(
        description="Dependency check outcomes, deployed schema revision and any drain flag.",
        examples=[{"postgres": "ok", "schema": SCHEMA_HEAD, "redis": "ok"}],
    )


@router.get(
    "/live",
    policy=Public("health"),
    summary="Liveness probe",
    description="Anyone can safely retry this process liveness check.",
    response_model=LiveResult,
    responses={},
)
async def live() -> LiveResult:
    return LiveResult()


@router.get(
    "/ready",
    policy=Public("health"),
    summary="Readiness probe",
    description=(
        "Anyone on the service network can safely retry this probe of PostgreSQL, Redis, the "
        "schema revision and drain status. Each check has a bounded wait."
    ),
    response_model=ReadyResult,
    responses={503: {"model": ReadyResult, "description": "not-ready"}},
)
async def ready(request: Request) -> JSONResponse:
    state = request.app.state
    checks: dict[str, str | bool | None] = {}
    ok = True

    if getattr(state, "draining", False):
        checks["draining"] = True
        ok = False

    try:
        async with asyncio.timeout(1.0):
            async with acquire(state.db, state.settings.db_pool_acquire_timeout_s) as conn:
                version = await conn.fetchval("SELECT version_num FROM alembic_version")
        checks["postgres"] = "ok"
        checks["schema"] = version
        if version != SCHEMA_HEAD:
            ok = False
    except Exception as exc:
        checks["postgres"] = f"error: {type(exc).__name__}"
        ok = False

    try:
        async with asyncio.timeout(1.0):
            await state.redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {type(exc).__name__}"
        ok = False

    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ready" if ok else "not-ready", "checks": checks},
    )
