"""Serialize writes by user and key, then save responses with their database effects."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Annotated, Any
from uuid import UUID

import asyncpg
import orjson
from fastapi import Header
from fastapi.responses import JSONResponse

from ticketing.core.errors import PROBLEM_CONTENT_TYPE, ProblemError

IdempotencyKey = Annotated[
    str,
    Header(
        min_length=8,
        max_length=128,
        description="Retry key scoped to the caller. Completed results are retained for 24 hours.",
        examples=["booking-6a443d72"],
    ),
]
RETRY_DESCRIPTION = (
    "Reuse the Idempotency-Key with the same request within 24 hours to replay the original "
    "response with Idempotent-Replayed: true. An in-flight request returns 409 and Retry-After: 1. "
    "After it completes, changing the operation, resource or body returns 422."
)


@dataclass(slots=True)
class Result:
    status: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)
    replayed: bool = False

    def response(self) -> JSONResponse:
        headers = dict(self.headers)
        if self.replayed:
            headers["Idempotent-Replayed"] = "true"
        return JSONResponse(
            self.body,
            status_code=self.status,
            headers=headers,
            media_type=PROBLEM_CONTENT_TYPE if self.status >= 400 else "application/json",
        )


def in_progress() -> ProblemError:
    return ProblemError(
        409,
        "idempotency-in-progress",
        "Request in progress",
        retryable=True,
        headers={"Retry-After": "1"},
    )


def request_hash(operation: str, resource: int | UUID, body: dict[str, Any]) -> bytes:
    return hashlib.sha256(
        orjson.dumps([operation, str(resource), body], option=orjson.OPT_SORT_KEYS)
    ).digest()


@dataclass(frozen=True, slots=True)
class Claim:
    user_id: int
    key: str
    operation: str
    digest: bytes

    @property
    def lock_id(self) -> int:
        value = hashlib.sha256(orjson.dumps(["idempotency", self.user_id, self.key])).digest()
        return int.from_bytes(value[:8], byteorder="big", signed=True)

    async def _locked_row(self, conn: asyncpg.Connection) -> asyncpg.Record | None:
        if not await conn.fetchval(
            "SELECT pg_try_advisory_xact_lock($1::bigint)",
            self.lock_id,
        ):
            raise in_progress()
        row = await conn.fetchrow(
            """SELECT request_hash, status, resource_ref, response_status, response_body,
                      response_headers
               FROM idempotency_keys WHERE user_id=$1 AND key=$2""",
            self.user_id,
            self.key,
        )
        if row is not None and row["request_hash"] != self.digest:
            raise ProblemError(422, "idempotency-key-reused", "Idempotency key already used")
        return row

    async def check(self, conn: asyncpg.Connection) -> Result | None:
        row = await self._locked_row(conn)
        if row is None:
            return None
        if row["status"] == "in_progress":
            raise in_progress()
        return _stored_result(row, replayed=True)

    async def payment_ref(self, conn: asyncpg.Connection) -> UUID | None:
        """A committed claim names the attempt even while the provider outcome is unknown."""
        row = await self._locked_row(conn)
        if row is None:
            return None
        return UUID(row["resource_ref"])

    async def save(
        self,
        conn: asyncpg.Connection,
        result: Result | None = None,
        *,
        resource_ref: str | UUID | None = None,
    ) -> None:
        await conn.execute(
            """INSERT INTO idempotency_keys
                 (user_id, key, operation, request_hash, status, resource_ref,
                  response_status, response_headers, response_body, created_at, updated_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,app_now(),app_now())""",
            self.user_id,
            self.key,
            self.operation,
            self.digest,
            "completed" if result else "in_progress",
            str(resource_ref) if resource_ref else None,
            result.status if result else None,
            orjson.dumps(result.headers).decode() if result else None,
            orjson.dumps(result.body).decode() if result else None,
        )


def _stored_result(row: asyncpg.Record, *, replayed: bool) -> Result:
    return Result(
        row["response_status"],
        orjson.loads(row["response_body"]),
        orjson.loads(row["response_headers"]),
        replayed=replayed,
    )


async def finish_payment(conn: asyncpg.Connection, attempt_id: UUID, result: Result) -> None:
    # Several keys can point to one intent. The order lock protects the result for all of them.
    await conn.execute(
        """UPDATE idempotency_keys SET status='completed', response_status=$2,
                  response_headers=$3, response_body=$4, updated_at=app_now()
           WHERE operation='create-payment' AND resource_ref=$1 AND status='in_progress'""",
        str(attempt_id),
        result.status,
        orjson.dumps(result.headers).decode(),
        orjson.dumps(result.body).decode(),
    )
