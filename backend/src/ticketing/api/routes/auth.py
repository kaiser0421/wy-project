"""Registration, sign-in, sign-out and the current-user endpoint."""

from __future__ import annotations

import asyncio
import re
from typing import Annotated, Literal

import asyncpg
from fastapi import Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ticketing.api.security import (
    Authenticated,
    Principal,
    Public,
    SecureRouter,
    check_origin,
    current_principal,
    session_cookie_name,
)
from ticketing.auth.sessions import create_session, publish_revocations, revoke_session
from ticketing.auth.tokens import csrf_token
from ticketing.core.db import acquire
from ticketing.core.errors import ProblemError
from ticketing.core.settings import Settings

router = SecureRouter(prefix="/api/v1/auth", tags=["auth"])
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s]{2,63}$")


def normalize_email(value: str) -> str:
    return value.strip().lower()


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254, examples=["user@example.com"])
    password: str = Field(min_length=1, max_length=256)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        value = normalize_email(value)
        if not _EMAIL.match(value):
            raise ValueError("invalid email address")
        return value


class Registration(Credentials):
    password: str = Field(min_length=10, max_length=128, description="10 to 128 characters")


class UserOut(BaseModel):
    id: int
    email: str
    role: Literal["admin", "user", "viewer"]


class SessionOut(BaseModel):
    user: UserOut
    csrf_token: str = Field(description="Send as X-CSRF-Token on POST/PUT/PATCH/DELETE")


def _csrf(settings: Settings, session_id: object) -> str:
    return csrf_token(settings.secret_key.get_secret_value(), settings.csrf_key_version, session_id)  # type: ignore[arg-type]


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _set_session_cookie(response: Response, request: Request, token: str) -> None:
    settings: Settings = request.app.state.settings
    response.set_cookie(
        session_cookie_name(request),
        token,
        max_age=settings.session_absolute_hours * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


_USER_COLUMNS = "SELECT email::text AS email, role::text AS role, is_active, password_hash "
_LOCK_FOR_LOGIN = _USER_COLUMNS + "FROM users WHERE id = $1 FOR SHARE"
_LOCK_FOR_REHASH = _USER_COLUMNS + "FROM users WHERE id = $1 FOR UPDATE"


def invalid_credentials() -> ProblemError:
    """A fresh error per failure (a shared instance would accumulate tracebacks)."""
    return ProblemError(
        401, "invalid-credentials", "Invalid credentials", "The email or password is incorrect."
    )


async def _penalize_failure(request: Request, email: str) -> None:
    """Count a failed sign-in for this email and slow down repeated guessing.

    Accounts are never locked (that would let anyone lock out a victim): after a few failures
    each attempt waits progressively longer (capped), and a generous hard cap bounds guessing.
    """
    settings: Settings = request.app.state.settings
    failures = await request.app.state.rate_limiter.count("login-fail", email, 900)
    if failures > 3:
        await asyncio.sleep(min(0.25 * 2 ** (failures - 4), 3.0))
    if failures > settings.rate_limit_login_per_email:
        raise ProblemError(
            429,
            "rate-limited",
            "Too many requests",
            "Too many failed sign-ins for this account; retry later.",
            retryable=True,
            headers={"Retry-After": "60"},
        )


@router.post(
    "/register",
    policy=Public("register"),
    status_code=201,
    summary="Create a User account",
    response_model=UserOut,
)
async def register(body: Registration, request: Request) -> UserOut:
    check_origin(request)
    state = request.app.state
    settings: Settings = state.settings
    await state.rate_limiter.hit(
        "register-ip", _client_ip(request), settings.rate_limit_register_per_ip, 3600
    )
    password_hash = await state.passwords.hash(body.password)
    try:
        async with acquire(state.db, settings.db_pool_acquire_timeout_s) as conn:
            row = await conn.fetchrow(
                "INSERT INTO users (email, password_hash, role) VALUES ($1, $2, 'user') "
                "RETURNING id, email::text AS email, role::text AS role",
                body.email,
                password_hash,
            )
    except asyncpg.UniqueViolationError as exc:
        raise ProblemError(409, "email-taken", "Email already registered") from exc
    return UserOut(id=row["id"], email=row["email"], role=row["role"])


@router.post(
    "/login",
    policy=Public("login"),
    response_model=SessionOut,
    summary="Sign in; sets the session cookie and returns the CSRF token",
)
async def login(body: Credentials, request: Request, response: Response) -> SessionOut:
    check_origin(request)
    state = request.app.state
    settings: Settings = state.settings
    limiter = state.rate_limiter
    await limiter.hit("login-ip", _client_ip(request), settings.rate_limit_login_per_ip, 60)
    if await limiter.count("login-fail", body.email, 900, increment=False) > (
        settings.rate_limit_login_per_email
    ):
        await _penalize_failure(request, body.email)
    # The global budget is charged only for requests admitted to password verification.
    await limiter.hit("login-global", "all", settings.rate_limit_login_global_per_s, 1)

    async with acquire(state.db, settings.db_pool_acquire_timeout_s) as conn:
        user = await conn.fetchrow(
            "SELECT id, password_hash FROM users WHERE email = $1", body.email
        )
    verified = await state.passwords.verify(user["password_hash"] if user else None, body.password)
    if not verified or user is None:
        await _penalize_failure(request, body.email)
        raise invalid_credentials()

    # Rehash (parameters changed) is computed before the transaction and applied atomically
    # with the session insert, so a failure cannot leave a session whose cookie was never sent.
    new_hash = None
    if state.passwords.needs_rehash(user["password_hash"]):
        new_hash = await state.passwords.hash(body.password)

    async with acquire(state.db, settings.db_pool_acquire_timeout_s) as conn, conn.transaction():
        # Re-read under lock: the account may have been disabled or its password changed
        # while the hash was being verified; disable/role change take FOR UPDATE on this row.
        locked = await conn.fetchrow(_LOCK_FOR_REHASH if new_hash else _LOCK_FOR_LOGIN, user["id"])
        if (
            locked is None
            or not locked["is_active"]
            or (locked["password_hash"] != user["password_hash"])
        ):
            raise invalid_credentials()
        if new_hash is not None:
            await conn.execute(
                "UPDATE users SET password_hash = $1 WHERE id = $2", new_hash, user["id"]
            )
        session_id, token = await create_session(
            conn, user["id"], settings, _client_ip(request), request.headers.get("user-agent")
        )

    _set_session_cookie(response, request, token)
    response.headers["Cache-Control"] = "no-store"
    return SessionOut(
        user=UserOut(id=user["id"], email=locked["email"], role=locked["role"]),
        csrf_token=_csrf(settings, session_id),
    )


@router.post(
    "/logout",
    policy=Authenticated("logout"),
    status_code=204,
    summary="Sign out; the session cookie stops working immediately",
)
async def logout(
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(current_principal)],
) -> Response:
    state = request.app.state
    async with acquire(state.db, state.settings.db_pool_acquire_timeout_s) as conn:
        await revoke_session(conn, principal.session_id, "logout")
    await publish_revocations(state.redis, [principal.session_id])
    result = Response(status_code=204)
    result.delete_cookie(
        session_cookie_name(request),
        path="/",
        secure=state.settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return result


@router.get(
    "/me",
    policy=Authenticated("me"),
    response_model=SessionOut,
    summary="Current user and the session's CSRF token",
)
async def me(
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(current_principal)],
) -> SessionOut:
    response.headers["Cache-Control"] = "no-store"
    return SessionOut(
        user=UserOut(id=principal.user_id, email=principal.email, role=principal.role),
        csrf_token=_csrf(request.app.state.settings, principal.session_id),
    )
