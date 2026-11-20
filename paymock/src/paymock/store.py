"""Persist provider state and delivery work atomically in a single-replica SQLite store."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from paymock.models import Event, Intent, IntentInput, Outcome, Refund, RefundInput

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
  merchant_id TEXT PRIMARY KEY, intent_id TEXT UNIQUE NOT NULL, amount INTEGER NOT NULL,
  expires REAL NOT NULL, callback_url TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
  event_id TEXT, capture_id TEXT UNIQUE, captured_at REAL
);
CREATE TABLE IF NOT EXISTS cancelled_merchants (merchant_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS refunds (
  refund_id TEXT PRIMARY KEY, capture_id TEXT NOT NULL UNIQUE, amount INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
  event_id TEXT PRIMARY KEY, url TEXT NOT NULL, body BLOB NOT NULL,
  next_try REAL NOT NULL, deadline REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  request_id TEXT
);
CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries(next_try);
"""


@dataclass
class Problem(Exception):
    status: int
    code: str
    title: str


@dataclass(frozen=True, slots=True)
class Delivery:
    event_id: str
    url: str
    body: bytes
    deadline: float
    attempts: int
    request_id: str | None


def _intent(row: sqlite3.Row) -> Intent:
    status = row["status"]
    event = None
    if status != "open":
        captured_at = row["captured_at"]
        event = Event(
            event_id=row["event_id"],
            event_type=status,
            merchant_payment_id=row["merchant_id"],
            amount_twd=row["amount"],
            capture_id=row["capture_id"],
            captured_at=datetime.fromtimestamp(captured_at, UTC)
            if captured_at is not None
            else None,
        )
    return Intent(
        intent_id=row["intent_id"],
        merchant_payment_id=row["merchant_id"],
        amount_twd=row["amount"],
        expires_at=datetime.fromtimestamp(row["expires"], UTC),
        status=status,
        event=event,
    )


