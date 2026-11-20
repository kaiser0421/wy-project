"""One-shot seed job: creates the Admin / User / Viewer test accounts (idempotent).

An account that already exists is left untouched (password, role and state): the seed never
grants privileges to an existing account just because its email matches.
"""

from __future__ import annotations

import asyncio
import sys

import asyncpg

from ticketing.core.logging import configure_logging, get_logger
from ticketing.core.passwords import PasswordHasher
from ticketing.core.schema import SCHEMA_HEAD
from ticketing.core.settings import Settings, get_settings

log = get_logger(__name__)


def seed_accounts(settings: Settings) -> list[tuple[str, str, str]]:
    return [
        (settings.seed_admin_email, settings.seed_admin_password.get_secret_value(), "admin"),
        (settings.seed_user_email, settings.seed_user_password.get_secret_value(), "user"),
        (settings.seed_viewer_email, settings.seed_viewer_password.get_secret_value(), "viewer"),
    ]


async def run_seed(settings: Settings) -> int:
    conn = await asyncpg.connect(settings.database_url)
    hasher = PasswordHasher(settings)
    try:
        version = await conn.fetchval("SELECT version_num FROM alembic_version")
        if version != SCHEMA_HEAD:
            log.error("seed.schema_mismatch", found=version, expected=SCHEMA_HEAD)
            return 1
        for email, password, role in seed_accounts(settings):
            email = email.strip().lower()
            existing = await conn.fetchrow(
                "SELECT role::text AS role FROM users WHERE email = $1", email
            )
            if existing is not None:
                if existing["role"] != role:
                    log.warning(
                        "seed.account_role_differs",
                        email=email,
                        expected=role,
                        actual=existing["role"],
                    )
                continue
            password_hash = await hasher.hash(password)
            await conn.execute(
                "INSERT INTO users (email, password_hash, role) VALUES ($1, $2, $3::user_role) "
                "ON CONFLICT (email) DO NOTHING",
                email,
                password_hash,
                role,
            )
            log.info("seed.account_created", email=email, role=role)
    finally:
        hasher.close()
        await conn.close()
    return 0


def main() -> None:
    settings = get_settings()
    configure_logging("seed", settings.log_level, settings.log_dir)
    sys.exit(asyncio.run(run_seed(settings)))


if __name__ == "__main__":
    main()
