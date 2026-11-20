# Session 2 DRAFT v1 — Real-time push, observability, performance

Assumptions (decided elsewhere): PostgreSQL 16 = source of truth; transactional outbox table written in same tx as seat/order changes; N FastAPI replicas (default 3) behind nginx; Redis available; mock payment service; import worker; hold-expiry worker. Docker Compose single host.

## 1. Real-time seat push

### 1.1 Transport: SSE vs WebSocket
Choice: **WebSocket** at `/ws/events/{event_id}` (auth via short-lived ticket query param obtained from `POST /api/v1/ws-tickets`), server->client only (client sends only `resync`/ping).
Alternative considered: SSE (`text/event-stream`) — simpler, built-in `Last-Event-ID` resume, HTTP/1.1 6-connection browser limit (irrelevant for one event page), works through proxies. Honestly SSE fits a one-directional feed better; WebSocket chosen because ... (open question — see Codex round).
Leaning: **SSE** with `Last-Event-ID`= seq, since the stream is one-way and reconnection semantics are standardized. Load tool (k6) supports WS natively; SSE via xk6-sse extension or plain HTTP streaming in Locust/custom asyncio client.

### 1.2 Sequence numbering
`events.seat_seq BIGINT` column on the activity (event) row. Every tx that changes seat state does
`UPDATE events SET seat_seq = seat_seq + 1 WHERE id=$1 RETURNING seat_seq` and writes outbox row `(event_id, seq, payload)`.
=> per-event total order assigned in DB; seq is gap-free and commit-ordered because the row lock on events serializes seat-changing txs for that event.
Concern: hot row contention — 3,000 VUs on one event all serialize on `events` row. Each tx holds the lock until commit (~1-3 ms). Max ~300-1000 tx/s per event. Alternative: batch seq assignment by the relay (relay assigns seq at publish time, single relay per event via advisory lock) -> seq order = relay order, not commit order, but still a total order consistent with what snapshot readers see IF snapshot reports `last_seq` from the same relay-maintained counter.

Draft decision: **relay assigns seq** (no hot row in the purchase path). Outbox rows get `id BIGSERIAL`; relay reads committed rows `ORDER BY id` with a safety on in-flight lower ids (xmin horizon / "commit gap" problem: BIGSERIAL id allocated before commit, so id 101 may commit before id 100). Mitigation: relay only advances past id X when no tx with smaller id is still in progress — use `pg_snapshot_xmin(pg_current_snapshot())` with an `xid8` column on outbox (`txid = pg_current_xact_id()`), and only publish rows whose `txid < snapshot_xmin`. Classic approach.

### 1.3 Fan-out
Relay (single leader via `pg_try_advisory_lock`, run in a dedicated `outbox-relay` container, 2 instances for HA, one active) -> `XADD seat:{event_id} MAXLEN ~ 10000 * seq <seq> payload <json>` into **Redis Streams**, then marks outbox rows published (or tracks last published id in `relay_cursor` table).
Each API replica: one background task per subscribed event: `XREAD BLOCK 1000 STREAMS seat:{event_id} $last_id` -> in-process fan-out to local subscribers' bounded asyncio queues.
Why Redis Streams over Pub/Sub: persistence/replay for replica reconnect, consumer catch-up by ID; vs LISTEN/NOTIFY: 8KB payload, connection per replica, no replay, notify queue in pg; vs NATS JetStream: extra infra.
Redis stream ID = `seq-0` explicit (`XADD key <seq>-0`) so stream ID == seq; enables `XRANGE seat:{id} (lastSeq +` for resync deltas.

### 1.4 Client protocol
Messages (JSON):
- server `snapshot`: `{type:"snapshot", event_id, seq, seats:[[seat_id,status_code],...] compressed as bitmap/base64 }`
- server `delta`: `{type:"seat", event_id, seq, changes:[{seat_id, status, ver}], ts}`
- server `resync_required`: `{type:"resync", reason:"gap|slow|stream_trimmed"}`
- server `hb`: `{type:"hb", seq: latest}` every 15s (lets idle client detect gap without new events)
Client: keeps `last_seq`; if `delta.seq != last_seq+1` -> gap -> fetch `GET /api/v1/events/{id}/seats/snapshot` (returns seats + `as_of_seq`), buffer deltas received meanwhile, apply those with seq > as_of_seq.
Snapshot consistency: snapshot read from Postgres in REPEATABLE READ tx, but seq is relay-assigned… problem: snapshot `as_of_seq` must correspond to DB state. Option: snapshot served from Redis materialized seat-state hash maintained by relay atomically with XADD (Lua: HSET + XADD + SET last_seq) — then snapshot and seq are consistent by construction. DB remains truth; Redis view is a cache rebuilt from DB on relay start.

