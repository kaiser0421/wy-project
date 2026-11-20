"""Seat snapshot and live seat stream (SSE).

Owned by workstream C. Register routes with the policy-aware decorators of SecureRouter.
"""

from __future__ import annotations

from ticketing.api.security import SecureRouter

router = SecureRouter(tags=["seats"])
