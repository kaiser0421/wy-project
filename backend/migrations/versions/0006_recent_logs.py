"""Index recent service warnings and errors.

Revision ID: 0006_recent_logs
Revises: 0005_monitoring
"""

from alembic import op

revision = "0006_recent_logs"
down_revision = "0005_monitoring"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE INDEX service_logs_recent ON service_logs (ts DESC)
           WHERE level IN ('warning', 'error', 'critical')"""
    )


def downgrade() -> None:
    op.execute("DROP INDEX service_logs_recent")
