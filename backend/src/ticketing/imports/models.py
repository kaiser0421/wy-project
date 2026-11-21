"""Seat import API schemas."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

Mode = Literal["all_or_nothing", "skip_invalid"]
Format = Literal["csv", "json"]
Status = Literal[
    "queued",
    "running",
    "succeeded",
    "partially_succeeded",
    "validation_failed",
    "cancelled",
    "system_failed",
]


class JobOut(BaseModel):
    id: UUID = Field(
        description="Seat import job ID", examples=["ddfb26f6-fe06-4aae-b557-6c268323f99a"]
    )
    event_id: int = Field(description="Parent event ID", examples=[42])
    mode: Mode = Field(
        description="Validation policy chosen for the first upload", examples=["all_or_nothing"]
    )
    format: Format = Field(description="Input file format", examples=["csv"])
    status: Status = Field(description="Current job state", examples=["running"])
    processed_rows: int = Field(description="Rows staged across attempts", examples=[10000])
    total_rows: int | None = Field(description="Input row count once known", examples=[12000])
    valid_rows: int | None = Field(description="Validated rows without errors", examples=[11999])
    error_rows: int | None = Field(description="Validated rows with errors", examples=[1])
    progress_percent: int = Field(
        description="Parsing is estimated up to 97, validation reports 98, writing reports 99, "
        "and terminal jobs report 100.",
        examples=[95],
    )
    failure_reason: str | None = Field(
        description="Machine-readable reason for terminal failure", examples=["too_many_rows"]
    )
