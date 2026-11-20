"""One-shot seed job: creates the Admin / User / Viewer test accounts (idempotent).

P0 stub: verifies the schema is migrated and exits 0. Accounts are created in T1.7.
"""

from __future__ import annotations

import asyncio
import sys

import asyncpg

from ticketing.core.logging import configure_logging, get_logger
from ticketing.core.schema import SCHEMA_HEAD
from ticketing.core.settings import get_settings

log = get_logger(__name__)


async def _run() -> int:
    settings = get_settings()
    conn = await asyncpg.connect(settings.database_url)
    try:
        version = await conn.fetchval("SELECT version_num FROM alembic_version")
    finally:
        await conn.close()
    if version != SCHEMA_HEAD:
        log.error("seed.schema_mismatch", found=version, expected=SCHEMA_HEAD)
        return 1
    log.info("seed.done", accounts="pending T1.7")
    return 0


def main() -> None:
    settings = get_settings()
    configure_logging("seed", settings.log_level, settings.log_dir)
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
