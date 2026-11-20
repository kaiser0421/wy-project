"""Index payment recovery work and retain the originating callback request ID.

Revision ID: 0002_booking
Revises: 0001_baseline
Create Date: 2026-11-21
"""

from alembic import op

revision = "0002_booking"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE payment_callbacks ADD COLUMN request_id text;
        CREATE INDEX idempotency_payment_pending ON idempotency_keys(resource_ref)
          WHERE operation='create-payment' AND status='in_progress';
        CREATE INDEX payment_attempts_poll_due
          ON payment_attempts (COALESCE(last_polled_at, updated_at), id)
          WHERE status IN ('creating','awaiting_customer')
             OR (status='captured' AND checkout_url IS NULL);
        CREATE INDEX provider_cancellations_due ON provider_cancellations(next_try_at, attempt_id)
          WHERE status <> 'succeeded';
    """)


def downgrade() -> None:
    op.execute("""
        DROP INDEX provider_cancellations_due, payment_attempts_poll_due, idempotency_payment_pending;
        ALTER TABLE payment_callbacks DROP COLUMN request_id;
    """)
