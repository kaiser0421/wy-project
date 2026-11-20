# Session 3 draft v1 — Security, bulk import, reporting, platform & delivery

Context: FastAPI (N replicas behind nginx/traefik), PostgreSQL = source of truth, Redis, background workers, Docker Compose. Requirements in requirement.txt.

## 1. AuthN
Decision: opaque session tokens (256-bit random, stored as SHA-256 hash in `sessions` table), NOT JWT.
- sessions(id, token_hash UNIQUE, user_id, created_at, expires_at, last_seen_at, revoked_at, user_agent, ip)
- users(id, email citext unique, password_hash, role, is_active, auth_version int, updated_at, version)
- Validation per request: Redis GET `sess:{hash}` -> {user_id, role, is_active, auth_version, exp} (TTL 30 s). Miss -> one indexed PG query (sessions JOIN users WHERE token_hash=$1 AND revoked_at IS NULL AND expires_at>now() AND is_active) -> SET with TTL 30s.
- Invalidation: logout = UPDATE sessions SET revoked_at=now() + DEL Redis key, in that order (DB commit first, then DEL; if DEL fails, TTL 30s bounds staleness). Disable/role change = UPDATE users SET is_active/role, auth_version=auth_version+1; revoke all sessions (disable) and DEL all `sess:*` keys for that user via a Redis SET `user_sess:{user_id}` index; also publish `auth:invalidate` to Redis pub/sub so WebSocket connections owned by that user are closed.
- Consistency bound: worst case = TTL (30 s) only if the Redis DEL fails AFTER DB commit; normal path = 0 (DEL synchronous before HTTP 204 response). Alternative: no cache at all — one PK lookup ~0.3 ms in PG; maybe just do that? Measurement plan: pytest-benchmark / locust comparing (a) no auth, (b) Redis hit, (c) PG lookup; report p50/p99 added latency and PG QPS.
- Role change: keep sessions alive but cached role is invalidated (DEL), so next request reads new role.
- Why not JWT: requirement says logout must invalidate immediately → JWT needs denylist lookup each request anyway → loses stateless benefit, adds key rotation complexity. Opaque token is simpler & revocable.
- Password: argon2id via argon2-cffi, m=19456 KiB (19 MiB), t=2, p=1 (OWASP min) — maybe m=64MiB,t=3,p=1 (RFC 9106 second recommendation). Hash in threadpool (run_in_threadpool) so event loop not blocked. Login under load test: tickets bought by pre-logged-in users so hashing cost does not dominate.
- Rate limiting: login: per IP+email 5/min sliding window in Redis + per IP 30/min; constant-time response for unknown user (verify against dummy hash). Register: per IP 10/h.
- Transport: SPA on same origin (nginx serves frontend & proxies /api, /ws). Use HttpOnly Secure SameSite=Lax cookie `sid` + CSRF double-submit token (header X-CSRF-Token compared to non-HttpOnly cookie) for unsafe methods. Also accept `Authorization: Bearer` for load-test scripts and API clients (no CSRF needed for bearer). WebSocket: cookie sent on handshake; validate Origin header against allowlist (prevents CSWSH); alternatively short-lived single-use WS ticket from POST /ws-tickets.

