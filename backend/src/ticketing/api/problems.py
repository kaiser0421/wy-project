"""Exception handlers that render every error as RFC 9457 problem+json."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import asyncpg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ticketing.core.context import current_request_id
from ticketing.core.errors import PROBLEM_CONTENT_TYPE, ProblemError, server_busy
from ticketing.core.logging import get_logger

log = get_logger(__name__)

_HTTP_CODES: dict[int, tuple[str, str]] = {
    400: ("bad-request", "Bad request"),
    401: ("unauthenticated", "Authentication required"),
    403: ("forbidden", "Forbidden"),
    404: ("not-found", "Not found"),
    405: ("method-not-allowed", "Method not allowed"),
    406: ("not-acceptable", "Not acceptable"),
    413: ("payload-too-large", "Payload too large"),
    415: ("unsupported-media-type", "Unsupported media type"),
    429: ("rate-limited", "Too many requests"),
}


def problem_responses(codes: Mapping[int, tuple[str, ...]]) -> dict[int | str, dict[str, Any]]:
    return {
        status: {
            "description": ", ".join(names),
            "content": {
                PROBLEM_CONTENT_TYPE: {
                    "examples": {
                        name: {"value": {"status": status, "code": name}} for name in names
                    },
                },
            },
        }
        for status, names in codes.items()
    }


def _render(problem: ProblemError, request: Request) -> JSONResponse:
    rid = current_request_id() or getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=problem.status,
        content=problem.to_body(instance=request.url.path, request_id=rid),
        media_type=PROBLEM_CONTENT_TYPE,
        headers={"Cache-Control": "private, no-store", **problem.headers},
    )


async def _problem_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ProblemError)
    return _render(exc, request)


async def _http_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code, title = _HTTP_CODES.get(exc.status_code, ("http-error", "HTTP error"))
    detail = (
        exc.detail if isinstance(exc.detail, str) and exc.detail.lower() != title.lower() else None
    )
    problem = ProblemError(exc.status_code, code, title, detail, headers=dict(exc.headers or {}))
    return _render(problem, request)


async def _validation_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors: list[dict[str, Any]] = [
        {"loc": list(err.get("loc", ())), "msg": err.get("msg"), "type": err.get("type")}
        for err in exc.errors()
    ]
    problem = ProblemError(422, "validation-failed", "Request validation failed", errors=errors)
    return _render(problem, request)


async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    log.error("request.internal_error", error_type=type(exc).__name__, path=request.url.path)
    problem = ProblemError(
        500,
        "internal-error",
        "Internal server error",
        "An unexpected error occurred. Quote the request_id when reporting it.",
    )
    return _render(problem, request)


# Database back-pressure (lock_timeout, statement_timeout, deadlock victim) is an overload
# signal, not a server bug: callers get the same retryable 503 as pool exhaustion.
_OVERLOAD_ERRORS = (
    asyncpg.LockNotAvailableError,
    asyncpg.QueryCanceledError,
    asyncpg.DeadlockDetectedError,
)


async def _overload_handler(request: Request, exc: Exception) -> JSONResponse:
    log.warning("request.db_overload", error_type=type(exc).__name__, path=request.url.path)
    return _render(server_busy(), request)


def install_problem_handlers(app: FastAPI) -> None:
    for error in _OVERLOAD_ERRORS:
        app.add_exception_handler(error, _overload_handler)
    app.add_exception_handler(ProblemError, _problem_handler)
    app.add_exception_handler(StarletteHTTPException, _http_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(Exception, _unhandled_handler)
