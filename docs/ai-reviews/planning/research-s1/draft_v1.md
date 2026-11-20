# Draft v1 — Booking core & data consistency (Session 1)

Context: requirement.txt (ticketing, FastAPI, Docker Compose). Scope: DB, schema, seat state machine, no-oversell, per-user limit, idempotency, mock payment, outbox/convergence, REST API for events/orders/payments, Scenario A perf.

## 1. Database
PostgreSQL 16 = single source of truth for seats/orders/payments/idempotency/outbox. Redis only for: fan-out (pub/sub / streams for realtime), rate limiting, cached read models (seat map snapshot, zone availability hint). Redis is NEVER consulted for a correctness decision. Rationale: one transactional boundary removes dual-write problems; PG row locks + SKIP LOCKED + conditional updates + CHECK/unique constraints give provable guarantees; 10k seats / ~1–2k booking TPS is well within one PG primary.

## 2. Schema sketch
- users(id bigserial, email citext unique, password_hash, role enum(admin,user,viewer), is_active, token_version int, created_at)
- events(id bigserial, owner_id FK users, title, description, category, venue, sales_start_at, sales_end_at, status enum(draft,published), version int (ETag), created_at, updated_at); idx (status, sales_start_at, id), (owner_id, id)
- zones(id, event_id FK ON DELETE CASCADE, code, name, price_twd int CHECK >= 0, unique(event_id, code))
- seats(id bigserial, event_id, zone_id, row_label, seat_number, sort_key int, status enum(available,held,sold), order_id bigint NULL, version int, updated_at; UNIQUE(event_id, zone_id, row_label, seat_number); CHECK ((status='available') = (order_id IS NULL)))
  - partial index for allocation: (zone_id, sort_key) WHERE status='available'
- orders(id bigserial, public_id uuid, user_id, event_id FK RESTRICT, status enum(pending_payment, paid, expired, cancelled, refund_pending, refunded), quantity smallint CHECK 1..4, subtotal_twd int, fee_twd int, total_twd int, expires_at timestamptz, created_at, paid_at, cancelled_at, version)
  - idx (user_id, created_at desc, id); partial (expires_at) WHERE status='pending_payment'
- order_items(order_id, seat_id, zone_id, unit_price_twd, released_at NULL; PK(order_id, seat_id); UNIQUE(seat_id) WHERE released_at IS NULL)  -- defense in depth against double sale
- user_event_quota(user_id, event_id, reserved int NOT NULL DEFAULT 0 CHECK (reserved BETWEEN 0 AND 4), PK(user_id,event_id))  -- counts held+sold tickets
- payments(id uuid, order_id, provider_ref unique, amount_twd, status enum(created, succeeded, failed, refund_pending, refunded), created_at, updated_at); UNIQUE(order_id) WHERE status IN ('created','succeeded')
- payment_callbacks(provider_event_id PK, payment_id, payload jsonb, received_at, processed_at)  -- replay/dedupe
- idempotency_keys(user_id, key, method_path, request_hash bytea, status_code, response_body jsonb, created_at, expires_at; PK(user_id, key))
- outbox(id bigserial, aggregate_type, aggregate_id, event_type, payload jsonb, created_at, published_at NULL); partial idx (id) WHERE published_at IS NULL
- import_jobs(id, event_id, file_sha256, mode, status, total_rows, processed_rows, error_rows, lease_owner, lease_until, created_by, created_at; UNIQUE(event_id, file_sha256)); import_errors(job_id, line_no, reason)

Money: integer TWD everywhere.

## 3. Seat/order state machine
seat: available -> held (booking txn) -> sold (payment success) ; held -> available (expiry / cancel) ; sold -> available (paid order cancelled+refund) .
order: pending_payment -> paid | expired | cancelled ; paid -> refund_pending -> refunded.
Hold TTL default 10 min (env HOLD_TTL_SECONDS). expires_at = now() + ttl computed by DB.

