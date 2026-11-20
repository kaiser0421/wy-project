"""Factories for workstream event integration tests."""

import asyncpg

EVENTS = "/api/v1/events"
BODY = {
    "title": "Summer Live",
    "description": "Concert",
    "category": "music",
    "venue": "Taipei Arena",
    "sales_start_at": "2030-05-01T00:00:00Z",
    "sales_end_at": "2030-06-01T00:00:00Z",
    "zones": [{"code": "A", "name": "Floor", "price_twd": 1200}],
}


async def seed_event(
    db: asyncpg.Connection,
    owner: int,
    *,
    published: bool = False,
    title: str = "Summer Live",
) -> int:
    return await db.fetchval(
        """INSERT INTO events (owner_id, title, category, venue, sales_start_at,
                               sales_end_at, status, published_at)
           VALUES ($1, $2, 'music', 'Taipei Arena', '2030-05-01T00:00Z', '2030-06-01T00:00Z',
                   CASE WHEN $3 THEN 'published'::event_status ELSE 'draft'::event_status END,
                   CASE WHEN $3 THEN app_now() END)
           RETURNING id""",
        owner,
        title,
        published,
    )


async def seed_zone(db: asyncpg.Connection, event: int, name: str = "Floor") -> int:
    return await db.fetchval(
        "INSERT INTO zones(event_id, code, name, price_twd) VALUES ($1, 'A', $2, 1200) "
        "RETURNING id",
        event,
        name,
    )


async def seed_seat(db: asyncpg.Connection, event: int, zone: int) -> None:
    await db.execute(
        """INSERT INTO seats (event_id, zone_id, row_label, seat_number,
                              norm_key, sort_key, seat_idx)
           VALUES ($1, $2, 'R', '1', 'A|R|1', 0, 0)""",
        event,
        zone,
    )
