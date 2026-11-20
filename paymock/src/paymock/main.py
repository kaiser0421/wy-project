"""Simulated payment provider.

P0 stub: health endpoint only. Intents, captures, refunds and signed callbacks are
implemented in T3.1.
"""

from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI

app = FastAPI(title="Payment Provider Simulator", version="0.1.0", docs_url="/docs")


@app.get("/healthz/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


def main() -> None:
    uvicorn.run(
        app,
        host="0.0.0.0",  # noqa: S104 - internal network only
        port=int(os.environ.get("PORT", "8000")),
        loop="uvloop",
        http="httptools",
        access_log=False,
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    main()
