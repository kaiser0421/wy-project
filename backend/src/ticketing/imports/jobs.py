"""Seat import admission and parent-scoped job reads."""

from uuid import UUID

import asyncpg

from ticketing.core.context import current_request_id
from ticketing.core.errors import ProblemError
from ticketing.events.access import EventActor, require_owner
from ticketing.events.service import active_import
from ticketing.imports.models import JobOut
from ticketing.imports.upload import Upload


def job_out(row: asyncpg.Record, max_rows: int) -> JobOut:
    if row["status"] == "queued":
        progress = 0
    elif row["status"] == "running":
        if row["total_rows"] is not None:
            progress = 99 if row["file_validated"] else 98
        else:
            progress = min(97, row["processed_rows"] * 100 // max_rows)
    else:
        progress = 100
    return JobOut.model_validate({**dict(row), "progress_percent": progress})


async def admit(
    conn: asyncpg.Connection, event: asyncpg.Record, actor: EventActor, upload: Upload
) -> tuple[asyncpg.Record, bool]:
    existing = await conn.fetchrow(
        """SELECT * FROM import_jobs WHERE event_id = $1 AND content_sha256 = $2 FOR UPDATE""",
        event["id"],
        upload.digest,
    )
    if existing is not None:
        return existing, False
    if event["status"] != "draft":
        raise ProblemError(409, "event-not-draft", "Only draft events accept new imports")
    if await active_import(conn, event["id"]):
        raise ProblemError(409, "import-running", "An import is active")
    row = await conn.fetchrow(
        """INSERT INTO import_jobs (event_id, created_by, content_sha256, format, mode,
                                    file_path, file_bytes, origin_request_id)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING *""",
        event["id"],
        actor.user_id,
        upload.digest,
        upload.format,
        upload.mode,
        str(upload.path),
        upload.size,
        current_request_id(),
    )
    assert row is not None
    return row, True


async def read_job(conn: asyncpg.Connection, actor: EventActor, job_id: UUID) -> asyncpg.Record:
    record = await conn.fetchrow(
        """SELECT j.*, e.owner_id, e.status AS event_status
           FROM import_jobs j JOIN events e ON e.id = j.event_id WHERE j.id = $1""",
        job_id,
    )
    if record is None:
        raise ProblemError(404, "not-found", "Seat import not found")
    require_owner(actor, record["owner_id"], record["event_status"])
    return record


def safe_cell(value: object) -> object:
    if isinstance(value, str) and value.lstrip(" ").startswith(
        ("=", "+", "-", "@", "\t", "\r", "\n")
    ):
        return "'" + value
    return value
