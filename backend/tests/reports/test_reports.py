import os
from datetime import datetime
from io import BytesIO
from pathlib import Path
from time import time

import asyncpg
import pytest
from anyio import to_thread
from asgi_lifespan import LifespanManager
from openpyxl import load_workbook

from tests.events.helpers import EVENTS, seed_event, seed_zone
from tests.support import create_user, login
from ticketing.reports.sales import REPORT_LOCK

pytestmark = pytest.mark.integration


def report(event_id: int) -> str:
    return f"{EVENTS}/{event_id}/reports/sales.xlsx"


async def test_report_authorization(db, make_client):
    owner = await create_user(db, "owner@example.com")
    draft = await seed_event(db, owner)
    published = await seed_event(db, owner, published=True)
    async with make_client() as client:
        assert (await client.get(report(published))).status_code == 401
    for role, email, drafts, public in (
        ("user", "other@example.com", 404, 403),
        ("viewer", "viewer@example.com", 200, 200),
        ("admin", "admin@example.com", 200, 200),
        ("user", "owner@example.com", 200, 200),
    ):
        if email != "owner@example.com":
            await create_user(db, email, role)
        async with make_client() as client:
            await login(client, email)
            for event, expected in ((draft, drafts), (published, public)):
                response = await client.get(report(event))
                assert response.status_code == expected, response.text[:300]
            assert (await client.get(report(999))).status_code == 404


async def _seed_orders(db, event, zone, count):
    async with db.transaction():
        await db.execute(
            """CREATE TEMP TABLE report_seed ON COMMIT DROP AS
               SELECT g, nextval('users_id_seq') AS buyer, nextval('orders_id_seq') AS ord,
                      nextval('seats_id_seq') AS seat,
                      CASE WHEN g % 2 = 1 THEN 'paid' ELSE 'refunded' END::order_status AS status,
                      '2025-03-09T06:59:59Z'::timestamptz + (1 - g % 2) * interval '1 second' AS ts
               FROM generate_series(1, $1::int) g""",
            count,
        )
        await db.execute(
            """INSERT INTO users(id, email, password_hash)
               SELECT buyer, '=buyer-' || buyer || '@example.com', 'fixture' FROM report_seed"""
        )
        await db.execute(
            """INSERT INTO orders(id, user_id, event_id, status, quantity, subtotal_twd, fee_twd,
                                  total_twd, created_at, paid_at, closed_at, expires_at)
               SELECT ord, buyer, $1, status, 1, 1200, 60, 1260, ts, ts,
                      CASE WHEN status='refunded' THEN ts + interval '1 minute' END,
                      ts + interval '5 minutes'
               FROM report_seed""",
            event,
        )
        await db.execute(
            """INSERT INTO seats(id, event_id, zone_id, row_label, seat_number, norm_key, sort_key,
                                 seat_idx, status, order_id)
               SELECT seat, $1, $2, 'R', g::text, 'A|R|' || g, g - 1, g - 1,
                      CASE WHEN status='paid' THEN 'sold' ELSE 'available' END::seat_status,
                      CASE WHEN status='paid' THEN ord END
               FROM report_seed""",
            event,
            zone,
        )
        await db.execute(
            """INSERT INTO order_items(order_id, seat_id, event_id, zone_id,
                                       unit_price_twd, released_at)
               SELECT ord, seat, $1, $2, 1200,
                      CASE WHEN status = 'refunded' THEN ts + interval '1 minute' END
               FROM report_seed""",
            event,
            zone,
        )
        await db.execute(
            """INSERT INTO user_event_quota(user_id, event_id, reserved)
               SELECT buyer, $1, 1 FROM report_seed WHERE status = 'paid'""",
            event,
        )