### 1.5 Slow consumer
Per-connection `asyncio.Queue(maxsize=256)`. On full: drop queue, send `resync_required{reason:"slow"}` (or close with code 4008), client fetches snapshot. Alternative: coalescing (latest status per seat in dict) — bounded by #seats (10k) — nice for seat maps since only latest state matters. Draft: coalesce into dirty-seat map + send-frame cap; if dirty map > 2,000 seats send snapshot instead.
Worst-case staleness: outbox poll interval (50ms w/ LISTEN wakeup) + relay batch + Redis XREAD + send queue; bound by socket send timeout 5s -> then disconnect; client reconnect + snapshot. Claim: worst case wrong for <= ~10s, and only the "available vs held/sold" status; server always re-validates on purchase (409 if seat taken), so staleness never causes oversell.

## 2. Scenario B: 2,000 WS connections on one event
- 3 replicas x 1 uvicorn worker each (or 2 workers each with uvloop) -> ~700 conns/process. Each idle WS ~ 30-60KB memory in Python (websockets lib) -> ~100MB total. Fine.
- Fan-out cost: 1 event -> 2,000 sends. Pre-serialize JSON once per event (orjson bytes), send same bytes. Python send ~ 20-50us each -> 2,000 sends ~ 40-100ms per event per process (split across 3 -> ~30ms). Under Scenario A purchase rate ~ 300-1000 seat events/s, 2,000 subscribers => 0.6-2M msgs/s — too much! => **batch deltas per tick**: per event, aggregate changes over 100ms window into one frame -> 10 frames/s x 2,000 = 20k sends/s total. OK.
- ulimits: nofile 65536 in compose for api and nginx; nginx `worker_connections 16384`, `proxy_read_timeout 3600s`, `proxy_buffering off`, `Upgrade`/`Connection` headers, `ip_hash` not needed (stateless resubscribe).
- Measure delivery latency: each delta carries `ts_commit` (DB `clock_timestamp()` at outbox insert) and `ts_relay`; load client records receive time; since load gen on same host, same clock -> latency = recv - ts_commit. Report p50/p95/p99 and % frames received, gaps, resyncs.

## 3. Monitoring
### 3.1 Measurement point
ASGI middleware in each API replica (pure ASGI, not BaseHTTPMiddleware) measures per-request latency, status, route template. Also nginx access log w/ `$request_time`, `$upstream_response_time` as cross-check (log json to stdout / file; optional).
### 3.2 Aggregation
Each replica keeps per-second buckets keyed by `floor(server_monotonic-mapped-time)`: count, errors (5xx; 4xx counted separately), fixed log-linear histogram (e.g. 0.5ms..60s, ~ 64 buckets, HDR-like with 2 significant digits) — mergeable by summing.
Every second replica pushes its finished bucket `(replica_id, sec, counts...)` to Redis via `HINCRBY metrics:{sec}` ... hmm or `XADD metrics_raw`. Aggregator (single leader, in "monitor" worker) merges buckets for second S at S+2s (watermark grace), computes p50/p95/p99 from merged histogram, publishes to admin WS channel and batches into Postgres `metrics_1s`.
Idempotency: per (replica_id, sec) unique key -> upsert ignore; no double count. Loss: replica crash loses <=1s unsent bucket — acceptable, flagged by "replicas_reporting" count per second.
### 3.3 Clock skew
Timestamp source: replicas stamp with own wall clock -> replica 20s slow would put its buckets 20s in the past: aggregator already closed that second -> late data dropped or causes 20s-old seconds to be revised; chart shows dip of 1/N RPS in current seconds + spike in old. Mitigation: aggregator assigns the time bucket by **receipt time** (aggregator clock) rather than replica timestamp, replicas send every 1s their "delta since last flush"; plus skew detection: compare replica-reported time vs Redis `TIME`, alert if |skew|>2s. Alternatively replicas use Redis TIME at startup to compute offset.
### 3.4 Live connections
Gauge: each replica reports current open WS count each second; aggregator sums latest gauge per replica with TTL 3s (stale replica excluded).
### 3.5 Which source to trust
Proxy (nginx) sees everything including requests that never reached app (502/504, connection refused) — trust nginx for RPS/error rate edge truth; app middleware for per-route business attribution and latency excluding queueing. For seat counts/orders: trust DB.

