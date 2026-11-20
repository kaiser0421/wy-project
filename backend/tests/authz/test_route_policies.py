"""Systematic authorization checks over the assembled application.

Every served route must carry an explicit policy; routes that are not API routes (the docs and
the OpenAPI document) are allowlisted by exact path. Registration paths that would bypass the
policy hook are rejected outright.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.routing import Mount, Route

from ticketing.api.app import create_app
from ticketing.api.security import Policy, Public, SecureRouter
from ticketing.core.settings import Settings

NON_API_ROUTES = {"/api/openapi.json", "/api/docs", "/api/docs/oauth2-redirect"}


def _walk(routes: list[object]) -> list[object]:
    flat: list[object] = []
    for route in routes:
        if isinstance(route, Mount):
            flat.extend(_walk(list(route.routes)))
        elif hasattr(route, "original_router"):  # FastAPI wraps included routers
            flat.extend(_walk(list(route.original_router.routes)))
        else:
            flat.append(route)
    return flat


@pytest.fixture(scope="module")
def app() -> FastAPI:
    return create_app(Settings(app_env="test"))


def test_every_api_route_declares_a_policy(app: FastAPI) -> None:
    missing = []
    for route in _walk(list(app.routes)):
        if isinstance(route, APIRoute):
            if not isinstance(getattr(route.endpoint, "__policy__", None), Policy):
                missing.append(f"{sorted(route.methods)} {route.path}")
        elif isinstance(route, Route):
            assert route.path in NON_API_ROUTES, f"unexpected non-API route {route.path}"
        else:
            pytest.fail(f"unexpected route type {type(route).__name__}")
    assert not missing, f"routes without policy: {missing}"


def test_public_routes_are_an_explicit_short_list(app: FastAPI) -> None:
    public = sorted(
        f"{','.join(sorted(r.methods))} {r.path}"
        for r in _walk(list(app.routes))
        if isinstance(r, APIRoute) and r.endpoint.__policy__.public
    )
    allowed_prefixes = (
        "/healthz/",
        "/api/v1/auth/login",
        "/api/v1/auth/register",
        "/api/v1/payments/callbacks",
    )
    unexpected = [p for p in public if not p.split(" ", 1)[1].startswith(allowed_prefixes)]
    assert not unexpected, f"unexpected public routes: {unexpected}"


def test_route_without_policy_is_rejected_at_registration() -> None:
    router = SecureRouter()
    with pytest.raises(TypeError):
        router.add_api_route("/x", lambda: None)
    with pytest.raises(TypeError):
        router.get("/x")(lambda: None)  # type: ignore[call-arg]


@pytest.mark.parametrize("method", ["add_route", "add_websocket_route", "mount", "api_route"])
def test_bypassing_registration_paths_are_blocked(method: str) -> None:
    router = SecureRouter()
    with pytest.raises(TypeError):
        getattr(router, method)("/x", lambda: None)


def test_public_policy_is_explicit() -> None:
    assert Public().public
    assert Public("health").name == "health"
