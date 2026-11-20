"""Catalog assertions for the hand-written SQL schema (replaces `alembic check`).

Each assertion protects an invariant the application relies on; dropping one of these objects
must fail the build.
"""

from __future__ import annotations

from datetime import timedelta

import asyncpg
import pytest

from tests.conftest import run_alembic

pytestmark = pytest.mark.integration


async def _index_def(db: asyncpg.Connection, name: str) -> str | None:
    value: str | None = await db.fetchval(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND indexname = $1", name
    )
    return value


@pytest.mark.parametrize(
    ("index", "fragment"),
    [
        ("order_items_one_active_claim", "WHERE (released_at IS NULL)"),
        ("one_chargeable_attempt", "WHERE (status = ANY"),
        ("one_accepted_capture", "WHERE (disposition = 'accepted'"),
        ("one_active_import", "WHERE (status = ANY"),
        ("seats_alloc", "WHERE (status = 'available'"),
        ("outbox_unassigned", "WHERE (seq IS NULL)"),
    ],
)
async def test_partial_unique_and_partial_indexes(
    db: asyncpg.Connection, index: str, fragment: str
) -> None:
    definition = await _index_def(db, index)
    assert definition is not None, f"missing index {index}"
    assert fragment in definition


@pytest.mark.parametrize(
    ("table", "fragment"),
    [
        ("user_event_quota", "reserved >= 0"),
        ("user_event_quota", "reserved <= 4"),
        ("orders", "quantity >= 1"),
        ("seats", "status = 'available'::seat_status) = (order_id IS NULL)"),
        ("orders", "total_twd = (subtotal_twd + fee_twd)"),
    ],
)
async def test_check_constraints(db: asyncpg.Connection, table: str, fragment: str) -> None:
    defs = await db.fetch(
        "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint "
        "WHERE conrelid = $1::regclass AND contype = 'c'",
        table,
    )
    assert any(fragment in row["d"] for row in defs), (table, [r["d"] for r in defs])


async def test_app_now_uses_clock_timestamp(db: asyncpg.Connection) -> None:
    body = await db.fetchval("SELECT prosrc FROM pg_proc WHERE proname = 'app_now'")
    assert "clock_timestamp()" in body
    t1 = await db.fetchval("SELECT app_now()")
    t2 = await db.fetchval("SELECT app_now()")
    assert t2 >= t1


@pytest.mark.parametrize(
    "parent",
    [
        "seat_events",
        "metrics_1s",
        "business_1s",
        "access_logs",
        "service_logs",
        "list_snapshot_rows",
    ],
)
async def test_partitioned_tables_have_current_partitions(
    db: asyncpg.Connection, parent: str
) -> None:
    kind = await db.fetchval("SELECT relkind FROM pg_class WHERE relname = $1", parent)
    assert kind == b"p" or kind == "p"
    covering = await db.fetchval(
        """
        SELECT count(*) FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
        WHERE i.inhparent = $1::regclass
          AND pg_get_expr(c.relpartbound, c.oid) LIKE 'FOR VALUES FROM%'
        """,
        parent,
    )
    assert covering >= 4
    # A row stamped "now" must be insertable (a partition covers the current time).
    created = await db.fetchval(
        "SELECT ensure_range_partitions($1, $2, 0, 0)",
        parent,
        timedelta(hours=1) if parent == "list_snapshot_rows" else timedelta(days=1),
    )
    assert created == 0


async def test_track_commit_timestamp_enabled(db: asyncpg.Connection) -> None:
    assert await db.fetchval("SHOW track_commit_timestamp") == "on"


async def test_migration_round_trip(db_url: str) -> None:
    run_alembic(db_url, "downgrade", "base")
    run_alembic(db_url, "upgrade", "head")
    conn = await asyncpg.connect(db_url)
    try:
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == "0001_baseline"
        assert await conn.fetchval("SELECT to_regclass('order_items')") is not None
    finally:
        await conn.close()
