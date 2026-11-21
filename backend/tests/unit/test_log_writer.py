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
    assert time.perf_counter() - started < 2
