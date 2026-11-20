"""Baseline schema: identity, events/seats, orders, payments, outbox, imports, pagination,
telemetry and logs. See PLAN.md §4 for the invariants these constraints enforce.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-11-20
"""

import os

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

UPGRADE_SQL = r"""
CREATE EXTENSION IF NOT EXISTS citext;

CREATE TYPE user_role AS ENUM ('admin', 'user', 'viewer');
CREATE TYPE event_status AS ENUM ('draft', 'published');
CREATE TYPE seat_status AS ENUM ('available', 'held', 'sold');
CREATE TYPE order_status AS ENUM
  ('pending_payment', 'paid', 'expired', 'cancelled', 'refund_pending', 'refunded');

-- Single clock for every deadline decision (hold expiry, payment deadline).
-- Tests replace this body with a table-driven clock; production must never do so.
CREATE FUNCTION app_now() RETURNS timestamptz
  LANGUAGE sql VOLATILE AS $$ SELECT clock_timestamp() $$;

-- Creates missing range partitions around now(): `back` steps behind, `ahead` steps ahead.
CREATE FUNCTION ensure_range_partitions(parent text, step interval, back int, ahead int)
RETURNS int LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  unit text := CASE WHEN step = interval '1 hour' THEN 'hour' ELSE 'day' END;
  fmt text := CASE WHEN step = interval '1 hour' THEN 'YYYYMMDDHH24' ELSE 'YYYYMMDD' END;
  base timestamptz := date_trunc(unit, now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC';
  lo timestamptz;
  pname text;
  created int := 0;
BEGIN
  FOR i IN -back..ahead LOOP
    lo := base + i * step;
    pname := parent || '_p' || to_char(lo AT TIME ZONE 'UTC', fmt);
    IF to_regclass(pname) IS NULL THEN
      EXECUTE format('CREATE TABLE %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
                     pname, parent, lo, lo + step);
      created := created + 1;
    END IF;
  END LOOP;
  RETURN created;
END $$;

-- ---------------------------------------------------------------- identity
CREATE TABLE users (
  id            bigserial PRIMARY KEY,
  email         citext NOT NULL UNIQUE CHECK (length(email) BETWEEN 3 AND 254),
  password_hash text NOT NULL,
  role          user_role NOT NULL DEFAULT 'user',
  is_active     boolean NOT NULL DEFAULT true,
  version       int NOT NULL DEFAULT 1,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sessions (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  token_hash      bytea NOT NULL UNIQUE,
  user_id         bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  csrf_secret     bytea NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  last_seen_at    timestamptz NOT NULL DEFAULT now(),
  idle_expires_at timestamptz NOT NULL,
  abs_expires_at  timestamptz NOT NULL,
  revoked_at      timestamptz,
  revoked_reason  text,
  ip              inet,
  user_agent      text
);
CREATE INDEX sessions_user_active ON sessions (user_id) WHERE revoked_at IS NULL;

CREATE TABLE audit_log (
  id          bigserial PRIMARY KEY,
  actor_id    bigint REFERENCES users(id),
  action      text NOT NULL,
  target_type text NOT NULL,
  target_id   text NOT NULL,
  before      jsonb,
  after       jsonb,
  request_id  text,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_log_target ON audit_log (target_type, target_id, created_at DESC);

-- ---------------------------------------------------------------- events & seats
CREATE TABLE events (
  id             bigserial PRIMARY KEY,
  owner_id       bigint NOT NULL REFERENCES users(id),
  title          text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
  description    text NOT NULL DEFAULT '' CHECK (length(description) <= 10000),
  category       text NOT NULL CHECK (length(category) BETWEEN 1 AND 50),
  venue          text NOT NULL CHECK (length(venue) BETWEEN 1 AND 200),
  sales_start_at timestamptz NOT NULL,
  sales_end_at   timestamptz NOT NULL,
  status         event_status NOT NULL DEFAULT 'draft',
  version        int NOT NULL DEFAULT 1,
  layout_ver     int NOT NULL DEFAULT 1,
  published_at   timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  CHECK (sales_end_at > sales_start_at),
  CHECK ((status = 'published') = (published_at IS NOT NULL))
);
CREATE INDEX events_owner ON events (owner_id, id DESC);
CREATE INDEX events_published_sales ON events (sales_start_at, id) WHERE status = 'published';

CREATE TABLE zones (
  id        bigserial PRIMARY KEY,
  event_id  bigint NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  code      text NOT NULL CHECK (code ~ '^[A-Z0-9_-]{1,16}$'),
  name      text NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
  price_twd bigint NOT NULL CHECK (price_twd BETWEEN 0 AND 10000000),
  UNIQUE (event_id, code),
  UNIQUE (event_id, id)
);

CREATE TABLE orders (
  id           bigserial PRIMARY KEY,
  public_id    uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
  user_id      bigint NOT NULL REFERENCES users(id),
  event_id     bigint NOT NULL REFERENCES events(id) ON DELETE RESTRICT,
  status       order_status NOT NULL DEFAULT 'pending_payment',
  quantity     smallint NOT NULL CHECK (quantity BETWEEN 1 AND 4),
  subtotal_twd bigint CHECK (subtotal_twd >= 0),
  fee_twd      bigint CHECK (fee_twd >= 0),
  total_twd    bigint,
  expires_at   timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  paid_at      timestamptz,
  closed_at    timestamptz,
  version      int NOT NULL DEFAULT 0,
  UNIQUE (event_id, id),
  CHECK ((subtotal_twd IS NULL AND fee_twd IS NULL AND total_twd IS NULL)
      OR (subtotal_twd IS NOT NULL AND fee_twd IS NOT NULL AND total_twd IS NOT NULL
          AND total_twd = subtotal_twd + fee_twd))
);
CREATE INDEX orders_user ON orders (user_id, id DESC);
CREATE INDEX orders_event ON orders (event_id, id);
CREATE INDEX orders_expiry ON orders (expires_at) WHERE status = 'pending_payment';

CREATE TABLE seats (
  id          bigserial PRIMARY KEY,
  event_id    bigint NOT NULL,
  zone_id     bigint NOT NULL,
  row_label   text NOT NULL,
  seat_number text NOT NULL,
  norm_key    text NOT NULL,
  sort_key    int NOT NULL,
  seat_idx    int NOT NULL CHECK (seat_idx >= 0),
  status      seat_status NOT NULL DEFAULT 'available',
  order_id    bigint,
  version     int NOT NULL DEFAULT 0,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (event_id, zone_id) REFERENCES zones (event_id, id) ON DELETE CASCADE,
  FOREIGN KEY (event_id, order_id) REFERENCES orders (event_id, id),
  UNIQUE (event_id, norm_key),
  UNIQUE (event_id, seat_idx),
  UNIQUE (event_id, id),
  UNIQUE (event_id, zone_id, id),
  CHECK ((status = 'available') = (order_id IS NULL))
);
CREATE INDEX seats_alloc ON seats (zone_id, sort_key, id) WHERE status = 'available';
CREATE INDEX seats_order ON seats (order_id) WHERE order_id IS NOT NULL;
ALTER TABLE seats SET (autovacuum_vacuum_scale_factor = 0.01, autovacuum_analyze_scale_factor = 0.02);

CREATE TABLE order_items (
  order_id       bigint NOT NULL,
  seat_id        bigint NOT NULL,
  event_id       bigint NOT NULL,
  zone_id        bigint NOT NULL,
  unit_price_twd bigint NOT NULL CHECK (unit_price_twd >= 0),
  released_at    timestamptz,
  PRIMARY KEY (order_id, seat_id),
  FOREIGN KEY (event_id, order_id) REFERENCES orders (event_id, id),
  FOREIGN KEY (event_id, zone_id, seat_id) REFERENCES seats (event_id, zone_id, id)
);
-- Third no-oversell layer: at most one active claim per seat.
CREATE UNIQUE INDEX order_items_one_active_claim ON order_items (seat_id) WHERE released_at IS NULL;

CREATE TABLE user_event_quota (
  user_id  bigint NOT NULL REFERENCES users(id),
  event_id bigint NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  reserved smallint NOT NULL DEFAULT 0 CHECK (reserved BETWEEN 0 AND 4),
  PRIMARY KEY (user_id, event_id)
);

CREATE TABLE idempotency_keys (
  user_id          bigint NOT NULL REFERENCES users(id),
  key              text NOT NULL CHECK (length(key) BETWEEN 8 AND 128),
  operation        text NOT NULL,
  request_hash     bytea NOT NULL,
  status           text NOT NULL CHECK (status IN ('in_progress', 'completed')),
  resource_ref     text,
  response_status  smallint,
  response_headers jsonb,
  response_body    jsonb,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, key)
);
CREATE INDEX idempotency_completed_age ON idempotency_keys (updated_at) WHERE status = 'completed';

-- ---------------------------------------------------------------- payments
CREATE TABLE payment_attempts (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),  -- merchant_payment_id
  order_id           bigint NOT NULL REFERENCES orders(id),
  amount_twd         bigint NOT NULL CHECK (amount_twd >= 0),
  status             text NOT NULL
                     CHECK (status IN ('creating', 'awaiting_customer', 'failed', 'captured', 'abandoned')),
  provider_intent_id text UNIQUE,
  checkout_url       text,
  expires_at         timestamptz NOT NULL,
  last_polled_at     timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  UNIQUE (id, order_id)
);
CREATE UNIQUE INDEX one_chargeable_attempt ON payment_attempts (order_id)
  WHERE status IN ('creating', 'awaiting_customer', 'captured');
CREATE INDEX payment_attempts_open ON payment_attempts (updated_at)
  WHERE status IN ('creating', 'awaiting_customer');

CREATE TABLE payment_captures (
  provider_capture_id text PRIMARY KEY,
  attempt_id          uuid NOT NULL,
  order_id            bigint NOT NULL REFERENCES orders(id),
  amount_twd          bigint NOT NULL,
  disposition         text NOT NULL CHECK (disposition IN ('accepted', 'refund_required')),
  captured_at         timestamptz NOT NULL,
  recorded_at         timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (attempt_id, order_id) REFERENCES payment_attempts (id, order_id)
);
CREATE UNIQUE INDEX one_accepted_capture ON payment_captures (order_id) WHERE disposition = 'accepted';

CREATE TABLE refunds (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),  -- stable provider refund key
  provider_capture_id text NOT NULL UNIQUE REFERENCES payment_captures(provider_capture_id),
  reason              text NOT NULL
                      CHECK (reason IN ('late_capture', 'duplicate_capture', 'order_cancelled')),
  status              text NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'succeeded', 'failed_retryable')),
  attempts            int NOT NULL DEFAULT 0,
  next_try_at         timestamptz NOT NULL DEFAULT now(),
  lease_owner         text,
  lease_until         timestamptz,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX refunds_due ON refunds (next_try_at) WHERE status <> 'succeeded';

CREATE TABLE payment_callbacks (
  provider_event_id text PRIMARY KEY,
  attempt_id        uuid,
  event_type        text NOT NULL,
  payload           jsonb NOT NULL,
  received_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE provider_cancellations (
  attempt_id  uuid PRIMARY KEY REFERENCES payment_attempts(id),
  status      text NOT NULL DEFAULT 'pending'
              CHECK (status IN ('pending', 'succeeded', 'failed_retryable')),
  attempts    int NOT NULL DEFAULT 0,
  next_try_at timestamptz NOT NULL DEFAULT now(),
  lease_owner text,
  lease_until timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- realtime outbox
CREATE TABLE outbox (
  id          bigserial PRIMARY KEY,
  event_id    bigint NOT NULL,
  kind        text NOT NULL,
  changes     jsonb NOT NULL,
  request_id  text,
  created_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
  writer_xid  xid8 NOT NULL DEFAULT pg_current_xact_id(),
  seq         bigint,
  assigned_at timestamptz,
  UNIQUE (event_id, seq)
);
CREATE INDEX outbox_unassigned ON outbox (id) WHERE seq IS NULL;
ALTER TABLE outbox SET (autovacuum_vacuum_scale_factor = 0.01, autovacuum_analyze_scale_factor = 0.02);

CREATE TABLE event_stream_state (
  event_id           bigint PRIMARY KEY,
  last_assigned_seq  bigint NOT NULL DEFAULT 0,
  last_delivered_seq bigint NOT NULL DEFAULT 0,
  gen                uuid NOT NULL DEFAULT gen_random_uuid(),
  CHECK (last_delivered_seq <= last_assigned_seq)
);

CREATE TABLE relay_lease (
  id          smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  epoch       bigint NOT NULL DEFAULT 0,
  holder      text,
  acquired_at timestamptz
);
INSERT INTO relay_lease (id, epoch) VALUES (1, 0);

CREATE TABLE seat_events (
  recorded_at  timestamptz NOT NULL,
  event_id     bigint NOT NULL,
  seq          bigint NOT NULL,
  seat_idx     int NOT NULL,
  status       text NOT NULL,
  seat_version int NOT NULL,
  request_id   text,
  published_at timestamptz,
  PRIMARY KEY (recorded_at, event_id, seq, seat_idx)
) PARTITION BY RANGE (recorded_at);

-- ---------------------------------------------------------------- seat import
CREATE TABLE import_jobs (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id          bigint NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  created_by        bigint NOT NULL REFERENCES users(id),
  content_sha256    bytea NOT NULL,
  format            text NOT NULL CHECK (format IN ('csv', 'json')),
  mode              text NOT NULL CHECK (mode IN ('all_or_nothing', 'skip_invalid')),
  status            text NOT NULL DEFAULT 'queued' CHECK (status IN
                    ('queued', 'running', 'succeeded', 'partially_succeeded',
                     'validation_failed', 'cancelled', 'system_failed')),
  failure_reason    text,
  file_path         text NOT NULL,
  file_bytes        bigint NOT NULL,
  origin_request_id text,
  total_rows        int,
  processed_rows    int NOT NULL DEFAULT 0,
  valid_rows        int,
  error_rows        int,
  attempt_no        int NOT NULL DEFAULT 0,
  fence             uuid,
  lease_expires_at  timestamptz,
  worker_id         text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  started_at        timestamptz,
  finished_at       timestamptz,
  UNIQUE (event_id, content_sha256)
);
CREATE UNIQUE INDEX one_active_import ON import_jobs (event_id) WHERE status IN ('queued', 'running');
CREATE INDEX import_jobs_claimable ON import_jobs (created_at) WHERE status IN ('queued', 'running');

-- LOGGED on purpose: UNLOGGED tables are truncated after a crash.
CREATE TABLE import_staging (
  fence       uuid NOT NULL,
  line        int,
  item_index  int,
  zone_code   text,
  row_label   text,
  seat_number text,
  norm_key    text,
  error       text
);
CREATE INDEX import_staging_key ON import_staging (fence, norm_key);

CREATE TABLE import_errors (
  job_id      uuid NOT NULL REFERENCES import_jobs(id) ON DELETE CASCADE,
  attempt_no  int NOT NULL,
  line        int,
  item_index  int,
  zone_code   text,
  row_label   text,
  seat_number text,
  reason      text NOT NULL
);
CREATE INDEX import_errors_job ON import_errors (job_id, attempt_no, line);

-- ---------------------------------------------------------------- list pagination snapshots
CREATE TABLE list_snapshots (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scope        text NOT NULL CHECK (scope IN ('public', 'principal')),
  principal_id bigint,
  query_hash   bytea NOT NULL,
  row_count    int NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL,
  CHECK ((scope = 'principal') = (principal_id IS NOT NULL))
);
CREATE INDEX list_snapshots_public ON list_snapshots (query_hash, created_at DESC) WHERE scope = 'public';
CREATE INDEX list_snapshots_principal ON list_snapshots (principal_id, created_at DESC)
  WHERE scope = 'principal';

CREATE TABLE list_snapshot_rows (
  created_at  timestamptz NOT NULL,
  snapshot_id uuid NOT NULL,
  ord         int NOT NULL,
  event_id    bigint NOT NULL,
  PRIMARY KEY (created_at, snapshot_id, ord)
) PARTITION BY RANGE (created_at);

-- ---------------------------------------------------------------- telemetry & logs
CREATE TABLE metrics_1s (
  ts              timestamptz NOT NULL,
  source          text NOT NULL CHECK (source IN ('edge', 'app')),
  scope           text NOT NULL DEFAULT 'all',
  req_total       int NOT NULL DEFAULT 0,
  err_4xx         int NOT NULL DEFAULT 0,
  err_5xx         int NOT NULL DEFAULT 0,
  p50_ms          real,
  p95_ms          real,
  p99_ms          real,
  hist            int[],
  conns_active    int,
  conns_sse       int,
  completeness    real,
  late_or_missing int NOT NULL DEFAULT 0,
  PRIMARY KEY (source, scope, ts)
) PARTITION BY RANGE (ts);

CREATE TABLE business_1s (
  ts            timestamptz NOT NULL,
  event_id      bigint NOT NULL,
  hold_ok       int NOT NULL DEFAULT 0,
  hold_conflict int NOT NULL DEFAULT 0,
  hold_soldout  int NOT NULL DEFAULT 0,
  hold_limit    int NOT NULL DEFAULT 0,
  pay_ok        int NOT NULL DEFAULT 0,
  pay_fail      int NOT NULL DEFAULT 0,
  cancels       int NOT NULL DEFAULT 0,
  expiries      int NOT NULL DEFAULT 0,
  PRIMARY KEY (event_id, ts)
) PARTITION BY RANGE (ts);

CREATE TABLE telemetry_producers (
  boot_uuid        uuid PRIMARY KEY,
  service          text NOT NULL,
  replica          text,
  started_at       timestamptz NOT NULL DEFAULT now(),
  last_interval_no bigint,
  last_seen_at     timestamptz,
  offset_ms        double precision,
  offset_err_ms    double precision
);

CREATE TABLE telemetry_ingest (
  boot_uuid   uuid NOT NULL,
  interval_no bigint NOT NULL,
  bucket_sec  timestamptz NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (boot_uuid, interval_no)
);

CREATE TABLE access_logs (
  ts          timestamptz NOT NULL,
  request_id  text,
  method      text,
  route       text,
  path        text,
  status      smallint,
  req_ms      real,
  upstream_ms real,
  upstream    text,
  bytes       bigint,
  user_id     bigint,
  load_run    text,
  user_agent  text
) PARTITION BY RANGE (ts);
CREATE INDEX access_logs_request ON access_logs (request_id);

CREATE TABLE service_logs (
  ts           timestamptz NOT NULL,
  request_id   text,
  service      text NOT NULL,
  boot_uuid    uuid,
  op_id        text,
  parent_op_id text,
  local_seq    bigint,
  level        text,
  event        text,
  attrs        jsonb
) PARTITION BY RANGE (ts);
CREATE INDEX service_logs_request ON service_logs (request_id, ts);

CREATE TABLE log_checkpoint (
  path       text PRIMARY KEY,
  inode      bigint NOT NULL,
  "offset"   bigint NOT NULL DEFAULT 0,
  done       boolean NOT NULL DEFAULT false,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE alert_events (
  id        bigserial PRIMARY KEY,
  rule_id   text NOT NULL,
  scope     text NOT NULL DEFAULT 'all',
  state     text NOT NULL CHECK (state IN ('pending', 'firing', 'resolved')),
  value     double precision,
  threshold double precision,
  detail    jsonb,
  ts        timestamptz NOT NULL DEFAULT now(),
  ack_by    bigint REFERENCES users(id),
  ack_at    timestamptz
);
CREATE INDEX alert_events_state ON alert_events (state, ts DESC);

-- ---------------------------------------------------------------- initial partitions
SELECT ensure_range_partitions('seat_events', interval '1 day', 1, 3);
SELECT ensure_range_partitions('metrics_1s', interval '1 day', 1, 3);
SELECT ensure_range_partitions('business_1s', interval '1 day', 1, 3);
SELECT ensure_range_partitions('access_logs', interval '1 day', 1, 3);
SELECT ensure_range_partitions('service_logs', interval '1 day', 1, 3);
SELECT ensure_range_partitions('list_snapshot_rows', interval '1 hour', 1, 3);
"""

