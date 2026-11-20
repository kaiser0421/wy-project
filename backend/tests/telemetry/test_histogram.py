from __future__ import annotations

import pytest
from starlette.types import Message, Receive, Scope, Send

from ticketing.telemetry import business
from ticketing.telemetry.api_setup import RequestMetrics
from ticketing.telemetry.histogram import BUCKETS, Counts, merge, percentiles


def test_histogram_merges_bucket_counts_before_calculating_quantiles() -> None:
    first, second, all_requests = Counts(), Counts(), Counts()
    for index in range(100):
        status, elapsed = (503, 1200) if index >= 94 else (200, 0.72)
        (first if index < 99 else second).observe(status, elapsed)
        all_requests.observe(status, elapsed)
    assert merge(first.hist, second.hist) == all_requests.hist
    first.add(second)
    assert first.status_classes == [0, 0, 94, 0, 0, 6]
    assert percentiles(first.hist) == (0.75, 1280, 1280)
    assert percentiles([0] * BUCKETS) == (None, None, None)
    overflow = Counts()
    overflow.observe(200, 90_000)
    assert percentiles(overflow.hist) == (60_000, 60_000, 60_000)


def test_business_counters_drain_once_and_ignore_invalid_outcomes() -> None:
    business.drain()
    business.record(42, "hold_ok")
    business.record(42, "hold_ok", 2)
    business.record(42, "unknown")
    business.record(42, "expiry", -1)
    assert business.drain() == [(42, "hold_ok", 3)]
    assert business.drain() == []


@pytest.mark.parametrize(
    ("path", "expected"),
    [("/api/v1/events/28/orders", True), ("/api/v1/events/28/seat-stream", False)],
)
async def test_asgi_observes_route_template_and_excludes_sse(path: str, expected: bool) -> None:
    class Route:
        path = "/api/v1/events/{id}/orders"

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        scope["route"] = Route()
        await send({"type": "http.response.start", "status": 409, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async def receive() -> Message:
        return {"type": "http.request"}

    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    measurements: dict[str, Counts] = {}
    await RequestMetrics(app, measurements)({"type": "http", "path": path}, receive, send)
    assert messages[0]["status"] == 409
    if expected:
        assert measurements[Route.path].status_classes[4] == 1
    else:
        assert measurements == {}
