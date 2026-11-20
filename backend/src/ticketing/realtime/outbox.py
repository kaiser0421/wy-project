"""Writing seat changes to the transactional outbox (shared contract, owned by Claude).

Call inside the same transaction that changed the seats. One call = one outbox row, so a
multi-seat change is never split across relay batches.
"""

from __future__ import annotations

from collections.abc import Sequence

import asyncpg
import orjson

from ticketing.core.context import current_request_id

SEAT_CHANGES = "seat_changes"
LAYOUT_CHANGED = "layout_changed"


async def emit_seat_changes(
    conn: asyncpg.Connection, event_id: int, changes: Sequence[tuple[int, str, int]]
) -> None:
    """Record `(seat_idx, status, version)` triples; status is available|held|sold."""
    if not changes:
        return
    await conn.execute(
        "INSERT INTO outbox (event_id, kind, changes, request_id) VALUES ($1, $2, $3::jsonb, $4)",
        event_id,
        SEAT_CHANGES,
        orjson.dumps([[int(i), s, int(v)] for i, s, v in changes]).decode(),
        current_request_id(),
    )


async def emit_layout_changed(conn: asyncpg.Connection, event_id: int, layout_ver: int) -> None:
    await conn.execute(
        "INSERT INTO outbox (event_id, kind, changes, request_id) VALUES ($1, $2, $3::jsonb, $4)",
        event_id,
        LAYOUT_CHANGED,
        orjson.dumps({"layout_ver": layout_ver}).decode(),
        current_request_id(),
    )
