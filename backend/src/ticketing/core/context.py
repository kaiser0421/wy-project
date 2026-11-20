"""Per-request / per-operation context propagated through contextvars."""

from __future__ import annotations

import secrets
import uuid
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
