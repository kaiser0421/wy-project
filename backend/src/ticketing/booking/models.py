"""Public booking request and response models."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, Strict, field_validator

BIGINT_MAX = 2**63 - 1
SeatId = Annotated[int, Strict(), Field(gt=0, le=BIGINT_MAX)]
OrderStatus = Literal[
    "pending_payment", "paid", "expired", "cancelled", "refund_pending", "refunded"
]


class SpecificSeats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seat_ids: list[SeatId] = Field(
        min_length=1,
        max_length=4,
        description="Distinct seat IDs from this event.",
        examples=[[12, 13]],
    )

    @field_validator("seat_ids")
    @classmethod
    def distinct(cls, ids: list[int]) -> list[int]:
        if len(set(ids)) != len(ids):
            raise ValueError("Seat IDs must be distinct.")
        return sorted(ids)


class ZoneSeats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    zone_code: str = Field(
        pattern=r"^[A-Z0-9_-]{1,16}$", description="Published zone code.", examples=["A"]
    )
    quantity: int = Field(
        strict=True, ge=1, le=4, description="Number of seats to allocate.", examples=[2]
    )


class OrderItem(BaseModel):
    seat_id: int = Field(description="Seat ID within the event.", examples=[12])
    zone_code: str = Field(description="Zone code.", examples=["A"])
    row_label: str = Field(description="Displayed row label.", examples=["1"])
    seat_number: str = Field(description="Displayed seat number.", examples=["12"])
    unit_price_twd: int = Field(description="Unit price at booking, in whole TWD.", examples=[2810])


class Order(BaseModel):
    public_id: UUID = Field(
        description="Public order ID.", examples=["58d20683-a297-4ca4-a009-42452bbfb212"]
    )
    user_id: int = Field(description="ID of the order owner.", examples=[42])
    event_id: int = Field(description="ID of the booked event.", examples=[25])
    status: OrderStatus = Field(description="Current order state.", examples=["pending_payment"])
    items: list[OrderItem] = Field(
        description="Seats selected at booking, including released seats.",
        examples=[
            [
                {
                    "seat_id": 12,
                    "zone_code": "A",
                    "row_label": "1",
                    "seat_number": "12",
                    "unit_price_twd": 2810,
                }
            ]
        ],
    )
    subtotal_twd: int = Field(description="Sum of the item prices in whole TWD.", examples=[2810])
    fee_twd: int = Field(
        description="Five percent of the subtotal, rounded half up to whole TWD.", examples=[141]
    )
    total_twd: int = Field(
        description="Amount due, including the service fee, in whole TWD.", examples=[2951]
    )
    expires_at: datetime = Field(
        description="UTC payment deadline.", examples=["2026-12-01T12:10:00Z"]
    )
    refund_status: Literal["none", "pending", "succeeded"] = Field(
        description="Refund status for an accepted payment cancelled by the customer.",
        examples=["none"],
    )


class OrderPage(BaseModel):
    items: list[Order] = Field(description="Orders newest first by internal ID.", examples=[[]])
    next_cursor: str | None = Field(
        description="Cursor for the next page, or null at the end.", examples=["108"]
    )
