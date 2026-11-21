"""Per-request / per-operation context propagated through contextvars."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

BOOT_UUID = uuid.uuid4().hex

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
op_id_var: ContextVar[str | None] = ContextVar("op_id", default=None)
parent_op_id_var: ContextVar[str | None] = ContextVar("parent_op_id", default=None)


def new_request_id() -> str:
    return uuid.uuid4().hex


def new_op_id() -> str:
    return secrets.token_hex(8)


def current_request_id() -> str | None:
    return request_id_var.get()


@contextmanager
def request_operation(request_id: str | None) -> Iterator[None]:
    """Continue a request in a deferred worker operation."""
    request_token = request_id_var.set(request_id or new_request_id())
    op_token = op_id_var.set(new_op_id())
    try:
        yield
    finally:
        op_id_var.reset(op_token)
        request_id_var.reset(request_token)
