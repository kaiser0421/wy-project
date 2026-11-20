"""OpenAPI components for booking and payment problems."""

from typing import Any

from ticketing.core.errors import PROBLEM_BASE, PROBLEM_CONTENT_TYPE

READ_ERRORS = {401: ("unauthenticated",), 422: ("validation-failed",), 503: ("server-busy",)}
WRITE_ERRORS = {
    **READ_ERRORS,
    403: ("forbidden", "csrf-failed", "origin-not-allowed"),
    404: ("not-found",),
    422: ("validation-failed", "idempotency-key-reused"),
}
REPLAY_HEADER = {
    "Idempotent-Replayed": {
        "description": "Present on replay.",
        "schema": {"type": "string", "example": "true"},
    }
}
RETRYABLE = frozenset(
    {"server-busy", "seat-contended", "allocation-contended", "idempotency-in-progress"}
)


def problem_responses(errors: dict[int, tuple[str, ...]]) -> dict[int | str, dict[str, Any]]:
    return {
        status: {
            "description": ", ".join(codes),
            "headers": {
                **(REPLAY_HEADER if "payment-declined" in codes else {}),
                **(
                    {
                        "Retry-After": {
                            "description": "Seconds to wait before retrying this problem.",
                            "schema": {"type": "integer", "example": 1},
                        }
                    }
                    if {"idempotency-in-progress", "server-busy"}.intersection(codes)
                    else {}
                ),
            },
            "content": {
                PROBLEM_CONTENT_TYPE: {
                    "schema": {
                        "type": "object",
                        "required": ["type", "title", "status", "code"],
                        "properties": {
                            "type": {"type": "string"},
                            "title": {"type": "string"},
                            "status": {"type": "integer"},
                            "code": {"type": "string", "enum": list(codes)},
                            "retryable": {"type": "boolean"},
                        },
                    },
                    "examples": {
                        code: {
                            "value": {
                                "type": PROBLEM_BASE + code,
                                "title": code.replace("-", " ").capitalize(),
                                "status": status,
                                "code": code,
                                "retryable": code in RETRYABLE,
                            }
                        }
                        for code in codes
                    },
                }
            },
        }
        for status, codes in errors.items()
    }
