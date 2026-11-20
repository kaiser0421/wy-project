from __future__ import annotations

import pytest

from ticketing.realtime.client_model import SeatState, SequenceGap, apply_delta
from ticketing.realtime.frames import Frame, coalesce
from ticketing.realtime.reader import Ring


def test_stale_and_overlapping_deltas_do_not_roll_back_newer_versions() -> None:
    state: SeatState = {0: ("sold", 3)}
    applied = apply_delta(
        state, 10, {"from": 9, "to": 12, "changes": [(0, "held", 2), (1, "available", 1)]}
    )
    assert applied == 12
    assert state == {0: ("sold", 3), 1: ("available", 1)}
    assert apply_delta(state, 12, {"from": 12, "to": 12, "changes": []}) == 12
    with pytest.raises(SequenceGap):
        apply_delta(state, 12, {"from": 14, "to": 15, "changes": [(0, "available", 4)]})
    assert state[0] == ("sold", 3)


def test_ring_eviction_by_bytes_and_by_age(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ticketing.realtime.reader.RING_BYTES", 8)
    ring = Ring()
    ring.append(Frame(1, 3, b"1234"), 1)
    assert ring.after(1) == Frame(1, 3, b"1234")
    ring.append(Frame(4, 4, b"56789"), 2)
    assert ring.size == 5
    assert ring.after(2) is None
    assert ring.after(3) == Frame(4, 4, b"56789")
    ring.prune(7)
    assert ring.size == 0
    assert ring.after(3) is None


def test_reader_rejects_a_gap_even_when_an_available_row_is_a_layout() -> None:
    with pytest.raises(SequenceGap):
        coalesce([{"seq": 2, "kind": "layout_changed", "changes": "{}"}], 0)
