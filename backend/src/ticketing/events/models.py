"""Schemas for event writes and snapshot reads."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_serializer

EventStatus = Literal["draft", "published"]
Title = Annotated[
    str, Field(min_length=1, max_length=200, description="Event title", examples=["Summer Live"])
]
Description = Annotated[
    str, Field(max_length=10000, description="Event description", examples=["Outdoor concert"])
]
Category = Annotated[
    str, Field(min_length=1, max_length=50, description="Event category", examples=["music"])
]
Venue = Annotated[
    str, Field(min_length=1, max_length=200, description="Venue name", examples=["Taipei Arena"])
]
SalesStart = Annotated[
    AwareDatetime, Field(description="Sales open time", examples=["2030-06-01T02:00:00Z"])
]
SalesEnd = Annotated[
    AwareDatetime, Field(description="Sales close time", examples=["2030-06-02T02:00:00Z"])
]


class ZoneInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(
        pattern=r"^[A-Z0-9_-]{1,16}$",
        description="Unique zone code within the event",
        examples=["A"],
    )
    name: str = Field(
        min_length=1, max_length=100, description="Zone display name", examples=["Floor"]
    )
    price_twd: int = Field(
        ge=0, le=10_000_000, strict=True, description="Whole TWD per ticket", examples=[1200]
    )


Zones = Annotated[
    list[ZoneInput],
    Field(
        description="Complete zone configuration",
        examples=[[{"code": "A", "name": "Floor", "price_twd": 1200}]],
    ),
]


class EventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Title
    description: Description
    category: Category
    venue: Venue
    sales_start_at: SalesStart
    sales_end_at: SalesEnd
    zones: Zones


class EventPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Title | None = Field(default=None, description="Replacement title", examples=["Live"])
    description: Description | None = Field(
        default=None, description="Replacement description", examples=["Outdoor concert"]
    )
    category: Category | None = Field(
        default=None, description="Replacement category", examples=["music"]
    )
    venue: Venue | None = Field(
        default=None, description="Replacement venue", examples=["Taipei Arena"]
    )
    sales_start_at: SalesStart | None = Field(
        default=None,
        description="Replacement draft sales open time",
        examples=["2030-06-01T02:00:00Z"],
    )
    sales_end_at: SalesEnd | None = Field(
        default=None,
        description="Replacement draft sales close time",
        examples=["2030-06-02T02:00:00Z"],
    )
    zones: Zones | None = Field(
        default=None,
        description="Replacement draft zones. Zones that contain seats must be retained.",
        examples=[[{"code": "A", "name": "Floor", "price_twd": 1200}]],
    )


class EventOut(EventCreate):
    model_config = ConfigDict(extra="ignore")

    id: int = Field(description="Event ID", examples=[42])
    owner_id: int = Field(description="Event owner's user ID", examples=[8])
    status: EventStatus = Field(description="Publication state", examples=["draft"])
    version: int = Field(description="Version used in the strong ETag", examples=[1])
    published_at: datetime | None = Field(description="First publication time", examples=[None])
    created_at: datetime = Field(description="Creation time", examples=["2030-05-01T02:00:00Z"])
    updated_at: datetime = Field(
        description="Last event or zone edit", examples=["2030-05-01T02:00:00Z"]
    )

    @field_serializer(
        "sales_start_at",
        "sales_end_at",
        "published_at",
        "created_at",
        "updated_at",
        when_used="json",
    )
    def utc(self, value: datetime | None) -> str | None:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


class ZoneOut(ZoneInput):
    id: int = Field(description="Zone ID", examples=[12])
    available: int = Field(description="Number of currently available seats", examples=[99])


class UnavailableEvent(BaseModel):
    id: int = Field(description="ID of the deleted event", examples=[42])
    unavailable: Literal[True] = Field(description="Snapshot member was deleted", examples=[True])


class EventPage(BaseModel):
    items: list[EventOut | UnavailableEvent] = Field(
        description="Frozen membership with current event details",
        examples=[[{"id": 42, "unavailable": True}]],
    )
    next_cursor: str | None = Field(
        description="Opaque cursor for the next page, if any", examples=[None]
    )
