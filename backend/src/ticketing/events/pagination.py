"""Principal-bound cursors over materialized event membership."""

import hashlib
from datetime import UTC
from typing import Annotated, Literal
from uuid import UUID

import asyncpg
from pydantic import AwareDatetime, BaseModel, Field

from ticketing.core.cursors import open_cursor, seal_cursor
from ticketing.core.errors import ProblemError
from ticketing.events.access import EventActor
from ticketing.events.models import EventOut, EventPage, EventStatus, UnavailableEvent, ZoneInput

Sort = Literal[
    "sales_start_at", "-sales_start_at", "created", "-created", "price_min", "-price_min"
]
_SORTS = {
    "sales_start_at": "e.sales_start_at ASC",
    "-sales_start_at": "e.sales_start_at DESC",
    "created": "e.created_at ASC",
    "-created": "e.created_at DESC",
    "price_min": "price.minimum ASC NULLS LAST",
    "-price_min": "price.minimum DESC NULLS LAST",
}
_MAX_ROWS = 50_000


class EventQuery(BaseModel):
    category: Annotated[
        str | None, Field(max_length=50, description="Exact category", examples=["music"])
    ] = None
    status: Annotated[
        EventStatus | None,
        Field(description="Filter visible events by publication state", examples=["draft"]),
    ] = None
    q: Annotated[
        str | None,
        Field(
            max_length=200,
            description="Literal substring in title, description or venue",
            examples=["summer"],
        ),
    ] = None
    sales_from: Annotated[
        AwareDatetime | None,
        Field(
            description="Earliest included sales start (inclusive)",
            examples=["2030-06-01T00:00:00Z"],
        ),
    ] = None
    sales_to: Annotated[
        AwareDatetime | None,
        Field(
            description="Latest included sales start (inclusive)", examples=["2030-06-30T23:59:59Z"]
        ),
    ] = None
    sort: Annotated[
        Sort,
        Field(
            description=(
                "Sort by sales start, creation or lowest zone price. Prefix with - for "
                "descending. Equal values use ascending event ID."
            ),
            examples=["-created"],
        ),
    ] = "sales_start_at"
    cursor: Annotated[
        str | None,
        Field(max_length=256, description="Cursor returned by the preceding page", examples=[None]),
    ] = None
    limit: Annotated[int, Field(ge=1, le=100, description="Page size", examples=[50])] = 50

    def digest(self) -> bytes:
        canonical = self.model_copy(
            update={
                key: value.astimezone(UTC)
                for key, value in (("sales_from", self.sales_from), ("sales_to", self.sales_to))
                if value is not None
            }
        )
        return hashlib.sha256(
            canonical.model_dump_json(exclude={"limit", "cursor"}).encode()
        ).digest()


def _invalid() -> ProblemError:
    return ProblemError(409, "cursor-invalidated", "Start again without a cursor")


def _scope(actor: EventActor, digest: bytes) -> bytes:
    return actor.user_id.to_bytes(8, "big") + actor.role.encode() + b"\0" + digest


def encode_cursor(snapshot: UUID, ord_: int, actor: EventActor, digest: bytes, secret: str) -> str:
    body = snapshot.bytes + ord_.to_bytes(4, "big")
    return seal_cursor(body, secret, b"event-list-v2\0", _scope(actor, digest))


def decode_cursor(token: str, actor: EventActor, digest: bytes, secret: str) -> tuple[UUID, int]:
    body = open_cursor(token, 20, secret, b"event-list-v2\0", _scope(actor, digest))
    return UUID(bytes=body[:16]), int.from_bytes(body[16:], "big")


async def _materialize(
    conn: asyncpg.Connection, actor: EventActor, query: EventQuery, digest: bytes
) -> asyncpg.Record:
    # Per-principal serialization makes the 20 live snapshot limit concurrency-safe.
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('event-list:' || $1::text, 0))",
        str(actor.user_id),
    )
    await conn.execute(
        """WITH expired AS (
               DELETE FROM list_snapshots
               WHERE scope = 'principal' AND principal_id = $1 AND expires_at <= app_now()
               RETURNING id, created_at)
           DELETE FROM list_snapshot_rows r USING expired x
           WHERE r.created_at = x.created_at AND r.snapshot_id = x.id""",
        actor.user_id,
    )
    snapshot = await conn.fetchrow(
        """INSERT INTO list_snapshots (scope, principal_id, query_hash, row_count,
                                       created_at, expires_at)
           SELECT 'principal', $1, $2, 0, stamp, stamp + interval '10 minutes'
           FROM (SELECT app_now() AS stamp) clock
           WHERE (SELECT count(*) FROM list_snapshots
                  WHERE scope = 'principal' AND principal_id = $1) < 20
           RETURNING *""",
        actor.user_id,
        digest,
    )
    if snapshot is None:
        raise ProblemError(
            429,
            "snapshot-limit",
            "Too many active event snapshots",
            retryable=True,
            headers={"Retry-After": "60"},
        )
    # Both the order and membership come from a single PostgreSQL statement snapshot.
    sort = _SORTS[query.sort]
    count = await conn.fetchval(
        f"""WITH inserted AS (
            INSERT INTO list_snapshot_rows (created_at, snapshot_id, ord, event_id)
            SELECT $1, $2, row_number() OVER (ORDER BY {sort}, e.id), e.id
            FROM events e
            LEFT JOIN LATERAL (
                SELECT min(price_twd) AS minimum FROM zones WHERE event_id = e.id
            ) price ON true
            WHERE ($3 = 'admin' OR e.status = 'published' OR ($3 = 'user' AND e.owner_id = $4))
              AND ($5::text IS NULL OR e.category = $5)
              AND ($6::text IS NULL OR e.status = $6::event_status)
              AND ($7::text IS NULL OR strpos(lower(e.title), lower($7)) > 0
                   OR strpos(lower(e.description), lower($7)) > 0
                   OR strpos(lower(e.venue), lower($7)) > 0)
              AND ($8::timestamptz IS NULL OR e.sales_start_at >= $8)
              AND ($9::timestamptz IS NULL OR e.sales_start_at <= $9)
            ORDER BY {sort}, e.id LIMIT 50001 RETURNING 1)
            SELECT count(*) FROM inserted""",  # noqa: S608 - sort is a fixed SQL constant
        snapshot["created_at"],
        snapshot["id"],
        actor.role,
        actor.user_id,
        query.category,
        query.status,
        query.q,
        query.sales_from,
        query.sales_to,
    )
    if count > _MAX_ROWS:
        raise ProblemError(422, "refine-filters", "Refine filters to match at most 50,000 events")
    saved = await conn.fetchrow(
        "UPDATE list_snapshots SET row_count = $2 WHERE id = $1 RETURNING *", snapshot["id"], count
    )
    assert saved is not None
    return saved


