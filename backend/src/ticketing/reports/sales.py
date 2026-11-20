"""Write sales spreadsheets with bounded database and workbook memory."""

from __future__ import annotations

import errno
import hmac
import shutil
import tempfile
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from threading import Event
from time import time
from typing import Any, BinaryIO
from zoneinfo import ZoneInfo

import psycopg
from xlsxwriter import Workbook  # type: ignore[import-untyped]
from xlsxwriter.exceptions import FileCreateError  # type: ignore[import-untyped]
from xlsxwriter.worksheet import Worksheet  # type: ignore[import-untyped]

from ticketing.core.errors import ProblemError, server_busy
from ticketing.core.settings import Settings
from ticketing.events.access import EventActor, can_view
from ticketing.reports.queries import ORDERS, SUMMARY, ZONES

REPORT_LOCK = 0x53414C4553584C53
MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_FETCH_SIZE = 5000
_STALE_AFTER_SECONDS = 86_400


class ExportCancelled(Exception):
    """The client disconnected during generation."""


def prune_stale_exports(root: Path) -> None:
    """Remove abandoned workbooks while allowing active downloads on shared storage."""
    cutoff = time() - _STALE_AFTER_SECONDS
    for directory in root.glob("sales-????????"):
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            if directory.stat().st_mtime < cutoff:
                shutil.rmtree(directory, ignore_errors=True)
        except FileNotFoundError:
            continue


def report_busy() -> ProblemError:
    return ProblemError(
        429,
        "report-busy",
        "Another sales report is in progress",
        retryable=True,
        headers={"Retry-After": "5"},
    )


def _disk_full() -> ProblemError:
    return ProblemError(
        507, "export-space-exhausted", "Insufficient disk space for the sales report"
    )


def _disk_usage(root: Path) -> int:
    size = 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                size += path.stat().st_size
        except FileNotFoundError:
            # A completed download can delete its output while this generation is running.
            continue
    return size


class _Output:
    """Cap ZIP writes and notice cancellation during compression as well as fetching."""

    def __init__(self, stream: BinaryIO, root: Path, quota: int, stop: Event) -> None:
        self.stream = stream
        self.root = root
        self.quota = quota
        self.stop = stop
        self.next_check = 0

    def write(self, data: bytes) -> int:
        if self.stop.is_set():
            raise ExportCancelled
        end = self.stream.tell() + len(data)
        if end > self.next_check:
            self.stream.flush()
            remaining = self.quota - _disk_usage(self.root)
            self.next_check = self.stream.tell() + min(remaining, 256 * 1024)
            if end > self.next_check and len(data) > remaining:
                raise _disk_full()
        return self.stream.write(data)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self.stream.seek(offset, whence)

    def tell(self) -> int:
        return self.stream.tell()

    def flush(self) -> None:
        self.stream.flush()


class _Sheets:
    def __init__(self, book: Workbook, limit: int, root: Path, stop: Event, tz: ZoneInfo) -> None:
        self.book = book
        self.number_format = book.add_format({"num_format": "#,##0"})
        self.date_format = book.add_format({"num_format": "yyyy-mm-dd hh:mm:ss"})
        self.header_format = book.add_format({"bold": True})
        self.limit = limit
        self.root = root
        self.stop = stop
        self.tz = tz

    def checkpoint(self) -> None:
        if self.stop.is_set():
            raise ExportCancelled
        if _disk_usage(self.root) >= self.limit:
            raise _disk_full()

    def check_packaging_space(self, staging: Path) -> None:
        # xlsxwriter assembles worksheet XML from its staged rows before zipping it.
        if _disk_usage(self.root) + _disk_usage(staging) + 16 * 1024 > self.limit:
            raise _disk_full()

    def row(
        self, sheet: Worksheet, index: int, values: Iterable[object], *, header: bool = False
    ) -> None:
        if index >= 1_048_576:
            raise ProblemError(
                422, "report-row-limit", "The report exceeds Excel's worksheet row limit"
            )
        for column, value in enumerate(values):
            if value is None:
                continue
            if isinstance(value, datetime):
                local = value.astimezone(self.tz).replace(tzinfo=None)
                sheet.write_datetime(index, column, local, self.date_format)
            elif isinstance(value, (int, Decimal)):
                sheet.write_number(index, column, float(value), self.number_format)
            else:
                text = str(value)
                if text.startswith(("=", "+", "-", "@", "\t", "\r")):
                    text = "'" + text
                sheet.write_string(index, column, text, self.header_format if header else None)
        if index % 256 == 0:
            self.checkpoint()

    def sheet(self, name: str, columns: Iterable[str]) -> Worksheet:
        sheet = self.book.add_worksheet(name)
        sheet.freeze_panes(1, 0)
        self.row(sheet, 0, columns, header=True)
        return sheet


