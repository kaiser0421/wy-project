import asyncio
import csv
from datetime import timedelta
from io import StringIO
from pathlib import Path

import asyncpg
import pytest
from anyio import to_thread

from tests.events.helpers import EVENTS, seed_seat
from tests.imports.helpers import draft, upload
from tests.support import create_user, login
from ticketing.imports.runner import LeaseLost, claim, finalize, stage, tick

pytestmark = pytest.mark.integration
HEADER = "zone_code,row_label,seat_number\n"
RECORDS = HEADER + "a,R,02\nA,r,2\nX,R,3\nA,=SUM(1),4\nA,R,01\nA,R,5\n"


@pytest.mark.parametrize("mode", ["all_or_nothing", "skip_invalid"])
async def test_sql_validation_and_error_csv(db, client, worker, mode):
    owner = await create_user(db, "owner@example.com")
    event = await draft(db, owner)
    await seed_seat(db, event, await db.fetchval("SELECT id FROM zones WHERE event_id = $1", event))
    session = await login(client, "owner@example.com")
    created = await upload(session, event, RECORDS, mode=mode)
    assert created.status_code == 202, created.text
    path = created.headers["location"]
    uploaded = await db.fetchval("SELECT file_path FROM import_jobs WHERE event_id = $1", event)
    assert await to_thread.run_sync(Path(uploaded).read_text) == RECORDS
    await tick(worker)
    job = (await client.get(path)).json()
    assert job["status"] == (
        "partially_succeeded" if mode == "skip_invalid" else "validation_failed"
    )
    assert (
        job["processed_rows"],
        job["valid_rows"],
        job["error_rows"],
        job["progress_percent"],
    ) == (6, 1, 5, 100)
    response = await client.get(f"{path}/errors.csv")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    rows = list(csv.DictReader(StringIO(response.text)))
    assert [(row["line"], row["reason"]) for row in rows] == [
        ("2", "duplicate_in_file"),
        ("3", "duplicate_in_file"),
        ("4", "unknown_zone"),
        ("5", "invalid_format"),
        ("6", "duplicate_existing"),
    ]
    assert rows[3]["row_label"] == "'=SUM(1)"
    seats = await db.fetch("SELECT seat_idx, seat_number FROM seats ORDER BY seat_idx")
    expected = [(0, "1"), (1, "5")] if mode == "skip_invalid" else [(0, "1")]
    assert [tuple(seat) for seat in seats] == expected
    assert await db.fetchval("SELECT count(*) FROM import_staging") == 0
    outbox = await db.fetch("SELECT kind, request_id FROM outbox WHERE event_id = $1", event)
    if mode == "skip_invalid":
        assert [tuple(row) for row in outbox] == [
            ("layout_changed", created.headers["x-request-id"])
        ]
        assert await db.fetchval("SELECT layout_ver FROM events WHERE id = $1", event) == 2
    else:
        assert not outbox
    assert not await to_thread.run_sync(Path(uploaded).exists)


async def test_import_authorization_matrix(db, make_client, worker):
    owner = await create_user(db, "owner@example.com")
    event = await draft(db, owner)
    async with make_client() as client:
        owner_session = await login(client, "owner@example.com")
        created = await upload(owner_session, event, HEADER + "A,R,1\n")
        assert created.status_code == 202, created.text
        path = created.headers["location"]
        await tick(worker)
        terminal_stream = await client.get(f"{path}/stream")
        assert terminal_stream.status_code == 200
        assert "event: progress" in terminal_stream.text
        assert '"status":"succeeded"' in terminal_stream.text
        for role, draft_status, published_status in (
            ("user", 404, 403),
            ("viewer", 403, 403),
            ("admin", 200, 200),
        ):
            await create_user(db, f"{role}@example.com", role)
            async with make_client() as other:
                assert (await other.get(path)).status_code == 401
                assert (await other.get(f"{path}/errors.csv")).status_code == 401
                assert (await other.get(f"{path}/stream")).status_code == 401
                assert (await other.post(f"{EVENTS}/{event}/seat-imports")).status_code == 401
                outsider = await login(other, f"{role}@example.com")
                for status in (draft_status, published_status):
                    assert (await other.get(path)).status_code == status
                    assert (await other.get(f"{path}/errors.csv")).status_code == status
                    assert (await other.get(f"{path}/stream")).status_code == status
                    replay = await upload(outsider, event, HEADER + "A,R,1\n")
                    assert replay.status_code == status
                    await db.execute(
                        "UPDATE events SET status = 'published', "
                        "published_at = app_now() WHERE id = $1",
                        event,
                    )
                await db.execute(
                    "UPDATE events SET status = 'draft', published_at = NULL WHERE id = $1", event
                )
        assert await db.fetchval("SELECT count(*) FROM import_jobs") == 1


