"""Payment attempts and the event format shared with the simulator."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

_EXAMPLE_ID = "58d20683-a297-4ca4-a009-42452bbfb212"
AttemptStatus = Literal["creating", "awaiting_customer", "failed", "captured", "abandoned"]
EventType = Literal["captured", "declined", "cancelled", "expired"]


class Checkout(BaseModel):
    attempt_id: UUID = Field(description="Merchant payment attempt ID.", examples=[_EXAMPLE_ID])
    checkout_url: str = Field(
        description="Payment simulator checkout URL.",
        examples=[f"http://localhost:8080/pay/{_EXAMPLE_ID}"],
    )
    expires_at: datetime = Field(
        description="UTC checkout deadline.", examples=["2026-12-01T12:10:00Z"]
    )


class PaidPayment(BaseModel):
    order_id: UUID = Field(description="Public ID of the paid order.", examples=[_EXAMPLE_ID])
    status: Literal["paid"] = Field(description="Current order status.", examples=["paid"])
    paid_at: datetime = Field(
        description="When the merchant accepted payment in UTC.",
        examples=["2026-12-01T12:08:01Z"],
    )


class PaymentAttempt(BaseModel):
    attempt_id: UUID = Field(description="Merchant payment attempt ID.", examples=[_EXAMPLE_ID])
    status: AttemptStatus = Field(
        description="Recorded payment state.", examples=["awaiting_customer"]
    )
    amount_twd: int = Field(description="Order total in whole TWD.", examples=[2951])
    checkout_url: str | None = Field(
        description="Checkout URL when one was issued.",
        examples=[f"http://localhost:8080/pay/{_EXAMPLE_ID}"],
    )
    expires_at: datetime = Field(
        description="UTC checkout deadline.", examples=["2026-12-01T12:10:00Z"]
    )


class PaymentEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9:_-]+$",
        description="Provider event ID.",
        examples=[_EXAMPLE_ID],
    )
    event_type: EventType = Field(description="Provider outcome.", examples=["captured"])
    merchant_payment_id: UUID = Field(
        description="Merchant payment attempt ID.", examples=[_EXAMPLE_ID]
    )
    amount_twd: int = Field(
        ge=0, le=42_000_000, description="Payment amount in TWD.", examples=[2951]
    )
    currency: Literal["TWD"] = Field(description="Payment currency.", examples=["TWD"])
    capture_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9:_-]+$",
        description="Stable provider capture ID.",
        examples=[_EXAMPLE_ID],
    )
    captured_at: AwareDatetime | None = Field(
        default=None,
        description="Provider capture timestamp in UTC.",
        examples=["2026-12-01T12:08:00Z"],
    )

    @model_validator(mode="after")
    def capture_fields(self) -> "PaymentEvent":
        capture = self.capture_id is not None and self.captured_at is not None
        any_capture_field = self.capture_id is not None or self.captured_at is not None
        if (self.event_type == "captured" and not capture) or (
            self.event_type != "captured" and any_capture_field
        ):
            raise ValueError("Capture fields are required only for captured events.")
        return self


class CallbackResult(BaseModel):
    disposition: Literal["accepted", "refund_required", "acknowledged"] = Field(
        description="Recorded treatment of the event.", examples=["accepted"]
    )
