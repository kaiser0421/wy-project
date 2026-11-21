"""Index recent access logs and retain request IDs for deferred payment work."""

from alembic import op

revision = "0008_observability"
down_revision = "0007_admin_orders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX access_logs_recent ON access_logs (ts DESC)")
    for table in ("payment_attempts", "provider_cancellations", "refunds"):
        op.execute(f"ALTER TABLE {table} ADD COLUMN origin_request_id text")


def downgrade() -> None:
    for table in ("refunds", "provider_cancellations", "payment_attempts"):
        op.execute(f"ALTER TABLE {table} DROP COLUMN origin_request_id")
    op.execute("DROP INDEX access_logs_recent")
