"""Lease-fenced worker for durable seat-import jobs."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Iterator
from functools import partial
from itertools import islice
from pathlib import Path
from time import monotonic, time
from uuid import UUID, uuid4

import asyncpg
from anyio import to_thread

from ticketing.core.context import request_operation
from ticketing.core.db import acquire
from ticketing.core.logging import get_logger
from ticketing.core.service import ServiceContext
from ticketing.imports.parser import InvalidLayout, Row, iter_rows
from ticketing.imports.validation import apply_layout, validate_existing, validate_file

_BATCH_SIZE = 10_000
_LEASE_SECONDS = 30
_MAX_ATTEMPTS = 3
_WORKER = uuid4().hex
_UPLOAD_NAME = re.compile(r"(?:[0-9a-f]{32}\.upload|\.[0-9a-f]{32}\.part)")
_STAGING_COLUMNS = ("fence", "line", "item_index", "zone_code", "row_label", "seat_number", "error")
log = get_logger(__name__)


class LeaseLost(Exception):
    """Another worker owns the job or its event was removed."""


async def claim(conn: asyncpg.Connection) -> asyncpg.Record | None:
    async with conn.transaction():
        job = await conn.fetchrow(
            """SELECT * FROM import_jobs
               WHERE status = 'queued' OR (status = 'running' AND lease_expires_at <= app_now())
               ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED"""
        )
        if job is None:
            return None
        if job["status"] == "running" and await conn.fetchval(
            "SELECT $1::timestamptz > app_now()",
            job["lease_expires_at"],
        ):
            return None
        if job["fence"] is not None:
            await conn.execute("SET LOCAL statement_timeout = '20s'")
            await _clear(conn, job["fence"])
        if job["attempt_no"] >= _MAX_ATTEMPTS:
            return await conn.fetchrow(
                """UPDATE import_jobs SET status = 'system_failed',
                                          failure_reason = 'attempts_exhausted',
                                          lease_expires_at = NULL, finished_at = app_now()
                   WHERE id = $1 AND fence = $2 AND status = 'running'
                     AND lease_expires_at <= app_now() RETURNING *""",
                job["id"],
                job["fence"],
            )
        return await conn.fetchrow(
            """UPDATE import_jobs SET status = 'running', fence = $2, worker_id = $3,
                                      lease_expires_at = app_now() + make_interval(secs => $4),
                                      attempt_no = attempt_no + 1, started_at = app_now(),
                                      processed_rows = 0, total_rows = NULL,
                                      valid_rows = NULL, error_rows = NULL
               WHERE id = $1 AND (status = 'queued' OR
                    (status = 'running' AND fence = $5 AND lease_expires_at <= app_now()))
               RETURNING *""",
            job["id"],
            uuid4(),
            _WORKER,
            _LEASE_SECONDS,
            job["fence"],
        )


async def _clear(conn: asyncpg.Connection, fence: UUID) -> None:
    await conn.execute("DELETE FROM import_staging WHERE fence = $1", fence, timeout=22)


async def _lock_fence(conn: asyncpg.Connection, job: asyncpg.Record) -> None:
    lease = await conn.fetchval(
        """SELECT lease_expires_at FROM import_jobs
           WHERE id = $1 AND fence = $2 AND status = 'running' FOR UPDATE""",
        job["id"],
        job["fence"],
    )
    if lease is None or not await conn.fetchval("SELECT $1::timestamptz > app_now()", lease):
        raise LeaseLost


async def _renew(conn: asyncpg.Connection, job: asyncpg.Record, processed: int) -> None:
    updated = await conn.fetchval(
        """UPDATE import_jobs SET processed_rows = $3,
                                  lease_expires_at = app_now() + make_interval(secs => $4)
           WHERE id = $1 AND fence = $2 AND status = 'running' RETURNING id""",
        job["id"],
        job["fence"],
        processed,
        _LEASE_SECONDS,
    )
    if updated is None:
        raise LeaseLost


async def stage(
    conn: asyncpg.Connection, job: asyncpg.Record, batch: list[Row], processed: int
) -> None:
    async with conn.transaction():
        await _lock_fence(conn, job)
        await conn.execute("SET LOCAL statement_timeout = '8s'")
        await conn.copy_records_to_table(
            "import_staging",
            columns=_STAGING_COLUMNS,
            records=[(job["fence"], *row) for row in batch],
            timeout=10,
        )
        await _renew(conn, job, processed)


async def _finish(
    conn: asyncpg.Connection,
    job: asyncpg.Record,
    status: str,
    *,
    reason: str | None = None,
    total: int | None = None,
    valid: int | None = None,
    errors: int | None = None,
) -> None:
    updated = await conn.fetchval(
        """UPDATE import_jobs SET status = $3, failure_reason = $4, total_rows = $5,
                                  valid_rows = $6, error_rows = $7,
                                  processed_rows = coalesce($5, processed_rows),
                                  finished_at = app_now(), lease_expires_at = NULL
           WHERE id = $1 AND fence = $2 AND status = 'running' RETURNING id""",
        job["id"],
        job["fence"],
        status,
        reason,
        total,
        valid,
        errors,
    )
    if updated is None:
        raise LeaseLost
    await _clear(conn, job["fence"])


async def _record_error(
    conn: asyncpg.Connection,
    job: asyncpg.Record,
    error: InvalidLayout,
) -> None:
    await conn.execute(
        """INSERT INTO import_errors (job_id, attempt_no, line, item_index, reason)
           VALUES ($1, $2, $3, $4, $5)""",
        job["id"],
        job["attempt_no"],
        error.line,
        error.index,
        error.reason,
    )


async def finalize(
    conn: asyncpg.Connection,
    job: asyncpg.Record,
    stat: int | InvalidLayout | None,
) -> None:
    """Check the fence under the event gate before applying any permanent change."""
    async with conn.transaction():
        creator = await conn.fetchrow(
            "SELECT is_active, role FROM users WHERE id = $1 FOR SHARE",
            job["created_by"],
        )
        event = await conn.fetchrow(
            "SELECT owner_id, status FROM events WHERE id = $1 FOR UPDATE",
            job["event_id"],
        )
        if event is None:
            raise LeaseLost
        await _lock_fence(conn, job)
        await conn.execute("SET LOCAL statement_timeout = '20s'")
        if creator is None or not creator["is_active"]:
            await _finish(conn, job, "cancelled", reason="creator_disabled")
            return
        if creator["role"] != "admin" and (
            creator["role"] != "user" or event["owner_id"] != job["created_by"]
        ):
            await _finish(conn, job, "cancelled", reason="creator_forbidden")
            return
        if event["status"] != "draft":
            await _finish(conn, job, "cancelled", reason="event_not_draft")
            return
        if isinstance(stat, InvalidLayout):
            await _record_error(conn, job, stat)
            await _finish(conn, job, "validation_failed", reason=stat.reason, errors=1)
            return
        if stat is None:
            # The early liveness observation raced with re-enabling the creator.
            # Relinquish the fence so the next attempt can parse the complete file.
            await _relinquish(conn, job)
            return
        await validate_existing(conn, job["fence"], job["event_id"])
        bad: int = await conn.fetchval(
            "SELECT count(*) FROM import_staging WHERE fence = $1 AND error IS NOT NULL",
            job["fence"],
        )
        valid = stat - bad
        if bad:
            await conn.execute(
                """INSERT INTO import_errors (job_id, attempt_no, line, item_index,
                                              zone_code, row_label, seat_number, reason)
                   SELECT $1, $2, line, item_index, zone_code, row_label, seat_number, error
                   FROM import_staging WHERE fence = $3 AND error IS NOT NULL""",
                job["id"],
                job["attempt_no"],
                job["fence"],
            )
        if bad and job["mode"] == "all_or_nothing":
            await _finish(
                conn,
                job,
                "validation_failed",
                reason="invalid_rows",
                total=stat,
                valid=valid,
                errors=bad,
            )
            return
        if valid:
            await apply_layout(conn, job)
        await _finish(
            conn,
            job,
            "partially_succeeded" if bad else "succeeded",
            total=stat,
            valid=valid,
            errors=bad,
        )


async def _relinquish(conn: asyncpg.Connection, job: asyncpg.Record) -> None:
    await conn.fetchval(
        """UPDATE import_jobs SET lease_expires_at = app_now()
           WHERE id = $1 AND fence = $2 AND status = 'running' RETURNING id""",
        job["id"],
        job["fence"],
    )


def _next_batch(source: Iterator[Row], limit: int) -> tuple[list[Row], InvalidLayout | None]:
    batch: list[Row] = []
    try:
        batch.extend(islice(source, limit))
    except InvalidLayout as exc:
        return batch, exc
    return batch, None


async def _signal(ctx: ServiceContext, job_id: UUID) -> None:
    try:
        async with asyncio.timeout(0.25):
            await ctx.redis.publish(f"seat-import:{job_id}", "changed")
    except Exception as exc:
        log.warning("import.progress_publish_failed", error=type(exc).__name__)


async def _process(ctx: ServiceContext, conn: asyncpg.Connection, job: asyncpg.Record) -> None:
    source = iter_rows(Path(job["file_path"]), job["format"])
    processed = 0
    limit = min(ctx.settings.import_max_rows, 100_000)
    try:
        while not ctx.stop.is_set():
            active = await conn.fetchval(
                "SELECT is_active FROM users WHERE id = $1", job["created_by"]
            )
            if not active:
                await finalize(conn, job, None)
                return
            batch, invalid = await to_thread.run_sync(
                _next_batch, source, min(_BATCH_SIZE, limit - processed + 1)
            )
            if processed + len(batch) > limit:
                line, index, *_ = batch[limit - processed]
                invalid = InvalidLayout("too_many_rows", line, index)
            if invalid:
                await finalize(conn, job, invalid)
                return
            if not batch:
                break
            processed += len(batch)
            await stage(conn, job, batch, processed)
            await _signal(ctx, job["id"])
        if ctx.stop.is_set():
            await _relinquish(conn, job)
            return
        async with conn.transaction():
            await _lock_fence(conn, job)
            await conn.execute("SET LOCAL statement_timeout = '20s'")
            await validate_file(conn, job["fence"], job["event_id"])
            await _renew(conn, job, processed)
        await finalize(conn, job, processed)
    finally:
        source.close()


def _old_files(folder: str) -> list[str]:
    """Allow ample time for files between fsync and admission to become visible in PostgreSQL."""
    cutoff = time() - 86400
    paths: list[str] = []
    try:
        with os.scandir(Path(folder).resolve()) as entries:
            for entry in entries:
                if _UPLOAD_NAME.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
                    try:
                        if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                            paths.append(entry.path)
                    except FileNotFoundError:
                        continue
                if len(paths) >= 500:
                    break
    except FileNotFoundError:
        pass
    return paths


_next_sweep = 0.0


async def _sweep(ctx: ServiceContext, conn: asyncpg.Connection) -> None:
    global _next_sweep
    if monotonic() < _next_sweep or ctx.stop.is_set():
        return
    _next_sweep = monotonic() + 60
    try:
        async with conn.transaction():
            # Retention must yield to active readers and the next tick can retry.
            await conn.execute("SET LOCAL lock_timeout = '25ms'")
            await conn.execute("SELECT prune_event_snapshot_partitions()")
            await conn.execute(
                """DELETE FROM list_snapshots WHERE id IN (
                       SELECT id FROM list_snapshots
                       WHERE created_at < date_trunc('hour', app_now()) - interval '1 hour'
                         AND expires_at <= app_now()
                       ORDER BY expires_at LIMIT 1000
                   )"""
            )
    except asyncpg.LockNotAvailableError:
        pass
    paths = await to_thread.run_sync(_old_files, ctx.settings.import_dir)
    if paths:
        protected = await conn.fetch(
            """SELECT file_path FROM import_jobs WHERE file_path = ANY($1::text[])
               AND status IN ('queued', 'running')""",
            paths,
        )
        for path in set(paths) - {row["file_path"] for row in protected}:
            await to_thread.run_sync(partial(Path(path).unlink, missing_ok=True))


async def tick(ctx: ServiceContext) -> None:
    """Handle at most one job per tick. A stopped worker leaves a reclaimable job."""
    if ctx.stop.is_set():
        return
    async with acquire(ctx.db, ctx.settings.db_pool_acquire_timeout_s) as conn:
        await _sweep(ctx, conn)
        job = await claim(conn)
        if job is None:
            return
        if job["status"] == "running":
            with request_operation(job["origin_request_id"]):
                log.info("import.job_started", job_id=str(job["id"]), attempt=job["attempt_no"])
                try:
                    await _process(ctx, conn, job)
                except LeaseLost:
                    return
                except Exception as exc:
                    log.warning(
                        "import.attempt_failed", error=type(exc).__name__, job_id=str(job["id"])
                    )
                    await _relinquish(conn, job)
                    return
        await _signal(ctx, job["id"])
        terminal = await conn.fetchval(
            """SELECT status NOT IN ('queued', 'running') FROM import_jobs
               WHERE id = $1 AND fence = $2""",
            job["id"],
            job["fence"],
        )
        if terminal:
            await to_thread.run_sync(lambda: Path(job["file_path"]).unlink(missing_ok=True))
