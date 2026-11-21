"""The schema version the services require must be the latest migration."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from ticketing.core.schema import SCHEMA_HEAD

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


def test_schema_head_matches_the_single_alembic_head() -> None:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert heads == [SCHEMA_HEAD]
