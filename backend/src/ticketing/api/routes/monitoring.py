"""Admin monitoring: metrics, alerts, request logs.

Owned by workstream D. Register routes with the policy-aware decorators of SecureRouter.
"""

from __future__ import annotations

from ticketing.api.security import SecureRouter

router = SecureRouter(tags=["monitoring"])
