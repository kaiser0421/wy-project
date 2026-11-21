from __future__ import annotations

import asyncio

import asyncpg
import pytest

from ticketing.core.service import ServiceContext
from ticketing.realtime import relay
from ticketing.realtime.outbox import emit_layout_changed, emit_seat_changes

pytestmark = pytest.mark.integration


async def test_concurrent_writers_preserve_per_seat_order(
    db: asyncpg.Connection, relay_context: ServiceContext
) -> None:
    await db.execute("CREATE TABLE relay_test_seat (idx int PRIMARY KEY, version int NOT NULL)")
    await db.execute("INSERT INTO relay_test_seat SELECT i, 0 FROM generate_series(0, 1) i")
    first_written = asyncio.Event()
    other_committed = asyncio.Event()

    async def writer_a() -> None:
        async with relay_context.db.acquire() as conn, conn.transaction():
            version = await conn.fetchval(
                "UPDATE relay_test_seat SET version = version + 1 WHERE idx = 0 RETURNING version"
            )
            await emit_seat_changes(conn, 1, [(0, "held", version)])
            first_written.set()
            await other_committed.wait()

    async def writer_b() -> None:
        await first_written.wait()
        async with relay_context.db.acquire() as conn:
            async with conn.transaction():
                await emit_seat_changes(conn, 1, [(1, "held", 1)])
                await emit_layout_changed(conn, 2, 3)
            await relay.tick(relay_context)
            other_committed.set()

    async def writer_c() -> None:
        await first_written.wait()
        async with relay_context.db.acquire() as conn, conn.transaction():
            await conn.execute("SET LOCAL lock_timeout='4s'; SET LOCAL statement_timeout='4s'")
            version = await conn.fetchval(
                "UPDATE relay_test_seat SET version = version + 1 WHERE idx = 0 RETURNING version"
            )
            await emit_seat_changes(conn, 1, [(0, "available", version)])

    async with asyncio.timeout(5):
        await asyncio.gather(writer_a(), writer_b(), writer_c())
    await relay.tick(relay_context)
    rows = await db.fetch(
        """
        SELECT o.seq, s.seat_idx, s.seat_version, s.recorded_at = o.created_at AS same_time,
               s.published_at = o.assigned_at AS same_publish
        FROM outbox o JOIN seat_events s USING (event_id, seq)
        ORDER BY o.seq
        """
    )
    assert [tuple(row) for row in rows] == [
        (1, 1, 1, True, True),
        (2, 0, 1, True, True),
        (3, 0, 2, True, True),
    ]
    assert await db.fetchval("SELECT seq FROM outbox WHERE event_id = 2") == 1


async def test_failed_second_batch_leaves_first_committed(
    db: asyncpg.Connection, relay_context: ServiceContext
) -> None:
    await db.execute(
        """
        INSERT INTO outbox (event_id, kind, changes)
        SELECT 1, 'seat_changes', jsonb_build_array(jsonb_build_array(0, 'available', i))
        FROM generate_series(1, $1::int) i
        """,
        relay.BATCH_SIZE + 1,
    )
    await relay.tick(relay_context)
    await db.execute(
        """
        CREATE FUNCTION interrupt_archive() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'test archive failure';
        END
        $$;
        CREATE TRIGGER interrupt_archive BEFORE INSERT ON seat_events
        FOR EACH ROW EXECUTE FUNCTION interrupt_archive();
        """
    )
    with pytest.raises(asyncpg.RaiseError):
        await relay.tick(relay_context)
    assert await db.fetchval("SELECT last_assigned_seq FROM event_stream_state") == relay.BATCH_SIZE
    assert await db.fetchval("SELECT count(*) FROM outbox WHERE seq IS NULL") == 1
    assert await db.fetchval("SELECT count(*) FROM seat_events") == relay.BATCH_SIZE
    await db.execute("DROP TRIGGER interrupt_archive ON seat_events")
    await relay.tick(relay_context)
    assert await db.fetchval("SELECT max(seq) FROM outbox") == relay.BATCH_SIZE + 1
    assert await db.fetchval("SELECT count(*) FROM seat_events") == relay.BATCH_SIZE + 1


async def test_two_relays_only_one_active(
    db: asyncpg.Connection, relay_context: ServiceContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with db.transaction():
        await emit_seat_changes(db, 1, [(0, "available", 1), (1, "available", 1)])
    entered = asyncio.Event()
    release = asyncio.Event()
    assign = relay._assign_batch

    async def blocking_assign(conn: asyncpg.Connection) -> None:
        assert not entered.is_set()
        entered.set()
        await release.wait()
        await assign(conn)

    monkeypatch.setattr(relay, "_assign_batch", blocking_assign)
    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        tasks.create_task(relay.tick(relay_context))
        await entered.wait()
        await relay.tick(relay_context)
        assert await db.fetchval("SELECT seq FROM outbox") is None
        release.set()
    assert await db.fetchval("SELECT seq FROM outbox") == 1
    assert await db.fetchval("SELECT count(*) FROM seat_events") == 2


async def test_maintenance_prunes_only_old_assigned_outbox_rows(
    db: asyncpg.Connection, relay_context: ServiceContext
) -> None:
    from ticketing import maintenance

    await db.execute(
        """
        INSERT INTO outbox (event_id, kind, changes, seq, assigned_at)
        VALUES (1, 'seat_changes', '[]', 1, clock_timestamp() - interval '2 hours'),
               (1, 'seat_changes', '[]', 2, clock_timestamp() - interval '5 minutes');
        INSERT INTO outbox (event_id, kind, changes, created_at)
        VALUES (1, 'seat_changes', '[]', clock_timestamp() - interval '2 hours');
        """
    )
    await maintenance.tick(relay_context)
    remaining = await db.fetch("SELECT seq FROM outbox ORDER BY id")
    # The old delivered row is gone; the recent one and the never-assigned one stay.
    assert [r["seq"] for r in remaining] == [2, None]