## 2. AuthZ
- Roles: admin, user, viewer. Permissions are actions: event:create, event:update(own), event:delete(own), event:publish(own), seat:import(own event), order:create, order:read(own), report:read(own event | viewer all | admin all), admin:users, admin:logs, metrics:read(admin), etc.
- Enforcement: every route declares `dependencies=[Depends(require(Policy.X))]` via a custom APIRouter subclass `SecureRouter` that refuses to register a route without `policy=` kwarg (raises at import). Public routes must declare `policy=Public` explicitly.
- Ownership: resource-level check in service layer: `load_event_for(user, event_id, action)` returning 404 if not visible, 403 if visible but not allowed. Queries scoped: orders WHERE user_id = :me unless admin.
- Systematic verification:
  1. Route introspection test: iterate app.routes (incl. WebSocket routes), assert each has policy metadata; fail listing missing.
  2. Matrix test: YAML/py table expected[(method,path)][role] ∈ {allow, deny, own-only}; parametrized test generates requests for each role against fixtures (own resource vs others' resource) and checks status codes (2xx/403/404). Test fails if a route is not in the matrix (completeness).
  3. Negative IDOR tests auto-generated: for every path param `{event_id}`, `{order_id}`, `{job_id}` use another user's resource.
- Easily missed endpoints: WS subscribe (no Depends by default for websockets in some patterns), import job progress / SSE (job_id IDOR), error report download, report download (Viewer allowed all; User only own), payment callback (no user auth → HMAC instead), admin log query, metrics WS, OpenAPI docs (should they be public?), healthcheck.
- Most likely missed: import job error-report download — it's a secondary resource keyed by job_id, often fetched via a link, and developers check event ownership on upload but not on the derived file.

## 3. Disabled user with running import job
Decision: cancel. Worker checks `users.is_active` and `events.created_by` at each batch boundary (job heartbeat) ; on disabled → mark job `cancelled` with reason `creator_disabled`, rollback staging (all_or_nothing: nothing committed; skip_invalid: we still commit at the end atomically, so nothing). Justification: disabling is a security action (compromised account); continuing would let an attacker's mutation complete after revocation; "permission at creation time" is a confused-deputy. Admin can re-run job as themselves.

## 4. OWASP API Top 10 (2023)
API1 BOLA: ownership checks + matrix tests; UUIDv7 ids (not enumerable but not relied on).
API2 Broken auth: opaque sessions, argon2id, rate limits, lockout-free (avoid DoS), session rotation on login.
API3 BOPLA / mass assignment: Pydantic models with `extra='forbid'`, separate Create/Update/Read schemas; `role`, `is_active`, `created_by`, `status` never in user-writable schemas; response models filter fields (no password_hash).
API4 Unrestricted resource consumption: body size limits in nginx (client_max_body_size 21m for import route, 1m elsewhere), page size cap 100, rate limits, upload row cap, Excel export concurrency limit (1 per user, N global), WS connection cap per user.
API5 BFLA: admin routes under /admin with router-level policy.
API6 sensitive business flows: bot-buying — per-user rate limit on hold/order, max 4 tickets, idempotency key; captcha out of scope.
API7 SSRF: no URL-fetching features. Payment callback URL is config, not user-provided.
API8 Misconfig: security headers (CSP, X-Content-Type-Options, frame-ancestors 'none', HSTS when TLS, Referrer-Policy), CORS: same-origin so CORS disabled; if needed explicit allowlist, no `*` with credentials. Docs: /docs available but could be disabled in prod via env.
API9 Inventory: OpenAPI as single source, versioned /api/v1.
API10 unsafe consumption: payment callbacks validated with HMAC-SHA256 over raw body + timestamp, 5-min window, constant-time compare, idempotent by payment_id; amount/currency cross-checked with order.
Uploads: size limit streamed (reject > 20MB while reading), rows > 100k reject, max line length 1 KiB, encoding UTF-8 (strip BOM) only, reject NUL bytes, no zip accepted (so no zip bombs), JSON parsed streaming (ijson) to avoid memory blowup and deeply nested docs; field regex validation (zone_code ^[A-Za-z0-9_-]{1,16}$ etc.).
CSV injection: in Excel export, values starting with = + - @ \t \r are prefixed with `'` or written as explicit strings (xlsxwriter write_string; set strings_to_formulas=False).
Secrets: .env (not committed), .env.example with placeholders; docker secrets optional; app refuses start if SECRET defaults in prod mode.
Supply chain: uv.lock pinned, pip-audit in CI, trivy image scan, ruff bandit rules (S), gitleaks.
Container: python:3.12-slim multi-stage, non-root uid 10001, read_only: true with tmpfs /tmp, cap_drop ALL, no-new-privileges, healthcheck.

## 5. Bulk seat import
API: POST /api/v1/events/{id}/seat-imports (multipart file, mode, optional Idempotency-Key) → 202 {job}; GET .../seat-imports/{job_id}; GET .../seat-imports/{job_id}/errors (CSV); progress via WS channel or SSE or polling.
Steps:
1. API streams upload to a temp file / or directly to PG large object? Decision: stream to shared volume `/data/imports/{uuid}` (or store bytes in PG bytea table `import_files` — 20MB ok). Compute SHA-256 while streaming; enforce 20MB.
2. Idempotency: UNIQUE (event_id, content_sha256, mode?) on import_jobs. INSERT ... ON CONFLICT DO NOTHING RETURNING; if conflict, return existing job (200 instead of 202). Question: should a failed job be re-runnable with same content? Decision: return existing job always (per requirement), unless previous job failed with system error → allow retry via explicit POST /retry.
3. Event must be draft (imports before publish). Lock: only one running import per event (partial unique index on import_jobs(event_id) WHERE status IN ('queued','running')).
4. Worker picks job (SELECT ... FOR UPDATE SKIP LOCKED with lease). Parse streaming: csv module over TextIOWrapper(utf-8-sig); JSON via ijson items. For each row: line_no, zone_code, row_label, seat_number, basic syntax error → error row. Write valid-syntax rows via COPY into UNLOGGED staging table `seat_import_rows(job_id, line_no, zone_code, row_label, seat_number, error text)` in batches of 10k; update progress (rows_parsed) after each batch.
5. Set-based validation in SQL:
   - unknown zone: UPDATE s SET error='unknown_zone' FROM ... WHERE NOT EXISTS zone
   - duplicates in file: window function `row_number() over (partition by zone,row,number order by line_no) > 1` → error 'duplicate_in_file' (first occurrence ok? or all occurrences error? decide: all occurrences after first flagged, first kept — or flag all? Requirement: "檔案內重複...的列，視為錯誤列" → I'd flag all but the first.)
   - existing seats: JOIN seats → 'duplicate_existing'.
6. Commit: all_or_nothing: if any error → job failed, nothing inserted, errors report. Else INSERT INTO seats SELECT ... FROM staging WHERE job_id AND error IS NULL ON CONFLICT DO NOTHING (protect race) — in one tx. skip_invalid: insert valid ones.
7. Error report: generated from staging on demand (GET .../errors streaming CSV) with line_no, raw value, reason; cap stored errors? store all.
8. Cleanup: DELETE staging rows for job after completion (or TRUNCATE partition per job).
Performance: 100k rows COPY ~0.3–1 s; validation queries < 2 s with index on (job_id, zone, row, number). Target total < 10 s.
Line numbers: CSV physical line numbers vs record numbers when quoted fields contain newlines: use csv reader.line_num (end line). JSON: array index + 1 (or "item index"), document in API.
Worker tech: custom Postgres job queue (jobs table, SKIP LOCKED, lease_expires_at, attempts) vs arq (Redis). Choose arq? I lean Postgres-based because job state already must be in PG for progress/idempotency and crash recovery is a SQL query on expired leases; LISTEN/NOTIFY for wakeup. Also used for export jobs and hold expiry sweeps.
Crash recovery: lease 30s renewed every batch; reaper resets expired leases → queued, attempts+1; max 3 attempts then failed. Job steps idempotent: staging rows deleted at start of each attempt; final insert in single tx so either committed fully or not.
Progress: worker updates import_jobs.processed_rows + NOTIFY / Redis pubsub `job:{id}` → API WS pushes to the job owner; client can also poll.

## 6. Excel report
- GET /api/v1/events/{id}/reports/sales.xlsx?tz=Asia/Taipei — decision: background job (POST /reports → job; GET /reports/{id}/download) vs streaming response. Draft decision: background job in worker with xlsxwriter constant_memory, file to shared volume, download via FileResponse; or for simplicity streaming response directly in API replica... Requirement says "API instance with 512MB memory" — suggests generation happens in API. I'd do: generate in API process within a threadpool, writing to temp file with xlsxwriter constant_memory=True, reading orders via server-side cursor (asyncpg cursor / psycopg named cursor) in batches of 5k, then stream file with FileResponse and delete after send. Concurrency limited via semaphore(1-2) per replica + Redis global limit.
- Sheets: Summary (event, generated_at in tz, tz name, totals: orders, tickets, gross, service fee, net, by status), Zones (zone, price, capacity, sold, held, available, revenue), Orders (order_id, created_at (tz), paid_at, user email(masked?), zone, seats, qty, subtotal, fee, total, status). Header "Created At (Asia/Taipei, UTC+08:00)".
- Money: NUMERIC in DB → write_number with num_format '#,##0'; integer TWD.
- Datetimes: convert in SQL (`created_at AT TIME ZONE $tz`) or in Python zoneinfo; write as Excel datetime (naive local) with format 'yyyy-mm-dd hh:mm:ss'.
- tz validated against zoneinfo.available_timezones().
- Memory test: docker run with --memory=512m; seed 500k orders via generate_series SQL; call endpoint; record peak RSS via /sys/fs/cgroup/memory.peak (cgroup v2) or psutil sampling; CI test marked slow; assert success & peak < 400MB.
- Authorization: admin all; viewer all; user own events only.

## 7. Event list pagination
- Keyset cursor: sort keys whitelist (sales_start_at, created_at, title) + id tiebreaker; cursor = base64url(JSON {k: last sort value, id, sort, dir, filters hash}) HMAC-signed to avoid tampering. WHERE (sales_start_at, id) > ($1,$2) ORDER BY sales_start_at, id LIMIT n+1. Inserts during paging: no duplicates/omissions for rows existing before; new rows appear if after cursor. Updates of sort key may move rows — documented; choose immutable default sort (created_at,id).
- Filters: category, status (published only for non-owners), q (title ILIKE / trigram), date range.
- Optimistic locking: events.version int; PATCH requires If-Match: "v3" (ETag) → UPDATE ... WHERE id=$1 AND version=$2 RETURNING; 0 rows → 412 Precondition Failed (or 409). Missing If-Match → 428.

## 8. Admin
- PATCH /admin/users/{id} {role, is_active} with If-Match; cannot demote/disable self or last admin; audit log table.
- Log query: all services emit structured JSON logs with request_id (propagated via X-Request-ID header from nginx $request_id → API → payment-mock → callback → worker job metadata). Shipping: each service batches logs to PG `service_logs` table (async batched insert, required anyway for "access logs saved to DB in batches"), partitioned by day. GET /admin/logs?request_id=... returns ordered across services. Alternative Loki+Grafana — heavier.

## 9. Repo & delivery
```
/ (monorepo)
  apps/api/ (FastAPI) src/ticketing/{api,core,domain,services,repositories,policies,ws,observability}
  apps/worker/ (shares package with api; same image different command)
  apps/payment-mock/
  apps/frontend/ (Vite + React/TS)
  loadtest/ (k6 scripts: scenario_a.js, scenario_b.js; consistency_check.sql)
  infra/ (nginx conf, compose overrides, grafana?)
  migrations/ (alembic)
  samples/ (seats_100k.csv, seats.json, invalid samples)
  docs/ (architecture.mmd, DESIGN.md ...)
  docker-compose.yml, docker-compose.test.yml, Makefile, .env.example, pyproject.toml (uv workspace)
```
Tooling: uv workspace, ruff (incl. S, B, ASYNC rules), mypy --strict, pytest + pytest-asyncio, hypothesis for parsers, testcontainers? → decision: compose-based test DB (docker compose -f docker-compose.test.yml run --rm tests) because one-command requirement and same PG version as prod; testcontainers needs docker socket inside container.
CI GitHub Actions: lint (ruff, mypy) → unit → integration (services: postgres:16, redis:7) → build images → trivy → pip-audit → compose smoke test (docker compose up -d --build, wait healthy, hit /readyz, run a short k6). 
Migrations: alembic upgrade head run by a one-shot `migrate` service; api depends_on migrate completed_successfully. Seeding: `seed` one-shot creating admin@/user@/viewer@ with passwords from env (defaults documented in README for demo).
Health: /livez (process), /readyz (PG + Redis ping). Graceful shutdown: uvicorn --timeout-graceful-shutdown 20; WS close 1012 on shutdown; worker finishes current batch, releases lease.
Commit strategy: conventional commits, one per logical step, per milestone; keep history.
Docs: README, DESIGN.md (Q1–Q6), LOADTEST.md, AI_USAGE.md, architecture diagram (mermaid in docs + PNG).

## 10. Milestones
M0 skeleton (compose, CI, health, migrations, seed) — DoD: `docker compose up -d --build` healthy, `make test` green in CI.
M1 auth+RBAC+policy tests.
M2 events CRUD, keyset pagination, optimistic locking.
M3 seat import pipeline.
M4 holds/orders/payment mock/callback (session 1/2 core).
M5 realtime WS seats.
M6 monitoring, logs, alerts.
M7 reports Excel.
M8 frontend.
M9 loadtest + docs.