## 4. Batch persistence
In-process `asyncio.Queue(maxsize=50_000)` for access logs; flusher task every 500ms or 2,000 rows -> `COPY access_logs FROM STDIN` via asyncpg `copy_records_to_table`. On full: drop + increment `dropped_logs` counter (exposed as metric + alert). Seat-change events: already in outbox → archived to `seat_events` by relay in batch (COPY), not dropped. Metrics 1s: aggregator inserts 1 row/s per scope; buffer up to 60s.
Tables partitioned by day (`PARTITION BY RANGE (ts)`), retention job drops partitions > 7 days (pg_partman not needed; simple maintenance task).

## 5. Business metrics & alerting
Metrics: purchase attempts/success/fail by reason (sold_out, conflict, limit, payment_failed), hold count, hold-expiry backlog (holds past expires_at not released), payment callback lag (now - payment created for pending), outbox lag (now - oldest unpublished created_at, and count), relay last heartbeat, WS resync rate, dropped log count, DB pool wait time, import job stalled.
Alert rules evaluated each second by aggregator; state machine (pending -> firing -> resolved), `for` duration, cooldown 60s, dedup key = rule+scope. Persist `alerts` table, push to Admin WS channel `/ws/admin`.

## 6. Tracing
`X-Request-ID` generated at nginx (`$request_id`) and passed to app; app uses contextvar; structured JSON logs (structlog or stdlib + orjson) with request_id, service, replica. Propagate to: outbox rows (request_id column) → relay → WS frames; mock payment calls with header; payment callback carries original request_id in payload; import jobs store request_id and worker logs with it.
Log storage: each service writes JSON logs to a central Postgres `service_logs` table via batched COPY (same pipeline as access logs). Admin endpoint `GET /api/v1/admin/traces/{request_id}` -> logs from all services ordered by ts.
No OpenTelemetry collector/Jaeger (too heavy); keep W3C traceparent-compatible ids optional. No Prometheus/Grafana — dashboard in app (requirement). Optionally expose `/metrics` Prometheus endpoint.

## 7. Load testing
Tool: **k6** (Go, can handle 3,000 VUs on one machine, native WS, thresholds, JSON summary). Locust python would need many workers for 3,000 VUs.
Scenario A: setup creates event with 10k seats, 3k users pre-registered (tokens preloaded via setup endpoint or SQL seed). VUs: loop: GET seats availability -> POST hold (specified seat random or zone+qty) -> pay -> poll. 60s constant-vus. Thresholds. Post-test consistency SQL: no seat sold twice (unique), count(sold) == sum(order tickets paid), per-user <=4, no hold past expiry not released after expiry window, outbox fully published, seat stream final seq == db.
Scenario B: k6 ws 2,000 VUs connect, hold 120s, while separate scenario generates seat changes at fixed rate (e.g. 50 changes/s); measure delivery latency & completeness.
Tuning: uvloop+httptools, orjson responses, asyncpg pool size per replica = 20 (3 replicas -> 60 < max_connections 200); pgbouncer optional (transaction mode breaks asyncpg prepared stmts unless statement_cache_size=0). gunicorn with uvicorn workers? Use uvicorn `--workers 2` per container.
LOADTEST.md: machine spec, versions, config, scenarios, results tables, charts screenshots, bottleneck analysis, consistency check output.

## 8. Front-end
React + Vite + TypeScript SPA served by nginx. Seat map 10k seats -> **Canvas** (or PixiJS) rendering, hit-testing via grid math; updates by seat index into typed array; redraw on rAF. Admin dashboard: uPlot (fast for per-second rolling charts) or ECharts.
