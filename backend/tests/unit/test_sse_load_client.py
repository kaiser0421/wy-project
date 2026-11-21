from collections import Counter
from importlib import import_module
from pathlib import Path
from types import ModuleType

import orjson
import pytest


@pytest.fixture
def sse_client(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3]))
    return import_module("loadtest.sse_client")


def test_worker_histograms_merge_by_sample_count(sse_client: ModuleType) -> None:
    merged = sse_client.Metrics()
    merged.merge(
        sse_client.Metrics(
            total=Counter(stream_attempts=90, stream_request_errors=1),
            latency=Counter({10: 90}),
            connect=Counter({1: 90}),
            snapshot=Counter({6: 1}),
        )
    )
    merged.merge(
        sse_client.Metrics(
            total=Counter(stream_attempts=10, stream_request_errors=2),
            latency=Counter({20: 5, 30: 4, 50: 1}),
            connect=Counter({2: 10}),
            snapshot=Counter({12: 1}),
        )
    )
    report = merged.summary(seconds=2, peak=42)
    assert report["delta_apply_samples"] == 100
    assert report["delta_apply_ms"] == {"p50": 10, "p95": 20, "p99": 30}
    assert report["connect_ms"] == {"p50": 1, "p95": 2, "p99": 2}
    assert report["snapshot_ms"] == {"p50": 6, "p95": 12, "p99": 12}
    assert report["stream_attempts"] == 100
    assert report["stream_error_rate"] == 0.03
    assert report["peak_active"] == 42
    assert sse_client.Metrics().summary(seconds=2, peak=0)["delta_apply_ms"] == {}


def test_latency_buckets_keep_signed_samples_and_bound_the_tail(sse_client: ModuleType) -> None:
    histogram: Counter[int] = Counter()
    for seconds in (-1000, -0.003, 0.005, 12.345, 1000, 2000):
        sse_client.record_latency(histogram, seconds)
    assert histogram == {-300_000: 1, -3: 1, 5: 1, 12_300: 1, 300_000: 2}


def test_delta_replay_keeps_newer_seats_and_requires_contiguous_intervals(
    sse_client: ModuleType,
) -> None:
    raw = orjson.dumps(
        {
            "from": 2,
            "to": 4,
            "ts": "2026-01-01T00:00:00Z",
            "changes": [[1, "held", 2], [2, "sold", 3]],
        }
    ).decode()
    probe = sse_client.Probe(2, 1, {1: ("available", 3), 2: ("held", 2)})
    assert sse_client.apply_delta(probe, 4, raw) is not None
    assert probe.applied == 4
    assert probe.seats == {1: ("available", 3), 2: ("sold", 3)}
    assert sse_client.apply_delta(probe, 4, raw) is None
    with pytest.raises(sse_client.SequenceGap):
        sse_client.apply_delta(sse_client.Probe(0, 1, {}), 4, raw)
    with pytest.raises(sse_client.SequenceGap):
        sse_client.apply_delta(probe, 3, raw)
