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
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic

import httpx
from openpyxl import load_workbook

COMPOSE = ["docker", "compose"]
ROWS = 500_000
CGROUP_SAMPLE = r"""
cd /sys/fs/cgroup || exit 1
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


def download(client, event_id, target, replicas):
    peaks = {replica: [0, 0, 0] for replica in replicas}
    stop = Event()
    with ExitStack() as stack:
        samplers = {
            replica: stack.enter_context(subprocess.Popen(
                ["docker", "exec", "-i", replica, "sh", "-ec", CGROUP_SAMPLE],
                text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            ))
            for replica in replicas
        }

        def sample():
            for replica, proc in samplers.items():
                proc.stdin.write("\n")
                proc.stdin.flush()
                reading = list(map(int, proc.stdout.readline().split()))
                if len(reading) != 3:
                    raise RuntimeError(f"Cannot read cgroup v2 memory counters for {replica}")
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
                        output.write(chunk)
                elapsed = monotonic() - start
            finally:
                stop.set()
                monitoring.result()
        sample()
    return request_id, elapsed, peaks


replicas = capture([*COMPOSE, "ps", "-q", "api"]).splitlines()
if not replicas:
    raise SystemExit("No running API containers found")
origin = os.environ.get("EXPORT_ORIGIN")
if not origin:
    binding = capture([*COMPOSE, "port", "nginx", "8080"])
    origin = f"http://127.0.0.1:{binding.rsplit(':', 1)[1]}"
email, password = subprocess.check_output([
    "docker", "exec", replicas[0], "sh", "-c",
    'printf "%s\\n%s" "$SEED_ADMIN_EMAIL" "$SEED_ADMIN_PASSWORD"',
], text=True).split("\n", 1)

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
