"""Monitoring durability, indexes and daily partition retention.

Revision ID: 0005_monitoring
Revises: 0004_realtime
"""

import os

from alembic import op

revision = "0005_monitoring"
down_revision = "0004_realtime"
branch_labels = None
depends_on = None

UPGRADE = r"""
ALTER TABLE metrics_1s ADD COLUMN status_classes int[], ADD COLUMN clock_skew_ms real;
CREATE INDEX metrics_1s_time ON metrics_1s (ts);
CREATE INDEX business_1s_time ON business_1s (ts, event_id);

ALTER TABLE telemetry_producers ADD COLUMN heartbeat_at timestamptz,
  ADD COLUMN wall_skew_ms double precision,
  ADD COLUMN latest_sse int;
ALTER TABLE telemetry_ingest ADD COLUMN delay_sample_ms double precision;
CREATE INDEX telemetry_ingest_bucket ON telemetry_ingest (bucket_sec, boot_uuid);
CREATE INDEX telemetry_ingest_received ON telemetry_ingest (received_at);
CREATE INDEX telemetry_ingest_calibration ON telemetry_ingest (boot_uuid, received_at)
  INCLUDE (delay_sample_ms);

ALTER TABLE access_logs ADD COLUMN ingested_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE service_logs ADD COLUMN ingested_at timestamptz NOT NULL DEFAULT now();
CREATE INDEX service_logs_links ON service_logs USING gin ((attrs->'links') jsonb_ops);
CREATE INDEX service_logs_origin ON service_logs ((attrs->>'origin_request_id'));

ALTER TABLE log_checkpoint DROP CONSTRAINT log_checkpoint_pkey,
  ADD COLUMN device bigint NOT NULL DEFAULT 0, ADD COLUMN anchor bytea,
  ADD COLUMN discarding boolean NOT NULL DEFAULT false;
DELETE FROM log_checkpoint a USING log_checkpoint b
WHERE a.inode = b.inode AND (a.updated_at, a.path) < (b.updated_at, b.path);
ALTER TABLE log_checkpoint ADD PRIMARY KEY (device, inode);
CREATE TABLE telemetry_edge_watermark (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    latest timestamptz NOT NULL
);

ALTER TABLE alert_events ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN fired_at timestamptz;
CREATE UNIQUE INDEX alert_events_open ON alert_events (rule_id, scope) WHERE state <> 'resolved';
CREATE INDEX alert_events_cooldown ON alert_events (rule_id, scope, updated_at DESC)
  WHERE state = 'resolved' AND fired_at IS NOT NULL;
CREATE INDEX monitoring_outbox_age ON outbox (created_at) WHERE seq IS NULL;

-- A provider poll timestamp alone gives no evidence that money was captured.
ALTER TABLE payment_attempts ADD COLUMN provider_captured_at timestamptz;
CREATE INDEX monitoring_capture_attempt ON payment_captures (attempt_id);
CREATE INDEX monitoring_provider_capture ON payment_attempts (provider_captured_at)
  WHERE provider_captured_at IS NOT NULL AND status = 'awaiting_customer';

CREATE FUNCTION telemetry_vector_add(a int[], b int[]) RETURNS int[]
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT ARRAY(SELECT COALESCE(a[i], 0) + b[i] FROM generate_subscripts(b, 1) AS i)
$$;

CREATE FUNCTION telemetry_quantile(hist int[], bounds double precision[], q double precision)
RETURNS double precision LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT bounds[least(i, cardinality(bounds))]
    FROM (
        SELECT i, sum(hist[i]) OVER (ORDER BY i) AS cumulative,
               sum(hist[i]) OVER () AS total
        FROM generate_subscripts(hist, 1) AS i
    ) AS buckets
    WHERE total > 0 AND cumulative >= ceil(total * q)
    ORDER BY i LIMIT 1
$$;

CREATE FUNCTION drop_expired_monitoring_partitions() RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
SET timezone = 'UTC'
AS $$
DECLARE
    child record;
    dropped integer := 0;
    today date := (clock_timestamp() AT TIME ZONE 'UTC')::date;
BEGIN
    -- Match only direct children of fixed public parents and read actual range bounds.
    FOR child IN
        SELECT c.relname, p.relname AS parent_name, keep.days,
               substring(pg_get_expr(c.relpartbound, c.oid)
                         FROM 'TO \(''([0-9]{4}-[0-9]{2}-[0-9]{2}) 00:00:00[+]00''\)$') AS high
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        JOIN pg_class p ON p.oid = i.inhparent
        JOIN pg_namespace n ON n.oid = p.relnamespace AND n.oid = c.relnamespace
        JOIN (VALUES ('access_logs', 7), ('service_logs', 7),
                     ('metrics_1s', 14), ('business_1s', 14), ('seat_events', 90))
              AS keep(name, days) ON p.relname = keep.name
        WHERE n.nspname = 'public' AND c.relispartition
        ORDER BY array_position(
                    ARRAY['metrics_1s', 'business_1s', 'access_logs', 'service_logs', 'seat_events'],
                    p.relname::text), c.relname
    LOOP
        IF child.high::date <= today - child.days THEN
            EXECUTE format('DROP TABLE IF EXISTS public.%I', child.relname);
            dropped := dropped + 1;
        END IF;
    END LOOP;
    RETURN dropped;
END
$$;
REVOKE ALL ON FUNCTION drop_expired_monitoring_partitions() FROM PUBLIC;

-- A collector introduced to an existing spool can ingest the retained portion.
SELECT ensure_range_partitions('access_logs', interval '1 day', 7, 3);
SELECT ensure_range_partitions('service_logs', interval '1 day', 7, 3);
SELECT ensure_range_partitions('metrics_1s', interval '1 day', 14, 3);
SELECT ensure_range_partitions('business_1s', interval '1 day', 14, 3);
"""

