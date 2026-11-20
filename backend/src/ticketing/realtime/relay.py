"""Assign sequence numbers and archive seat events in one PostgreSQL transaction."""

from __future__ import annotations

import asyncpg

from ticketing.core.db import acquire
from ticketing.core.service import ServiceContext
from ticketing.realtime.outbox import SEAT_CHANGES

RELAY_LOCK = 0x5345415452454C41
BATCH_SIZE = 500

_ASSIGN = """
WITH ranked AS (
    SELECT id, event_id, row_number() OVER (PARTITION BY event_id ORDER BY id) AS rank,
           count(*) OVER (PARTITION BY event_id) AS batch_size
    FROM outbox
    WHERE id = ANY($1::bigint[])
), counters AS (
    INSERT INTO event_stream_state (event_id, last_assigned_seq)
    SELECT DISTINCT event_id, batch_size FROM ranked ORDER BY event_id
    ON CONFLICT (event_id) DO UPDATE
    SET last_assigned_seq = event_stream_state.last_assigned_seq + excluded.last_assigned_seq
    RETURNING event_id, last_assigned_seq
), assigned AS (
    UPDATE outbox o
    SET seq = c.last_assigned_seq - r.batch_size + r.rank, assigned_at = now()
    FROM ranked r JOIN counters c USING (event_id)
    WHERE o.id = r.id AND o.seq IS NULL
    RETURNING o.*
), archived AS (
    INSERT INTO seat_events
        (recorded_at, event_id, seq, seat_idx, status, seat_version, request_id, published_at)
    SELECT a.created_at, a.event_id, a.seq, (s.change->>0)::int, s.change->>1,
           (s.change->>2)::int, a.request_id, now()
    FROM assigned a
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN a.kind = $2 THEN a.changes ELSE '[]'::jsonb END
    ) AS s(change)
)
SELECT count(*) FROM assigned
"""


async def _assign_batch(conn: asyncpg.Connection) -> None:
    """Assign committed rows by id without waiting for unrelated writers.

    Writers lock and modify seats before inserting one outbox row per transaction.
    For two updates of the same seat, the second writer can allocate its outbox id
    only after the first commits. Both rows are then visible in id order, or only
    the first is visible. The relay is the only locker of committed outbox rows.
    Unrelated seats can be sequenced in a different order from their commits.
    """
    async with conn.transaction():
        rows = await conn.fetch(
            """
            SELECT id FROM outbox WHERE seq IS NULL
            ORDER BY id LIMIT $1 FOR UPDATE SKIP LOCKED
            """,
            BATCH_SIZE,
        )
        if not rows:
            return
        count = await conn.fetchval(_ASSIGN, [row["id"] for row in rows], SEAT_CHANGES)
        if count != len(rows):
            raise RuntimeError("Outbox assignment count mismatch")


async def tick(ctx: ServiceContext) -> None:
    """Lease one database session for the whole bounded batch and its advisory lock."""
    if ctx.stop.is_set():
        return
    async with acquire(ctx.db, ctx.settings.db_pool_acquire_timeout_s) as conn:
        if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", RELAY_LOCK):
            return
        try:
            if not ctx.stop.is_set():
                await _assign_batch(conn)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", RELAY_LOCK)
