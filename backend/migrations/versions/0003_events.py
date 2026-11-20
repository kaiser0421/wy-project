"""Indexes for event snapshots, the import queue and streaming reports.

Revision ID: 0003_events
Revises: 0002_booking
Create Date: 2026-11-21
"""

from alembic import op

revision = "0003_events"
down_revision = "0002_booking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE INDEX list_snapshots_expiry ON list_snapshots (expires_at);
        CREATE INDEX import_jobs_running_lease ON import_jobs (lease_expires_at)
          WHERE status = 'running';
        CREATE INDEX import_jobs_file_path ON import_jobs (file_path);
        CREATE INDEX order_items_event_zone ON order_items (event_id, zone_id)
          INCLUDE (order_id, unit_price_twd);

        CREATE FUNCTION prune_event_snapshot_partitions() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
        DECLARE
            part text;
            cutoff text := to_char(
                public.app_now() AT TIME ZONE 'UTC' - interval '1 hour', 'YYYYMMDDHH24'
            );
        BEGIN
            -- Keep the preceding hour for shared partition maintenance. The 10-minute
            -- lifetime of even the last snapshot in an older partition has elapsed.
            IF NOT pg_try_advisory_xact_lock(520301581674307075) THEN
                RETURN;
            END IF;
            FOR part IN
                SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE i.inhparent = 'public.list_snapshot_rows'::regclass
                  AND n.nspname = 'public' AND c.relname ~ '^list_snapshot_rows_p[0-9]{10}$'
                  AND right(c.relname, 10) < cutoff
                ORDER BY c.relname LIMIT 4
            LOOP
                EXECUTE format('DROP TABLE public.%I', part);
            END LOOP;
        END $$;
    """)


def downgrade() -> None:
    op.execute("""
        DROP FUNCTION prune_event_snapshot_partitions();
        DROP INDEX order_items_event_zone, import_jobs_file_path,
                   import_jobs_running_lease, list_snapshots_expiry;
    """)
