"""Liveness and readiness probes."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ticketing.core.schema import SCHEMA_HEAD

router = APIRouter(prefix="/healthz", tags=["health"])


@router.get("/live", summary="Liveness probe")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready", summary="Readiness probe (PostgreSQL, Redis, schema revision, draining)")
async def ready(request: Request) -> JSONResponse:
    state = request.app.state
    checks: dict[str, Any] = {}
    ok = True

    if getattr(state, "draining", False):
        checks["draining"] = True
        ok = False

    try:
        async with asyncio.timeout(1.0):
            version = await state.db.fetchval("SELECT version_num FROM alembic_version")
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
