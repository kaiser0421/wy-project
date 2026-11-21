"""Run Scenario B with distinct authenticated SSE sessions and audit state against PostgreSQL."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import math
import multiprocessing
import os
import random
from array import array
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from multiprocessing.connection import Connection, wait
from pathlib import Path
from time import monotonic, time
from typing import Any

import asyncpg
import httpx
import orjson

from ticketing.realtime.client_model import SeatState, SequenceGap
from ticketing.realtime.snapshot import STATUS_CHAR

_CHAR_STATUS = {value: key for key, value in STATUS_CHAR.items()}


@dataclass(slots=True)
class Probe:
    applied: int
    layout_ver: int
    seats: SeatState
    opened: bool = False
    last_progress_at: float | None = None


@dataclass(slots=True)
class Metrics:
    total: Counter[str] = field(default_factory=Counter)
    latency: Counter[int] = field(default_factory=Counter)
    connect: Counter[int] = field(default_factory=Counter)
    snapshot: Counter[int] = field(default_factory=Counter)

    def merge(self, other: Metrics) -> None:
        self.total.update(other.total)
        self.latency.update(other.latency)
        self.connect.update(other.connect)
        self.snapshot.update(other.snapshot)

    def summary(self, seconds: float, peak: int) -> dict[str, Any]:
        return {
            "gaps": self.total["gaps"],
            "resyncs": self.total["resyncs"],
            "request_errors": self.total["request_errors"],
            **dict(self.total),
            "peak_active": peak,
            "sse_body_bytes_per_second": round(self.total["sse_body_bytes"] / seconds),
            "delta_apply_samples": self.latency.total(),
            "delta_apply_ms": percentiles(self.latency),
            "connect_ms": percentiles(self.connect),
            "snapshot_ms": percentiles(self.snapshot),
            "stream_error_rate": round(
                self.total["stream_request_errors"] / max(1, self.total["stream_attempts"]), 6
            ),
        }


def record_latency(histogram: Counter[int], seconds: float) -> None:
    # Fixed bounds keep worker memory and IPC size independent of the sample count.
    ms = max(-300_000, min(300_000, round(seconds * 1000)))
    if abs(ms) >= 10_000:
        ms = round(ms, -2)
    elif abs(ms) >= 1000:
        ms = round(ms, -1)
    histogram[ms] += 1


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
) -> AsyncIterator[tuple[str, int | None, str]]:
    fields: dict[str, str] = {}
    async for line in response.aiter_lines():
        metrics.total["sse_body_bytes"] += len(line.encode()) + 1
        if not line:
            if "event" in fields:
                seq = int(fields["id"]) if "id" in fields else None
                yield fields["event"], seq, fields["data"]
            fields.clear()
        elif not line.startswith(":"):
            name, _, value = line.partition(":")
            fields[name] = value.removeprefix(" ")


@lru_cache(maxsize=128)
def decode_delta(raw: str) -> tuple[int, int, float, list[tuple[int, tuple[str, int]]]]:
    data = orjson.loads(raw)
    # Subscribers often receive the same frame. Reuse its immutable seat values.
    changes = [(idx, (status, version)) for idx, status, version in data["changes"]]
    return data["from"], data["to"], datetime.fromisoformat(data["ts"]).timestamp(), changes


def apply_delta(probe: Probe, seq: int | None, raw: str) -> float | None:
    first, last, created, changes = decode_delta(raw)
    if seq != last:
        raise SequenceGap("Frame ID disagrees with its interval")
    if last <= probe.applied:
        return None
    if first > probe.applied + 1:
        raise SequenceGap("Delta starts after the next expected sequence")
    seats = probe.seats
    for idx, value in changes:
        previous = seats.get(idx)
        if previous is None or value[1] > previous[1]:
            seats[idx] = value
    probe.applied = last
    return created


@lru_cache(maxsize=2)
def decode_snapshot(raw: bytes) -> tuple[int, int, SeatState]:
    data = orjson.loads(raw)
    seats = {
        seat["idx"]: (_CHAR_STATUS[status], version)
        for seat, status, version in zip(
            data["seats"], data["status"], data["versions"], strict=True
        )
    }
    return data["last_seq"], data["layout_ver"], seats


class Scenario:
    def __init__(self, args: argparse.Namespace, active: Any) -> None:
        self.url = f"/api/v1/events/{args.event_id}"
        self.cookie_name = args.cookie_name
        self.slow_delay = args.slow_delay
        self.active = active
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
        head, version, seats = decode_snapshot(response.content)
        if version < min_layout:
            raise SequenceGap("Layout snapshot is behind the requested version")
        probe = Probe(head, version, seats.copy())
        record_latency(self.metrics.snapshot, loop.time() - started)
        return probe

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
            record_latency(self.metrics.connect, loop.time() - start)
            if not probe.opened:
                probe.opened = True
                self.metrics.total["connections_established"] += 1
            with self.active.get_lock():
                self.active[0] += 1
                self.active[1] = max(self.active[1], self.active[0])
            try:
                async for kind, seq, raw in events(response, self.metrics):
                    if kind == "resync":
                        self.metrics.total["resyncs"] += 1
                        return 0
                    if kind == "layout":
                        if seq is None or seq > probe.applied + 1:
                            raise SequenceGap("Layout sequence is beyond the next expected row")
                        if seq > probe.applied:
                            self.metrics.total["layout_refreshes"] += 1
                            return int(orjson.loads(raw)["layout_ver"])
                    elif kind == "delta":
                        created = apply_delta(probe, seq, raw)
                        if created is not None:
                            record_latency(self.metrics.latency, time() - created)
                            self.metrics.total["deltas_applied"] += 1
                    elif kind == "progress":
                        data = orjson.loads(raw)
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
                with self.active.get_lock():
                    self.active[0] -= 1
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
                    probe.applied = snapshot.applied
                    probe.layout_ver = snapshot.layout_ver
                    probe.seats = snapshot.seats
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


@dataclass(slots=True)
class DatabaseState:
    head: int
    pending: int
    layout_ver: int
    seats: SeatState
    archive_mismatches: int

    def digest(self) -> str:
        # Keep the report fingerprint compatible with previous runs.
        digest = hashlib.sha256(str(self.layout_ver).encode())
        digest.update(hashlib.sha256(array("I", self.seats).tobytes()).digest())
        digest.update("".join(STATUS_CHAR[status] for status, _ in self.seats.values()).encode())
        digest.update(array("I", (version for _, version in self.seats.values())))
        return digest.hexdigest()


async def pg_state(conn: asyncpg.Connection, event_id: int) -> DatabaseState:
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
        # A retained suffix must still be contiguous and end at the stream head.
        archive_mismatches = await conn.fetchval(
            """
            WITH retained AS (
                SELECT min(seq) AS lo, max(seq) AS hi, count(*) AS n
                FROM outbox WHERE event_id=$1 AND seq IS NOT NULL
            ), expected AS (
                SELECT o.seq, (c->>0)::int AS idx, c->>1 AS status, (c->>2)::int AS version,
                       o.created_at AS recorded_at
                FROM outbox o
                CROSS JOIN LATERAL jsonb_array_elements(
                    CASE WHEN kind='seat_changes' THEN changes ELSE '[]'::jsonb END
                ) c WHERE event_id=$1 AND seq IS NOT NULL
            ), actual AS (
                SELECT seq, seat_idx AS idx, status, seat_version AS version, recorded_at
                FROM seat_events CROSS JOIN retained
                WHERE event_id=$1 AND seq BETWEEN lo AND hi
            ), differences AS (
                SELECT count(*) AS n FROM expected FULL JOIN actual
                USING (seq, idx, status, version, recorded_at)
                WHERE expected.seq IS NULL OR actual.seq IS NULL
            )
            SELECT differences.n + CASE
                WHEN hi - lo + 1 <> retained.n OR hi <> $2 THEN 1 ELSE 0 END
            FROM differences CROSS JOIN retained
            """,
            event_id,
            info["head"],
        )
    seats = {row["seat_idx"]: (row["status"], row["version"]) for row in rows}
    return DatabaseState(
        info["head"], info["pending"], info["layout_ver"], seats, archive_mismatches
    )


async def measure_shard(
    args: argparse.Namespace,
    tokens: list[str],
    shard: int,
    channel: Connection,
    progress: Any,
    active: Any,
) -> tuple[Metrics, int]:
    scenario = Scenario(args, active)
    probes: list[Probe] = []
    async with AsyncExitStack() as stack:
        # Small pools keep HTTP connection searches short during reconnects.
        clients = [
            await stack.enter_async_context(
                httpx.AsyncClient(
                    base_url=args.base_url,
                    limits=httpx.Limits(max_connections=72),
                    timeout=httpx.Timeout(20.0, connect=10.0),
                    trust_env=False,
                )
            )
            for _ in range(math.ceil(len(tokens) / 64))
        ]
        initial = await scenario.snapshot(clients[0], f"{args.cookie_name}={tokens[0]}", 0)
        channel.send(None)
        started = channel.recv()

        async def connect(local_index: int, token: str) -> None:
            index = shard + local_index * args.processes
            offset = args.ramp_seconds * index / args.connections
            await asyncio.sleep(max(0, started + offset - monotonic()))
            probe = Probe(initial.applied, initial.layout_ver, initial.seats.copy())
            probes.append(probe)
            await scenario.run(clients[local_index // 64], token, probe, index % 10 == 0)

        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(connect(i, token)) for i, token in enumerate(tokens)]
            while not channel.poll():
                if len(probes) == len(tokens):
                    progress[shard] = min(probe.applied for probe in probes)
                await asyncio.sleep(0.1)
            final: DatabaseState = channel.recv()
            for task in tasks:
                task.cancel()

    matched = sum(
        probe.applied == final.head
        and probe.layout_ver == final.layout_ver
        and probe.seats == final.seats
        for probe in probes
    )
    return scenario.metrics, matched


def worker(
    args: argparse.Namespace,
    tokens: list[str],
    shard: int,
    channel: Connection,
    progress: Any,
    active: Any,
) -> None:
    with channel:
        channel.send(asyncio.run(measure_shard(args, tokens, shard, channel, progress, active)))


def receive(channel: Connection) -> Any:
    try:
        return channel.recv()
    except (EOFError, OSError) as error:
        raise SystemExit("An SSE worker exited before completing its report") from error


async def drain(
    args: argparse.Namespace, progress: Any, channels: list[Connection]
) -> DatabaseState:
    deadline = monotonic() + args.drain_timeout
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        while True:
            if wait(channels, timeout=0):
                raise SystemExit("An SSE worker exited during the drain")
            final = await pg_state(conn, args.event_id)
            if (not final.pending and min(progress[:]) >= final.head) or monotonic() >= deadline:
                return final
            await asyncio.sleep(0.25)
    finally:
        await conn.close()


def run_scenario(args: argparse.Namespace, tokens: list[str]) -> bool:
    ctx = multiprocessing.get_context("spawn")
    active = ctx.Array("i", [0, 0])  # Current and peak simultaneous connections across workers.
    progress = ctx.Array("q", [-1] * args.processes)
    channels: list[Connection] = []
    processes = []
    try:
        for shard in range(args.processes):
            parent, child = ctx.Pipe()
            channels.append(parent)
            with child:
                process = ctx.Process(
                    target=worker,
                    args=(args, tokens[shard :: args.processes], shard, child, progress, active),
                )
                process.start()
            processes.append(process)

        pending = set(channels)
        while pending:
            for channel in wait(pending):
                receive(channel)
                pending.remove(channel)

        started = monotonic()
        for channel in channels:
            channel.send(started)
        if wait(channels, timeout=max(0, started + args.duration - monotonic())):
            raise SystemExit("An SSE worker exited during the load phase")
        final = asyncio.run(drain(args, progress, channels))
        elapsed = monotonic() - started
        for channel in channels:
            channel.send(final)

        metrics = Metrics()
        matched = 0
        pending = set(channels)
        while pending:
            for channel in wait(pending):
                shard_metrics, shard_matched = receive(channel)
                metrics.merge(shard_metrics)
                matched += shard_matched
                pending.remove(channel)

        for process in processes:
            process.join()
            if process.exitcode != 0:
                raise SystemExit(f"An SSE worker exited with status {process.exitcode}")

        peak = active[1]
        report = metrics.summary(elapsed, peak)
        report.update(
            {
                "target_connections": len(tokens),
                "processes": args.processes,
                "final_head": final.head,
                "outbox_pending": final.pending,
                "archive_mismatches": final.archive_mismatches,
                "pg_state_sha256": final.digest(),
                "matching_clients": matched,
            }
        )
        print(orjson.dumps(report, option=orjson.OPT_SORT_KEYS).decode())
        return (
            matched == len(tokens)
            and peak == len(tokens)
            and final.pending == final.archive_mismatches == 0
            and report["delta_apply_samples"] > 0
        )
    finally:
        for channel in channels:
            channel.close()
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join()
            process.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--event-id", type=int, required=True)
    parser.add_argument("--sessions-file", required=True, type=Path)
    parser.add_argument("--connections", type=int, default=2000)
    parser.add_argument("--duration", type=float, default=75)
    parser.add_argument(
        "--processes",
        type=int,
        default=min(8, os.cpu_count() or 1),
        help="worker processes, capped at the connection count (default: %(default)s)",
    )
    parser.add_argument(
        "--ramp-seconds",
        type=float,
        default=10,
        help="spread initial connection starts over this many seconds (default: %(default)s)",
    )
    parser.add_argument("--drain-timeout", type=float, default=10)
    parser.add_argument("--slow-delay", type=float, default=0.25)
    parser.add_argument("--cookie-name", choices=["sid", "__Host-sid"], default="sid")
    args = parser.parse_args()
    if (
        args.connections <= 0
        or args.processes <= 0
        or args.duration <= 0
        or min(args.drain_timeout, args.slow_delay, args.ramp_seconds) < 0
    ):
        parser.error(
            "connections, processes and duration must be positive; delays must be nonnegative"
        )
    if "DATABASE_URL" not in os.environ:
        parser.error("Set DATABASE_URL for the final PostgreSQL audit")
    tokens = orjson.loads(args.sessions_file.read_bytes())[: args.connections]
    if len(tokens) != args.connections or len(set(tokens)) != args.connections:
        parser.error("Provide a JSON array with one distinct session token per connection")
    args.processes = min(args.processes, args.connections)
    raise SystemExit(0 if run_scenario(args, tokens) else 1)


if __name__ == "__main__":
    main()
