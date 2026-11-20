"""Use the started app directly so each ASGI request owns its scope state."""

import httpx
import pytest


@pytest.fixture
def make_client(asgi_app, running_app):
    # LifespanManager.app injects the same state dictionary into every request.
    # The production ASGI server gives each request a separate state dictionary.
    def factory():
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=asgi_app), base_url="http://testserver"
        )

    return factory
