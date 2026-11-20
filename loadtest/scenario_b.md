# Scenario B · seat push

Use a disposable migrated PostgreSQL database. Point the API, relay and scripts at it. The writer creates a dedicated published event and pre-creates one authenticated session per subscriber. It toggles available and held seats in disjoint ranges, using the same transactional outbox writer as booking. Its synthetic order is specific to this fixture, which measures seat delivery.

## Run the Python subscriber and audit

From the repository root, set `DATABASE_URL` and start the API and relay. To exercise routing across replicas, use the nginx address as `--base-url`.

```sh
bench_dir=$(mktemp -d)
.venv/bin/python loadtest/scenario_b_writer.py seed \
  --seats 10000 --connections 2000 --sessions-file "$bench_dir/sessions.json"
```

Use the printed event ID for both commands. Open two terminals so the writer and subscriber overlap.

```sh
.venv/bin/python loadtest/scenario_b_writer.py write \
  --event-id 1 --workers 4 --rate 300 --start-delay 15 --duration 60
```

```sh
.venv/bin/python loadtest/sse_client.py \
  --base-url http://127.0.0.1:8080 --event-id 1 \
  --sessions-file "$bench_dir/sessions.json" --connections 2000 \
  --duration 80 --drain-timeout 20
```

Run with `--rate 50` and `--rate 300`. For a paced start, use `--ramp-seconds` and extend the writer's delay and subscriber duration accordingly. With the default ramp of zero, the client attempts every connection at once. Every tenth connection reads slowly by default. Keep writing finished before the audit starts. During fault runs, stop one API replica while the others remain available, restart it, then separately restart Redis. Allow subscribers to resume using the last sequence they applied.

The Python client seeds the probes from one authenticated compact snapshot, applies versioned changes for every probe, and reports successful connections, peak simultaneous streams, HTTP failures, stream bytes/s, snapshot and connection latency, delta apply latency, gaps, and resyncs. It records progress samples with more than two seconds of unassigned outbox age and reconnects from the last applied cursor if it remains behind the reported head for more than three seconds. Repeated short-lived connections back off with jitter. The audit compares every probe's indexed seat status and version hash with a PostgreSQL snapshot, checks the unassigned outbox count, and compares archived changes with assigned outbox rows. The exit status is nonzero if the run misses the connection or consistency target. `ts` is the earliest `outbox.created_at` in a coalesced delta, so latency from `ts` includes the remaining writer transaction time and is an upper estimate of commit-to-apply latency on synchronized clocks.

The session file contains active credentials. The seed command creates it with owner-only permissions. Keep it out of source control and remove it with the disposable database after the run.

## Optional k6 driver

The k6 driver requires the [xk6-sse extension](https://github.com/phymbert/xk6-sse). Use the Python driver when the extension is unavailable and when you need the PostgreSQL comparison.

```sh
BASE_URL=http://127.0.0.1:8080 EVENT_ID=1 \
SESSIONS_FILE="$bench_dir/sessions.json" CONNECTIONS=2000 DURATION=80s \
k6 run loadtest/scenario_b.js
```

## Ordering and recovery

A writer holds the seat lock when updating its version and allocating the outbox ID. A single relay assigns contiguous per-event sequence numbers in the same PostgreSQL transaction that archives the seat changes. An API process polls that assigned log once per watched event. Every replica sees the same sequence. Each process coalesces a poll to the highest version per seat and encodes the resulting frame once for its subscribers. A layout change ends the current interval so clients can fetch the new mapping before applying subsequent deltas.

A client uses its last applied sequence as the replay cursor. An interval that starts after the next expected sequence indicates a gap. The API replays from its ring, then from PostgreSQL as needed. If history is missing or the replay budget is exhausted, the client receives a resync frame and fetches a snapshot. Clients apply only higher seat versions because the snapshot may include committed changes that the relay has yet to sequence. The progress frame also identifies a growing assigned head or old unassigned events even during periods with few deltas.

Each process retains approximately five seconds or 2 MiB of encoded frames per watched event. Slow clients have one write in flight and a five-second deadline. On deadline the server closes the stream and sends a resync frame if the transport can accept it. The client may temporarily display outdated state for any seat. Recovery time depends on detection, reconnect, and snapshot latency. Sustained database or relay loss has no finite recovery bound. Booking still validates current state in PostgreSQL.

## Local measurements

Runs used a Darwin x86_64 host with one local Uvicorn process and the test containers for PostgreSQL 16.16 and Redis 7.4.12. The sandbox denied CPU and memory inventory. The table reports achieved rates, measured apply latency, and final-state checks.

| Run | Peak streams | Writer changes/s | Apply p50 / p95 / p99 | Database audit |
| --- | ---: | ---: | --- | --- |
| 2,000-client burst, 300 changes/s target | 86 | 13.5 | No delta samples | 0/2,000 matching clients, 0 pending rows, 0 archive mismatches |
| 24 clients, 3-second ramp, 50 changes/s target | 24 | 48.7 | 180 / 2,909 / 3,817 ms | 24/24 matching clients, 0 pending rows, 0 archive mismatches |

The burst had 6,110 HTTP request errors over 8,337 stream attempts and frequent database recheck timeouts. The paced run recorded zero gaps and resyncs, 10 request errors over 115 stream attempts, and 81 stream EOFs. All paced probes recovered and reached the same sequence and seat state. The run did not establish the 2,000-connection or 500 ms p99 target. Multi-replica fault scenarios and per-process resource measurements remain unverified. The k6 driver was syntax-checked and k6 was unavailable locally.