async def test_duplicate_upload_and_publish_serialize(db, make_client, worker):
    owner = await create_user(db, "owner@example.com")
    event = await draft(db, owner)
    async with make_client() as one, make_client() as two:
        a = await login(one, "owner@example.com")
        b = await login(two, "owner@example.com")
        content = HEADER + "A,R,1\n"
        left, right = await asyncio.gather(
            upload(a, event, content),
            upload(b, event, content, mode="skip_invalid"),
        )
        assert sorted((left.status_code, right.status_code)) == [200, 202]
        assert left.json() == right.json()
        assert await db.fetchval("SELECT count(*) FROM import_jobs") == 1
        conflict = await upload(a, event, HEADER + "A,R,2\n")
        assert conflict.json()["code"] == "import-running"
        headers = {**a.headers(), "If-Match": '"v1"'}
        assert (await one.post(f"{EVENTS}/{event}/publish", headers=headers)).json()[
            "code"
        ] == "import-running"
        await tick(worker)
        published = await one.post(f"{EVENTS}/{event}/publish", headers=headers)
        assert published.status_code == 200, published.text
        assert (await upload(a, event, content)).status_code == 200
        assert (await upload(a, event, HEADER + "A,R,2\n")).json()["code"] == "event-not-draft"


async def test_upload_and_publish_race_with_existing_seats(db, make_client):
    owner = await create_user(db, "owner@example.com")
    event = await draft(db, owner)
    await seed_seat(db, event, await db.fetchval("SELECT id FROM zones WHERE event_id = $1", event))
    async with make_client() as one, make_client() as two:
        a = await login(one, "owner@example.com")
        b = await login(two, "owner@example.com")
        admitted, published = await asyncio.gather(
            upload(a, event, HEADER + "A,R,2\n"),
            two.post(f"{EVENTS}/{event}/publish", headers={**b.headers(), "If-Match": '"v1"'}),
        )
    if published.status_code == 200:
        assert admitted.json()["code"] == "event-not-draft"
        assert await db.fetchval("SELECT count(*) FROM import_jobs") == 0
    else:
        assert published.json()["code"] == "import-running"
        assert admitted.status_code == 202
        assert await db.fetchval("SELECT status FROM events WHERE id = $1", event) == "draft"


async def test_stale_worker_cannot_stage_or_finalize(db, db_url, client, worker, test_clock):
    owner = await create_user(db, "owner@example.com")
    event = await draft(db, owner)
    session = await login(client, "owner@example.com")
    pending = await upload(session, event, HEADER + "A,R,1\n")
    assert pending.status_code == 202, pending.text
    old = await claim(db)
    await stage(db, old, [(2, None, "A", "R", "1", None)], 1)
    await test_clock.advance(timedelta(seconds=31))
    contender = await asyncpg.connect(db_url)
    try:
        new = await claim(contender)
        assert new["attempt_no"] == 2
        assert new["fence"] != old["fence"]
        assert await db.fetchval("SELECT count(*) FROM import_staging") == 0
        with pytest.raises(LeaseLost):
            await stage(db, old, [(2, None, "A", "R", "1", None)], 1)
        with pytest.raises(LeaseLost):
            await finalize(db, old, 1)
        assert await db.fetchval("SELECT count(*) FROM seats") == 0
        await test_clock.advance(timedelta(seconds=31))
        await tick(worker)
    finally:
        await contender.close()
    assert await db.fetchval("SELECT count(*) FROM seats") == 1
    assert await db.fetchval("SELECT count(*) FROM outbox") == 1


async def test_expired_third_attempt_becomes_terminal(db, client, worker, test_clock):
    owner = await create_user(db, "owner@example.com")
    event = await draft(db, owner)
    session = await login(client, "owner@example.com")
    pending = await upload(session, event, HEADER + "A,R,1\n")
    for number in range(1, 4):
        assert (await claim(db))["attempt_no"] == number
        await test_clock.advance(timedelta(seconds=31))
    await tick(worker)
    job = (await client.get(pending.headers["location"])).json()
    assert (job["status"], job["failure_reason"]) == ("system_failed", "attempts_exhausted")


async def test_disable_creator_after_staging_cancels_finalize(db, db_url, client):
    owner = await create_user(db, "owner@example.com")
    event = await draft(db, owner)
    session = await login(client, "owner@example.com")
    pending = await upload(session, event, HEADER + "A,R,1\n")
    assert pending.status_code == 202
    job = await claim(db)
    await stage(db, job, [(2, None, "A", "R", "1", None)], 1)
    other = await asyncpg.connect(db_url)
    try:
        await other.execute("UPDATE users SET is_active = false WHERE id = $1", owner)
        await finalize(db, job, 1)
    finally:
        await other.close()
    result = await db.fetchrow("SELECT status, failure_reason FROM import_jobs")
    assert tuple(result) == ("cancelled", "creator_disabled")
    assert await db.fetchval("SELECT count(*) FROM seats") == 0


@pytest.mark.parametrize(
    "settings_overrides", [{"import_max_bytes": 100, "import_max_rows": 2}], indirect=True
)
async def test_upload_byte_cap_and_worker_row_cap(db, client, worker, settings_overrides):
    # This test replaces the normal fixture's limits but still uses a test-owned upload directory.
    owner = await create_user(db, "owner@example.com")
    event = await draft(db, owner)
    session = await login(client, "owner@example.com")
    too_big = await upload(session, event, HEADER + "x" * 100)
    assert too_big.status_code == 413, too_big.text
    assert await db.fetchval("SELECT count(*) FROM import_jobs") == 0
    pending = await upload(session, event, HEADER + "A,R,1\nA,R,2\nA,R,3\n")
    assert pending.status_code == 202
    await tick(worker)
    job = (await client.get(pending.headers["location"])).json()
    assert (job["status"], job["failure_reason"]) == ("validation_failed", "too_many_rows")
    assert await db.fetchval("SELECT line FROM import_errors") == 4
    assert await db.fetchval("SELECT count(*) FROM seats") == 0
