"""Seat imports: upload, job status/progress, error report.

Owned by workstream B. Register routes with the policy-aware decorators of SecureRouter.
"""

from __future__ import annotations

from ticketing.api.security import SecureRouter

router = SecureRouter(tags=["imports"])
