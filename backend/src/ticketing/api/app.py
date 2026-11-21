"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
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
from ticketing.api.security import ALL_ROLES, UNSAFE_METHODS, Policy
from ticketing.auth.sessions import LastSeenRefresher
from ticketing.core.db import create_pool
from ticketing.core.errors import PROBLEM_CONTENT_TYPE
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

**Authentication** — Call `POST /api/v1/auth/login` from an allowed Origin or Referer. Registration
also requires this. The browser sends the HttpOnly session cookie automatically on same-origin
requests. Call `GET /api/v1/auth/me` to obtain the session's `csrf_token`, then enter that token
as `X-CSRF-Token` when authorizing Swagger UI. Try a write such as `POST /api/v1/auth/logout`.
The payment provider callback authenticates separately using the signed `Mockpay-Signature` header.

**Errors** — API problems use `application/problem+json` (RFC 9457) with a stable `code`, a
`retryable` flag and the `request_id` to quote when reporting problems. The readiness probe
reports dependency checks in its own diagnostic response.
"""

_PROBLEM_SCHEMA = {
    "type": "object",
    "required": ["type", "title", "status", "code", "retryable"],
    "properties": {
        "type": {
            "type": "string",
            "format": "uri",
            "description": "URI identifying the problem type.",
            "examples": ["https://ticketing.example/problems/validation-failed"],
        },
        "title": {
            "type": "string",
            "description": "Short summary of this problem type.",
            "examples": ["Request validation failed"],
        },
        "status": {"type": "integer", "description": "HTTP response status.", "examples": [422]},
        "code": {
            "type": "string",
            "description": "Stable machine-readable identifier.",
            "examples": ["validation-failed"],
        },
        "retryable": {
            "type": "boolean",
            "description": "Whether repeating the same request may succeed.",
            "examples": [False],
        },
        "detail": {
            "type": "string",
            "description": "Additional information about this occurrence.",
            "examples": ["Select up to one hour."],
        },
        "instance": {
            "type": "string",
            "format": "uri-reference",
            "description": "Path of the request that failed.",
            "examples": ["/api/v1/auth/register"],
        },
        "request_id": {
            "type": "string",
            "description": "Correlation ID for looking up this request.",
            "examples": ["657f152f5d414b80b54903dde00693a8"],
        },
        "errors": {
            "type": "array",
            "description": "Individual request validation issues, when available.",
            "examples": [
                [{"loc": ["body", "email"], "msg": "Invalid email", "type": "value_error"}]
            ],
            "items": {
                "type": "object",
                "properties": {
                    "loc": {"type": "array", "items": {"type": ["string", "integer"]}},
                    "msg": {"type": "string"},
                    "type": {"type": "string"},
                },
            },
        },
    },
}

# These handlers predate the route-level problem declarations used by the other routers.
_ROUTE_DOCS: dict[object, tuple[str, dict[int, tuple[str, ...]]]] = {
    health.live: ("Public probe for process liveness. A read has no side effects.", {}),
    health.ready: (
        "Public probe for database, Redis and schema readiness. Each read checks current state.",
        {},
    ),
    auth.register: (
        "A caller from an allowed origin can create a User account. The email is unique. "
        "Repeating a successful registration returns a conflict. Requests are rate-limited.",
        {
            403: ("origin-not-allowed",),
            409: ("email-taken",),
            429: ("rate-limited",),
            503: ("server-busy",),
        },
    ),
    auth.login: (
        "A caller from an allowed origin can sign in with an active account. Each successful "
        "request creates a session and returns a CSRF token. Requests are rate-limited.",
        {
            401: ("invalid-credentials",),
            403: ("origin-not-allowed",),
            429: ("rate-limited",),
            503: ("server-busy",),
        },
    ),
    auth.logout: (
        "Revoke the current authenticated session. Revocation takes effect on the next "
        "request. Repeating the request with the revoked cookie returns 401.",
        {503: ("server-busy",)},
    ),
    auth.me: ("Read the authenticated user's current account and CSRF token.", {}),
    admin_users.list_users: (
        "Admins can list accounts in ascending ID order. Each page uses the last ID as its "
        "cursor, so inserts do not shift accounts already returned.",
        {},
    ),
    admin_users.get_user: (
        "Admins can read an account and its current ETag.",
        {404: ("not-found",)},
    ),
    admin_users.patch_user: (
        "Admins can update an account's role or active state using its ETag. Concurrent writes "
        "serialize and a stale ETag returns 412. Changes revoke the account's sessions.",
        {
            404: ("not-found",),
            409: ("self-modification", "last-admin"),
            412: ("precondition-failed",),
            428: ("precondition-required",),
            503: ("server-busy",),
        },
    ),
}


_EXAMPLE_ACCOUNT = {"id": 42, "email": "buyer@example.com", "role": "user"}
_ACCOUNT_FIELDS: dict[str, tuple[str, object]] = {
    "id": ("Account ID.", 42),
    "email": ("Account's normalized email address.", "buyer@example.com"),
    "role": ("Role governing the account's permissions.", "user"),
}
_MODEL_FIELDS: dict[str, dict[str, tuple[str, object]]] = {
    "Credentials": {
        "email": _ACCOUNT_FIELDS["email"],
        "password": ("Account password.", "example-passphrase"),
    },
    "Registration": {
        "email": _ACCOUNT_FIELDS["email"],
        "password": ("New password of 10 to 128 characters.", "example-passphrase"),
    },
    "UserOut": _ACCOUNT_FIELDS,
    "SessionOut": {
        "user": ("Account associated with the session.", _EXAMPLE_ACCOUNT),
        "csrf_token": ("Send in X-CSRF-Token on authenticated writes.", "example-csrf-token"),
    },
    "AdminUserOut": {
        **_ACCOUNT_FIELDS,
        "is_active": ("Whether the account can sign in.", True),
        "version": ("Current revision used to produce the ETag.", 2),
    },
    "AdminUserPage": {
        "items": (
            "Accounts in ascending ID order.",
            [{**_EXAMPLE_ACCOUNT, "is_active": True, "version": 2}],
        ),
        "next_cursor": ("Cursor for the next page, or null at the end.", "42"),
    },
    "AdminUserPatch": {
        "role": ("New role, if provided.", "viewer"),
        "is_active": ("New sign-in status, if provided.", False),
    },
}

_USER_PARAMETERS: dict[str, tuple[str, object]] = {
    "user_id": ("Managed account ID.", 42),
    "limit": ("Maximum number of accounts to return.", 50),
    "cursor": ("Last account ID from the preceding page.", 42),
    "if-match": ("Strong ETag of the account revision to update.", '"v2"'),
}


def _ensure_problem(responses: dict[str, Any], status: int, codes: tuple[str, ...] = ()) -> None:
    response = responses.setdefault(str(status), {})
    content = response.setdefault("content", {})
    if status == 422 and response.get("description") == "Validation Error":
        content.pop("application/json", None)
        response.pop("description")
    media = content.setdefault(PROBLEM_CONTENT_TYPE, {})
    media["schema"] = {"$ref": "#/components/schemas/Problem"}
    description = response.get("description", "")
    missing = [code for code in codes if code not in description]
    if missing:
        response["description"] = " ".join(filter(None, (description, ", ".join(missing))))
    if status == 429 or {"idempotency-in-progress", "server-busy"} & set(codes):
        response.setdefault("headers", {}).setdefault(
            "Retry-After",
            {
                "description": "Wait before retrying when present.",
                "schema": {"type": "string", "examples": ["1"]},
            },
        )


def _document_operations(schema: dict[str, Any], routes: Iterable[APIRoute]) -> None:
    for route in routes:
        operations = schema["paths"].get(route.path_format, {})
        policy: Policy = route.endpoint.__policy__  # type: ignore[attr-defined]
        for method in route.methods or ():
            operation = operations.get(method.lower())
            if operation is None:
                continue
            responses = operation["responses"]
            for status in list(responses):
                if status.isdecimal() and int(status) >= 400:
                    _ensure_problem(responses, int(status))
            description, codes = _ROUTE_DOCS.get(route.endpoint, ("", {}))
            if description:
                operation["description"] = description
            for status_code, problems in codes.items():
                _ensure_problem(responses, status_code, problems)
            _ensure_problem(responses, 500, ("internal-error",))
            if "422" in responses:
                _ensure_problem(responses, 422, ("validation-failed",))
            if not policy.public:
                requirement: dict[str, list[str]] = {"sessionCookie": []}
                _ensure_problem(responses, 401, ("unauthenticated",))
                assert policy.roles is not None
                operation["x-allowed-roles"] = sorted(policy.roles)
                if policy.roles != ALL_ROLES:
                    _ensure_problem(responses, 403, ("forbidden",))
                if method in UNSAFE_METHODS:
                    requirement["csrfHeader"] = []
                    _ensure_problem(responses, 403, ("csrf-failed", "origin-not-allowed"))
                operation["security"] = [requirement]
            if route.path.startswith("/api/v1/admin/users"):
                for parameter in operation.get("parameters", []):
                    description, example = _USER_PARAMETERS[parameter["name"].lower()]
                    parameter.setdefault("description", description)
                    parameter["schema"].setdefault("examples", [example])
            if route.endpoint is health.ready:
                for status, value, description in (
                    ("200", "ready", "All readiness checks passed."),
                    ("503", "not-ready", "One or more readiness checks failed."),
                ):
                    responses[status] = {
                        "description": description,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["status", "checks"],
                                    "properties": {
                                        "status": {"type": "string", "const": value},
                                        "checks": {"type": "object", "additionalProperties": True},
                                    },
                                }
                            }
                        },
                    }


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
    documented_routes: list[APIRoute] = []
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
        documented_routes.extend(
            route for route in module.router.routes if isinstance(route, APIRoute)
        )
    telemetry_setup.install(app)
    realtime_setup.install(app)

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title, version=app.version, description=app.description, routes=app.routes
        )
        components = schema.setdefault("components", {})
        components["securitySchemes"] = {
            "sessionCookie": {
                "type": "apiKey",
                "in": "cookie",
                "name": "__Host-sid" if settings.cookie_secure else "sid",
                "description": "HttpOnly session cookie sent automatically by the browser.",
            },
            "csrfHeader": {"type": "apiKey", "in": "header", "name": "X-CSRF-Token"},
        }
        models = components.setdefault("schemas", {})
        models["Problem"] = _PROBLEM_SCHEMA
        for name, fields in _MODEL_FIELDS.items():
            for field, (description, example) in fields.items():
                property_schema = models[name]["properties"][field]
                property_schema.setdefault("description", description)
                property_schema.setdefault("examples", [example])
        _document_operations(schema, documented_routes)
        models.pop("HTTPValidationError", None)
        models.pop("ValidationError", None)
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