def _summary(
    conn: psycopg.Connection[tuple[Any, ...]],
    sheets: _Sheets,
    event: tuple[Any, ...],
    generated_at: datetime,
) -> None:
    event_id, owner_id, status, title, description, category, venue, sales_start, sales_end = event
    offset = generated_at.astimezone(sheets.tz).strftime("%z")
    sheet = sheets.sheet("Summary", ("Field", "Value"))
    fields = (
        ("Event ID", event_id),
        ("Title", title),
        ("Description", description),
        ("Category", category),
        ("Venue", venue),
        ("Status", status),
        ("Owner ID", owner_id),
        ("Sales Start", sales_start),
        ("Sales End", sales_end),
        ("Generated At", generated_at),
        ("Timezone", f"All times in {sheets.tz.key} (UTC{offset[:3]}:{offset[3:]} at generation)"),
        ("Zone Revenue", "Paid ticket subtotals exclude service fees"),
    )
    for index, data in enumerate(fields, start=1):
        sheets.row(sheet, index, data)
    sheets.row(
        sheet, 14, ("Status", "Orders", "Subtotal (TWD)", "Fees (TWD)", "Amount (TWD)"), header=True
    )
    totals = dict.fromkeys(("paid", "refund_pending", "refunded"), 0)
    for index, data in enumerate(conn.execute(SUMMARY, (event_id,)), start=15):
        sheets.row(sheet, index, data)
        if data[0] in totals:
            totals[data[0]] = data[-1]
    sheets.row(sheet, 22, ("Revenue (TWD)", totals["paid"]))
    sheets.row(sheet, 23, ("Refund Pending (TWD)", totals["refund_pending"]))
    sheets.row(sheet, 24, ("Refunded (TWD)", totals["refunded"]))


def _zones(conn: psycopg.Connection[tuple[Any, ...]], sheets: _Sheets, event_id: int) -> None:
    sheet = sheets.sheet(
        "Zones",
        (
            "Code",
            "Zone",
            "Price (TWD)",
            "Capacity",
            "Sold",
            "Held",
            "Available",
            "Revenue (TWD)",
        ),
    )
    with conn.cursor(name="report_zones") as cursor:
        cursor.itersize = _FETCH_SIZE
        cursor.execute(ZONES, (event_id, event_id, event_id))
        for index, data in enumerate(cursor, start=1):
            sheets.row(sheet, index, data)


def _orders(
    conn: psycopg.Connection[tuple[Any, ...]],
    sheets: _Sheets,
    event_id: int,
    pseudonymous: bool,
    secret: str,
) -> None:
    tz = sheets.tz.key
    sheet = sheets.sheet(
        "Orders",
        (
            "Order Number",
            f"Created At ({tz})",
            f"Paid At ({tz})",
            "Status",
            "Seats",
            "Quantity",
            "Subtotal (TWD)",
            "Fee (TWD)",
            "Total (TWD)",
            "Buyer",
        ),
    )
    with conn.cursor(name="report_orders") as cursor:
        cursor.itersize = _FETCH_SIZE
        cursor.execute(ORDERS, (pseudonymous, event_id))
        for index, data in enumerate(cursor, start=1):
            values, (user_id, email) = data[:-2], data[-2:]
            buyer = (
                hmac.digest(secret.encode(), f"{event_id}:{user_id}".encode(), "sha256").hex()
                if pseudonymous
                else email
            )
            sheets.row(sheet, index, (*values, buyer))


