"""Structured JSON logging.

Every line carries service, boot_uuid, request_id, op_id and a process-local sequence
number so the collector can rebuild per-request timelines across services.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Lock, Thread
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

    # Never block the event loop on logging: when the sink cannot keep up, records are dropped
    # and counted, and the count is written as its own record once the sink recovers.
    CLOSE_TIMEOUT_S = 5.0

    def __init__(self, path: Path | None, *, background: bool, capacity: int = 8192) -> None:
        self._file = path.open("a", encoding="utf-8") if path else None
        self._queue: Queue[str | None] | None = Queue(maxsize=capacity) if background else None
        self._dropped = 0
        self._lock = Lock()
        self._thread = (
            Thread(target=self._drain, name="service-logs", daemon=True) if background else None
        )
        if self._thread is not None:
            self._thread.start()

    def msg(self, message: str) -> None:
        if self._queue is None:
            self._write(message + "\n")
        else:
            try:
                self._queue.put_nowait(message)
            except Full:
                with self._lock:
                    self._dropped += 1

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    def _take_dropped(self) -> int:
        with self._lock:
            dropped, self._dropped = self._dropped, 0
            return dropped

    def _write(self, lines: str) -> None:
        if self._file is not None:
            self._file.write(lines)
            self._file.flush()
        sys.stdout.write(lines)
        sys.stdout.flush()

    def _drain(self) -> None:
        queue = self._queue
        assert queue is not None
        message = queue.get()
        while message is not None:
            batch = [message]
            stopping = False
            for _ in range(255):
                try:
                    message = queue.get_nowait()
                except Empty:
                    break
                if message is None:
                    stopping = True
                    break
                batch.append(message)
            dropped = self._take_dropped()
            if dropped:
                batch.append(
                    orjson.dumps(
                        {"event": "service_logs.dropped", "level": "warning", "count": dropped}
                    ).decode()
                )
            try:
                self._write("\n".join(batch) + "\n")
            except (OSError, ValueError) as exc:
                print(f"Service log write failed: {type(exc).__name__}", file=sys.stderr)
            if stopping:
                break
            message = queue.get()

    def close(self) -> None:
        if self._thread is not None and self._queue is not None:
            # Drain accepted records within a deadline; a stalled sink must not hang shutdown.
            with suppress(Full):
                self._queue.put(None, timeout=self.CLOSE_TIMEOUT_S)
            self._thread.join(timeout=self.CLOSE_TIMEOUT_S)
            if self._thread.is_alive():
                print(
                    f"Service log writer did not drain within {self.CLOSE_TIMEOUT_S:.0f} s; "
                    f"about {self._queue.qsize()} records not written",
                    file=sys.stderr,
                )
                return
            if self.dropped:
                print(f"Service log records dropped: {self.dropped}", file=sys.stderr)
        if self._file is not None:
            self._file.close()

    log = debug = info = warning = warn = error = critical = exception = msg


def configure_logging(
    service: str, level: str = "INFO", log_dir: str | None = None, *, background: bool = False
) -> _TeeWriter:
    path: Path | None = None
    if log_dir:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{service}-{BOOT_UUID}.json"
    writer = _TeeWriter(path, background=background)

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
        cache_logger_on_first_use=not background,
    )
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    return writer


@asynccontextmanager
async def background_logging(service: str, level: str, log_dir: str | None) -> AsyncIterator[None]:
    previous = structlog.get_config()
    writer = configure_logging(service, level, log_dir, background=True)
    try:
        yield
    finally:
        structlog.configure(**previous)
        await asyncio.to_thread(writer.close)


def get_logger(name: str | None = None) -> structlog.typing.FilteringBoundLogger:
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger(name)
    return logger
