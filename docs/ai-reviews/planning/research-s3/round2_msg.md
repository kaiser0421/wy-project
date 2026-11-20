# Round 2 — my responses to your critique (draft v1 = ticketing platform security/import/report design)

Accepted as-is: #3 (finalize tx rechecks permission under lock), #4 (fencing token per attempt, DB clock leases, polling + NOTIFY as wakeup only), #6 (flag every member of a duplicate group; no silent ON CONFLICT DO NOTHING — conflict = job error), #7 (CSV start line incl. header; JSON item index + start line), #8, #9 (sync generation in API to disk temp file + FileResponse, cgroup memory.peak measurement), #11 (428/412/409 semantics), #13-#17.

Pushbacks / questions:

A. Auth cache. I agree the naive DEL-then-refill race is real. But "PG on every request" means 3,000 VUs hammering holds adds ~1 extra query per request on the hottest path. Proposal v2:
   - Default: NO cache; sessions validated by a single PK-index query `SELECT s.user_id,u.role,u.is_active FROM sessions s JOIN users u ... WHERE s.token_hash=$1 AND s.revoked_at IS NULL AND s.expires_at>now()` on the same pooled connection used by the request (not a separate checkout). Measure. 
   - Optional, behind a flag, only if measurement shows >X% cost: Redis cache with race-free invalidation:
     * logout: after DB commit, `SET sess:{h} "REVOKED" EX 3600` (tombstone, unconditional), never DEL.
     * reader on miss: read `ugen:{uid}` FIRST, then PG, then `SET sess:{h} {payload,gen} NX EX 30`. NX fails if tombstone present.
     * disable/role change: after DB commit, `INCR ugen:{uid}`; on hit, reader MGETs `sess:{h}` + `ugen:{uid}`, rejects entry if gen mismatch → falls back to PG.
     * Redis unavailable or keys evicted → fallback to PG (fail-closed to DB, not fail-open). maxmemory-policy noeviction for auth keys? Or separate logical concerns.
     * Remaining window = requests that read PG before the revocation commit — that's linearization, not staleness.
   Is this cache sound? What race still exists (Redis restart losing tombstones but keeping cache entries via AOF? replica lag?) Would you still reject it?

B. Role change revoking ALL sessions including promotion: with per-request DB role read there's no stale role. Revoke-on-promotion matters only as "renew session id on privilege change" (anti-fixation). Demotion: revoke is not needed for correctness. My position: revoke all sessions on disable; on role change bump nothing, role is read live; on promotion also rotate (revoke) existing sessions to follow OWASP session renewal. Demotion: keep sessions (user stays logged in with fewer rights). Agree?

C. Import dedupe & mode. Requirement literally: same content on same event → return existing job. You say even if mode differs return existing. Then a user whose all_or_nothing job failed cannot run skip_invalid on the same file except by editing the file. My proposal: dedupe key = (event_id, sha256) regardless of mode; response 200 with existing job + `"deduplicated": true`; to change mode, `POST /seat-imports/{job_id}/attempts {mode}` allowed only when job is terminal-failed (validation_failed or system_failed) and never when succeeded/partially succeeded. Each attempt has own fencing token & error report. Agree? Any abuse (DoS by retry loops)?

D. Pagination. Materializing the ordered ID list per query (Redis, TTL) feels heavy. Counter-proposal: only allow sort keys and filters on fields where the guarantee can hold, define result set "as of page 1":
   - cursor carries snapshot_ts = DB now() at page 1 and is HMAC-signed with filters/sort/subject.
   - Allowed sorts: created_at (immutable), sales_start_at, title. For immutable sort: keyset `(created_at,id) > (...)` AND `created_at <= snapshot_ts` → inserts after page 1 are excluded (no dup/omission). Late-commit insert with created_at < snapshot_ts can appear in a later page: it's a new row not in page-1 view, so appearing = "extra", not duplicate/omission. Acceptable?
   - For mutable sort keys (sales_start_at, title) and mutable filters (status, category): updates between pages can cause a dup or a skip. Options: (i) make them immutable after publish (sales_start can't change after publish? unrealistic), (ii) materialize ID snapshot only for mutable sort (cap 10k ids, 10 min TTL), (iii) temporal table events_history and query "AS OF snapshot_ts" (WHERE valid_from <= ts < valid_to) — heavy.
   Which do you recommend for a take-home graded on correctness + clarity? I lean: keyset on (created_at,id) default + snapshot_ts; for mutable sorts store a versioned row history? or (ii)?

E. Argon2id: RFC 64MiB/p=4 × concurrent logins in a 512MB container is dangerous. Plan: m=19MiB t=2 p=1 (OWASP minimum) + asyncio.Semaphore(4) per replica dedicated thread pool → max 76 MiB; login beyond capacity gets 503 + Retry-After quickly rather than queuing. Benchmark target 30–60 ms/hash. OK?

F. Cookie vs bearer: `__Host-sid` Secure cookie on http://localhost — Chrome/Firefox treat localhost as potentially trustworthy so Secure cookies work; but if users access via LAN IP over http it breaks. Plan: COOKIE_SECURE env (default true), name `__Host-sid` when secure else `sid`. Bearer allowed for non-browser clients (loadtest, Swagger "Authorize"); a request presenting both → 400. Bearer requests skip CSRF. Risk: an XSS could read a bearer token only if the SPA stores one — SPA never gets a bearer token (login sets cookie only; bearer issued via separate `POST /auth/tokens`?). Is this split worth it, or just cookies everywhere incl. k6?

F2. WebSocket authorization re-check: "check authoritative session state before dispatching protected data" for 2,000 sockets per seat update is expensive. Seat-map data is readable by all roles incl. Viewer — is seat-state stream even "protected data" beyond being authenticated? My plan: revalidate each socket's session in a batched query every 5 s (SELECT token_hash FROM sessions WHERE id = ANY($1) AND valid) + immediate pubsub close; worst-case stale window 5 s for a public-ish stream; admin metrics/alerts sockets revalidate per message batch (low fan-out). Acceptable bound?

G. Concrete attack scenarios I want you to try to break:
   G1. Viewer calls GET /events/{id}/seat-imports/{job_id}/errors for a job on a draft event — should Viewer see drafts? (spec: Viewer browses events, seats, all reports). My policy: Viewer sees only published events & their seats; reports for all events (incl. draft? reports of drafts are empty). Errors report: owner + admin only.
   G2. User A uploads a file to his event, B uploads the same bytes to B's event: no cross-event dedupe leakage since key includes event_id. OK.
   G3. Payment callback replay with a valid HMAC for order X but body tampered amount — HMAC over raw body + timestamp + nonce header; dedupe on (provider_txn_id).
   G4. Import of 100k rows into an event, then the owner publishes the event mid-import → finalize tx locks event FOR UPDATE and sees status != draft → job fails 'event_published'. Publish tx must also FOR UPDATE the event; does publish need to wait for/cancel running imports?
Please answer A–G concretely, say where you still disagree and why.
