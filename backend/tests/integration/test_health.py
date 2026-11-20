from __future__ import annotations

import httpx
import pytest
from starlette.types import ASGIApp

pytestmark = pytest.mark.integration


async def test_live(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz/live")
    assert response.status_code == 200


async def test_ready_checks_database_redis_and_schema(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz/ready")
    assert response.status_code == 200, response.text
    checks = response.json()["checks"]
    assert checks["postgres"] == "ok"
    assert checks["redis"] == "ok"
    assert checks["schema"] == "0001_baseline"


async def test_ready_reports_draining(client: httpx.AsyncClient, asgi_app: ASGIApp) -> None:
    asgi_app.fastapi.state.draining = True  # type: ignore[attr-defined]
    response = await client.get("/healthz/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["draining"] is True
