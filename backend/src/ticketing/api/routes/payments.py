"""Payments: create payment, list attempts, provider callback.

Owned by workstream A. Register routes with the policy-aware decorators of SecureRouter.
"""

from __future__ import annotations

from ticketing.api.security import SecureRouter

router = SecureRouter(tags=["payments"])
