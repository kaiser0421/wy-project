import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import orjson
import pytest
from asgi_lifespan import LifespanManager

from paymock.delivery import send_due, signature
from paymock.main import create_app
from paymock.store import Store
from ticketing.payments.callbacks import verify_signature

SECRET = "simulator-test-secret"
TOKEN = "simulator-test-token"
TOKEN_HEADER = "X-Payment-Provider-Token"
CALLBACK_URL = "http://api:8000/api/v1/payments/callbacks"


@pytest.fixture(autouse=True)
def simulator_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYMOCK_STORE_PATH", str(tmp_path / "mock.sqlite3"))
    monkeypatch.setenv("PAYMENT_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("PAYMENT_PROVIDER_TOKEN", TOKEN)
    monkeypatch.setenv("PAYMOCK_CALLBACK_URL", CALLBACK_URL)
    monkeypatch.setenv("PAYMOCK_DROP_CALLBACKS", "1")


async def test_simulator_capture_refund_expiry_and_restart(monkeypatch):
    app = create_app()
    inputs = {
        "merchant_payment_id": str(uuid4()),
        "amount_twd": 53,
        "currency": "TWD",
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://paymock",
            headers={TOKEN_HEADER: TOKEN},
        ) as client,
    ):
        a, b = await asyncio.gather(*(client.post("/v1/intents", json=inputs) for _ in range(2)))
        assert a.status_code == b.status_code == 200
        assert a.json() == b.json()
        conflict = await client.post("/v1/intents", json={**inputs, "amount_twd": 54})
        assert conflict.status_code == 409
        intent = a.json()["intent_id"]
        checkout = await client.get(f"/pay/{intent}")
        assert "<button>Pay</button>" in checkout.text
        assert "<button>Decline</button>" in checkout.text
        a, b = await asyncio.gather(*(client.post(f"/pay/{intent}/confirm") for _ in range(2)))
        assert a.status_code == b.status_code == 200
        capture_id = a.json()["event"]["capture_id"]
        assert capture_id == b.json()["event"]["capture_id"]
        assert (await client.post(f"/pay/{intent}/decline")).status_code == 409

        dead = await client.post(
            "/v1/intents",
            json={
                **inputs,
                "merchant_payment_id": str(uuid4()),
                "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            },
        )
        assert dead.json()["status"] == "expired"
        dead_intent = dead.json()["intent_id"]
        assert (await client.post(f"/pay/{dead_intent}/confirm")).status_code == 409
        merchant = str(uuid4())
        assert (await client.post(f"/v1/intents/{merchant}/cancel")).json() is None
        tombstoned = await client.post(
            "/v1/intents", json={**inputs, "merchant_payment_id": merchant}
        )
        assert tombstoned.json()["status"] == "cancelled"

    new_callback_url = "http://api-new:8000/callbacks"
    monkeypatch.setenv("PAYMOCK_CALLBACK_URL", new_callback_url)
    restart = create_app()
    async with (
        LifespanManager(restart),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restart),
            base_url="http://paymock",
            headers={TOKEN_HEADER: TOKEN},
        ) as client,
    ):
        restored = await client.get(f"/v1/intents/{inputs['merchant_payment_id']}")
        assert restored.json()["event"]["capture_id"] == capture_id
        replay = await client.post("/v1/intents", json=inputs)
        assert replay.json() == restored.json()
        assert {delivery.url for delivery in restart.state.store.due()} == {new_callback_url}
        refund = {
            "refund_id": str(uuid4()),
            "amount_twd": 53,
            "capture_id": capture_id,
            "currency": "TWD",
        }
        first, retry = await asyncio.gather(
            *(client.post("/v1/refunds", json=refund) for _ in range(2))
        )
        assert first.status_code == retry.status_code == 200
        assert first.json() == retry.json()
        duplicate = await client.post("/v1/refunds", json={**refund, "refund_id": str(uuid4())})
        assert duplicate.status_code == 409


