"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.types import ASGIApp

from ticketing.api import health
from ticketing.api.middleware import RequestContextMiddleware
from ticketing.api.problems import install_problem_handlers
from ticketing.core.db import create_pool
from ticketing.core.http import create_http_client
from ticketing.core.logging import configure_logging, get_logger
from ticketing.core.redis import create_redis
from ticketing.core.settings import Settings, get_settings

log = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.draining = False
        app.state.db = await create_pool(settings)
        app.state.redis = create_redis(settings)
        app.state.http = create_http_client(settings.payment_provider_url)
        log.info("api.started", env=settings.app_env)
        try:
            yield
        finally:
            await app.state.http.aclose()
            await app.state.redis.aclose()
            await app.state.db.close()
            log.info("api.stopped")

    app = FastAPI(
        title="Ticketing Platform API",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    install_problem_handlers(app)
    app.include_router(health.router)
    return app


def create_asgi_app(settings: Settings | None = None) -> ASGIApp:
    """The deployable ASGI callable: FastAPI wrapped by the outermost request-context middleware."""
    settings = settings or get_settings()
    configure_logging("api", settings.log_level, settings.log_dir)
    fastapi_app = create_app(settings)
    wrapped = RequestContextMiddleware(fastapi_app)
    wrapped.fastapi = fastapi_app  # type: ignore[attr-defined]
    return wrapped