async def _read_page(
    conn: asyncpg.Connection, snapshot: asyncpg.Record, after: int, actor: EventActor, limit: int
) -> list[asyncpg.Record]:
    # Check all surviving members before returning any data. Using one statement prevents
    # changes between authorization and serialization of a member or its zone information.
    rows: list[asyncpg.Record] = await conn.fetch(
        """WITH authorized AS (
             SELECT NOT EXISTS (
                 SELECT FROM list_snapshot_rows r JOIN events e ON e.id = r.event_id
                 WHERE r.created_at = $1 AND r.snapshot_id = $2
                   AND NOT ($3 = 'admin' OR e.status = 'published' OR
                            ($3 = 'user' AND e.owner_id = $4))
             ) AS allowed
           ), page AS (
             SELECT ord, event_id FROM list_snapshot_rows
             WHERE created_at = $1 AND snapshot_id = $2 AND ord > $5
             ORDER BY ord LIMIT $6
           )
           SELECT a.allowed, p.ord, p.event_id, e.*,
                  ARRAY(SELECT json_build_object(
                              'code', code, 'name', name, 'price_twd', price_twd)::text
                        FROM zones WHERE event_id = e.id ORDER BY code) AS zone_json
           FROM authorized a LEFT JOIN page p ON a.allowed
           LEFT JOIN events e ON e.id = p.event_id ORDER BY p.ord""",
        snapshot["created_at"],
        snapshot["id"],
        actor.role,
        actor.user_id,
        after,
        limit,
    )
    if not rows[0]["allowed"]:
        raise _invalid()
    return rows if rows[0]["ord"] is not None else []


async def list_events(
    conn: asyncpg.Connection, actor: EventActor, query: EventQuery, secret: str
) -> EventPage:
    if any(value is not None and "\x00" in value for value in (query.category, query.q)):
        raise ProblemError(422, "validation-failed", "Search fields cannot contain NUL")
    if query.sales_from and query.sales_to and query.sales_from > query.sales_to:
        raise ProblemError(422, "validation-failed", "sales_from must be at or before sales_to")
    digest = query.digest()
    if query.cursor is None:
        snapshot = await _materialize(conn, actor, query, digest)
        after = 0
    else:
        snapshot_id, after = decode_cursor(query.cursor, actor, digest, secret)
        snapshot = await conn.fetchrow(
            "SELECT *, expires_at <= app_now() AS expired FROM list_snapshots WHERE id = $1",
            snapshot_id,
        )
        if snapshot is None or snapshot["expired"]:
            raise ProblemError(410, "cursor-expired", "Start a new list. This cursor has expired")
        if snapshot["principal_id"] != actor.user_id or snapshot["query_hash"] != digest:
            raise _invalid()
    rows = await _read_page(conn, snapshot, after, actor, query.limit)
    items: list[EventOut | UnavailableEvent] = []
    for row in rows:
        if row["id"] is None:
            items.append(UnavailableEvent(id=row["event_id"], unavailable=True))
        else:
            items.append(
                EventOut.model_validate(
                    {
                        **dict(row),
                        "zones": [ZoneInput.model_validate_json(raw) for raw in row["zone_json"]],
                    }
                )
            )
    cursor = None
    if rows and rows[-1]["ord"] < snapshot["row_count"]:
        cursor = encode_cursor(snapshot["id"], rows[-1]["ord"], actor, digest, secret)
    elif query.cursor is None:
        # A single-page result has no continuation to retain or count against the quota.
        await conn.execute(
            "DELETE FROM list_snapshot_rows WHERE created_at = $1 AND snapshot_id = $2",
            snapshot["created_at"],
            snapshot["id"],
        )
        await conn.execute("DELETE FROM list_snapshots WHERE id = $1", snapshot["id"])
    return EventPage(items=items, next_cursor=cursor)
