"""Index the Admin order listing by creation time.

Revision ID: 0007_admin_orders
Revises: 0006_recent_logs
"""

from alembic import op

revision = "0007_admin_orders"
down_revision = "0006_recent_logs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX orders_created ON orders (created_at DESC, id DESC)")


def downgrade() -> None:
    op.execute("DROP INDEX orders_created")
