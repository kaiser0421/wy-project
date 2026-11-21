# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27"]
# ///
"""End-to-end flow against a running stack (docker compose up -d --build).

Walks the main business path through nginx exactly as a browser would:
organizer creates an event, imports seats, publishes; a buyer books seats, pays through the
payment simulator; the order becomes paid via the signed callback; the organizer downloads the
sales report; an admin traces the booking request across services by its request_id.

Usage:  uv run python scripts/e2e_flow.py [--base http://127.0.0.1:8080]
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
ORGANIZER = ("user@example.com", "User#Dev2026!")
ADMIN = ("admin@example.com", "Admin#Dev2026!")


class Session:
    def __init__(self, base: str, email: str, password: str) -> None:
        self.base = base
        self.client = httpx.Client(base_url=base, timeout=30, headers={"Origin": base})
        response = self.client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        )
        check(response, 200, f"login {email}")
        self.csrf = response.json()["csrf_token"]

    def write(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        headers = {"X-CSRF-Token": self.csrf, **dict(kwargs.pop("headers", {}) or {})}
        return self.client.request(method, path, headers=headers, **kwargs)  # type: ignore[arg-type]


def check(response: httpx.Response, status: int, what: str) -> None:
    if response.status_code != status:
        print(f"FAIL {what}: expected {status}, got {response.status_code}: {response.text[:500]}")
        sys.exit(1)
    print(
        f"ok   {what} ({response.status_code}, request_id={response.headers.get('x-request-id')})"
    )


def wait_for(what: str, probe, timeout_s: float = 60.0, interval_s: float = 0.5):  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = probe()
        if result:
            print(f"ok   {what}")
            return result
        time.sleep(interval_s)
    print(f"FAIL {what}: timed out after {timeout_s:.0f}s")
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8080")
    base = parser.parse_args().base

    organizer = Session(base, *ORGANIZER)
    now = datetime.now(UTC)
    created = organizer.write(
        "POST",
        "/api/v1/events",
        json={
            "title": f"E2E Concert {uuid.uuid4().hex[:6]}",
            "description": "End-to-end verification event",
            "category": "concert",
            "venue": "Taipei Arena",
            "sales_start_at": (now - timedelta(minutes=1)).isoformat(),
            "sales_end_at": (now + timedelta(days=1)).isoformat(),
            "zones": [
                {"code": "A", "name": "Zone A", "price_twd": 2800},
                {"code": "B", "name": "Zone B", "price_twd": 1800},
            ],
        },
    )
    check(created, 201, "create event")
    event = created.json()
    event_id = event["id"]

    sample = (ROOT / "samples/seats_sample.csv").read_bytes()
    upload = organizer.write(
        "POST",
        f"/api/v1/events/{event_id}/seat-imports",
        files={"file": ("seats.csv", sample, "text/csv")},
        data={"mode": "all_or_nothing"},
    )
    if upload.status_code not in (200, 202):
        check(upload, 202, "upload seats")
    print(f"ok   upload seats ({upload.status_code})")
    job_id = upload.json()["id"]

    def job_done():  # type: ignore[no-untyped-def]
        job = organizer.client.get(f"/api/v1/seat-imports/{job_id}").json()
        return job if job["status"] not in ("queued", "running") else None

    job = wait_for("seat import finished", job_done)
    if job["status"] != "succeeded":
        print(f"FAIL import status {job['status']}: {job}")
        sys.exit(1)

    etag = organizer.client.get(f"/api/v1/events/{event_id}").headers["etag"]
    check(
        organizer.write("POST", f"/api/v1/events/{event_id}/publish", headers={"If-Match": etag}),
        200,
        "publish event",
    )

    buyer = Session(base, *ADMIN)  # admins may book too; keeps the demo to seeded accounts
    seats = buyer.client.get(f"/api/v1/events/{event_id}/seats")
    check(seats, 200, "seat snapshot")
    available = [s["id"] for s in seats.json()["seats"] if s["status"] == "available"][:2]
    order = buyer.write(
        "POST",
        f"/api/v1/events/{event_id}/orders",
        json={"seat_ids": available},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    check(order, 201, f"book seats {available}")
    booking_request_id = order.headers["x-request-id"]
    body = order.json()
    order_id = body["public_id"]
    print(f"     subtotal={body['subtotal_twd']} fee={body['fee_twd']} total={body['total_twd']}")

    payment = buyer.write(
        "POST",
        f"/api/v1/orders/{order_id}/payments",
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    check(payment, 201, "start payment")
    checkout = payment.json()["checkout_url"]
    intent = checkout.rstrip("/").rsplit("/", 1)[-1]
    check(httpx.post(f"{base}/pay/{intent}/confirm", timeout=30), 200, "confirm at provider")

    wait_for(
        "order paid via signed callback",
        lambda: buyer.client.get(f"/api/v1/orders/{order_id}").json()["status"] == "paid",
    )

    report = organizer.client.get(
        f"/api/v1/events/{event_id}/reports/sales.xlsx", params={"tz": "Asia/Taipei"}
    )
    check(report, 200, f"sales report ({len(report.content)} bytes)")

    def traced():  # type: ignore[no-untyped-def]
        logs = buyer.client.get(f"/api/v1/admin/requests/{booking_request_id}/logs")
        if logs.status_code != 200:
            return None
        payload = logs.json()
        entries = payload.get("entries") or payload.get("service_logs") or []
        return payload if (payload.get("access_log") or entries) else None

    wait_for(f"request {booking_request_id} visible in admin log lookup", traced, timeout_s=45)
    print("E2E flow passed")


if __name__ == "__main__":
    main()
