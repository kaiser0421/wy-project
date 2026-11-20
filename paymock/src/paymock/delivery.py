"""Retry callback delivery with the same event ID and a fresh signature."""

import asyncio
import hashlib
import hmac
import logging
import sqlite3
import time

import httpx

from paymock.store import Delivery, Store

log = logging.getLogger(__name__)


def signature(secret: str, body: bytes, stamp: int) -> str:
    tag = hmac.new(secret.encode(), str(stamp).encode() + b"." + body, hashlib.sha256)
    return f"t={stamp},v1={tag.hexdigest()}"


async def deliver(http: httpx.AsyncClient, delivery: Delivery, secret: str) -> bool:
    if time.time() >= delivery.deadline:
        return True
    headers = {
        "Content-Type": "application/json",
        "Mockpay-Signature": signature(secret, delivery.body, int(time.time())),
    }
    if delivery.request_id:
        headers["X-Request-ID"] = delivery.request_id
    try:
        async with asyncio.timeout(5):
            response = await http.post(delivery.url, content=delivery.body, headers=headers)
    except (httpx.HTTPError, TimeoutError):
        return False
    return response.is_success


async def send_due(http: httpx.AsyncClient, store: Store, secret: str, duplicate: bool) -> None:
    async def send(delivery: Delivery) -> None:
        ok = await deliver(http, delivery, secret)
        if ok and duplicate:
            await deliver(http, delivery, secret)
        await asyncio.to_thread(store.mark_delivery, delivery, ok)

    await asyncio.gather(*(send(delivery) for delivery in await asyncio.to_thread(store.due)))


async def run(store: Store, secret: str, duplicate: bool) -> None:
    async with httpx.AsyncClient(trust_env=False) as http:
        while True:
            try:
                await send_due(http, store, secret, duplicate)
            except sqlite3.Error as exc:
                log.warning("Callback store unavailable (%s)", type(exc).__name__)
            await asyncio.sleep(0.25)
