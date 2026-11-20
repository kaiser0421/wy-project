"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from starlette.types import ASGIApp

from ticketing.api import health
from ticketing.api.middleware import RequestContextMiddleware
from ticketing.api.problems import install_problem_handlers
from ticketing.api.routes import (
    admin_users,
    auth,
    events,
    imports,
    monitoring,
    orders,
    payments,
    reports,
    seats,
)
from ticketing.auth.sessions import LastSeenRefresher
from ticketing.core.db import create_pool
from ticketing.core.http import create_http_client
from ticketing.core.logging import configure_logging, get_logger
from ticketing.core.passwords import PasswordHasher
from ticketing.core.ratelimit import RateLimiter
from ticketing.core.redis import create_redis
from ticketing.core.settings import Settings, get_settings
from ticketing.realtime import api_setup as realtime_setup
from ticketing.telemetry import api_setup as telemetry_setup

log = get_logger(__name__)

API_DESCRIPTION = """
High-concurrency ticketing platform API.

**Authentication** — `POST /api/v1/auth/login` sets an HttpOnly session cookie (`sid` on plain
HTTP, `__Host-sid` with `COOKIE_SECURE=true`) and returns a `csrf_token`. Send it as the
`X-CSRF-Token` header on every POST/PUT/PATCH/DELETE. `GET /api/v1/auth/me` returns it again.

**Errors** — every error is `application/problem+json` (RFC 9457) with a stable `code`, a
`retryable` flag and the `request_id` to quote when reporting problems.
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.draining = False
        app.state.db = await create_pool(settings)
        app.state.redis = create_redis(settings)
        app.state.http = create_http_client(settings.payment_provider_url)
        app.state.passwords = PasswordHasher(settings)
        app.state.rate_limiter = RateLimiter(app.state.redis)
        app.state.last_seen = LastSeenRefresher(app.state.db, settings)
        app.state.last_seen.start()
        log.info("api.started", env=settings.app_env)
        try:
            yield
        finally:
            await app.state.last_seen.stop()
            app.state.passwords.close()
            await app.state.http.aclose()
            await app.state.redis.aclose()
            await app.state.db.close()
            log.info("api.stopped")

    app = FastAPI(
        title="Ticketing Platform API",
        version="0.1.0",
        description=API_DESCRIPTION,
        docs_url="/api/docs",
        swagger_ui_oauth2_redirect_url="/api/docs/oauth2-redirect",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    install_problem_handlers(app)
    for module in (
        health,
        auth,
        admin_users,
        events,
        imports,
        reports,
        orders,
        payments,
        seats,
        monitoring,
    ):
        app.include_router(module.router)
    telemetry_setup.install(app)
    realtime_setup.install(app)

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title, version=app.version, description=app.description, routes=app.routes
        )
        schema.setdefault("components", {})["securitySchemes"] = {
            "sessionCookie": {"type": "apiKey", "in": "cookie", "name": "sid"},
            "csrfHeader": {"type": "apiKey", "in": "header", "name": "X-CSRF-Token"},
        }
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
    return app


def create_asgi_app(settings: Settings | None = None) -> ASGIApp:
    """The deployable ASGI callable: FastAPI wrapped by the outermost request-context middleware."""
    settings = settings or get_settings()
    configure_logging("api", settings.log_level, settings.log_dir)
    fastapi_app = create_app(settings)
    wrapped = RequestContextMiddleware(fastapi_app)
    wrapped.fastapi = fastapi_app  # type: ignore[attr-defined]
    return wrapped
