"""Systematic authorization checks over the assembled application.

Every served route must carry an explicit policy; routes that are not API routes (the docs and
the OpenAPI document) are allowlisted by exact method and path. Registration paths that would
bypass the policy hook are rejected outright.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute, Mount, Route

from ticketing.api.app import create_app
from ticketing.api.security import Policy, Public, SecureRouter
from ticketing.core.settings import Settings

NON_API_ROUTES = {
    (method, path)
    for method in ("GET", "HEAD")
    for path in ("/api/openapi.json", "/api/docs", "/api/docs/oauth2-redirect")
}
PUBLIC_API_ROUTES = {
    ("GET", "/healthz/live"),
    ("GET", "/healthz/ready"),
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/register"),
    ("POST", "/api/v1/payments/callbacks"),
}


def _walk(routes: Sequence[BaseRoute], prefix: str = "") -> Iterator[tuple[APIRoute | Route, str]]:
    for route in routes:
        if isinstance(route, Mount):
            pytest.fail(f"Unexpected mount bypassing the API policy router: {prefix}{route.path}")
        elif hasattr(route, "original_router"):  # FastAPI wraps included routers
            yield from _walk(route.original_router.routes, prefix + route.include_context.prefix)
        elif isinstance(route, APIRoute | Route):
            yield route, prefix + route.path
        else:
            pytest.fail(f"unexpected route type {type(route).__name__}")


@pytest.fixture(scope="module")
def app() -> FastAPI:
    return create_app(Settings(app_env="test"))


def test_every_api_route_declares_a_policy(app: FastAPI) -> None:
    missing = []
    non_api = set()
    for route, path in _walk(app.routes):
        if isinstance(route, APIRoute):
            if not isinstance(getattr(route.endpoint, "__policy__", None), Policy):
                missing.append(f"{sorted(route.methods)} {path}")
        else:
            assert route.methods, f"non-API route accepts all methods: {path}"
            non_api.update((method, path) for method in route.methods)
    assert not missing, f"routes without policy: {missing}"
    assert non_api == NON_API_ROUTES


def _public_routes(app: FastAPI) -> set[tuple[str, str]]:
    return {
        (method, path)
        for route, path in _walk(app.routes)
        if isinstance(route, APIRoute) and route.endpoint.__policy__.public
        for method in route.methods
    }


def test_public_routes_are_an_explicit_short_list(app: FastAPI) -> None:
    assert _public_routes(app) == PUBLIC_API_ROUTES


def test_public_route_inspection_checks_methods_and_included_prefixes() -> None:
    app = FastAPI()
    router = SecureRouter()
    router.get("/healthz/live", policy=Public())(lambda: None)
    router.post("/healthz/live", policy=Public())(lambda: None)
    app.include_router(router)
    app.include_router(router, prefix="/internal")
    assert _public_routes(app) - PUBLIC_API_ROUTES == {
        ("POST", "/healthz/live"),
        ("GET", "/internal/healthz/live"),
        ("POST", "/internal/healthz/live"),
    }


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