async def test_excel_reconciles_and_respects_dst(db, client, settings):
    owner = await create_user(db, "owner@example.com")
    await create_user(db, "viewer@example.com", "viewer")
    event = await seed_event(db, owner, published=True, title="=SUM(1)")
    zone = await seed_zone(db, event, name="+Floor")
    await _seed_orders(db, event, zone, 3000)
    await login(client, "owner@example.com")
    response = await client.get(report(event), params={"tz": "America/New_York"})
    assert response.status_code == 200, response.text[:300]
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "private, no-store"
    workbook = load_workbook(BytesIO(response.content), read_only=True)
    try:
        assert workbook.sheetnames == ["Summary", "Zones", "Orders"]
        summary = list(workbook["Summary"].values)
        assert summary[2][1] == "'=SUM(1)"
        assert "America/New_York" in summary[11][1]
        assert summary[22][:2] == ("Revenue (TWD)", 1500 * 1260)
        assert summary[24][:2] == ("Refunded (TWD)", 1500 * 1260)
        assert list(workbook["Zones"].values)[1] == (
            "A",
            "'+Floor",
            1200,
            3000,
            1500,
            0,
            1500,
            1500 * 1200,
        )
        rows = workbook["Orders"].iter_rows()
        headers, first, second = next(rows), next(rows), next(rows)
        assert headers[1].value == "Created At (America/New_York)"
        assert headers[2].value == "Paid At (America/New_York)"
        assert first[1].value == datetime(2025, 3, 9, 1, 59, 59)
        assert second[1].value == datetime(2025, 3, 9, 3)
        assert first[9].value.startswith("'=buyer-")
        assert first[9].data_type == "s"
        assert all(
            first[col].data_type == "n" and first[col].number_format == "#,##0"
            for col in (5, 6, 7, 8)
        )
        all_totals = [first[8].value, second[8].value, *(row[8].value for row in rows)]
        assert len(all_totals) == 3000
        assert sum(all_totals) == summary[22][1] + summary[24][1]
    finally:
        workbook.close()

    assert await to_thread.run_sync(lambda: list(Path(settings.export_dir).iterdir())) == []


@pytest.mark.parametrize("role", ["viewer", "admin", "user"])
async def test_excel_buyer_email_visibility(db, client, role):
    owner = await create_user(db, "owner@example.com")
    event = await seed_event(db, owner)
    zone = await seed_zone(db, event)
    await _seed_orders(db, event, zone, 2)
    email = "owner@example.com"
    if role != "user":
        email = f"{role}@example.com"
        await create_user(db, email, role)
    await login(client, email)
    response = await client.get(report(event))
    assert response.status_code == 200, response.text[:300]
    workbook = load_workbook(BytesIO(response.content), read_only=True)
    try:
        headers, *rows = workbook["Orders"].values
        assert headers[1] == "Created At (Asia/Taipei)"
        assert headers[9] == "Buyer"
        assert len(rows) == 2
        if role == "viewer":
            assert all(row[9] is None for row in rows)
        else:
            assert all(row[9].startswith("'=buyer-") for row in rows)
    finally:
        workbook.close()


async def test_advisory_lock_rejects_concurrent_export(db, db_url, client):
    owner = await create_user(db, "owner@example.com")
    event = await seed_event(db, owner)
    await login(client, "owner@example.com")
    assert (await client.get(report(event), params={"tz": "invalid/tz"})).json()[
        "code"
    ] == "invalid-timezone"
    contender = await asyncpg.connect(db_url)
    try:
        await contender.execute("SELECT pg_advisory_lock($1)", REPORT_LOCK)
        busy = await client.get(report(event))
        assert busy.status_code == 429
        assert busy.json()["code"] == "report-busy"
        assert "retry-after" in busy.headers
    finally:
        await contender.close()
    assert (await client.get(report(event))).status_code == 200


@pytest.mark.parametrize("settings_overrides", [{"export_disk_quota_bytes": 32}], indirect=True)
async def test_report_disk_quota_cleans_temporary_files(db, client, settings):
    owner = await create_user(db, "owner@example.com")
    event = await seed_event(db, owner)
    await login(client, "owner@example.com")
    result = await client.get(report(event))
    assert result.status_code == 507, result.text
    assert result.json()["code"] == "export-space-exhausted"
    assert await to_thread.run_sync(lambda: list(Path(settings.export_dir).iterdir())) == []


async def test_startup_prunes_only_stale_report_directories(asgi_app, settings):
    root = Path(settings.export_dir)
    stale, fresh, unrelated = (
        root / name for name in ("sales-12345678", "sales-87654321", "other")
    )

    def seed() -> None:
        old = time() - 2 * 86_400
        for directory in (stale, fresh, unrelated):
            directory.mkdir(parents=True)
            (directory / "sales.xlsx").write_bytes(b"fixture")
        for directory in (stale, unrelated):
            os.utime(directory, (old, old))

    await to_thread.run_sync(seed)
    async with LifespanManager(asgi_app):
        assert set(await to_thread.run_sync(lambda: list(root.iterdir()))) == {fresh, unrelated}
