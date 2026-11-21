"""Set-based duplicate checks and layout application."""

from uuid import UUID

import asyncpg

from ticketing.core.context import request_id_var
from ticketing.realtime.outbox import emit_layout_changed


async def validate_file(
    conn: asyncpg.Connection, job_id: UUID, event_id: int, *, command_timeout: float
) -> None:
    # Rank once even before PostgreSQL has statistics for the freshly copied staging rows.
    await conn.execute(
        """WITH ranked AS (
               SELECT ctid AS row_tid, norm_key, count(*) OVER (PARTITION BY norm_key) AS copies
               FROM import_staging WHERE job_id = $1 AND error IS NULL
           ), invalid AS MATERIALIZED (
               SELECT r.row_tid,
                      CASE WHEN r.copies > 1 THEN 'duplicate_in_file'
                           ELSE 'unknown_zone' END AS error
               FROM ranked r
               LEFT JOIN zones z ON z.event_id = $2 AND z.code = split_part(r.norm_key, '|', 1)
               WHERE r.copies > 1 OR z.id IS NULL
           )
           UPDATE import_staging s SET error = i.error
           FROM invalid i WHERE s.ctid = i.row_tid AND s.job_id = $1""",
        job_id,
        event_id,
        timeout=command_timeout,
    )


async def validate_existing(
    conn: asyncpg.Connection, job_id: UUID, event_id: int, *, command_timeout: float
) -> None:
    await conn.execute(
        """UPDATE import_staging s SET error = 'duplicate_existing'
           FROM seats x WHERE s.job_id = $1 AND s.error IS NULL
             AND x.event_id = $2 AND x.norm_key = s.norm_key""",
        job_id,
        event_id,
        timeout=command_timeout,
    )


async def apply_layout(
    conn: asyncpg.Connection, job: asyncpg.Record, *, command_timeout: float
) -> None:
    event_id = job["event_id"]
    # Number new seats only after existing indices, preserving the realtime bitmap mapping.
    await conn.execute(
        """WITH numbered AS (
               SELECT z.id AS zone_id, upper(s.row_label COLLATE "C") AS row_label,
                      ltrim(s.seat_number, '0') AS seat_number, s.norm_key,
                      row_number() OVER (
                          ORDER BY z.code, upper(s.row_label COLLATE "C"),
                                   ltrim(s.seat_number, '0')::bigint
                      ) - 1 AS offset
               FROM import_staging s JOIN zones z
                 ON z.event_id = $2 AND z.code = upper(s.zone_code COLLATE "C")
               WHERE s.job_id = $1 AND s.error IS NULL
           ), base AS (
               SELECT coalesce(max(seat_idx), -1) + 1 AS idx FROM seats WHERE event_id = $2
           )
           INSERT INTO seats (event_id, zone_id, row_label, seat_number,
                              norm_key, sort_key, seat_idx)
           SELECT $2, n.zone_id, n.row_label, n.seat_number, n.norm_key,
                  b.idx + n.offset, b.idx + n.offset
           FROM numbered n CROSS JOIN base b ORDER BY n.offset""",
        job["id"],
        event_id,
        timeout=command_timeout,
    )
    version = await conn.fetchval(
        """UPDATE events SET layout_ver = layout_ver + 1
           WHERE id = $1 AND status = 'draft' RETURNING layout_ver""",
        event_id,
    )
    assert version is not None
    token = request_id_var.set(job["origin_request_id"])
    try:
        await emit_layout_changed(conn, event_id, version)
    finally:
        request_id_var.reset(token)
