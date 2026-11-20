"""Process-local outcome counters for callers on the service event loop."""

from __future__ import annotations

from collections import Counter

OUTCOMES = (
    "hold_ok",
    "hold_conflict",
    "hold_soldout",
    "hold_limit",
    "pay_ok",
    "pay_fail",
    "cancel",
    "expiry",
)
MAX_KEYS = 20_000
_pending: Counter[tuple[int, str]] = Counter()


def record(event_id: int, outcome: str, count: int = 1) -> None:
    """Count a supported outcome without allocating an unbounded backlog."""
    if outcome not in OUTCOMES or event_id <= 0 or count <= 0:
        return
    key = (event_id, outcome)
    if len(_pending) < MAX_KEYS or key in _pending:
        _pending[key] += count


def drain() -> list[tuple[int, str, int]]:
    global _pending
    batch, _pending = _pending, Counter()
    return [(event_id, outcome, count) for (event_id, outcome), count in batch.items()]
