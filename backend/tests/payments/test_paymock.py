import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import orjson
from asgi_lifespan import LifespanManager

from paymock.delivery import send_due, signature
from paymock.main import create_app
from paymock.store import Store
from ticketing.payments.callbacks import verify_signature

SECRET = "simulator-test-secret"


async def test_simulator_capture_refund_expiry_and_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYMOCK_STORE_PATH", str(tmp_path / "mock.sqlite3"))
    monkeypatch.setenv("PAYMENT_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("PAYMOCK_DROP_CALLBACKS", "1")
    app = create_app()
    inputs = {
        "merchant_payment_id": str(uuid4()),
        "amount_twd": 53,
        "currency": "TWD",
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "callback_url": "http://api/api/v1/payments/callbacks",
    }
    async with (
        LifespanManager(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://paymock",
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

    restart = create_app()
    async with (
        LifespanManager(restart),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restart),
            base_url="http://paymock",
        ) as client,
    ):
        restored = await client.get(f"/v1/intents/{inputs['merchant_payment_id']}")
        assert restored.json()["event"]["capture_id"] == capture_id
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


async def test_callback_backoff_resigns_and_duplicates(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYMOCK_STORE_PATH", str(tmp_path / "mock.sqlite3"))
    monkeypatch.setenv("PAYMENT_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("PAYMOCK_DROP_CALLBACKS", "1")
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
        ) as client,
    ):
        intent = await client.post(
            "/v1/intents",
            json={
                "merchant_payment_id": str(uuid4()),
                "amount_twd": 100,
                "currency": "TWD",
                "expires_at": expires,
                "callback_url": "http://api/callback",
            },
        )
        result = await client.post(f"/pay/{intent.json()['intent_id']}/confirm")
        assert result.status_code == 200
        stored: Store = app.state.store
        assert stored.due() == []
        received = []

        async def receiving(request):
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
