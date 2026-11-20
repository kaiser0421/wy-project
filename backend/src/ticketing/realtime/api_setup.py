"""Per-process seat stream readers and session revocation watcher.

Owned by workstream C. `install(app)` is called once by the app factory; use it to add
middleware or lifespan-managed background tasks (via app.state / app.router.lifespan_context).
"""

from __future__ import annotations

from fastapi import FastAPI


def install(app: FastAPI) -> None:
    return None
