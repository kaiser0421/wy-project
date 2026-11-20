import asyncio
import sys
from pathlib import Path
from time import perf_counter

import pytest

from tests.imports.helpers import draft, upload
from tests.support import create_user, login
from ticketing.imports.runner import tick

pytestmark = [pytest.mark.integration, pytest.mark.slow]
_GENERATOR = Path(__file__).resolve().parents[3] / "scripts" / "gen_seats.py"


@pytest.mark.parametrize("format_", ["csv", "json"])
async def test_import_100k_under_60s(db, client, worker, tmp_path, format_):
    owner = await create_user(db, "owner@example.com")
    event = await draft(db, owner)
    session = await login(client, "owner@example.com")
    source = tmp_path / f"seats.{format_}"
    generator = await asyncio.create_subprocess_exec(
        sys.executable, str(_GENERATOR), "100000", str(source)
    )
    assert await generator.wait() == 0
    start = perf_counter()
    with source.open("rb") as stream:
        pending = await upload(session, event, stream, format_=format_)
    assert pending.status_code == 202, pending.text
    await tick(worker)
    job = (await client.get(pending.headers["location"])).json()
    elapsed = perf_counter() - start
    print(f"{format_.upper()} 100,000 rows from upload to terminal status took {elapsed:.2f}s")
    assert (job["status"], job["valid_rows"]) == ("succeeded", 100000), job
    assert elapsed < 60
    assert await db.fetchval("SELECT count(*) FROM seats WHERE event_id = $1", event) == 100000
