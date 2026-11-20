"""Shared fixtures.

Integration tests run against a real PostgreSQL (same image/config as production). The schema
is migrated once into a template database; each test that needs isolation gets its own clone
(`CREATE DATABASE ... TEMPLATE`), so concurrency and fault tests never share state.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import httpx
import pytest
from asgi_lifespan import LifespanManager
from starlette.types import ASGIApp

from tests.support import TestClock, install_test_clock
from ticketing.api.app import create_asgi_app
from ticketing.core.settings import Settings

BACKEND_DIR = Path(__file__).resolve().parents[1]
TEMPLATE_DB = "ticketing_template"
ADMIN_URL = os.environ.get("TEST_ADMIN_DATABASE_URL")


def _db_url(name: str) -> str:
    assert ADMIN_URL is not None
    base, _, _ = ADMIN_URL.rpartition("/")
    return f"{base}/{name}"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if ADMIN_URL:
        return
    skip = pytest.mark.skip(reason="TEST_ADMIN_DATABASE_URL not set (run `make test`)")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


def run_alembic(db_url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": db_url}
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(BACKEND_DIR / "alembic.ini"), *args],
        check=True,
        env=env,
        cwd=BACKEND_DIR,
    )


@pytest.fixture(scope="session")
def template_db() -> str:
    """Create and migrate the template database once per test session."""
    import asyncio

    async def _create() -> None:
        conn = await asyncpg.connect(ADMIN_URL)
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{TEMPLATE_DB}" WITH (FORCE)')
            await conn.execute(f'CREATE DATABASE "{TEMPLATE_DB}"')
        finally:
            await conn.close()

    asyncio.run(_create())
    run_alembic(_db_url(TEMPLATE_DB), "upgrade", "head")
    return TEMPLATE_DB


@pytest.fixture
async def db_url(template_db: str) -> AsyncIterator[str]:
    """A fresh database cloned from the migrated template, dropped after the test."""
    name = f"t_{uuid.uuid4().hex[:16]}"
    admin = await asyncpg.connect(ADMIN_URL)
    try:
        await admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template_db}"')
    finally:
        await admin.close()
    try:
        yield _db_url(name)
    finally:
        admin = await asyncpg.connect(ADMIN_URL)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()


@pytest.fixture
async def db(db_url: str) -> AsyncIterator[asyncpg.Connection]:
    conn = await asyncpg.connect(db_url)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def test_clock(db: asyncpg.Connection) -> TestClock:
    """Freezable database clock for deadline tests (no sleeping)."""
    return await install_test_clock(db)


@pytest.fixture
def settings(db_url: str) -> Settings:
    return Settings(
        app_env="test",
        database_url=db_url,
        redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        enable_test_hooks=True,
        shutdown_drain_seconds=0,
    )


@pytest.fixture
def asgi_app(settings: Settings) -> ASGIApp:
    return create_asgi_app(settings)


@pytest.fixture
async def client(asgi_app: ASGIApp) -> AsyncIterator[httpx.AsyncClient]:
    async with LifespanManager(asgi_app) as manager:  # type: ignore[arg-type]
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            yield c
