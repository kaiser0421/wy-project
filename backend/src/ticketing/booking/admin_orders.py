"""Read-only order summaries across all buyers."""

from datetime import UTC, datetime, timedelta
from struct import Struct
from typing import Annotated
from uuid import UUID

import asyncpg
from pydantic import AwareDatetime, BaseModel, Field

from ticketing.booking.models import BIGINT_MAX, OrderStatus
from ticketing.core.cursors import open_cursor, seal_cursor
from ticketing.core.errors import ProblemError

_CURSOR = Struct(">qQQ")
_DOMAIN = b"admin-order-list-v1\0"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)


class AdminOrderQuery(BaseModel):
    event_id: Annotated[int | None, Field(gt=0, le=BIGINT_MAX, description="Booked event ID.")] = (
        None
    )
    status: Annotated[OrderStatus | None, Field(description="Current order status.")] = None
    buyer_email: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=254,
            pattern=r"^[^\x00]+$",
            description="Literal, case-insensitive buyer email prefix.",
            examples=["buyer@"],
        ),
    ] = None
    created_from: Annotated[
        AwareDatetime | None, Field(description="Earliest creation time, inclusive.")
    ] = None
    created_to: Annotated[
        AwareDatetime | None, Field(description="Latest creation time, inclusive.")
    ] = None
    cursor: Annotated[
        str | None, Field(max_length=256, description="Signed cursor from the preceding page.")
    ] = None
    limit: Annotated[int, Field(ge=1, le=100, description="Maximum page size.")] = 50


class AdminOrderSummary(BaseModel):
    public_id: Annotated[UUID, Field(description="Public order ID.")]
    event_id: int
    event_title: str
    buyer_email: str
    status: OrderStatus
    quantity: int
    total_twd: Annotated[int | None, Field(description="Amount due including fees, in whole TWD.")]
    created_at: datetime
    expires_at: Annotated[datetime | None, Field(description="Payment deadline, if recorded.")]


class AdminOrderPage(BaseModel):
    items: list[AdminOrderSummary]
    next_cursor: Annotated[str | None, Field(description="Cursor for the next page, if any.")]


async def admin_order_page(
    conn: asyncpg.Connection, query: AdminOrderQuery, admin_id: int, secret: str
) -> AdminOrderPage:
    if (
        query.created_from is not None
        and query.created_to is not None
        and query.created_from > query.created_to
    ):
        raise ProblemError(
            422,
            "validation-failed",
            "Invalid creation time range",
            "created_from must be at or before created_to.",
        )

    scope = (
        admin_id.to_bytes(8, "big") + query.model_dump_json(exclude={"cursor", "limit"}).encode()
    )
    after_time = after_id = high_id = None
    if query.cursor is not None:
        body = open_cursor(query.cursor, _CURSOR.size, secret, _DOMAIN, scope)
        micros, after_id, high_id = _CURSOR.unpack(body)
        after_time = _EPOCH + timedelta(microseconds=micros)

    # Capture the insertion watermark in the same statement as the first page.
    # Eligibility and status still reflect the database at the time of each read.
    rows = await conn.fetch(
        """WITH boundary AS (
               SELECT coalesce($8::bigint, (SELECT max(id) FROM orders)) AS high_id
           )
           SELECT o.public_id, o.event_id, e.title AS event_title, u.email AS buyer_email,
                  o.status, o.quantity, o.total_twd, o.created_at, o.expires_at, o.id,
                  (SELECT high_id FROM boundary) AS high_id
           FROM orders o
           JOIN events e ON e.id = o.event_id
           JOIN users u ON u.id = o.user_id
           WHERE ($1::bigint IS NULL OR o.event_id = $1)
             AND ($2::order_status IS NULL OR o.status = $2)
             AND ($3::text IS NULL OR starts_with(lower(u.email::text), lower($3)))
             AND o.created_at >= coalesce($4::timestamptz, '-infinity')
             AND o.created_at <= coalesce($5::timestamptz, 'infinity')
             AND (o.created_at, o.id) < (coalesce($6::timestamptz, 'infinity'),
                                        coalesce($7::bigint, 9223372036854775807))
             AND o.id <= (SELECT high_id FROM boundary)
           ORDER BY o.created_at DESC, o.id DESC
           LIMIT $9""",
        query.event_id,
        query.status,
        query.buyer_email,
        query.created_from,
        query.created_to,
        after_time,
        after_id,
        high_id,
        query.limit + 1,
    )
    page = rows[: query.limit]
    next_cursor = None
    if len(rows) > query.limit:
        last = page[-1]
        micros = (last["created_at"] - _EPOCH) // _MICROSECOND
        body = _CURSOR.pack(micros, last["id"], last["high_id"])
        next_cursor = seal_cursor(body, secret, _DOMAIN, scope)
    return AdminOrderPage(
        items=[AdminOrderSummary.model_validate(dict(row)) for row in page],
        next_cursor=next_cursor,
    )