async def test_callback_backoff_resigns_and_duplicates(monkeypatch):
    monkeypatch.setenv("PAYMOCK_DELAY_CALLBACK_S", "2")
    from paymock import delivery, store

    clock = datetime.now(UTC)
    expires = (clock + timedelta(minutes=10)).isoformat()
    stamp = clock.timestamp()
    monkeypatch.setattr(store, "time", type("Clock", (), {"time": staticmethod(lambda: stamp)}))
    monkeypatch.setattr(delivery, "time", store.time)
    app = create_app()
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://paymock",
            headers={TOKEN_HEADER: TOKEN},
        ) as client,
    ):
        intent = await client.post(
            "/v1/intents",
            json={
                "merchant_payment_id": str(uuid4()),
                "amount_twd": 100,
                "currency": "TWD",
                "expires_at": expires,
            },
        )
        result = await client.post(f"/pay/{intent.json()['intent_id']}/confirm")
        assert result.status_code == 200
        stored: Store = app.state.store
        assert stored.due() == []
        with closing(sqlite3.connect(stored.path)) as conn, conn:
            conn.execute("UPDATE deliveries SET url='http://legacy.invalid/callback'")

        received = []

        async def receiving(request):
            assert str(request.url) == CALLBACK_URL
            raw = await request.aread()
            received.append((request.headers["Mockpay-Signature"], raw))
            return httpx.Response(503 if len(received) == 1 else 200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(receiving)) as target:
            stamp += 2
            await send_due(target, stored, SECRET, duplicate=True)
            first_stamp = int(stamp)
            stamp += 2
            await send_due(target, stored, SECRET, duplicate=True)
        assert len(received) == 3
        assert received[0][1] == received[1][1] == received[2][1]
        assert received[0][0] == signature(SECRET, received[0][1], first_stamp)
        assert received[1][0] == signature(SECRET, received[1][1], int(stamp))
        assert orjson.loads(received[0][1])["event_id"] == result.json()["event"]["event_id"]
        verify_signature(received[1][1], received[1][0], SECRET)
        assert stored.due() == []


@pytest.mark.parametrize("token", [None, "wrong-token", SECRET])
async def test_merchant_routes_require_service_token(token):
    app = create_app()
    merchant = uuid4()
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://paymock",
        ) as client,
    ):
        for method, path in (
            ("POST", "/v1/intents"),
            ("GET", f"/v1/intents/{merchant}"),
            ("POST", f"/v1/intents/{merchant}/cancel"),
            ("POST", "/v1/refunds"),
        ):
            headers = {TOKEN_HEADER: token} if token is not None else {}
            response = await client.request(method, path, headers=headers)
            assert response.status_code == 401
            assert response.headers["content-type"] == "application/problem+json"
            assert response.json()["code"] == "invalid-service-token"
        assert (await client.get("/healthz/live")).status_code == 200
        assert (await client.get(f"/pay/{merchant}")).status_code == 404


async def test_request_cannot_choose_callback_url():
    app = create_app()
    payload = {
        "merchant_payment_id": str(uuid4()),
        "amount_twd": 53,
        "currency": "TWD",
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "callback_url": "http://caller.invalid/callback",
    }
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://paymock",
            headers={TOKEN_HEADER: TOKEN},
        ) as client,
    ):
        response = await client.post("/v1/intents", json=payload)
        assert response.status_code == 422
        assert response.json()["code"] == "validation-failed"
        assert (
            await client.get(f"/v1/intents/{payload['merchant_payment_id']}")
        ).status_code == 404


@pytest.mark.parametrize("token", ["dev-provider-token-change-me", SECRET, ""])
async def test_simulator_rejects_unsafe_production_tokens(monkeypatch, token):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("PAYMENT_PROVIDER_TOKEN", token)
    with pytest.raises(RuntimeError, match="PAYMENT_PROVIDER_TOKEN"):
        async with LifespanManager(create_app()):
            pytest.fail("Unsafe simulator configuration was accepted")