## 4. Booking transaction (READ COMMITTED)
1. pg_try_advisory_xact_lock(hashtextextended(user_id||':'||idem_key)) -> if false: 409 idempotency in progress (Retry-After: 1).
2. SELECT idempotency row; if exists: same hash -> replay stored status/body; different -> 422.
3. Read event (plain SELECT, no lock): must be published and now() in [sales_start, sales_end].
4. Quota: INSERT INTO user_event_quota ... ON CONFLICT DO NOTHING; UPDATE user_event_quota SET reserved = reserved + :n WHERE user_id=:u AND event_id=:e AND reserved + :n <= 4 RETURNING reserved -> 0 rows => 409 purchase-limit.
5a. Specific seats: SELECT id, zone_id FROM seats WHERE event_id=:e AND id = ANY(:ids) AND status='available' ORDER BY id FOR UPDATE SKIP LOCKED; if count < n -> rollback, 409 seat-unavailable.
5b. Zone+qty: SELECT id FROM seats WHERE zone_id=:z AND status='available' ORDER BY sort_key LIMIT :n FOR UPDATE SKIP LOCKED; if count < n -> 409 insufficient-seats.
6. INSERT order (expires_at = now() + ttl), order_items (price snapshot), UPDATE seats SET status='held', order_id=:o, version=version+1 WHERE id = ANY(:locked) AND status='available' (assert rowcount == n).
7. INSERT outbox rows (seat.held per seat, order.created).
8. INSERT idempotency row with response 201 body. COMMIT.
Business 4xx results (409/422) are also stored under the key in a separate tiny txn; 5xx/lock timeouts are not stored (retry re-executes).
Lock order: advisory -> quota row -> seats (SKIP LOCKED, never waits) -> new rows. Sweeper: orders -> seats -> quota. Booking never waits on seats so no cycle.

## 5. Expiry sweeper
Loop every 1s in worker container(s), any number of replicas:
WITH exp AS (SELECT id FROM orders WHERE status='pending_payment' AND expires_at <= now() ORDER BY expires_at LIMIT 500 FOR UPDATE SKIP LOCKED)
UPDATE orders o SET status='expired' FROM exp WHERE o.id=exp.id AND o.status='pending_payment' RETURNING o.id, o.user_id, o.event_id, o.quantity;
then UPDATE seats SET status='available', order_id=NULL, version=version+1 WHERE order_id = ANY(:ids) AND status='held'; UPDATE order_items SET released_at=now(); UPDATE quota SET reserved = reserved - qty; outbox rows. One txn.
Correctness under two workers: SKIP LOCKED + `status='pending_payment'` guard -> each order transitions once.
Clock: DB now(). Payment callback also compares expires_at > now() in DB, so correctness does not depend on sweeper latency.

## 6. Idempotency
Header Idempotency-Key required on POST /orders, POST /orders/{id}/payments, POST /orders/{id}/cancel. Scope (user_id, key); TTL 24h. request_hash = sha256(method + path + canonical JSON body).
- First succeeded: replay same status + body + header Idempotent-Replayed: true.
- In progress: 409 problem type idempotency-request-in-progress + Retry-After: 1.
- Same key, different payload: 422 idempotency-key-reused.
Because the whole operation is one DB txn, there is no orphan in_progress row after kill -9: either the txn committed (row + order) or nothing.

