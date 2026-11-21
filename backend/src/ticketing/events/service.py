"""Event writes keep the event ETag and zone configuration in one transaction."""

import re
from datetime import datetime
from typing import Any

import asyncpg

from ticketing.core.errors import ProblemError
from ticketing.events.models import EventCreate, EventOut, EventPatch, ZoneInput

_ETAG = re.compile(r'"v([1-9][0-9]{0,9})"')


def etag(version: int) -> str:
    return f'"v{version}"'


def expected_version(value: str | None) -> int | None:
    if value is None:
        return None
    match = _ETAG.fullmatch(value.strip())
    return int(match[1]) if match and int(match[1]) <= 2**31 - 1 else 0


def precondition(current: EventOut, *, missing: bool = False) -> ProblemError:
    return ProblemError(
        428 if missing else 412,
        "precondition-required" if missing else "precondition-failed",
        "Send the current event ETag in If-Match" if missing else "Event ETag does not match",
        headers={"ETag": etag(current.version)},
        extra={"current": current.model_dump(mode="json")},
    )


def validate_window(start: datetime, end: datetime) -> None:
    if end <= start:
        raise ProblemError(422, "validation-failed", "Sales must close after opening")


def validate_zones(zones: list[ZoneInput]) -> None:
    if any("\x00" in zone.name for zone in zones):
        raise ProblemError(422, "validation-failed", "Zone names cannot contain NUL")
    if len({zone.code for zone in zones}) != len(zones):
        raise ProblemError(422, "validation-failed", "Zone codes must be unique")


async def representation(conn: asyncpg.Connection, row: asyncpg.Record) -> EventOut:
    zones = await conn.fetch(
        "SELECT code, name, price_twd FROM zones WHERE event_id = $1 ORDER BY code", row["id"]
    )
    return EventOut.model_validate({**dict(row), "zones": [dict(zone) for zone in zones]})


async def _put_zones(conn: asyncpg.Connection, event_id: int, zones: list[ZoneInput]) -> None:
    await conn.executemany(
        """INSERT INTO zones (event_id, code, name, price_twd) VALUES ($1, $2, $3, $4)
           ON CONFLICT (event_id, code) DO UPDATE
             SET name = EXCLUDED.name, price_twd = EXCLUDED.price_twd""",
        [(event_id, zone.code, zone.name, zone.price_twd) for zone in zones],
    )


async def create_event(conn: asyncpg.Connection, owner_id: int, body: EventCreate) -> EventOut:
    validate_window(body.sales_start_at, body.sales_end_at)
    validate_zones(body.zones)
    row = await conn.fetchrow(
        """INSERT INTO events (owner_id, title, description, category, venue,
                               sales_start_at, sales_end_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *""",
        owner_id,
        body.title,
        body.description,
        body.category,
        body.venue,
        body.sales_start_at,
        body.sales_end_at,
    )
    await _put_zones(conn, row["id"], body.zones)
    return await representation(conn, row)


async def active_import(conn: asyncpg.Connection, event_id: int) -> bool:
    found = await conn.fetchval(
        """SELECT 1 FROM import_jobs WHERE event_id = $1
           AND status IN ('queued', 'running') FOR UPDATE""",
        event_id,
    )
    return found is not None


async def _replace_zones(conn: asyncpg.Connection, event_id: int, zones: list[ZoneInput]) -> None:
    codes = [zone.code for zone in zones]
    if await conn.fetchval(
        """SELECT EXISTS (SELECT FROM zones z JOIN seats s ON s.zone_id = z.id
                          WHERE z.event_id = $1 AND z.code <> ALL($2::text[]))""",
        event_id,
        codes,
    ):
        raise ProblemError(409, "zone-has-seats", "A zone with imported seats cannot be removed")
    await conn.execute(
        "DELETE FROM zones WHERE event_id = $1 AND code <> ALL($2::text[])", event_id, codes
    )
    await _put_zones(conn, event_id, zones)


async def patch_event(
    conn: asyncpg.Connection, row: asyncpg.Record, body: EventPatch, expected: int | None
) -> EventOut:
    current = await representation(conn, row)
    if expected != current.version:
        raise precondition(current, missing=expected is None)
    updates: dict[str, Any] = body.model_dump(exclude_unset=True)
    if not updates or any(
        value is None or (isinstance(value, str) and "\x00" in value) for value in updates.values()
    ):
        raise ProblemError(422, "validation-failed", "Provide at least one valid, non-null field")
    start = updates.get("sales_start_at", current.sales_start_at)
    end = updates.get("sales_end_at", current.sales_end_at)
    zones = body.zones
    if current.status == "published" and (
        "zones" in updates or start != current.sales_start_at or end != current.sales_end_at
    ):
        raise ProblemError(
            409, "field-frozen-after-publish", "The sales window and zones are frozen"
        )
    validate_window(start, end)
    if zones is not None:
        validate_zones(zones)
        if await active_import(conn, current.id):
            raise ProblemError(409, "import-running", "An import is active")
    updated = await conn.fetchrow(
        """UPDATE events SET title = $3, description = $4, category = $5, venue = $6,
                             sales_start_at = $7, sales_end_at = $8,
                             version = version + 1, updated_at = app_now()
           WHERE id = $1 AND version = $2 RETURNING *""",
        current.id,
        expected,
        updates.get("title", current.title),
        updates.get("description", current.description),
        updates.get("category", current.category),
        updates.get("venue", current.venue),
        start,
        end,
    )
    if updated is None:
        raise precondition(current)
    if zones is not None:
        await _replace_zones(conn, current.id, zones)
    return await representation(conn, updated)


async def publish_event(
    conn: asyncpg.Connection, row: asyncpg.Record, expected: int | None
) -> EventOut:
    current = await representation(conn, row)
    if expected != current.version:
        raise precondition(current, missing=expected is None)
    if current.status == "published":
        return current
    if await active_import(conn, current.id):
        raise ProblemError(409, "import-running", "An import is active")
    if not await conn.fetchval("SELECT EXISTS (SELECT FROM seats WHERE event_id = $1)", current.id):
        raise ProblemError(409, "no-seats", "Import at least one seat before publishing")
    updated = await conn.fetchrow(
        """UPDATE events SET status = 'published', published_at = app_now(),
                             updated_at = app_now(), version = version + 1
           WHERE id = $1 AND version = $2 AND status = 'draft' RETURNING *""",
        current.id,
        expected,
    )
    if updated is None:
        raise precondition(current)
    return await representation(conn, updated)


async def delete_event(conn: asyncpg.Connection, event_id: int) -> None:
    if await conn.fetchval("SELECT EXISTS (SELECT FROM orders WHERE event_id = $1)", event_id):
        raise ProblemError(409, "event-has-orders", "Events with orders cannot be deleted")
    deleted = await conn.fetchval("DELETE FROM events WHERE id = $1 RETURNING id", event_id)
    assert deleted is not None
