"""Business outcome counters (hold ok/conflict/sold-out/limit, payments, cancels, expiries).

Owned by workstream D. Other workstreams call `record()`; it must never raise or block.
"""

from __future__ import annotations


def record(event_id: int, outcome: str, count: int = 1) -> None:
    """Count a business outcome for the per-second business metrics (no-op until P7)."""
    return None
