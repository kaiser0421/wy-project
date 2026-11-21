"""Install per-process real-time services into the FastAPI lifespan."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from ticketing.core.db import create_pool
from ticketing.core.settings import Settings
from ticketing.realtime import revocation
from ticketing.realtime.reader import Readers
from ticketing.realtime.snapshot import SnapshotCache


def install(app: FastAPI) -> None:
    previous = app.router.lifespan_context
    app.state.sse_active_connections = lambda: 0

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[Mapping[str, Any]]:
        async with previous(app) as state:
            settings: Settings = app.state.settings
            timeout = settings.db_pool_acquire_timeout_s
            readers = Readers(app.state.db, timeout)
            snapshots = SnapshotCache(app.state.db, timeout)
            check_settings = settings.model_copy(
                update={"db_statement_timeout_ms": revocation.RECHECK_QUERY_TIMEOUT_MS}
            )
            check_pool = await create_pool(
                check_settings, min_size=2, max_size=2, application_name="api-sse-recheck"
            )
            revocations = revocation.Revocations(check_pool, app.state.redis)
            app.state.seat_readers = readers
            app.state.seat_snapshots = snapshots
            app.state.seat_revocations = revocations
            app.state.sse_active_connections = lambda: revocations.active
            try:
                yield state if state is not None else {}
            finally:
                await revocations.close()
                await readers.close()
                await snapshots.close()
                await check_pool.close()

    app.router.lifespan_context = lifespan
