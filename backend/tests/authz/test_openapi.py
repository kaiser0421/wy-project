"""Contract checks for generated request security and problem responses."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.routing import APIRoute

from tests.authz.test_route_policies import _walk
from tests.support import ORIGIN
from ticketing.api.app import create_app
from ticketing.api.security import ALL_ROLES, UNSAFE_METHODS
from ticketing.core.errors import PROBLEM_CONTENT_TYPE
from ticketing.core.settings import Settings

PROBLEM_REF = {"$ref": "#/components/schemas/Problem"}


def test_openapi_covers_each_operation() -> None:
    app = create_app(Settings(app_env="test"))
    schema = app.openapi()
    operations = {
        (path, method.upper()): operation
        for path, item in schema["paths"].items()
        for method, operation in item.items()
    }
    assert operations
    for route in _walk(list(app.routes)):
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        policy = route.endpoint.__policy__
        for method in route.methods:
            operation = operations.pop((route.path_format, method))
            responses = operation["responses"]
            assert operation["summary"]
            assert operation["description"]
            assert "internal-error" in responses["500"]["description"]
            if policy.public:
                assert "sessionCookie" not in str(operation.get("security", []))
                assert "csrfHeader" not in str(operation.get("security", []))
            else:
                requirements: dict[str, list[str]] = {"sessionCookie": []}
                if method in UNSAFE_METHODS:
                    requirements["csrfHeader"] = []
                    assert "csrf-failed" in responses["403"]["description"]
                    assert "origin-not-allowed" in responses["403"]["description"]
                if policy.roles != ALL_ROLES:
                    assert "forbidden" in responses["403"]["description"]
                assert operation["security"] == [requirements]
                assert operation["x-allowed-roles"] == sorted(policy.roles)
                assert "unauthenticated" in responses["401"]["description"]
            if operation.get("parameters") or "requestBody" in operation:
                assert "validation-failed" in responses["422"]["description"]
            for status, response in responses.items():
                if not status.isdecimal() or int(status) < 400:
                    continue
                if route.path == "/healthz/ready" and status == "503":
                    assert "application/json" in response["content"]
                    continue
                assert set(response["content"]) == {PROBLEM_CONTENT_TYPE}
                assert response["content"][PROBLEM_CONTENT_TYPE]["schema"] == PROBLEM_REF
    assert not operations
    assert {"type", "title", "status", "code", "retryable"} <= set(
        schema["components"]["schemas"]["Problem"]["required"]
    )


def test_cookie_scheme_matches_secure_deployment() -> None:
    app = create_app(Settings(app_env="test", cookie_secure=True))
    assert app.openapi()["components"]["securitySchemes"]["sessionCookie"]["name"] == "__Host-sid"


def test_openapi_export_matches_the_application() -> None:
    path = Path(__file__).resolve().parents[3] / "docs" / "openapi.json"
    expected = create_app(Settings(app_env="test", cookie_secure=False)).openapi()
    assert json.loads(path.read_text()) == expected


@pytest.mark.integration
async def test_auth_validation_and_conflict_match_openapi(client: httpx.AsyncClient) -> None:
    schema = (await client.get("/api/openapi.json")).json()
    responses = schema["paths"]["/api/v1/auth/register"]["post"]["responses"]
    body = {"email": "openapi@example.test", "password": "a-valid-password"}
    invalid = await client.post(
        "/api/v1/auth/register", json={**body, "password": "x"}, headers=ORIGIN
    )
    assert invalid.json()["errors"][0]["loc"] == ["body", "password"]
    assert (
        await client.post("/api/v1/auth/register", json=body, headers=ORIGIN)
    ).status_code == 201
    duplicate = await client.post("/api/v1/auth/register", json=body, headers=ORIGIN)
    for response, status, code in (
        (invalid, 422, "validation-failed"),
        (duplicate, 409, "email-taken"),
    ):
        assert response.status_code == status
        assert response.headers["Content-Type"] == PROBLEM_CONTENT_TYPE
        assert response.json()["code"] == code
        assert code in responses[str(status)]["description"]
        assert responses[str(status)]["content"][PROBLEM_CONTENT_TYPE]["schema"] == PROBLEM_REF
