"""Orders: create (specific seats or zone+quantity), list, get, cancel.

Owned by workstream A. Register routes with the policy-aware decorators of SecureRouter.
"""

from __future__ import annotations

from ticketing.api.security import SecureRouter

router = SecureRouter(tags=["orders"])
