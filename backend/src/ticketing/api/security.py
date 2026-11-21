"""Authentication, authorization policies and the policy-enforcing router.

Every route is registered through `SecureRouter`, whose decorators require a `policy=` argument;
a route without one fails at import time. The policy is attached to the endpoint function
(`__policy__`) so tests can introspect the assembled application and prove every served route
declares one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from fastapi import APIRouter, Depends, Request
from fastapi.params import Depends as DependsParam

from ticketing.auth.sessions import authenticate
from ticketing.auth.tokens import csrf_matches
from ticketing.core.db import acquire
from ticketing.core.errors import ProblemError

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
ALL_ROLES = frozenset({"admin", "user", "viewer"})


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: int
    email: str
    role: str
    session_id: Any  # uuid.UUID

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


@dataclass(frozen=True, slots=True)
class Policy:
    """Who may call a route. `roles=None` means public (no session required)."""

    name: str
    roles: frozenset[str] | None = field(default=None)

    @property
    def public(self) -> bool:
        return self.roles is None


def Public(name: str = "public") -> Policy:
    return Policy(name=name, roles=None)


def Authenticated(name: str = "authenticated") -> Policy:
    return Policy(name=name, roles=ALL_ROLES)


def Roles(*roles: str, name: str | None = None) -> Policy:
    unknown = set(roles) - ALL_ROLES
    if unknown:
        raise ValueError(f"unknown roles: {sorted(unknown)}")
    return Policy(name=name or "roles:" + ",".join(sorted(roles)), roles=frozenset(roles))


def session_cookie_name(request: Request) -> str:
    return "__Host-sid" if request.app.state.settings.cookie_secure else "sid"


def problem_unauthenticated() -> ProblemError:
    return ProblemError(
        401, "unauthenticated", "Authentication required", "Sign in to access this resource."
    )


def problem_forbidden(detail: str = "Your role does not allow this action.") -> ProblemError:
    return ProblemError(403, "forbidden", "Forbidden", detail)


def check_origin(request: Request) -> None:
    """Exact Origin allowlist (Referer fallback) for state-changing browser requests."""
    allowed: tuple[str, ...] = request.app.state.settings.allowed_origins
    origin = request.headers.get("origin")
    if origin is not None:
        if origin in allowed:
            return
    else:
        referer = request.headers.get("referer", "")
        if any(referer == o or referer.startswith(o + "/") for o in allowed):
            return
    raise ProblemError(
        403,
        "origin-not-allowed",
        "Origin not allowed",
        "Requests must come from the ticketing web application.",
    )


async def resolve_principal(request: Request) -> Principal | None:
    token = request.cookies.get(session_cookie_name(request))
    if not token or len(token) > 128:
        return None
    state = request.app.state
    async with acquire(state.db, state.settings.db_pool_acquire_timeout_s) as conn:
        found = await authenticate(conn, token)
    if found is None:
        return None
    if found.needs_refresh:
        state.last_seen.touch(found.session_id)
    return Principal(
        user_id=found.user_id, email=found.email, role=found.role, session_id=found.session_id
    )


def _enforce(policy: Policy) -> Callable[[Request], Any]:
    async def dependency(request: Request) -> Principal | None:
        if policy.public:
            request.state.principal = None
            return None
        principal = await resolve_principal(request)
        if principal is None:
            raise problem_unauthenticated()
        assert policy.roles is not None
        if principal.role not in policy.roles:
            raise problem_forbidden()
        if request.method in UNSAFE_METHODS:
            settings = request.app.state.settings
            presented = request.headers.get("x-csrf-token", "")
            if not csrf_matches(
                settings.secret_key.get_secret_value(),
                settings.csrf_key_version,
                principal.session_id,
                presented,
            ):
                raise ProblemError(
                    403,
                    "csrf-failed",
                    "CSRF check failed",
                    "Send the X-CSRF-Token header returned by /auth/me.",
                )
            if request.headers.get("origin") is not None:
                check_origin(request)
        request.state.principal = principal
        return principal

    dependency.__name__ = f"policy_{policy.name.replace(':', '_').replace(',', '_')}"
    return dependency


def current_principal(request: Request) -> Principal:
    """Inject the principal resolved by the route's policy (only valid on non-public routes)."""
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is None:
        raise problem_unauthenticated()
    return principal


F = TypeVar("F", bound=Callable[..., Any])


class SecureRouter(APIRouter):
    """APIRouter whose route decorators require an explicit authorization policy."""

    def add_api_route(
        self,
        path: str,
        endpoint: Callable[..., Any],
        *,
        policy: Policy | None = None,
        **kwargs: Any,
    ) -> None:
        if policy is None:
            raise TypeError(f"route {path!r} registered without policy=")
        dependencies: list[DependsParam] = list(kwargs.pop("dependencies", None) or [])
        dependencies.insert(0, Depends(_enforce(policy)))
        endpoint.__policy__ = policy  # type: ignore[attr-defined]
        super().add_api_route(path, endpoint, dependencies=dependencies, **kwargs)

    def _secure(
        self, path: str, methods: list[str], policy: Policy, **kwargs: Any
    ) -> Callable[[F], F]:
        def decorator(func: F) -> F:
            self.add_api_route(path, func, methods=methods, policy=policy, **kwargs)
            return func

        return decorator

    def get(self, path: str, *, policy: Policy, **kwargs: Any) -> Callable[[F], F]:  # type: ignore[override]
        return self._secure(path, ["GET"], policy, **kwargs)

    def post(self, path: str, *, policy: Policy, **kwargs: Any) -> Callable[[F], F]:  # type: ignore[override]
        return self._secure(path, ["POST"], policy, **kwargs)

    def put(self, path: str, *, policy: Policy, **kwargs: Any) -> Callable[[F], F]:  # type: ignore[override]
        return self._secure(path, ["PUT"], policy, **kwargs)

    def patch(self, path: str, *, policy: Policy, **kwargs: Any) -> Callable[[F], F]:  # type: ignore[override]
        return self._secure(path, ["PATCH"], policy, **kwargs)

    def delete(self, path: str, *, policy: Policy, **kwargs: Any) -> Callable[[F], F]:  # type: ignore[override]
        return self._secure(path, ["DELETE"], policy, **kwargs)

    def api_route(self, *_args: Any, **_kwargs: Any) -> Any:
        raise TypeError("use the policy-aware get/post/put/patch/delete decorators")

    def add_api_websocket_route(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("websocket routes must declare a policy (not supported yet)")

    def add_route(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("plain Starlette routes bypass policies; use the secure decorators")

    def add_websocket_route(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("plain websocket routes bypass policies")

    def mount(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("mounted apps bypass policies")
