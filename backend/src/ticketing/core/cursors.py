"""Signed, URL-safe pagination cursors."""

import hmac
from base64 import urlsafe_b64decode, urlsafe_b64encode

from ticketing.core.errors import ProblemError


def _encode(value: bytes) -> str:
    return urlsafe_b64encode(value).decode().rstrip("=")


def seal_cursor(body: bytes, secret: str, domain: bytes, scope: bytes) -> str:
    return _encode(body + hmac.digest(secret.encode(), domain + body + scope, "sha256"))


def open_cursor(token: str, size: int, secret: str, domain: bytes, scope: bytes) -> bytes:
    invalid = ProblemError(409, "cursor-invalidated", "Start again without a cursor")
    try:
        value = urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, UnicodeError) as exc:
        raise invalid from exc
    body = value[:size]
    if len(body) != size or not hmac.compare_digest(
        seal_cursor(body, secret, domain, scope), token
    ):
        raise invalid
    return body
