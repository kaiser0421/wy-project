"""Outermost ASGI middleware: request id binding and propagation.

It wraps the whole FastAPI application (including Starlette's ServerErrorMiddleware), so the
X-Request-ID header is present on every response, including unhandled 500s.
"""

from __future__ import annotations

import re
from time import perf_counter

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ticketing.core.context import (
    new_op_id,
    new_request_id,
    op_id_var,
    parent_op_id_var,
    request_id_var,
)
from ticketing.core.logging import get_logger
from ticketing.core.safe_exc import exception_summary

_VALID_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
log = get_logger(__name__)
_HEALTH_ROUTES = {"/healthz/live", "/healthz/ready"}


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            text: str = value.decode("latin-1")
            return text
    return None


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        incoming = _header(scope, b"x-request-id")
        rid = incoming if incoming and _VALID_ID.match(incoming) else new_request_id()
        parent = _header(scope, b"x-parent-op")
        tokens = (
            request_id_var.set(rid),
            op_id_var.set(new_op_id()),
            parent_op_id_var.set(parent if parent and _VALID_ID.match(parent) else None),
        )
        scope.setdefault("state", {})["request_id"] = rid

        started = perf_counter()
        status: int | None = None
        stream = False

        async def send_with_id(message: Message) -> None:
            nonlocal status, stream
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [(k, v) for k, v in message.get("headers", []) if k != b"x-request-id"]
                stream = any(
                    key == b"content-type" and value.split(b";", 1)[0] == b"text/event-stream"
                    for key, value in headers
                )
                headers.append((b"x-request-id", rid.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception as exc:
            # Never persist the exception message (it may carry user data or secrets).
            log.error(
                "request.unhandled_exception",
                route=getattr(scope.get("route"), "path", None),
                **exception_summary(exc),
            )
            if status is None:
                raise
            # The 500 problem response was already sent; stop the server from logging it again.
        finally:
            route = getattr(scope.get("route"), "path", None)
            if status is not None and not stream and route not in _HEALTH_ROUTES:
                principal = scope["state"].get("principal")
                user = {"user_id": principal.user_id} if principal is not None else {}
                log.info(
                    "request.completed",
                    method=scope["method"],
                    route=route,
                    status=status,
                    duration_ms=round((perf_counter() - started) * 1_000, 3),
                    **user,
                )
            parent_op_id_var.reset(tokens[2])
            op_id_var.reset(tokens[1])
            request_id_var.reset(tokens[0])