DOWNGRADE_SQL = r"""
DROP TABLE IF EXISTS alert_events, log_checkpoint, service_logs, access_logs, telemetry_ingest,
  telemetry_producers, business_1s, metrics_1s, list_snapshot_rows, list_snapshots,
  import_errors, import_staging, import_jobs, seat_events, relay_lease, event_stream_state,
  outbox, provider_cancellations, payment_callbacks, refunds, payment_captures,
  payment_attempts, idempotency_keys, user_event_quota, order_items, seats, orders, zones,
  events, audit_log, sessions, users CASCADE;
DROP FUNCTION IF EXISTS ensure_range_partitions(text, interval, int, int);
DROP FUNCTION IF EXISTS app_now();
DROP TYPE IF EXISTS order_status, seat_status, event_status, user_role;
"""


GRANTS_SQL = r"""
DO $$
DECLARE app_role text := %(role)s;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = app_role) THEN
    EXECUTE format('GRANT USAGE ON SCHEMA public TO %%I', app_role);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO %%I', app_role);
    EXECUTE format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO %%I', app_role);
    EXECUTE format('GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO %%I', app_role);
    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public '
                   'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %%I', app_role);
    EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public '
                   'GRANT USAGE, SELECT ON SEQUENCES TO %%I', app_role);
  END IF;
END $$;
"""


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def upgrade() -> None:
    op.execute(UPGRADE_SQL)
    role = os.environ.get("APP_DB_USER", "ticketing_app")
    op.execute(GRANTS_SQL % {"role": _quote_literal(role)})


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
