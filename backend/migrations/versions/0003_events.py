"""Events, imports and reports schema changes.

Revision ID: 0003_events
Revises: 0002_booking
Create Date: 2026-11-21

Owned by workstream B. Keep this revision even if it stays empty.
"""

from alembic import op  # noqa: F401

revision = "0003_events"
down_revision = "0002_booking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
