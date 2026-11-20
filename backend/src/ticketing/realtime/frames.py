"""Encode contiguous outbox rows once for all subscribers in a process."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg
import orjson

from ticketing.realtime.client_model import SeatChange, SequenceGap
from ticketing.realtime.outbox import LAYOUT_CHANGED, SEAT_CHANGES


def encode(event: str, data: object, seq: int | None = None) -> bytes:
    message = b"event: " + event.encode() + b"\ndata: " + orjson.dumps(data) + b"\n\n"
    if seq is not None:
        return b"id: " + str(seq).encode() + b"\n" + message
    return message


@dataclass(frozen=True, slots=True)
class Frame:
    start: int
    end: int
    body: bytes


def _delta(start: int, end: int, changes: dict[int, SeatChange], ts: datetime) -> Frame:
    return Frame(
        start,
        end,
        encode(
            "delta", {"from": start, "to": end, "changes": list(changes.values()), "ts": ts}, end
        ),
    )


def coalesce(rows: list[asyncpg.Record], after: int) -> list[Frame]:
    """A layout row is a barrier because the index mapping may have changed."""
    frames: list[Frame] = []
    changes: dict[int, SeatChange] = {}
    start = after + 1
    ts: datetime | None = None
    for row in rows:
        seq: int = row["seq"]
        if seq != after + 1:
            raise SequenceGap("Outbox sequences are not contiguous")
        payload: Any = orjson.loads(row["changes"])
        if row["kind"] == LAYOUT_CHANGED:
            if ts is not None:
                frames.append(_delta(start, after, changes, ts))
                changes.clear()
                ts = None
            frames.append(Frame(seq, seq, encode("layout", payload, seq)))
            start = seq + 1
        elif row["kind"] == SEAT_CHANGES:
            created_at: datetime = row["created_at"]
            ts = created_at if ts is None else min(ts, created_at)
            for idx, status, version in payload:
                previous = changes.get(idx)
                if previous is None or version > previous[2]:
                    changes[idx] = (idx, status, version)
        else:
            raise SequenceGap("Unknown outbox row kind")
        after = seq
    if ts is not None:
        frames.append(_delta(start, after, changes, ts))
    return frames
