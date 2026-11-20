"""Reference rules for applying seat deltas after a snapshot."""

from __future__ import annotations

from typing import Protocol, TypedDict

type SeatChange = tuple[int, str, int]
type SeatState = dict[int, tuple[str, int]]


class SeatStore(Protocol):
    def get(self, idx: int, /) -> tuple[str, int] | None: ...

    def __setitem__(self, idx: int, value: tuple[str, int], /) -> None: ...


Delta = TypedDict("Delta", {"from": int, "to": int, "changes": list[SeatChange]})


class SequenceGap(ValueError):
    """The caller must replay from its last applied sequence or fetch a snapshot."""


def apply_delta(state: SeatStore, applied: int, delta: Delta) -> int:
    """Apply an overlapping coalesced interval without rolling back newer seat state."""
    if delta["to"] <= applied:
        return applied
    if delta["from"] > applied + 1:
        raise SequenceGap("Delta starts after the next expected sequence")
    for idx, status, version in delta["changes"]:
        previous = state.get(idx)
        if previous is None or version > previous[1]:
            state[idx] = (status, version)
    return delta["to"]
