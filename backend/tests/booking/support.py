"""Seed published inventory independently of the events API."""

import asyncio
from collections.abc import Awaitable, Iterable

import asyncpg

from tests.support import Session
from ticketing.auth.sessions import create_session
from ticketing.auth.tokens import csrf_token


async def race[T](requests: Iterable[Awaitable[T]]) -> list[T]:
    pending = list(requests)
    barrier = asyncio.Barrier(len(pending))

    async def start(request: Awaitable[T]) -> T:
        await barrier.wait()
        return await request

    tasks = [asyncio.create_task(start(request)) for request in pending]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def ticket_users(db, settings, make_client, count):
    async with db.transaction():
        rows = await db.fetch(
            """INSERT INTO users(email,password_hash)
               SELECT 'buyer-' || gen_random_uuid() || '@example.com','unused'
               FROM generate_series(1,$1) RETURNING id""",
            count,
        )
        sessions = []
        for row in rows:
            session_id, token = await create_session(db, row["id"], settings, None, None)
            client = make_client()
            client.cookies.set("sid", token)
            csrf = csrf_token(
                settings.secret_key.get_secret_value(), settings.csrf_key_version, session_id
            )
            sessions.append(Session(client, row["id"], csrf))
    return sessions


async def inventory(db, owner, count, price=2810):
    event = await db.fetchval(
        """INSERT INTO events(owner_id,title,category,venue,sales_start_at,sales_end_at,
                              status,published_at)
           VALUES ($1,'Concert','music','Arena', app_now()-interval '1 hour',
                   app_now()+interval '1 day','published',app_now()) RETURNING id""",
        owner,
    )
    zone = await db.fetchval(
        "INSERT INTO zones(event_id,code,name,price_twd) VALUES($1,'A','Arena',$2) RETURNING id",
        event,
        price,
    )
    rows = await db.fetch(
        """INSERT INTO seats(event_id,zone_id,row_label,seat_number,norm_key,sort_key,seat_idx)
           SELECT $1,$2,'1',(i+1)::text,'A:1:' || (i+1),i,i
           FROM generate_series(0,$3-1) AS i RETURNING id""",
        event,
        zone,
        count,
    )
    return event, [row["id"] for row in rows]


async def book(session, event, key, **body):
    return await session.client.post(
        f"/api/v1/events/{event}/orders",
        headers={**session.headers(), "Idempotency-Key": key},
        json=body,
    )


async def cancel(session, order, key):
    return await session.client.post(
        f"/api/v1/orders/{order}/cancel",
        headers={**session.headers(), "Idempotency-Key": key},
    )


INVARIANTS = """
WITH claims AS (
  SELECT s.id, s.status, s.order_id, count(i.order_id) AS actual,
         count(*) FILTER (WHERE i.order_id=s.order_id AND
              ((s.status='held' AND o.status='pending_payment') OR
               (s.status='sold' AND o.status IN ('paid','refund_pending')))) AS matching
  FROM seats s LEFT JOIN order_items i ON i.seat_id=s.id AND i.released_at IS NULL
  LEFT JOIN orders o ON o.id=i.order_id GROUP BY s.id
), quota AS (
  SELECT user_id,event_id,sum(quantity) AS expected FROM orders
  WHERE status IN ('pending_payment','paid','refund_pending') GROUP BY user_id,event_id
)
SELECT 'seat active claims' AS invariant, id FROM claims
WHERE (status='available' AND actual<>0) OR (status<>'available' AND (actual<>1 OR matching<>1))
UNION ALL
SELECT 'order active items', o.id FROM orders o
LEFT JOIN order_items i ON i.order_id=o.id AND i.released_at IS NULL GROUP BY o.id
HAVING count(i.seat_id) <> CASE WHEN o.status IN ('pending_payment','paid','refund_pending')
                                 THEN o.quantity ELSE 0 END
UNION ALL
SELECT 'user event quota', coalesce(q.user_id,a.user_id) FROM quota q
FULL JOIN user_event_quota a USING (user_id,event_id)
WHERE coalesce(q.expected,0) <> coalesce(a.reserved,0) OR q.expected > 4 OR a.reserved > 4
"""


async def assert_invariants(db: asyncpg.Connection) -> None:
    violations = [dict(row) for row in await db.fetch(INVARIANTS)]
    assert violations == [], f"Booking invariant violations: {violations}"
