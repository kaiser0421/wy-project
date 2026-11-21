"""The background service-log writer never blocks callers and never hangs shutdown."""

from __future__ import annotations

import threading
import time

from ticketing.core.logging import _TeeWriter


def test_full_queue_drops_and_counts_instead_of_blocking(monkeypatch) -> None:
    release = threading.Event()
    written: list[str] = []

    def stalled_write(self: _TeeWriter, lines: str) -> None:
        release.wait(5)
        written.append(lines)

    monkeypatch.setattr(_TeeWriter, "_write", stalled_write)
    writer = _TeeWriter(None, background=True, capacity=4)
    started = time.perf_counter()
    for index in range(50):
        writer.msg(f'{{"event": "e{index}"}}')
    assert time.perf_counter() - started < 0.5  # callers were never blocked
    assert writer.dropped > 0
    release.set()
    writer.close()
    assert any("service_logs.dropped" in chunk for chunk in written)


def test_close_returns_within_deadline_when_the_sink_is_stalled(monkeypatch) -> None:
    def stuck_write(self: _TeeWriter, lines: str) -> None:
        threading.Event().wait(30)

    monkeypatch.setattr(_TeeWriter, "_write", stuck_write)
    monkeypatch.setattr(_TeeWriter, "CLOSE_TIMEOUT_S", 0.3)
    writer = _TeeWriter(None, background=True, capacity=2)
    for index in range(10):
        writer.msg(f'{{"event": "e{index}"}}')
    started = time.perf_counter()
    writer.close()
    assert time.perf_counter() - started < 1  # the deadline holds even though the sink is stuck


def test_dropped_record_is_collector_compatible(monkeypatch) -> None:
    import orjson

    writer = _TeeWriter(None, background=False, service="api")
    record = orjson.loads(writer._dropped_record(3))
    assert record["event"] == "service_logs.dropped"
    assert record["count"] == 3
    assert {"ts", "service", "boot_uuid", "level"} <= set(record)
    assert record["service"] == "api"


def test_record_accepted_during_the_last_empty_wait_is_written(monkeypatch) -> None:
    """A record accepted (and close() called) while the writer is inside an empty wait."""
    from queue import Empty

    written: list[str] = []
    monkeypatch.setattr(_TeeWriter, "_write", lambda self, lines: written.append(lines))
    writer = _TeeWriter(None, background=False)  # no thread yet: drive _drain by hand
    writer._queue = __import__("queue").Queue(maxsize=8)
    real_get = writer._queue.get
    calls = 0

    def racing_get(block: bool = True, timeout: float | None = None):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            # During the first empty wait: a record is accepted, then close() sets the flag.
            writer.msg('{"event": "last"}')
            writer._closing.set()
            raise Empty
        return real_get(block, 0.01 if block else None)

    writer._queue.get = racing_get  # type: ignore[method-assign]
    writer._drain()
    assert any('"last"' in chunk for chunk in written)
