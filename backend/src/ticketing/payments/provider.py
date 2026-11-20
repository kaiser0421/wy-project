"""Validate provider responses before applying them to merchant state."""

import asyncio
from datetime import datetime
from typing import Literal
from uuid import UUID

import httpx
from pydantic import AwareDatetime, BaseModel, ValidationError

from ticketing.core.settings import Settings
from ticketing.payments.models import PaymentEvent


class ProviderUnavailable(Exception):
    """The provider outcome is unknown. Recovery must reuse the merchant identifier."""


class Intent(BaseModel):
    intent_id: UUID | None
    merchant_payment_id: UUID
    amount_twd: int
    currency: Literal["TWD"]
    expires_at: AwareDatetime
    status: Literal["open", "captured", "declined", "cancelled", "expired"]
    event: PaymentEvent | None = None


class RefundReceipt(BaseModel):
    refund_id: UUID
    capture_id: str
    amount_twd: int
    currency: Literal["TWD"]
    status: Literal["succeeded"]


class Provider:
    def __init__(self, http: httpx.AsyncClient, settings: Settings) -> None:
        self.http = http
        self.settings = settings

    async def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        try:
            # A total deadline also bounds slow streaming reads, which httpx times individually.
            async with asyncio.timeout(5):
                response = await self.http.request(method, path, json=kwargs or None)
            response.raise_for_status()
        except (httpx.HTTPError, TimeoutError) as exc:
            raise ProviderUnavailable from exc
        return response

    @staticmethod
    def _intent(response: httpx.Response, merchant_id: UUID) -> Intent:
        try:
            intent = Intent.model_validate_json(response.content)
        except ValidationError as exc:
            raise ProviderUnavailable from exc
        event = intent.event
        if (
            intent.merchant_payment_id != merchant_id
            or (intent.status == "open" and (event is not None or intent.intent_id is None))
            or (
                intent.status != "open"
                and (
                    event is None
                    or event.event_type != intent.status
                    or event.merchant_payment_id != merchant_id
                    or event.amount_twd != intent.amount_twd
                )
            )
            or (intent.status == "captured" and intent.intent_id is None)
        ):
            raise ProviderUnavailable
        return intent

    async def create(self, merchant_id: UUID, amount: int, expires_at: datetime) -> Intent:
        try:
            response = await self._request(
                "POST",
                "/v1/intents",
                merchant_payment_id=str(merchant_id),
                amount_twd=amount,
                currency="TWD",
                expires_at=expires_at.isoformat(),
                callback_url=self.settings.payment_callback_url,
            )
        except ProviderUnavailable as exc:
            cause = exc.__cause__
            if isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code == 402:
                # A definitive create rejection can safely free the chargeable slot.
                return Intent(
                    intent_id=None,
                    merchant_payment_id=merchant_id,
                    amount_twd=amount,
                    currency="TWD",
                    expires_at=expires_at,
                    status="declined",
                    event=PaymentEvent(
                        event_id=f"create-declined:{merchant_id}",
                        event_type="declined",
                        merchant_payment_id=merchant_id,
                        amount_twd=amount,
                        currency="TWD",
                    ),
                )
            raise
        return self._intent(response, merchant_id)

    async def poll(self, merchant_id: UUID) -> Intent:
        return self._intent(await self._request("GET", f"/v1/intents/{merchant_id}"), merchant_id)

    async def refund(self, refund_id: UUID, capture_id: str, amount: int) -> None:
        response = await self._request(
            "POST",
            "/v1/refunds",
            refund_id=str(refund_id),
            capture_id=capture_id,
            amount_twd=amount,
            currency="TWD",
        )
        try:
            receipt = RefundReceipt.model_validate_json(response.content)
        except ValidationError as exc:
            raise ProviderUnavailable from exc
        if (receipt.refund_id, receipt.capture_id, receipt.amount_twd) != (
            refund_id,
            capture_id,
            amount,
        ):
            raise ProviderUnavailable

    async def cancel(self, merchant_id: UUID) -> Intent | None:
        result = await self._request("POST", f"/v1/intents/{merchant_id}/cancel")
        if result.content.strip() == b"null":
            return None
        intent = self._intent(result, merchant_id)
        if intent.event is None:
            raise ProviderUnavailable
        return intent
