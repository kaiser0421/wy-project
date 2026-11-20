"""Structured JSON logging.

Every line carries service, boot_uuid, request_id, op_id and a process-local sequence
number so the collector can rebuild per-request timelines across services.
"""

from __future__ import annotations

import itertools
import logging
import os
import sys
from pathlib import Path
from typing import Any

import orjson
import structlog

from ticketing.core.context import BOOT_UUID, op_id_var, parent_op_id_var, request_id_var

_seq = itertools.count(1)
_REDACT = {"password", "authorization", "cookie", "set-cookie", "token", "secret", "signature"}


def _add_context(service: str) -> structlog.types.Processor:
    def processor(
        _logger: Any, _method: str, event_dict: structlog.types.EventDict
    ) -> structlog.types.EventDict:
        event_dict.setdefault("service", service)
        event_dict["boot_uuid"] = BOOT_UUID
        event_dict["local_seq"] = next(_seq)
        rid = request_id_var.get()
        if rid is not None:
            event_dict.setdefault("request_id", rid)
        op = op_id_var.get()
        if op is not None:
            event_dict.setdefault("op_id", op)
        parent = parent_op_id_var.get()
        if parent is not None:
            event_dict.setdefault("parent_op_id", parent)
        return event_dict

    return processor


def _redact(
    _logger: Any, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    for key in list(event_dict):
        if key.lower() in _REDACT:
            event_dict[key] = "[redacted]"
    return event_dict


def _dumps(obj: Any, **_: Any) -> str:
    return orjson.dumps(obj, default=str).decode()


class _TeeWriter:
    """Writes each rendered line to stdout and, optionally, a per-process JSON file."""

    def __init__(self, path: Path | None) -> None:
        self._file = path.open("a", buffering=1, encoding="utf-8") if path else None

    def msg(self, message: str) -> None:
        sys.stdout.write(message + "\n")
        sys.stdout.flush()
        if self._file is not None:
            self._file.write(message + "\n")

    log = debug = info = warning = warn = error = critical = exception = msg


def configure_logging(service: str, level: str = "INFO", log_dir: str | None = None) -> None:
    path: Path | None = None
    if log_dir:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{service}-{BOOT_UUID}.json"
    writer = _TeeWriter(path)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
            _add_context(service),
            _redact,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(serializer=_dumps),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=lambda *_args: writer,
        cache_logger_on_first_use=True,
    )
    os.environ.setdefault("PYTHONUNBUFFERED", "1")


def get_logger(name: str | None = None) -> structlog.typing.FilteringBoundLogger:
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger(name)
    return logger
