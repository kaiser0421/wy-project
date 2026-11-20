"""Request metrics middleware and per-second telemetry flush.

Owned by workstream D. `install(app)` is called once by the app factory; use it to add
middleware or lifespan-managed background tasks (via app.state / app.router.lifespan_context).
"""

from __future__ import annotations

from fastapi import FastAPI


def install(app: FastAPI) -> None:
    return None
