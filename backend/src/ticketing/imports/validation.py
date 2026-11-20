"""Set-based normalization, duplicate checks and layout application."""

from uuid import UUID

import asyncpg

from ticketing.core.context import request_id_var
from ticketing.realtime.outbox import emit_layout_changed


async def validate_file(conn: asyncpg.Connection, fence: UUID, event_id: int) -> None:
    await conn.execute(
        """WITH normalized AS (
               SELECT ctid AS row_tid, upper(zone_code COLLATE "C") AS zone,
                      CASE WHEN zone_code COLLATE "C" ~ '^[A-Za-z0-9_-]{1,16}$'
                             AND row_label COLLATE "C" ~ '^[A-Za-z0-9_-]{1,32}$'
                             AND seat_number COLLATE "C" ~ '^[0-9]{1,16}$'
                             AND ltrim(seat_number, '0') <> ''
                           THEN upper(zone_code COLLATE "C") || '|'
                                || upper(row_label COLLATE "C") || '|'
                                || ltrim(seat_number, '0') END AS key
               FROM import_staging WHERE fence = $1 AND error IS NULL
           ), ranked AS (
               SELECT *, count(*) OVER (PARTITION BY key) AS copies FROM normalized
           )
           UPDATE import_staging s SET norm_key = r.key,
               error = CASE WHEN r.key IS NULL THEN 'invalid_format'
                            WHEN r.copies > 1 THEN 'duplicate_in_file'
                            WHEN z.id IS NULL THEN 'unknown_zone' END
           FROM ranked r LEFT JOIN zones z ON z.event_id = $2 AND z.code = r.zone
           WHERE s.ctid = r.row_tid AND s.fence = $1""",
        fence,
        event_id,
        timeout=22,
    )


async def validate_existing(conn: asyncpg.Connection, fence: UUID, event_id: int) -> None:
    await conn.execute(
        """UPDATE import_staging s SET error = 'duplicate_existing'
           FROM seats x WHERE s.fence = $1 AND s.error IS NULL
             AND x.event_id = $2 AND x.norm_key = s.norm_key""",
        fence,
        event_id,
        timeout=22,
    )


async def apply_layout(conn: asyncpg.Connection, job: asyncpg.Record) -> None:
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
               WHERE s.fence = $1 AND s.error IS NULL
           ), base AS (
               SELECT coalesce(max(seat_idx), -1) + 1 AS idx FROM seats WHERE event_id = $2
           )
           INSERT INTO seats (event_id, zone_id, row_label, seat_number,
                              norm_key, sort_key, seat_idx)
           SELECT $2, n.zone_id, n.row_label, n.seat_number, n.norm_key,
                  b.idx + n.offset, b.idx + n.offset
           FROM numbered n CROSS JOIN base b ORDER BY n.offset""",
        job["fence"],
        event_id,
        timeout=22,
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
