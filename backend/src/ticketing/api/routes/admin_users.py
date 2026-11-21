"""Admin user management: list users, change role or active state.

A role change or disable revokes every session of the target in the same transaction, so the
target's existing cookies stop working on their next request.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

import orjson
from fastapi import Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ticketing.api.security import Principal, Roles, SecureRouter, current_principal
from ticketing.auth.sessions import lock_live_session, publish_revocations, revoke_user_sessions
from ticketing.core.context import current_request_id
from ticketing.core.db import acquire
from ticketing.core.errors import ProblemError

router = SecureRouter(prefix="/api/v1/admin/users", tags=["admin"])
ADMIN = Roles("admin", name="admin")

Role = Literal["admin", "user", "viewer"]


class AdminUserOut(BaseModel):
    id: int
    email: str
    role: Role
    is_active: bool
    version: int


class AdminUserPage(BaseModel):
    items: list[AdminUserOut]
    next_cursor: str | None = Field(description="Pass as `cursor` to fetch the next page")


class AdminUserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role | None = None
    is_active: bool | None = None


def _etag(version: int) -> str:
    return f'"v{version}"'


def _parse_if_match(value: str | None) -> int:
    if value is None:
        raise ProblemError(
            428,
            "precondition-required",
            "Precondition required",
            "Send If-Match with the user's current ETag.",
        )
    raw = value.strip()
    if not (raw.startswith('"v') and raw.endswith('"') and raw[2:-1].isdigit()):
        raise ProblemError(
            412,
            "precondition-failed",
            "Precondition failed",
            "If-Match must be an ETag returned by this API.",
        )
    return int(raw[2:-1])


_COLUMNS = "id, email::text AS email, role::text AS role, is_active, version"


@router.get("", policy=ADMIN, response_model=AdminUserPage, summary="List users (keyset paging)")
async def list_users(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[int | None, Query(ge=0, description="Last id from the previous page")] = None,
) -> AdminUserPage:
    state = request.app.state
    async with acquire(state.db, state.settings.db_pool_acquire_timeout_s) as conn:
        rows = await conn.fetch(
            f"SELECT {_COLUMNS} FROM users WHERE id > $1 ORDER BY id LIMIT $2",
            cursor or 0,
            limit + 1,
        )
    items = [AdminUserOut(**dict(r)) for r in rows[:limit]]
    next_cursor = str(items[-1].id) if len(rows) > limit and items else None
    return AdminUserPage(items=items, next_cursor=next_cursor)


@router.get("/{user_id}", policy=ADMIN, response_model=AdminUserOut, summary="Get one user")
async def get_user(user_id: int, request: Request, response: Response) -> AdminUserOut:
    state = request.app.state
    async with acquire(state.db, state.settings.db_pool_acquire_timeout_s) as conn:
        row = await conn.fetchrow(
            f"SELECT {_COLUMNS} FROM users WHERE id = $1",
            user_id,
        )
    if row is None:
        raise ProblemError(404, "not-found", "Not found", "User not found.")
    response.headers["ETag"] = _etag(row["version"])
    return AdminUserOut(**dict(row))


@router.patch(
    "/{user_id}",
    policy=ADMIN,
    response_model=AdminUserOut,
    summary="Change a user's role or active state (requires If-Match)",
)
async def patch_user(
    user_id: int,
    body: AdminUserPatch,
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(current_principal)],
    if_match: Annotated[str | None, Header()] = None,
) -> AdminUserOut:
    expected_version = _parse_if_match(if_match)
    if body.role is None and body.is_active is None:
        raise ProblemError(
            422, "validation-failed", "Nothing to change", "Provide role and/or is_active."
        )
    state = request.app.state
    revoked: list[uuid.UUID] = []
    async with (
        acquire(state.db, state.settings.db_pool_acquire_timeout_s) as conn,
        conn.transaction(),
    ):
        # Lock order: users (active admins, then target) before sessions.
        admins = await conn.fetch(
            "SELECT id FROM users WHERE role = 'admin' AND is_active ORDER BY id FOR UPDATE"
        )
        admin_ids = {r["id"] for r in admins}
        if principal.user_id not in admin_ids:
            raise ProblemError(403, "forbidden", "Forbidden", "Your admin access was revoked.")
        if not await lock_live_session(conn, principal.session_id, principal.user_id):
            raise ProblemError(
                401, "unauthenticated", "Authentication required", "Your session ended."
            )
        before = await conn.fetchrow(
            f"SELECT {_COLUMNS} FROM users WHERE id = $1 FOR UPDATE",
            user_id,
        )
        if before is None:
            raise ProblemError(404, "not-found", "Not found", "User not found.")
        if before["version"] != expected_version:
            raise ProblemError(
                412,
                "precondition-failed",
                "Precondition failed",
                "The user was modified by someone else; reload and retry.",
                extra={"current": AdminUserOut(**dict(before)).model_dump()},
            )
        new_role = body.role or before["role"]
        new_active = before["is_active"] if body.is_active is None else body.is_active
        changed = new_role != before["role"] or new_active != before["is_active"]
        if not changed:
            response.headers["ETag"] = _etag(before["version"])
            return AdminUserOut(**dict(before))
        if user_id == principal.user_id:
            raise ProblemError(
                409,
                "self-modification",
                "Cannot change your own access",
                "Ask another admin to change your role or active state.",
            )
        leaves_admin_set = user_id in admin_ids and (new_role != "admin" or not new_active)
        if leaves_admin_set and not (admin_ids - {user_id}):
            raise ProblemError(409, "last-admin", "Cannot remove the last active admin")

        after = await conn.fetchrow(
            f"""UPDATE users SET role = $2::user_role, is_active = $3, version = version + 1,
                       updated_at = now()
                WHERE id = $1 RETURNING {_COLUMNS}""",
            user_id,
            new_role,
            new_active,
        )
        reason = "disabled" if not new_active else "role_changed"
        revoked = await revoke_user_sessions(conn, user_id, reason)
        await conn.execute(
            """INSERT INTO audit_log (actor_id, action, target_type, target_id, before, after,
                                      request_id)
               VALUES ($1, 'user.update', 'user', $2, $3::jsonb, $4::jsonb, $5)""",
            principal.user_id,
            str(user_id),
            orjson.dumps(dict(before)).decode(),
            orjson.dumps(dict(after)).decode(),
            current_request_id(),
        )
    await publish_revocations(state.redis, revoked)
    response.headers["ETag"] = _etag(after["version"])
    return AdminUserOut(**dict(after))
