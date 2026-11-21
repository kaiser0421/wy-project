#!/usr/bin/env bash
# Run against a disposable, already running Compose stack.
set -euo pipefail
cd "$(dirname "$0")/.."

uv run --all-packages python - <<'PY'
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path
from select import select
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic

import httpx
from openpyxl import load_workbook

COMPOSE = ["docker", "compose"]
ROWS = 500_000
SAMPLER_TIMEOUT = 5
CGROUP_SAMPLE = r"""
cd /sys/fs/cgroup || exit 1
read -r limit < memory.max || exit 1
[ "$limit" = 536870912 ] || exit 1
while IFS= read -r request; do
    read -r current < memory.current || exit 1
    anon=$(awk '$1 == "anon" {print $2}' memory.stat) || exit 1
    peak=0
    if [ -r memory.peak ]; then read -r peak < memory.peak || exit 1; fi
    printf '%s %s %s\n' "$current" "$anon" "$peak"
done
"""


def capture(args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def sampler_command(replica):
    return ["docker", "exec", "-i", replica, "sh", "-ec", CGROUP_SAMPLE]


def parse_reading(output, replica):
    fields = output.split()
    if len(fields) != 3 or not all(field.isdigit() for field in fields):
        raise RuntimeError(f"Cannot read cgroup v2 memory counters for {replica}")
    return list(map(int, fields))


@contextmanager
def open_sampler(replica):
    proc = subprocess.Popen(
        sampler_command(replica), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    )
    try:
        yield proc
    finally:
        try:
            proc.stdin.close()
            proc.wait(timeout=SAMPLER_TIMEOUT)
        except subprocess.TimeoutExpired:
            pass
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            proc.stdout.close()


def read_sample(proc, replica):
    # Only one byte is ever outstanding on stdin. Read stdout without buffering
    # so a partial line cannot take the read past its deadline.
    os.write(proc.stdin.fileno(), b"\n")
    deadline = monotonic() + SAMPLER_TIMEOUT
    output = b""
    while not output.endswith(b"\n"):
        ready, _, _ = select([proc.stdout], [], [], max(0, deadline - monotonic()))
        if not ready:
            proc.kill()
            proc.wait()
            raise TimeoutError(f"Timed out reading cgroup v2 memory counters for {replica}")
        chunk = os.read(proc.stdout.fileno(), 128)
        if not chunk or len(output) + len(chunk) > 128:
            raise RuntimeError(f"Invalid cgroup v2 sampler output from {replica}")
        output += chunk
    return parse_reading(output, replica)


def download(client, event_id, target, replicas):
    peaks = {replica: [0, 0, 0] for replica in replicas}
    stop = Event()
    with ExitStack() as stack:
        samplers = {
            replica: stack.enter_context(open_sampler(replica))
            for replica in replicas
        }

        def sample():
            for replica, proc in samplers.items():
                if stop.is_set():
                    break
                reading = read_sample(proc, replica)
                peaks[replica] = [max(a, b) for a, b in zip(peaks[replica], reading, strict=True)]

        def monitor():
            while not stop.wait(0.2):
                sample()

        sample()
        with ThreadPoolExecutor(max_workers=1) as executor:
            monitoring = executor.submit(monitor)
            start = monotonic()
            try:
                with client.stream(
                    "GET", f"/api/v1/events/{event_id}/reports/sales.xlsx", params={"tz": "Asia/Taipei"}
                ) as response, target.open("wb") as output:
                    response.raise_for_status()
                    request_id = response.headers["x-request-id"]
                    for chunk in response.iter_bytes():
                        if monitoring.done():
                            monitoring.result()
                        output.write(chunk)
                elapsed = monotonic() - start
            finally:
                stop.set()
            monitoring.result()
        stop.clear()
        sample()
    return request_id, elapsed, peaks


replicas = capture([*COMPOSE, "ps", "-q", "api"], timeout=SAMPLER_TIMEOUT).splitlines()
if not replicas:
    raise SystemExit("No running API containers found")
for replica in replicas:
    try:
        reading = capture(sampler_command(replica), input="\n", timeout=SAMPLER_TIMEOUT)
        parse_reading(reading, replica)
    except (subprocess.SubprocessError, RuntimeError) as error:
        raise SystemExit(
            f"API container {replica} must expose readable cgroup v2 memory counters "
            f"with memory.max=536870912: {error}"
        ) from error
origin = os.environ.get("EXPORT_ORIGIN")
if not origin:
    binding = capture([*COMPOSE, "port", "nginx", "8080"], timeout=SAMPLER_TIMEOUT)
    origin = f"http://127.0.0.1:{binding.rsplit(':', 1)[1]}"
email, password = subprocess.check_output([
    "docker", "exec", replicas[0], "sh", "-c",
    'printf "%s\\n%s" "$SEED_ADMIN_EMAIL" "$SEED_ADMIN_PASSWORD"',
], text=True, timeout=SAMPLER_TIMEOUT).split("\n", 1)

with httpx.Client(base_url=origin, headers={"Origin": origin}, timeout=330, trust_env=False) as client:
    login = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    login.raise_for_status()
    if login.json()["user"]["role"] != "admin":
        raise SystemExit("The seed account must have the Admin role")
    print(f"Seeding {ROWS:,} orders in Compose PostgreSQL…", flush=True)
    with Path("scripts/seed_orders_500k.sql").open() as seed:
        result = capture([
            *COMPOSE, "exec", "-T", "postgres", "sh", "-c",
            'exec psql -X -v ON_ERROR_STOP=1 -v rows="$1" -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
            "sh", str(ROWS),
        ], stdin=seed)
    match = re.search(rf"^Seeded {ROWS} orders for event ([0-9]+)$", result, re.MULTILINE)
    if not match:
        raise SystemExit(f"Could not read seeded event ID from psql:\n{result}")
    event_id = match[1]
    # Refresh the session after the seed, which can take several minutes.
    client.post("/api/v1/auth/login", json={"email": email, "password": password}).raise_for_status()
    output = Path(f"sales-500k-{event_id}.xlsx")
    with TemporaryDirectory(prefix=".sales-500k-", dir=output.parent) as scratch:
        target = Path(scratch) / output.name
        print(f"Downloading event {event_id} through nginx…", flush=True)
        request_id, elapsed, peaks = download(client, event_id, target, replicas)
        workbook = load_workbook(target, read_only=True)
        try:
            rows = workbook["Orders"].iter_rows(values_only=True)
            if next(rows)[0] != "Order Number":
                raise SystemExit("Unexpected Orders header")
            count = sum(1 for _ in rows)
            if count != ROWS:
                raise SystemExit(f"Expected {ROWS:,} orders, got {count:,}")
        finally:
            workbook.close()
        os.link(target, output)

entry = capture([
    *COMPOSE, "exec", "-T", "nginx", "sh", "-c",
    'grep -F "$1" /data/logs/nginx-access.json | tail -n 1',
    "sh", f'"request_id":"{request_id}"',
])
upstream = json.loads(entry)["upstream"] if entry else ""
print(f"{count:,} orders verified in {output}")
print(f"Download: {elapsed:.2f} s (request {request_id}, upstream {upstream or 'unavailable'})")
for replica, (current, anon, peak) in peaks.items():
    details = capture([
        "docker", "inspect", "-f",
        "{{.Name}} {{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", replica,
    ]).split()
    serving = " (export upstream)" if any(f"{ip}:8000" in upstream for ip in details[1:]) else ""
    print(f"{details[0].lstrip('/')}{serving}: sampled memory.current peak={current:,} bytes, "
          f"sampled anon peak={anon:,} bytes"
          + (f", lifetime memory.peak={peak:,} bytes" if peak else ""))
PY