DOWNGRADE = """
DROP FUNCTION drop_expired_monitoring_partitions();
DROP FUNCTION telemetry_quantile(int[], double precision[], double precision);
DROP FUNCTION telemetry_vector_add(int[], int[]);
DROP INDEX monitoring_provider_capture, monitoring_capture_attempt, monitoring_outbox_age,
    alert_events_open, alert_events_cooldown, service_logs_links, service_logs_origin,
    telemetry_ingest_received, telemetry_ingest_bucket, telemetry_ingest_calibration,
    metrics_1s_time, business_1s_time;
DROP TABLE telemetry_edge_watermark;
-- The old layout can store only one checkpoint per path.
DELETE FROM log_checkpoint a USING log_checkpoint b WHERE a.path = b.path
  AND (a.updated_at, a.device, a.inode) < (b.updated_at, b.device, b.inode);
ALTER TABLE log_checkpoint DROP CONSTRAINT log_checkpoint_pkey,
  DROP COLUMN device, DROP COLUMN anchor, DROP COLUMN discarding, ADD PRIMARY KEY (path);
ALTER TABLE access_logs DROP COLUMN ingested_at;
ALTER TABLE service_logs DROP COLUMN ingested_at;
ALTER TABLE metrics_1s DROP COLUMN status_classes, DROP COLUMN clock_skew_ms;
ALTER TABLE telemetry_producers DROP COLUMN heartbeat_at, DROP COLUMN wall_skew_ms,
  DROP COLUMN latest_sse;
ALTER TABLE telemetry_ingest DROP COLUMN delay_sample_ms;
ALTER TABLE payment_attempts DROP COLUMN provider_captured_at;
ALTER TABLE alert_events DROP COLUMN updated_at, DROP COLUMN fired_at;
"""


GRANT_RETENTION = """
DO $$
DECLARE app_role text := convert_from(decode('%(role_hex)s', 'hex'), 'UTF8');
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = app_role) THEN
        EXECUTE format(
            'GRANT EXECUTE ON FUNCTION drop_expired_monitoring_partitions() TO %%I', app_role);
    END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(UPGRADE)
    role_hex = os.environ.get("APP_DB_USER", "ticketing_app").encode().hex()
    op.execute(GRANT_RETENTION % {"role_hex": role_hex})


def downgrade() -> None:
    op.execute(DOWNGRADE)