## 7. Mock payment
Separate container `mockpay` (FastAPI). Flow: POST /api/v1/orders/{id}/payments (Idempotency-Key) -> API inserts payments(created), calls mockpay POST /intents {merchant_payment_id, amount, callback_url} (merchant_payment_id is provider idempotency key) -> returns checkout_url. User "pays" in mockpay UI/API (POST /intents/{id}/confirm {outcome}). mockpay sends callback POST /api/v1/payments/mock/callback with headers X-Mockpay-Event-Id, X-Mockpay-Timestamp, X-Mockpay-Signature = hex(HMAC-SHA256(secret, timestamp + "." + raw_body)); retries with exponential backoff up to 24h until 2xx; can deliberately duplicate/reorder (chaos flags).
API callback handler: verify signature (constant-time), |now - ts| <= 300s else 401; INSERT payment_callbacks ON CONFLICT DO NOTHING (duplicate -> 200 no-op); then txn: SELECT order FOR UPDATE, payment FOR UPDATE:
- success & order pending_payment & expires_at > now(): order paid, seats held->sold (WHERE order_id=:o AND status='held'), payment succeeded, outbox.
- success & order expired/cancelled: payment succeeded_late -> refund_pending; outbox refund command -> refund worker calls mockpay POST /refunds (idempotent by payment id).
- failure: payment failed; order stays pending until TTL (user may retry payment).
- out-of-order (failure after success): payment status monotonic, ignored.
Return 200 after durable record; 401 bad signature; 400 malformed.
Reconciler: every 30s, payments in 'created' older than 2 min -> GET mockpay /intents/{id} and apply same handler.
Fee: fee = (subtotal*5 + 50) // 100 (integer arithmetic, round-half-up; subtotal >=0). Never Python round() (banker's) nor float.
Cancellation: POST /orders/{id}/cancel: pending_payment -> cancelled (release seats & quota); paid -> refund_pending (release seats immediately, refund via outbox).

## 8. Outbox & partial failure windows
Relay worker: SELECT ... FROM outbox WHERE published_at IS NULL ORDER BY id LIMIT 500 FOR UPDATE SKIP LOCKED -> publish to Redis stream per event -> UPDATE published_at. At-least-once; consumers dedupe by (seat_id, seat.version).
Windows: (W1) booking committed, response lost -> idempotent replay. (W2) seat changed, outbox not relayed -> relay resumes. (W3) payment captured at provider, callback lost/late, hold expired -> refund. (W4) expiry past but sweeper dead -> callback still rejects by DB clock; seats unavailable until sweeper restarts. (W5) payments row created but provider call crashed -> reconciler. (W6) import half done -> lease expiry, restart from staging.
Most dangerous: W3 (money taken, seat resold). Converges via provider retry + reconciler + auto refund, no manual intervention.

## 9. API
/api/v1; RFC 9457 problem+json {type,title,status,detail,instance,code,request_id,errors[]}.
Events: GET /events (cursor keyset on (sales_start_at,id) or (created_at,id); filters category, status, venue, q, from/to; sort whitelist), POST /events 201, GET /events/{id} (ETag W/"v"), PATCH /events/{id} If-Match required (428 if missing, 412 mismatch), DELETE /events/{id} 204 / 409 has-orders, POST /events/{id}/publish 200 (409 if no seats). Zones nested. Seats: GET /events/{id}/seats (full map snapshot + stream_seq), imports: POST /events/{id}/seat-imports 202 (200 for existing same file), GET /seat-imports/{job_id}, GET /seat-imports/{job_id}/errors.
Orders: POST /events/{id}/orders 201, GET /orders (mine, cursor), GET /orders/{id}, POST /orders/{id}/cancel, POST /orders/{id}/payments 201, POST /payments/mock/callback.

## 10. Scenario A perf
Target: booking p95 < 150 ms, p99 < 400 ms, 0 5xx, 0 oversell, ≥1500 RPS mixed. Txn ~3–5 ms, asyncpg pool per process 10, 4 uvicorn workers x 2 replicas -> 80 conns; PG max_connections 200. Fast reject: cached zone-availability hint (refreshed 500 ms) -> if 0 available, reply 409 without DB (hint only; worst case false negative ≤ refresh interval). SKIP LOCKED avoids hot-row waits; partial index keeps allocation O(log n).

## 11. Tests
- Concurrency: 200 coroutines, distinct users, same seat via real HTTP against real PG -> exactly 1 success. Zone: 500 users x 2 tickets on 300 seats -> exactly 150 successes, no seat in two active order_items. Quota: same user 2 keys x 3 tickets concurrently -> exactly one success.
- Break test: env flag/monkeypatch to remove `AND status='available'` and FOR UPDATE (read-then-write), and drop unique partial index -> test shows >1 success.
- Clock: set expires_at to past via SQL (time travel data) and call sweeper.run_once(); app clock skew injected (+1h) shows no effect.
