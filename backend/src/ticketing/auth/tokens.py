"""Session tokens and session-bound CSRF tokens.

- Session token: 256 random bits; only its SHA-256 is stored.
- CSRF token: HMAC-SHA256(SECRET_KEY, "csrf:v<version>:<session_id>"). Stable for the session's
  lifetime, derivable again after a page reload (GET /auth/me), never stored.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid

SESSION_TOKEN_BYTES = 32


def new_session_token() -> str:
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def csrf_token(secret_key: str, version: int, session_id: uuid.UUID) -> str:
    message = f"csrf:v{version}:{session_id}".encode()
    digest = hmac.new(secret_key.encode(), message, hashlib.sha256).hexdigest()
    return f"v{version}.{digest}"


def csrf_matches(secret_key: str, version: int, session_id: uuid.UUID, presented: str) -> bool:
    expected = csrf_token(secret_key, version, session_id)
    return hmac.compare_digest(expected.encode(), presented.encode())
