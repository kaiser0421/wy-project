"""Run Scenario B with distinct authenticated SSE sessions and audit state against PostgreSQL."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import random
from array import array
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from time import time
from typing import Any, cast

import asyncpg
import httpx
import orjson

from ticketing.realtime.client_model import Delta, SequenceGap, apply_delta
from ticketing.realtime.snapshot import STATUS_CHAR

_CHAR_STATUS = {ord(value): key for key, value in STATUS_CHAR.items()}


@dataclass(frozen=True, slots=True)
class Layout:
    version: int
    positions: dict[int, int]
    fingerprint: bytes

    @classmethod
    def create(cls, version: int, indices: list[int]) -> Layout:
        index_hash = hashlib.sha256(array("I", indices).tobytes()).digest()
        return cls(version, {idx: pos for pos, idx in enumerate(indices)}, index_hash)


@dataclass(slots=True)
class Seats:
    layout: Layout
    status: bytearray
    versions: array[int]

    def clone(self) -> Seats:
        return Seats(self.layout, self.status.copy(), self.versions[:])

    def get(self, idx: int, /) -> tuple[str, int]:
        pos = self.layout.positions[idx]
        return _CHAR_STATUS[self.status[pos]], self.versions[pos]

    def __setitem__(self, idx: int, value: tuple[str, int], /) -> None:
        pos = self.layout.positions[idx]
        status, version = value
        self.status[pos] = ord(STATUS_CHAR[status])
        self.versions[pos] = version

    def digest(self) -> str:
        digest = hashlib.sha256(str(self.layout.version).encode())
        digest.update(self.layout.fingerprint)
        digest.update(self.status)
        digest.update(self.versions)
        return digest.hexdigest()


@dataclass(slots=True)
class Probe:
    applied: int
    seats: Seats
    opened: bool = False
    last_progress_at: float | None = None


@dataclass(slots=True)
class Metrics:
    total: Counter[str] = field(default_factory=Counter)
    latency: Counter[int] = field(default_factory=Counter)
    connect: Counter[int] = field(default_factory=Counter)
    snapshot: Counter[int] = field(default_factory=Counter)
    active: int = 0
    peak: int = 0

    def summary(self, seconds: float) -> dict[str, Any]:
        return {
            "gaps": self.total["gaps"],
            "resyncs": self.total["resyncs"],
            "request_errors": self.total["request_errors"],
            **dict(self.total),
            "peak_active": self.peak,
            "sse_body_bytes_per_second": round(self.total["sse_body_bytes"] / seconds),
            "delta_apply_ms": percentiles(self.latency),
            "connect_ms": percentiles(self.connect),
            "snapshot_ms": percentiles(self.snapshot),
            "stream_error_rate": round(
                self.total["stream_request_errors"] / max(1, self.total["stream_attempts"]), 6
            ),
        }


def percentiles(histogram: Counter[int]) -> dict[str, int]:
    if not histogram:
        return {}
    quantiles = {}
    count = histogram.total()
    ordered = sorted(histogram.items())
    for p in (50, 95, 99):
        remaining = math.ceil(count * p / 100)
        for ms, freq in ordered:
            remaining -= freq
            if remaining <= 0:
                quantiles[f"p{p}"] = ms
                break
    return quantiles


async def events(
    response: httpx.Response, metrics: Metrics
) -> AsyncIterator[tuple[str, int | None, dict[str, Any]]]:
    fields: dict[str, str] = {}
    async for line in response.aiter_lines():
        metrics.total["sse_body_bytes"] += len(line.encode()) + 1
        if not line:
            if "event" in fields:
                seq = int(fields["id"]) if "id" in fields else None
                yield fields["event"], seq, orjson.loads(fields["data"])
            fields.clear()
        elif not line.startswith(":"):
            name, _, value = line.partition(":")
            fields[name] = value.removeprefix(" ")


class Scenario:
    def __init__(self, event_id: int, cookie_name: str, slow_delay: float) -> None:
        self.url = f"/api/v1/events/{event_id}"
        self.cookie_name = cookie_name
        self.slow_delay = slow_delay
        self.layouts: dict[int, Layout] = {}
        self.metrics = Metrics()

    async def snapshot(self, client: httpx.AsyncClient, cookie: str, min_layout: int) -> Probe:
        loop = asyncio.get_running_loop()
        started = loop.time()
        response = await client.get(
            self.url + "/seats",
            headers={"Cookie": cookie},
            params={"format": "compact", "min_layout_ver": min_layout},
        )
        response.raise_for_status()
        data = response.json()
        self.metrics.snapshot[round((loop.time() - started) * 1000)] += 1
        version = data["layout_ver"]
        if version < min_layout:
            raise SequenceGap("Layout snapshot is behind the requested version")
        layout = self.layouts.get(version)
        if layout is None:
            layout = Layout.create(version, [seat["idx"] for seat in data["seats"]])
            self.layouts[version] = layout
        return Probe(
            data["last_seq"],
            Seats(layout, bytearray(data["status"], "ascii"), array("I", data["versions"])),
        )

    async def subscribe(
        self, client: httpx.AsyncClient, cookie: str, probe: Probe, delay: float
    ) -> int | None:
        """Return a required layout version when replay needs a fresh snapshot."""
        loop = asyncio.get_running_loop()
        start = loop.time()
        self.metrics.total["stream_attempts"] += 1
        headers = {"Cookie": cookie, "Last-Event-ID": str(probe.applied)}
        async with client.stream("GET", self.url + "/seat-stream", headers=headers) as response:
            response.raise_for_status()
            self.metrics.connect[round((loop.time() - start) * 1000)] += 1
            if not probe.opened:
                probe.opened = True
                self.metrics.total["connections_established"] += 1
            self.metrics.active += 1
            self.metrics.peak = max(self.metrics.peak, self.metrics.active)
            try:
                async for kind, seq, data in events(response, self.metrics):
                    if kind == "resync":
                        self.metrics.total["resyncs"] += 1
                        return 0
                    if kind == "layout":
                        if seq is None or seq > probe.applied + 1:
                            raise SequenceGap("Layout sequence is beyond the next expected row")
                        if seq > probe.applied:
                            self.metrics.total["layout_refreshes"] += 1
                            return int(data["layout_ver"])
                    elif kind == "delta":
                        if seq != data["to"]:
                            raise SequenceGap("Frame ID disagrees with its interval")
                        applied = apply_delta(probe.seats, probe.applied, cast(Delta, data))
                        if applied > probe.applied:
                            probe.applied = applied
                            created = datetime.fromisoformat(data["ts"]).timestamp()
                            self.metrics.latency[round((time() - created) * 1000)] += 1
                            self.metrics.total["deltas_applied"] += 1
                    elif kind == "progress":
                        if data["oldest_unassigned_age_ms"] > 2000:
                            self.metrics.total["outbox_lag_samples"] += 1
                        if data["head"] <= probe.applied:
                            probe.last_progress_at = None
                        elif probe.last_progress_at is None:
                            probe.last_progress_at = loop.time()
                        elif loop.time() - probe.last_progress_at > 3:
                            self.metrics.total["stalled_progress"] += 1
                            raise SequenceGap("Applied sequence is behind progress for over 3 s")
                    if delay:
                        await asyncio.sleep(delay)
            finally:
                self.metrics.active -= 1
        self.metrics.total["stream_eofs"] += 1
        return None

    async def run(self, client: httpx.AsyncClient, token: str, probe: Probe, slow: bool) -> None:
        cookie = f"{self.cookie_name}={token}"
        minimum: int | None = None
        retries = 0
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            applied = probe.applied
            probe.last_progress_at = None
            try:
                if minimum is not None:
                    snapshot = await self.snapshot(client, cookie, minimum)
                    probe.applied, probe.seats = snapshot.applied, snapshot.seats
                    minimum = None
                minimum = await self.subscribe(
                    client, cookie, probe, self.slow_delay if slow else 0
                )
            except SequenceGap:
                self.metrics.total["gaps"] += 1
            except (KeyError, ValueError):
                self.metrics.total["invalid_frames"] += 1
                minimum = 0
            except httpx.HTTPStatusError as error:
                self.metrics.total["request_errors"] += 1
                stage = "stream" if minimum is None else "snapshot"
                self.metrics.total[f"{stage}_request_errors"] += 1
                if error.response.status_code in (401, 403, 404):
                    return
            except httpx.HTTPError:
                self.metrics.total["request_errors"] += 1
                stage = "stream" if minimum is None else "snapshot"
                self.metrics.total[f"{stage}_request_errors"] += 1
            # Leave the applied cursor unchanged across disconnects and gap recovery.
            if probe.applied != applied or loop.time() - started >= 5:
                retries = 0
            delay = 0.25 * 2**retries
            await asyncio.sleep(random.uniform(delay / 2, delay))  # noqa: S311 - reconnect jitter
            retries = min(4, retries + 1)


async def pg_state(conn: asyncpg.Connection, event_id: int) -> tuple[int, int, str, int]:
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        info = await conn.fetchrow(
            """
            SELECT e.layout_ver, coalesce(s.last_assigned_seq, 0) AS head,
                   (SELECT count(*) FROM outbox WHERE event_id=$1 AND seq IS NULL) AS pending
            FROM events e LEFT JOIN event_stream_state s ON s.event_id=e.id
            WHERE e.id=$1
            """,
            event_id,
        )
        if info is None:
            raise SystemExit("Audit event is missing")
        rows = await conn.fetch(
            "SELECT seat_idx, status, version FROM seats WHERE event_id=$1 ORDER BY seat_idx",
            event_id,
        )
        archive_mismatches = await conn.fetchval(
            """
            WITH expected AS (
                SELECT o.seq, (c->>0)::int AS idx, c->>1 AS status, (c->>2)::int AS version,
                       o.created_at AS recorded_at
                FROM outbox o
                CROSS JOIN LATERAL jsonb_array_elements(
                    CASE WHEN kind='seat_changes' THEN changes ELSE '[]'::jsonb END
                ) c WHERE event_id=$1 AND seq IS NOT NULL
            ), actual AS (
                SELECT seq, seat_idx AS idx, status, seat_version AS version, recorded_at
                FROM seat_events WHERE event_id=$1
            )
            SELECT count(*) FROM expected FULL JOIN actual
            USING (seq, idx, status, version, recorded_at)
            WHERE expected.seq IS NULL OR actual.seq IS NULL
            """,
            event_id,
        )
    seats = Seats(
        Layout.create(info["layout_ver"], [row["seat_idx"] for row in rows]),
        bytearray("".join(STATUS_CHAR[row["status"]] for row in rows), "ascii"),
        array("I", (row["version"] for row in rows)),
    )
    return info["head"], info["pending"], seats.digest(), archive_mismatches


async def run_scenario(args: argparse.Namespace, tokens: list[str]) -> bool:
    loop = asyncio.get_running_loop()
    scenario = Scenario(args.event_id, args.cookie_name, args.slow_delay)
    clients: list[httpx.AsyncClient] = []
    tasks: list[asyncio.Task[None]] = []
    async with AsyncExitStack() as stack:
        # Smaller pools avoid a quadratic connection search during a 2,000-client reconnect.
        for _ in range(math.ceil(len(tokens) / 64)):
            clients.append(
                await stack.enter_async_context(
                    httpx.AsyncClient(
                        base_url=args.base_url,
                        limits=httpx.Limits(max_connections=72),
                        timeout=httpx.Timeout(20.0, connect=10.0),
                        trust_env=False,
                    )
                )
            )
        initial = await scenario.snapshot(clients[0], f"{args.cookie_name}={tokens[0]}", 0)
        probes = [Probe(initial.applied, initial.seats.clone()) for _ in tokens]
        started = loop.time()

        async def connect(index: int, probe: Probe, token: str) -> None:
            if args.ramp_seconds:
                await asyncio.sleep(args.ramp_seconds * index / len(tokens))
            await scenario.run(clients[index // 64], token, probe, index % 10 == 0)

        for i, (probe, token) in enumerate(zip(probes, tokens, strict=True)):
            tasks.append(asyncio.create_task(connect(i, probe, token)))
        try:
            await asyncio.sleep(args.duration)
            deadline = loop.time() + args.drain_timeout
            conn = await asyncpg.connect(os.environ["DATABASE_URL"])
            try:
                while True:
                    head, pending, digest, archive_errors = await pg_state(conn, args.event_id)
                    if not pending and all(probe.applied >= head for probe in probes):
                        break
                    if loop.time() >= deadline:
                        break
                    await asyncio.sleep(0.25)
            finally:
                await conn.close()
            matched = sum(
                probe.applied == head and probe.seats.digest() == digest for probe in probes
            )
            report = scenario.metrics.summary(loop.time() - started)
            report.update(
                {
                    "target_connections": len(tokens),
                    "final_head": head,
                    "outbox_pending": pending,
                    "archive_mismatches": archive_errors,
                    "pg_state_sha256": digest,
                    "matching_clients": matched,
                }
            )
            print(json.dumps(report, sort_keys=True))
            return (
                matched == len(tokens)
                and scenario.metrics.peak == len(tokens)
                and pending == archive_errors == 0
            )
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--event-id", type=int, required=True)
    parser.add_argument("--sessions-file", required=True, type=Path)
    parser.add_argument("--connections", type=int, default=2000)
    parser.add_argument("--duration", type=float, default=75)
    parser.add_argument("--ramp-seconds", type=float, default=0)
    parser.add_argument("--drain-timeout", type=float, default=10)
    parser.add_argument("--slow-delay", type=float, default=0.25)
    parser.add_argument("--cookie-name", choices=["sid", "__Host-sid"], default="sid")
    args = parser.parse_args()
    if (
        args.connections <= 0
        or args.duration <= 0
        or min(args.drain_timeout, args.slow_delay, args.ramp_seconds) < 0
    ):
        parser.error("connections and duration must be positive. Delays must be nonnegative")
    if "DATABASE_URL" not in os.environ:
        parser.error("Set DATABASE_URL for the final PostgreSQL audit")
    tokens = json.loads(args.sessions_file.read_text())[: args.connections]
    if len(tokens) != args.connections or len(set(tokens)) != args.connections:
        parser.error("Provide a JSON array with one distinct session token per connection")
    raise SystemExit(0 if asyncio.run(run_scenario(args, tokens)) else 1)


if __name__ == "__main__":
    main()
