"""Seed and update a dedicated Scenario B event on a disposable database."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
from pathlib import Path
from uuid import uuid4

import asyncpg
from argon2 import PasswordHasher

from ticketing.auth.sessions import create_session
from ticketing.core.settings import get_settings
from ticketing.realtime.outbox import emit_seat_changes

CATEGORY = "SSE load fixture"


def _private_file(path: str, flags: int) -> int:
    return os.open(path, flags, mode=0o600)


async def seed(conn: asyncpg.Connection, args: argparse.Namespace) -> None:
    settings = get_settings()
    tokens: list[str] = []
    password_hash = PasswordHasher().hash(secrets.token_urlsafe(32))
    # Create the file exclusively to avoid replacing an existing credential set.
    path = Path(args.sessions_file)
    # Reserve a small local credentials file before starting the transaction.
    with open(path, "x", encoding="utf-8", opener=_private_file) as destination:  # noqa: ASYNC230
        async with conn.transaction():
            owner = await conn.fetchval(
                """
                INSERT INTO users(email, password_hash, role) VALUES($1, $2, 'user')
                RETURNING id
                """,
                f"sse-load-{uuid4().hex}@example.test",
                password_hash,
            )
            event_id = await conn.fetchval(
                """
                INSERT INTO events (owner_id, title, category, venue, status, published_at,
                                    sales_start_at, sales_end_at)
                VALUES ($1, 'Scenario B', $2, 'Load fixture', 'published', now(),
                        now(), now() + interval '1 day')
                RETURNING id
                """,
                owner,
                CATEGORY,
            )
            zone = await conn.fetchval(
                """
                INSERT INTO zones (event_id, code, name, price_twd)
                VALUES ($1, 'A', 'Floor', 1000) RETURNING id
                """,
                event_id,
            )
            await conn.execute(
                """
                INSERT INTO seats (event_id, zone_id, row_label, seat_number, norm_key,
                                   sort_key, seat_idx)
                SELECT $1, $2, (i / 100)::text, (i % 100 + 1)::text, i::text, i, i
                FROM generate_series(0, $3::int - 1) i
                """,
                event_id,
                zone,
                args.seats,
            )
            # A synthetic order satisfies the held-seat FK. The scenario measures seat transport.
            await conn.execute(
                "INSERT INTO orders (event_id, user_id, quantity) VALUES ($1, $2, 1)",
                event_id,
                owner,
            )
            for _ in range(args.connections):
                _, token = await create_session(conn, owner, settings, None, "scenario-b")
                tokens.append(token)
        json.dump(tokens, destination)
    print(json.dumps({"event_id": event_id, "seats": args.seats, "sessions": len(tokens)}))


async def write(conn: asyncpg.Connection, args: argparse.Namespace) -> None:
    fixture = await conn.fetchrow(
        """
        SELECT o.id AS order_id, (SELECT count(*) FROM seats WHERE event_id=e.id) AS seats
        FROM events e JOIN orders o ON o.event_id=e.id AND o.user_id=e.owner_id
        WHERE e.id=$1 AND e.category=$2 AND e.title='Scenario B'
        ORDER BY o.id LIMIT 1
        """,
        args.event_id,
        CATEGORY,
    )
    if fixture is None:
        raise SystemExit("The event must be seeded by this script")
    workers = args.workers
    if fixture["seats"] < args.batch * workers:
        raise SystemExit("The fixture must have at least batch times workers seats")
    loop = asyncio.get_running_loop()
    start = loop.time() + args.start_delay

    async def worker(index: int) -> int:
        low = fixture["seats"] * index // workers
        high = fixture["seats"] * (index + 1) // workers
        # Offset each lane to spread transaction starts across the rate interval.
        lane_start = start + index * args.batch / args.rate
        conn = await asyncpg.connect(get_settings().database_url)
        try:
            return await generate(conn, args, fixture["order_id"], low, high, lane_start)
        finally:
            await conn.close()

    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(worker(i)) for i in range(workers)]
    written = sum(task.result() for task in tasks)
    elapsed = loop.time() - start
    print(
        json.dumps({"changes": written, "seconds": elapsed, "changes_per_sec": written / elapsed})
    )
    if not written:
        raise SystemExit("Writer committed no seat changes")


async def generate(
    conn: asyncpg.Connection,
    args: argparse.Namespace,
    order_id: int,
    low: int,
    high: int,
    start: float,
) -> int:
    loop = asyncio.get_running_loop()
    await asyncio.sleep(max(0, start - loop.time()))
    written = 0
    while loop.time() - start < args.duration:
        indices = [low + (written + offset) % (high - low) for offset in range(args.batch)]
        async with conn.transaction():
            rows = await conn.fetch(
                """
                UPDATE seats
                SET status=CASE WHEN status='available' THEN 'held'::seat_status
                                ELSE 'available'::seat_status END,
                    order_id=CASE WHEN status='available' THEN $3::bigint ELSE NULL END,
                    version=version+1, updated_at=app_now()
                WHERE event_id=$1 AND seat_idx=ANY($2::int[])
                  AND (status='available' OR (status='held' AND order_id=$3))
                RETURNING seat_idx, status, version
                """,
                args.event_id,
                indices,
                order_id,
            )
            if len(rows) != len(indices):
                raise RuntimeError("Writer did not update all targeted fixture seats")
            await emit_seat_changes(
                conn,
                args.event_id,
                [(row["seat_idx"], row["status"], row["version"]) for row in rows],
            )
        written += len(rows)
        await asyncio.sleep(max(0, start + written * args.workers / args.rate - loop.time()))
    return written


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    setup = subcommands.add_parser("seed")
    setup.add_argument("--sessions-file", required=True)
    setup.add_argument("--connections", type=int, default=2000)
    setup.add_argument("--seats", type=int, default=10_000)
    run = subcommands.add_parser("write")
    run.add_argument("--event-id", type=int, required=True)
    run.add_argument("--rate", type=float, default=300)
    run.add_argument("--duration", type=float, default=60)
    run.add_argument("--start-delay", type=float, default=10)
    run.add_argument("--workers", type=int, default=4)
    run.add_argument("--batch", type=int, choices=range(1, 5), default=4)
    args = parser.parse_args()
    if args.command == "seed" and min(args.connections, args.seats) < 1:
        parser.error("connections and seats must be positive")
    if args.command == "write" and (
        min(args.rate, args.duration, args.workers) <= 0 or args.start_delay < 0
    ):
        parser.error("rate and duration must be positive and start delay must be nonnegative")
    return args


async def main() -> None:
    args = parse_args()
    if "DATABASE_URL" not in os.environ:
        raise SystemExit("Set DATABASE_URL to a disposable migrated database")
    conn = await asyncpg.connect(get_settings().database_url)
    try:
        await (seed(conn, args) if args.command == "seed" else write(conn, args))
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
