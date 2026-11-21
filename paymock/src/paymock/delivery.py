"""Retry callback delivery with the same event ID and a fresh signature."""

import asyncio
import hashlib
import hmac
import sqlite3
import time

import httpx

from paymock.logging import ServiceLog
from paymock.store import Delivery, Store


def signature(secret: str, body: bytes, stamp: int) -> str:
    tag = hmac.new(secret.encode(), str(stamp).encode() + b"." + body, hashlib.sha256)
    return f"t={stamp},v1={tag.hexdigest()}"


async def deliver(
    http: httpx.AsyncClient, delivery: Delivery, secret: str, service_log: ServiceLog | None
) -> bool:
    if time.time() >= delivery.deadline:
        return True
    headers = {
        "Content-Type": "application/json",
        "Mockpay-Signature": signature(secret, delivery.body, int(time.time())),
    }
    if delivery.request_id:
        headers["X-Request-ID"] = delivery.request_id
    status = None
    error = None
    try:
        async with asyncio.timeout(5):
            response = await http.post(delivery.url, content=delivery.body, headers=headers)
        status = response.status_code
        ok = response.is_success
    except (httpx.HTTPError, TimeoutError) as exc:
        error = type(exc).__name__
        ok = False
    if service_log is not None:
        service_log.write(
            "callback.delivery",
            level="info" if ok else "warning",
            request_id=delivery.request_id,
            event_id=delivery.event_id,
            status=status,
            error=error,
        )
    return ok


async def send_due(
    http: httpx.AsyncClient,
    store: Store,
    secret: str,
    duplicate: bool,
    service_log: ServiceLog | None = None,
) -> None:
    async def send(delivery: Delivery) -> None:
        ok = await deliver(http, delivery, secret, service_log)
        if ok and duplicate:
            await deliver(http, delivery, secret, service_log)
        await asyncio.to_thread(store.mark_delivery, delivery, ok)

    await asyncio.gather(*(send(delivery) for delivery in await asyncio.to_thread(store.due)))


async def run(store: Store, secret: str, duplicate: bool, service_log: ServiceLog) -> None:
    async with httpx.AsyncClient(trust_env=False) as http:
        while True:
            try:
                await send_due(http, store, secret, duplicate, service_log)
            except sqlite3.Error as exc:
                service_log.write(
                    "callback.store_unavailable", level="warning", error=type(exc).__name__
                )
            await asyncio.sleep(0.25)
