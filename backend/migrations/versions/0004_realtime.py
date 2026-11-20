"""Index the oldest unassigned row for each event's stream progress.

Revision ID: 0004_realtime
Revises: 0003_events
Create Date: 2026-11-21
"""

from alembic import op

revision = "0004_realtime"
down_revision = "0003_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX outbox_unassigned_event_age ON outbox (event_id, created_at) "
        "WHERE seq IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX outbox_unassigned_event_age")
