from argparse import Namespace
from collections import Counter
from importlib import import_module
from multiprocessing import Array
from pathlib import Path
from time import monotonic
from types import ModuleType
from unittest.mock import MagicMock

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
    initial = sse_client.decode_snapshot(snapshot(2, 1, [1, 2], "AH", [3, 2]), {})
    probe = initial.fork()
    assert sse_client.apply_delta(probe, 4, raw) is not None
    assert probe.applied == 4
    assert probe.layout is initial.layout
    assert probe.statuses.tolist() == [ord("A"), ord("S")]
    assert probe.versions.tolist() == [3, 3]
    assert initial.statuses.tolist() == [ord("A"), ord("H")]
    assert initial.versions.tolist() == [3, 2]
    assert sse_client.apply_delta(probe, 4, raw) is None
    behind = initial.fork()
    behind.applied = 0
    with pytest.raises(sse_client.SequenceGap):
        sse_client.apply_delta(behind, 4, raw)
    with pytest.raises(sse_client.SequenceGap):
        sse_client.apply_delta(probe, 3, raw)


def snapshot(seq: int, layout: int, seats: list[int], statuses: str, versions: list[int]) -> bytes:
    return orjson.dumps(
        {
            "last_seq": seq,
            "layout_ver": layout,
            "seats": [{"idx": idx} for idx in seats],
            "status": statuses,
            "versions": versions,
        }
    )


def test_snapshot_refresh_shares_layout_and_packs_final_database_state(
    sse_client: ModuleType,
) -> None:
    layouts: dict[int, dict[int, int]] = {}
    initial = sse_client.decode_snapshot(snapshot(2, 1, [42, 7], "AH", [1, 2]), layouts)
    refreshed = sse_client.decode_snapshot(snapshot(3, 1, [7, 42], "SH", [3, 2]), layouts)
    changed = sse_client.decode_snapshot(snapshot(4, 2, [7, 42, 99], "SHA", [3, 2, 1]), layouts)
    assert refreshed.layout is initial.layout
    assert changed.layout is not initial.layout
    final = sse_client.DatabaseState(3, 0, 1, {7: ("sold", 3), 42: ("held", 2)}, 0)
    assert final.packed_seats(refreshed.layout) == (refreshed.statuses, refreshed.versions)
    assert final.packed_seats(initial.layout) != (initial.statuses, initial.versions)
    assert final.packed_seats(changed.layout) is None
    assert final.packed_seats({7: 0, 99: 1}) is None


def test_probe_array_storage_scales_with_seat_count(sse_client: ModuleType) -> None:
    count = 10_000
    initial = sse_client.decode_snapshot(
        snapshot(0, 1, list(range(count)), "A" * count, [1] * count), {}
    )
    probe = initial.fork()
    assert probe.layout is initial.layout
    assert (probe.statuses.itemsize + probe.versions.itemsize) * count == 50_000


def test_writer_waits_for_live_connections_and_records_its_peak(
    sse_client: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    args = Namespace(
        connections=2,
        event_id=1,
        writer_log=tmp_path / "writer.txt",
        write_rate=100,
        write_duration=1,
    )
    active = Array("i", [1, 2, -1])
    writer = MagicMock()
    writer.__enter__.return_value.poll.return_value = 0
    launch = MagicMock(return_value=writer)
    monkeypatch.setattr(sse_client.subprocess, "Popen", launch)

    assert sse_client.run_writer(args, active, [], monotonic() - 1) == (False, 0)
    launch.assert_not_called()
    assert "Writer skipped" in args.writer_log.read_text()

    def connect(*_args: object, **_kwargs: object) -> list[object]:
        launch.assert_not_called()
        active[0] = 2
        return []

    monkeypatch.setattr(sse_client, "wait", connect)
    assert sse_client.run_writer(args, active, [], monotonic() + 1) == (True, 2)
    launch.assert_called_once()
    assert active[2] == -1
