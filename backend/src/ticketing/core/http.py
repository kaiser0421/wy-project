"""Outbound HTTP client that propagates X-Request-ID and the parent operation id."""

from __future__ import annotations

import httpx

from ticketing.core.context import new_request_id, op_id_var, request_id_var

REQUEST_ID_HEADER = "X-Request-ID"
PARENT_OP_HEADER = "X-Parent-Op"


async def _inject_context(request: httpx.Request) -> None:
    rid = request_id_var.get() or new_request_id()
    request.headers.setdefault(REQUEST_ID_HEADER, rid)
    op = op_id_var.get()
    if op is not None:
        request.headers.setdefault(PARENT_OP_HEADER, op)


def create_http_client(base_url: str, timeout_s: float = 5.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(timeout_s),
        event_hooks={"request": [_inject_context]},
    )
