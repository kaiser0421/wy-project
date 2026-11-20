"""Fixed millisecond bounds, inclusive on the right, shared by edge and app.

Use eight equal-width subdivisions per doubling from 0.5 ms to 60,000 ms.
The exact 1,000 ms alert boundary is also a cut. The first bucket includes zero,
and the final bucket holds overflow. Percentiles use the nearest rank and return
the upper bound of its bucket, capped at 60,000 ms.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from math import ceil

BOUNDS = tuple(
    sorted(
        (
            0.5,
            *(
                0.5 * 2**power * (1 + step / 8)
                for power in range(17)
                for step in range(1, 9)
                if 0.5 * 2**power * (1 + step / 8) < 60_000
            ),
            1_000.0,
            60_000.0,
        )
    )
)
BUCKETS = len(BOUNDS) + 1


def merge(left: list[int], right: list[int]) -> list[int]:
    return [a + b for a, b in zip(left, right, strict=True)]


def percentiles(hist: list[int]) -> tuple[float | None, float | None, float | None]:
    total = sum(hist)
    if total == 0:
        return None, None, None
    ranks = [ceil(total * quantile) for quantile in (0.50, 0.95, 0.99)]
    cumulative = 0
    values: list[float] = []
    for index, count in enumerate(hist):
        cumulative += count
        while len(values) < len(ranks) and cumulative >= ranks[len(values)]:
            values.append(BOUNDS[min(index, len(BOUNDS) - 1)])
    return values[0], values[1], values[2]


@dataclass(slots=True)
class Counts:
    status_classes: list[int] = field(default_factory=lambda: [0] * 6)
    hist: list[int] = field(default_factory=lambda: [0] * BUCKETS)

    def observe(self, status: int, elapsed_ms: float) -> None:
        self.status_classes[status // 100 if 100 <= status <= 599 else 0] += 1
        self.hist[bisect_left(BOUNDS, elapsed_ms)] += 1

    def add(self, other: Counts) -> None:
        self.status_classes = merge(self.status_classes, other.status_classes)
        self.hist = merge(self.hist, other.hist)