def _event(
    conn: psycopg.Connection[tuple[Any, ...]],
    actor: EventActor,
    event_id: int,
) -> tuple[Any, ...]:
    event = conn.execute(
        """SELECT id, owner_id, status::text, title, description, category, venue,
                  sales_start_at, sales_end_at FROM events WHERE id = %s""",
        (event_id,),
    ).fetchone()
    if event is None or (actor.role != "viewer" and not can_view(actor, event[1], event[2])):
        raise ProblemError(404, "not-found", "Event not found")
    if actor.role == "user" and event[1] != actor.user_id:
        raise ProblemError(403, "forbidden", "Users may only export reports for their own events")
    return event


def _generate(
    conn: psycopg.Connection[tuple[Any, ...]],
    root: Path,
    path: Path,
    settings: Settings,
    actor: EventActor,
    event_id: int,
    tz: ZoneInfo,
    stop: Event,
) -> None:
    with path.open("w+b") as output:
        quota = settings.export_disk_quota_bytes
        book = Workbook(
            _Output(output, root, quota, stop),
            {"constant_memory": True, "tmpdir": str(path.parent), "strings_to_formulas": False},
        )
        sheets = _Sheets(book, quota, root, stop, tz)
        try:
            sheets.checkpoint()
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                conn.execute("SET LOCAL statement_timeout = '120s'")
                event = _event(conn, actor, event_id)
                generated = conn.execute("SELECT app_now()").fetchone()
                assert generated is not None
                _summary(conn, sheets, event, generated[0])
                _zones(conn, sheets, event_id)
                _orders(
                    conn,
                    sheets,
                    event_id,
                    actor.role == "viewer",
                    settings.secret_key.get_secret_value(),
                )
            sheets.checkpoint()
            sheets.check_packaging_space(path.parent)
            book.close()
            sheets.checkpoint()
        finally:
            # xlsxwriter has no abort method. Close its constant-memory row handles before
            # the executor removes the report directory after a failed generation.
            for sheet in book.worksheets():
                handle = getattr(sheet, "row_data_fh", None)
                if handle is not None and not handle.closed:
                    handle.close()


def generate(
    settings: Settings,
    actor: EventActor,
    event_id: int,
    tz: ZoneInfo,
    stop: Event,
) -> Path:
    """The executor owns partial files and its dedicated connection until the task completes."""
    root = Path(settings.export_dir)
    directory = None
    done = False
    try:
        with psycopg.connect(settings.database_url, autocommit=True, connect_timeout=5) as conn:
            lock = conn.execute("SELECT pg_try_advisory_lock(%s)", (REPORT_LOCK,)).fetchone()
            if not lock or not lock[0]:
                raise report_busy()
            root.mkdir(mode=0o750, parents=True, exist_ok=True)
            directory = Path(tempfile.mkdtemp(prefix="sales-", dir=root))
            path = directory / "sales.xlsx"
            _generate(conn, root, path, settings, actor, event_id, tz, stop)
            if stop.is_set():
                raise ExportCancelled
        done = True
        return path
    except psycopg.OperationalError as exc:
        raise server_busy("Sales report database connection unavailable") from exc
    except FileCreateError as exc:
        cause = exc.__cause__ or (exc.args[0] if exc.args else None)
        if isinstance(cause, OSError) and cause.errno in (errno.ENOSPC, errno.EDQUOT):
            raise _disk_full() from exc
        raise
    except OSError as exc:
        if exc.errno in (errno.ENOSPC, errno.EDQUOT):
            raise _disk_full() from exc
        raise
    finally:
        if directory is not None and not done:
            shutil.rmtree(directory, ignore_errors=True)
