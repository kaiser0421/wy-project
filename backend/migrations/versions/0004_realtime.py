"""Realtime schema changes.

Revision ID: 0004_realtime
Revises: 0003_events
Create Date: 2026-11-21

Owned by workstream C. Keep this revision even if it stays empty.
"""

from alembic import op  # noqa: F401

revision = "0004_realtime"
down_revision = "0003_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
