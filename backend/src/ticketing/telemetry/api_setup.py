"""Raw ASGI measurements and lifespan-managed producer and watchdog."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast

import orjson
from fastapi import FastAPI
from redis.asyncio import Redis
from starlette.datastructures import State
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ticketing.core.context import BOOT_UUID
from ticketing.core.db import acquire
from ticketing.core.logging import get_logger
from ticketing.telemetry import business
from ticketing.telemetry.alerts import collector_watchdog
from ticketing.telemetry.common import STREAM, is_stream
from ticketing.telemetry.histogram import Counts

log = get_logger(__name__)
STREAM_LIMIT = 100_000


class RequestMetrics:
    def __init__(self, app: ASGIApp, routes: dict[str, Counts]) -> None:
        self.app = app
        self.routes = routes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or is_stream(scope["path"]):
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        status = 500

        async def observe_start(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, observe_start)
        finally:
            route = getattr(scope.get("route"), "path", "<unmatched>")
            if route not in self.routes:
                self.routes[route] = Counts()
            self.routes[route].observe(status, (time.monotonic() - started) * 1000)


async def _register(state: State) -> None:
    async with acquire(state.db, state.settings.db_pool_acquire_timeout_s) as conn:
        await conn.execute(
            """INSERT INTO telemetry_producers (boot_uuid, service, heartbeat_at)
               VALUES ($1, 'api', clock_timestamp())
               ON CONFLICT (boot_uuid) DO UPDATE SET heartbeat_at = EXCLUDED.heartbeat_at""",
            BOOT_UUID,
        )


async def _heartbeat(state: State) -> None:
    while True:
        try:
            await _register(state)
        except Exception as exc:
            log.warning("telemetry.registry_failed", error_type=type(exc).__name__)
        await asyncio.sleep(1)


async def _flush_loop(state: State, routes: dict[str, Counts]) -> None:
    redis = cast(Redis, state.redis)
    deadline = time.monotonic() + 1
    interval_no = 0
    dropped = 0
    while True:
        await asyncio.sleep(max(0, deadline - time.monotonic()))
        now = time.monotonic()
        elapsed = max(1, int(now - deadline) + 1)
        deadline += elapsed
        interval_no += elapsed
        route_payload = {
            route: [counts.status_classes, counts.hist] for route, counts in routes.items()
        }
        routes.clear()
        gauge = getattr(state, "sse_active", None)
        outcomes = business.drain()
        try:
            async with asyncio.timeout(0.4):
                await redis.xadd(
                    STREAM,
                    {
                        "boot_uuid": BOOT_UUID,
                        "interval_no": interval_no,
                        "mono_ms": int(now * 1000),
                        "wall_ts": datetime.now(UTC).isoformat(),
                        "payload": orjson.dumps(
                            {
                                "routes": route_payload,
                                "business": outcomes,
                                "sse_active": gauge if isinstance(gauge, int) else None,
                            }
                        ),
                    },
                    maxlen=STREAM_LIMIT,
                    approximate=True,
                )
        except Exception as exc:
            dropped += 1
            log.warning("telemetry.flush_dropped", count=dropped, error_type=type(exc).__name__)


def install(app: FastAPI) -> None:
    routes: dict[str, Counts] = {}
    app.add_middleware(RequestMetrics, routes=routes)
    upstream = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        async with upstream(application):
            tasks = [
                asyncio.create_task(_heartbeat(application.state)),
                asyncio.create_task(_flush_loop(application.state, routes)),
                asyncio.create_task(collector_watchdog(application.state)),
            ]
            try:
                yield
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    app.router.lifespan_context = lifespan
