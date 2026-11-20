"""Alembic environment. Migrations are hand-written SQL; there is no ORM metadata.

Schema drift is checked by catalog assertions in tests/integration/test_schema.py instead of
`alembic check` (which needs ORM metadata).
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool


def _url() -> str:
    url = os.environ.get(
        "DATABASE_URL", "postgresql://ticketing:ticketing@localhost:5432/ticketing"
    )
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=None, transaction_per_migration=True
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    context.configure(url=_url(), target_metadata=None, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    run_migrations_online()
