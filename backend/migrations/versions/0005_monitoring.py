"""Monitoring schema changes.

Revision ID: 0005_monitoring
Revises: 0004_realtime
Create Date: 2026-11-21

Owned by workstream D. Keep this revision even if it stays empty.
"""

from alembic import op  # noqa: F401

revision = "0005_monitoring"
down_revision = "0004_realtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
