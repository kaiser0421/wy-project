"""Public simulator request and response models."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

ID = "58d20683-a297-4ca4-a009-42452bbfb212"
UTC = "2026-12-01T12:10:00Z"
Status = Literal["open", "captured", "declined", "cancelled", "expired"]
Outcome = Literal["captured", "declined", "cancelled", "expired"]


class IntentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    merchant_payment_id: UUID = Field(description="Stable merchant attempt ID.", examples=[ID])
    amount_twd: int = Field(
        ge=0, le=42_000_000, description="Whole TWD to charge.", examples=[2951]
    )
    currency: Literal["TWD"] = Field(description="Payment currency.", examples=["TWD"])
    expires_at: AwareDatetime = Field(
        description="Latest time when checkout can charge.", examples=[UTC]
    )


class Event(BaseModel):
    event_id: str = Field(description="Stable event ID across deliveries.", examples=[ID])
    event_type: Outcome = Field(description="Provider outcome.", examples=["captured"])
    merchant_payment_id: UUID = Field(description="Stable merchant attempt ID.", examples=[ID])
    amount_twd: int = Field(description="Whole TWD for this intent.", examples=[2951])
    currency: Literal["TWD"] = Field(
        default="TWD", description="Payment currency.", examples=["TWD"]
    )
    capture_id: str | None = Field(
        default=None, description="ID of a successful capture.", examples=[ID]
    )
    captured_at: datetime | None = Field(
        default=None, description="Time when payment was captured.", examples=[UTC]
    )


class Intent(BaseModel):
    intent_id: UUID = Field(description="ID used in the checkout path.", examples=[ID])
    merchant_payment_id: UUID = Field(description="Stable merchant attempt ID.", examples=[ID])
    amount_twd: int = Field(description="Whole TWD for this intent.", examples=[2951])
    currency: Literal["TWD"] = Field(
        default="TWD", description="Payment currency.", examples=["TWD"]
    )
    expires_at: datetime = Field(
        description="Latest time when checkout can charge.", examples=[UTC]
    )
    status: Status = Field(description="Current provider state.", examples=["open"])
    event: Event | None = Field(
        default=None, description="Recorded terminal outcome.", examples=[None]
    )


class RefundInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refund_id: UUID = Field(description="Stable merchant refund ID.", examples=[ID])
    capture_id: UUID = Field(description="Provider capture ID.", examples=[ID])
    amount_twd: int = Field(
        ge=0, le=42_000_000, description="Full capture amount in TWD.", examples=[2951]
    )
    currency: Literal["TWD"] = Field(description="Refund currency.", examples=["TWD"])


class Refund(RefundInput):
    status: Literal["succeeded"] = Field(
        default="succeeded",
        description="This full refund has been recorded.",
        examples=["succeeded"],
    )


class Health(BaseModel):
    status: Literal["ok"] = Field(default="ok", description="Process is running.", examples=["ok"])