class Store:
    def __init__(self, path: Path, delay_s: float) -> None:
        self.path = path
        self.delay_s = delay_s

    def initialize(self) -> None:
        with closing(sqlite3.connect(self.path, timeout=5)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        # BEGIN IMMEDIATE serializes confirm, decline, expiry and cancellation on one clock sample.
        with closing(sqlite3.connect(self.path, timeout=5, isolation_level=None)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _finish(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        status: Outcome,
        now: float,
        request_id: str | None,
    ) -> Intent:
        event_id = str(uuid4())
        capture_id = str(uuid4()) if status == "captured" else None
        row = conn.execute(
            """UPDATE intents SET status=?,event_id=?,capture_id=?,captured_at=?
               WHERE merchant_id=? AND status='open' RETURNING *""",
            (status, event_id, capture_id, now if capture_id else None, row["merchant_id"]),
        ).fetchone()
        model = _intent(row)
        assert model.event is not None
        conn.execute(
            """INSERT INTO deliveries(event_id,url,body,next_try,deadline,request_id)
               VALUES(?,?,?,?,?,?)""",
            (
                event_id,
                row["callback_url"],
                model.event.model_dump_json().encode(),
                now + self.delay_s,
                now + 86400,
                request_id,
            ),
        )
        return model

    def _resolve(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        request_id: str | None,
        outcome: Outcome | None = None,
    ) -> Intent:
        if row["status"] == "open":
            now = time.time()
            if now >= row["expires"]:
                return self._finish(conn, row, "expired", now, request_id)
            if outcome is not None:
                return self._finish(conn, row, outcome, now, request_id)
        return _intent(row)

    def create(self, body: IntentInput, request_id: str | None) -> Intent:
        merchant = str(body.merchant_payment_id)
        with self.write() as conn:
            row = conn.execute("SELECT * FROM intents WHERE merchant_id=?", (merchant,)).fetchone()
            if row is None:
                row = conn.execute(
                    """INSERT INTO intents(merchant_id,intent_id,amount,expires,callback_url)
                       VALUES(?,?,?,?,?) RETURNING *""",
                    (
                        merchant,
                        str(uuid4()),
                        body.amount_twd,
                        body.expires_at.timestamp(),
                        str(body.callback_url),
                    ),
                ).fetchone()
            elif (row["amount"], row["expires"], row["callback_url"]) != (
                body.amount_twd,
                body.expires_at.timestamp(),
                str(body.callback_url),
            ):
                raise Problem(409, "intent-key-reused", "Merchant ID already used")
            tombstone = conn.execute(
                "SELECT 1 FROM cancelled_merchants WHERE merchant_id=?", (merchant,)
            ).fetchone()
            return self._resolve(conn, row, request_id, "cancelled" if tombstone else None)

    def lookup(self, merchant: UUID, request_id: str | None) -> Intent:
        with self.write() as conn:
            row = conn.execute(
                "SELECT * FROM intents WHERE merchant_id=?", (str(merchant),)
            ).fetchone()
            if row is None:
                raise Problem(404, "intent-not-found", "Payment intent not found")
            return self._resolve(conn, row, request_id)

    def checkout(self, intent_id: UUID, request_id: str | None, outcome: Outcome | None) -> Intent:
        with self.write() as conn:
            row = conn.execute(
                "SELECT * FROM intents WHERE intent_id=?", (str(intent_id),)
            ).fetchone()
            if row is None:
                raise Problem(404, "intent-not-found", "Payment intent not found")
            return self._resolve(conn, row, request_id, outcome)

    def cancel(self, merchant: UUID, request_id: str | None) -> Intent | None:
        with self.write() as conn:
            row = conn.execute(
                "SELECT * FROM intents WHERE merchant_id=?", (str(merchant),)
            ).fetchone()
            if row is None:
                # An earlier create could still arrive after this cancellation.
                conn.execute(
                    "INSERT OR IGNORE INTO cancelled_merchants VALUES (?)", (str(merchant),)
                )
                return None
            return self._resolve(conn, row, request_id, "cancelled")

    def refund(self, body: RefundInput) -> Refund:
        refund_id = str(body.refund_id)
        capture_id = str(body.capture_id)
        with self.write() as conn:
            prior = conn.execute("SELECT * FROM refunds WHERE refund_id=?", (refund_id,)).fetchone()
            if prior is not None:
                if (prior["capture_id"], prior["amount"]) != (capture_id, body.amount_twd):
                    raise Problem(409, "refund-key-reused", "Refund ID already used")
            else:
                capture = conn.execute(
                    "SELECT amount FROM intents WHERE capture_id=?", (capture_id,)
                ).fetchone()
                if capture is None:
                    raise Problem(404, "capture-not-found", "Payment capture not found")
                if capture["amount"] != body.amount_twd:
                    raise Problem(422, "refund-amount-mismatch", "Refund must equal the capture")
                saved = conn.execute(
                    """INSERT INTO refunds(refund_id,capture_id,amount) VALUES(?,?,?)
                       ON CONFLICT(capture_id) DO NOTHING RETURNING refund_id""",
                    (refund_id, capture_id, body.amount_twd),
                ).fetchone()
                if saved is None:
                    raise Problem(409, "capture-already-refunded", "Capture already refunded")
        return Refund(**body.model_dump())

    def due(self) -> list[Delivery]:
        now = time.time()
        with self.write() as conn:
            conn.execute("DELETE FROM deliveries WHERE deadline <= ?", (now,))
            rows = conn.execute(
                "SELECT * FROM deliveries WHERE next_try <= ? ORDER BY next_try LIMIT 64",
                (now,),
            ).fetchall()
        return [
            Delivery(
                r["event_id"], r["url"], r["body"], r["deadline"], r["attempts"], r["request_id"]
            )
            for r in rows
        ]

    def mark_delivery(self, delivery: Delivery, ok: bool) -> None:
        with self.write() as conn:
            if ok:
                conn.execute("DELETE FROM deliveries WHERE event_id=?", (delivery.event_id,))
            else:
                delay = min(900, 2 ** min(delivery.attempts, 10))
                conn.execute(
                    "UPDATE deliveries SET attempts=attempts+1,next_try=? WHERE event_id=?",
                    (time.time() + delay, delivery.event_id),
                )
