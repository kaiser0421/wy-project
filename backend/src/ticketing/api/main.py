"""API entrypoint.

On SIGTERM the server first flips readiness to 503 and keeps serving for
`SHUTDOWN_DRAIN_SECONDS` (so the proxy stops routing new work here), then stops accepting
connections and waits up to 20 s for in-flight requests (Uvicorn graceful timeout).
"""

from __future__ import annotations

import asyncio
import os
import socket

import uvicorn
from fastapi import FastAPI

from ticketing.api.app import create_asgi_app
from ticketing.core.logging import get_logger
from ticketing.core.settings import get_settings

log = get_logger(__name__)


class DrainingServer(uvicorn.Server):
    drain_seconds: float = 0.0
    fastapi_app: FastAPI | None = None

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        if self.fastapi_app is not None:
            self.fastapi_app.state.draining = True
        log.info("api.draining", seconds=self.drain_seconds)
        await asyncio.sleep(self.drain_seconds)
        await super().shutdown(sockets=sockets)


def main() -> None:
    settings = get_settings()
    app = create_asgi_app(settings)
    config = uvicorn.Config(
        app,
        host="0.0.0.0",  # noqa: S104 - container network only; nginx is the ingress
        port=int(os.environ.get("PORT", "8000")),
        loop="uvloop",
        http="httptools",
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "*"),
        timeout_graceful_shutdown=20,
        # Longer than nginx's upstream keepalive_timeout (60 s), so nginx closes idle ones first.
        timeout_keep_alive=75,
        backlog=4096,
        log_config=None,
    )
    server = DrainingServer(config)
    server.drain_seconds = settings.shutdown_drain_seconds
    server.fastapi_app = app.fastapi  # type: ignore[attr-defined]
    server.run()


if __name__ == "__main__":
    main()
