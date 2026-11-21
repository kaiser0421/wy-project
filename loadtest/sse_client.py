"""Run Scenario B with distinct authenticated SSE sessions and audit state against PostgreSQL."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import math
import multiprocessing
import os
import random
import subprocess
import sys
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

_STATUS_CODE = {status: ord(char) for status, char in STATUS_CHAR.items()}
_CHAR_CODE = {char: ord(char) for char in STATUS_CHAR.values()}


@dataclass(slots=True)
class Probe:
    applied: int
    layout_ver: int
    layout: dict[int, int]
    statuses: array[int]
    versions: array[int]
    opened: bool = False
    last_progress_at: float | None = None

    def fork(self) -> Probe:
        return Probe(self.applied, self.layout_ver, self.layout, self.statuses[:], self.versions[:])


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
def decode_delta(raw: str) -> tuple[int, int, float, list[tuple[int, int, int]]]:
    data = orjson.loads(raw)
    # Subscribers often receive the same frame. Reuse its immutable seat values.
    changes = [(idx, _STATUS_CODE[status], version) for idx, status, version in data["changes"]]
    return data["from"], data["to"], datetime.fromisoformat(data["ts"]).timestamp(), changes


def apply_delta(probe: Probe, seq: int | None, raw: str) -> float | None:
    first, last, created, changes = decode_delta(raw)
    if seq != last:
        raise SequenceGap("Frame ID disagrees with its interval")
    if last <= probe.applied:
        return None
    if first > probe.applied + 1:
        raise SequenceGap("Delta starts after the next expected sequence")
    for idx, status, version in changes:
        offset = probe.layout[idx]
        if version > probe.versions[offset]:
            probe.versions[offset] = version
            probe.statuses[offset] = status
    probe.applied = last
    return created


def decode_snapshot(raw: bytes, layouts: dict[int, dict[int, int]]) -> Probe:
    data = orjson.loads(raw)
    layout_ver = data["layout_ver"]
    layout = layouts.get(layout_ver)
    if layout is None:
        layout = {seat["idx"]: offset for offset, seat in enumerate(data["seats"])}
    if len(layout) != len(data["seats"]):
        raise ValueError("Snapshot seat indices disagree with the layout")
    statuses = array("b", [0]) * len(layout)
    # Seat versions are PostgreSQL int columns.
    versions = array("i", [0]) * len(layout)
    for seat, status, version in zip(data["seats"], data["status"], data["versions"], strict=True):
        offset = layout[seat["idx"]]
        if statuses[offset]:
            raise ValueError("Snapshot repeats a seat index")
        statuses[offset] = _CHAR_CODE[status]
        versions[offset] = version
    layouts[layout_ver] = layout
    return Probe(data["last_seq"], layout_ver, layout, statuses, versions)


class Scenario:
    def __init__(self, args: argparse.Namespace, active: Any) -> None:
        self.url = f"/api/v1/events/{args.event_id}"
        self.cookie_name = args.cookie_name
        self.slow_delay = args.slow_delay
        self.active = active
        self.metrics = Metrics()
        self.layouts: dict[int, dict[int, int]] = {}
        self.cached_snapshot: tuple[bytes, Probe] | None = None

    async def snapshot(self, client: httpx.AsyncClient, cookie: str, min_layout: int) -> Probe:
        loop = asyncio.get_running_loop()
        started = loop.time()
        response = await client.get(
            self.url + "/seats",
            headers={"Cookie": cookie},
            params={"format": "compact", "min_layout_ver": min_layout},
        )
        response.raise_for_status()
        if self.cached_snapshot is None or self.cached_snapshot[0] != response.content:
            self.cached_snapshot = response.content, decode_snapshot(response.content, self.layouts)
        template = self.cached_snapshot[1]
        if template.layout_ver < min_layout:
            raise SequenceGap("Layout snapshot is behind the requested version")
        record_latency(self.metrics.snapshot, loop.time() - started)
        return template.fork()

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
                if self.active[2] >= 0:
                    self.active[2] = max(self.active[2], self.active[0])
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
                    probe.layout = snapshot.layout
                    probe.statuses = snapshot.statuses
                    probe.versions = snapshot.versions
                    minimum = None
                minimum = await self.subscribe(
                    client, cookie, probe, self.slow_delay if slow else 0
                )
            except SequenceGap:
                self.metrics.total["gaps"] += 1
            except (KeyError, OverflowError, ValueError):
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

    def packed_seats(self, layout: dict[int, int]) -> tuple[array[int], array[int]] | None:
        if len(self.seats) != len(layout):
            return None
        statuses = array("b", [0]) * len(layout)
        versions = array("i", [0]) * len(layout)
        for idx, (status, version) in self.seats.items():
            offset = layout.get(idx)
            if offset is None:
                return None
            statuses[offset] = _STATUS_CODE[status]
            versions[offset] = version
        return statuses, versions

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
            probe = initial.fork()
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

    layout = scenario.layouts.get(final.layout_ver)
    expected = final.packed_seats(layout) if layout is not None else None
    matched = sum(
        probe.applied == final.head
        and probe.layout_ver == final.layout_ver
        and (probe.statuses, probe.versions) == expected
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


def run_writer(
    args: argparse.Namespace, active: Any, channels: list[Connection], deadline: float
) -> tuple[bool, int]:
    with args.writer_log.open("w") as log:
        while True:
            with active.get_lock():
                if active[0] == args.connections:
                    break
            remaining = deadline - monotonic()
            if remaining <= 0:
                print(
                    f"Writer skipped: fewer than {args.connections} concurrent SSE connections",
                    file=log,
                )
                return False, 0
            if wait(channels, timeout=min(0.1, remaining)):
                raise SystemExit("An SSE worker exited while waiting for connections")

        command = [
            sys.executable,
            str(Path(__file__).with_name("scenario_b_writer.py")),
            "write",
            "--event-id",
            str(args.event_id),
            "--workers",
            "4",
            "--rate",
            str(args.write_rate),
            "--start-delay",
            "0",
            "--duration",
            str(args.write_duration),
        ]
        with subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT) as writer:  # noqa: S603
            with active.get_lock():
                active[2] = active[0]
            try:
                while (status := writer.poll()) is None:
                    if wait(channels, timeout=0.1):
                        raise SystemExit("An SSE worker exited while the writer was running")
            finally:
                if writer.poll() is None:
                    writer.kill()
                with active.get_lock():
                    peak = active[2]
                    active[2] = -1
    return status == 0, peak


def run_scenario(args: argparse.Namespace, tokens: list[str]) -> bool:
    ctx = multiprocessing.get_context("spawn")
    # Current, overall peak, and writer-window peak (-1 when the writer is inactive).
    active = ctx.Array("i", [0, 0, -1])
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
        deadline = started + args.duration
        writer_ok, write_peak = True, None
        if args.writer_log is not None:
            writer_ok, write_peak = run_writer(args, active, channels, deadline)
        if wait(channels, timeout=max(0, deadline - monotonic())):
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
        if write_peak is not None:
            report["peak_active_during_writes"] = write_peak
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
            writer_ok
            and matched == len(tokens)
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
    parser.add_argument(
        "--writer-log", type=Path, help="run the writer after all streams connect and log here"
    )
    parser.add_argument("--write-rate", type=float, default=100)
    parser.add_argument("--write-duration", type=float, default=55)
    args = parser.parse_args()
    if (
        args.connections <= 0
        or args.processes <= 0
        or args.duration <= 0
        or min(args.write_rate, args.write_duration) <= 0
        or min(args.drain_timeout, args.slow_delay, args.ramp_seconds) < 0
    ):
        parser.error(
            "connections, processes, durations and write rate must be positive;"
            " delays must be nonnegative"
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
