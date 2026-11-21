"""Keep staged rows and file validation checkpoints across worker leases."""

from alembic import op

revision = "0009_import_resume"
down_revision = "0008_observability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE import_jobs ADD COLUMN file_validated boolean NOT NULL DEFAULT false;
        DELETE FROM import_staging s
        WHERE NOT EXISTS (SELECT FROM import_jobs j WHERE j.fence = s.fence);
        WITH staged AS (
            SELECT s.ctid AS row_tid, j.id AS job_id,
                   coalesce(s.norm_key, CASE
                       WHEN s.error IS NULL AND s.zone_code COLLATE "C" ~ '^[A-Za-z0-9_-]{1,16}$'
                         AND s.row_label COLLATE "C" ~ '^[A-Za-z0-9_-]{1,32}$'
                         AND s.seat_number COLLATE "C" ~ '^[0-9]{1,16}$'
                         AND ltrim(s.seat_number, '0') <> ''
                       THEN upper(s.zone_code COLLATE "C") || '|' ||
                            upper(s.row_label COLLATE "C") || '|' || ltrim(s.seat_number, '0')
                   END) AS key
            FROM import_staging s JOIN import_jobs j ON j.fence = s.fence
        )
        UPDATE import_staging s SET fence = staged.job_id, norm_key = staged.key,
            error = coalesce(s.error, CASE WHEN staged.key IS NULL THEN 'invalid_format' END)
        FROM staged WHERE s.ctid = staged.row_tid;
        ALTER TABLE import_staging RENAME COLUMN fence TO job_id;
        ALTER TABLE import_staging ADD CONSTRAINT import_staging_job_fk
            FOREIGN KEY (job_id) REFERENCES import_jobs(id) ON DELETE CASCADE;
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE import_staging DROP CONSTRAINT import_staging_job_fk;
        ALTER TABLE import_staging RENAME COLUMN job_id TO fence;
        UPDATE import_staging s SET fence = j.fence FROM import_jobs j WHERE j.id = s.fence;
        UPDATE import_jobs SET total_rows = NULL WHERE status = 'running';
        ALTER TABLE import_jobs DROP COLUMN file_validated;
    """)
