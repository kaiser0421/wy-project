"""RFC 9457 problem+json errors."""

from __future__ import annotations

from typing import Any

PROBLEM_CONTENT_TYPE = "application/problem+json"
PROBLEM_BASE = "https://ticketing.example/problems/"


class ProblemError(Exception):
    """An error that is rendered as application/problem+json.

    `code` is a stable machine-readable identifier (also the last segment of `type`);
    `retryable` tells clients whether repeating the same request may succeed.
    """

    def __init__(
        self,
        status: int,
        code: str,
        title: str,
        detail: str | None = None,
        *,
        retryable: bool = False,
        errors: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail or title)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.retryable = retryable
        self.errors = errors or []
        self.headers = headers or {}
        self.extra = extra or {}

    def to_body(self, *, instance: str | None, request_id: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": PROBLEM_BASE + self.code,
            "title": self.title,
            "status": self.status,
            "code": self.code,
            "retryable": self.retryable,
        }
        if self.detail:
            body["detail"] = self.detail
        if instance:
            body["instance"] = instance
        if request_id:
            body["request_id"] = request_id
        if self.errors:
            body["errors"] = self.errors
        body.update(self.extra)
        return body


def server_busy(detail: str = "The server is busy, retry shortly.") -> ProblemError:
    return ProblemError(
        503, "server-busy", "Server busy", detail, retryable=True, headers={"Retry-After": "1"}
    )
