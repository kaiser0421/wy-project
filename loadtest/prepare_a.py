"""Prepare Scenario A: a published event with 10,000 seats and N signed-in buyers.

- The event, zones and seat import go through the public API (the normal organizer flow).
- Buyers and their sessions are written directly to PostgreSQL so preparing 3,000 accounts does
  not hit the login rate limits or spend 3,000 argon2 hashes; the session rows are exactly what
  POST /auth/login creates (SHA-256 token hash, HMAC-derived CSRF token).

Outputs (default under ./loadtest/out/): sessions.json, inventory.json, event.json.

Usage:
  uv run python loadtest/prepare_a.py --buyers 3000 --seats 10000
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psycopg

ZONES = [
    ("VIP", "VIP Floor", 6800),
    ("A", "Lower Bowl", 3800),
    ("B", "Upper Bowl", 2800),
    ("C", "Balcony", 1800),
]


def csrf_token(secret_key: str, session_id: uuid.UUID, version: int = 1) -> str:
    message = f"csrf:v{version}:{session_id}".encode()
    return f"v{version}." + hmac.new(secret_key.encode(), message, hashlib.sha256).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8080")
    parser.add_argument(
        "--origin",
        default="http://localhost:8080",
        help="Origin header (must be in ALLOWED_ORIGINS)",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("LOADTEST_DATABASE_URL")
        or os.environ.get(
            "DATABASE_URL", "postgresql://ticketing:ticketing@127.0.0.1:5432/ticketing"
        ),
    )
    parser.add_argument(
        "--secret-key", default=os.environ.get("SECRET_KEY", "dev-insecure-change-me")
    )
    parser.add_argument("--buyers", type=int, default=3000)
    parser.add_argument("--seats", type=int, default=10000)
    parser.add_argument("--out", type=Path, default=Path("loadtest/out"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    base = args.base
    organizer = httpx.Client(base_url=base, timeout=120, headers={"Origin": args.origin})
    login = organizer.post(
        "/api/v1/auth/login", json={"email": "user@example.com", "password": "User#Dev2026!"}
    )
    login.raise_for_status()
    csrf = login.json()["csrf_token"]
    now = datetime.now(UTC)
    event = organizer.post(
        "/api/v1/events",
        headers={"X-CSRF-Token": csrf},
        json={
            "title": f"Load test concert {now:%m%d-%H%M}",
            "description": "Scenario A: 10,000 seats, 3,000 concurrent buyers",
            "category": "concert",
            "venue": "Taipei Dome",
            "sales_start_at": (now - timedelta(minutes=1)).isoformat(),
            "sales_end_at": (now + timedelta(days=1)).isoformat(),
            "zones": [{"code": c, "name": n, "price_twd": p} for c, n, p in ZONES],
        },
    )
    event.raise_for_status()
    event_id = event.json()["id"]

    per_zone, rows_per = args.seats // len(ZONES), 50
    lines = ["zone_code,row_label,seat_number"]
    for code, _name, _price in ZONES:
        for i in range(per_zone):
            lines.append(f"{code},R{i // rows_per + 1},{i % rows_per + 1}")
    started = time.monotonic()
    upload = organizer.post(
        f"/api/v1/events/{event_id}/seat-imports",
        headers={"X-CSRF-Token": csrf},
        files={"file": ("seats.csv", "\n".join(lines).encode(), "text/csv")},
        data={"mode": "all_or_nothing"},
    )
    upload.raise_for_status()
    job_id = upload.json()["id"]
    while (job := organizer.get(f"/api/v1/seat-imports/{job_id}").json())["status"] in (
        "queued",
        "running",
    ):
        time.sleep(0.5)
    assert job["status"] == "succeeded", job
    print(f"imported {len(lines) - 1} seats in {time.monotonic() - started:.1f}s")
    etag = organizer.get(f"/api/v1/events/{event_id}").headers["etag"]
    organizer.post(
        f"/api/v1/events/{event_id}/publish", headers={"X-CSRF-Token": csrf, "If-Match": etag}
    ).raise_for_status()

    seats = organizer.get(f"/api/v1/events/{event_id}/seats").json()["seats"]
    inventory = [{"id": s["id"], "zone_code": s["zone"]} for s in seats]
    (args.out / "inventory.json").write_text(json.dumps(inventory))

    password_hash = "$argon2id$v=19$m=8192,t=1,p=1$bG9hZHRlc3RzYWx0$" + "A" * 43  # never used
    sessions = []
    with psycopg.connect(args.database_url, autocommit=False) as conn, conn.cursor() as cur:
        run = uuid.uuid4().hex[:6]
        cur.execute(
            """INSERT INTO users (email, password_hash, role)
               SELECT 'lt-' || %s || '-' || g || '@example.invalid', %s, 'user'
               FROM generate_series(1, %s) AS g RETURNING id""",
            (run, password_hash, args.buyers),
        )
        user_ids = [r[0] for r in cur.fetchall()]
        for user_id in user_ids:
            token = secrets.token_urlsafe(32)
            cur.execute(
                """INSERT INTO sessions (token_hash, user_id, csrf_secret, idle_expires_at,
                                         abs_expires_at)
                   VALUES (%s, %s, 'v1', now() + interval '12 hours', now() + interval '12 hours')
                   RETURNING id""",
                (hashlib.sha256(token.encode()).digest(), user_id),
            )
            session_id = cur.fetchone()[0]
            sessions.append(
                {
                    "user_id": user_id,
                    "cookie": f"sid={token}",
                    "csrf_token": csrf_token(args.secret_key, session_id),
                }
            )
        conn.commit()
    (args.out / "sessions.json").write_text(json.dumps(sessions))
    (args.out / "event.json").write_text(json.dumps({"event_id": event_id, "seats": len(seats)}))
    print(f"event {event_id}: {len(seats)} seats, {len(sessions)} buyer sessions -> {args.out}")


if __name__ == "__main__":
    main()
