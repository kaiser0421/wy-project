"""Wire names and clock helpers shared across telemetry components."""

from __future__ import annotations

import re
from datetime import UTC, datetime

STREAM = "telemetry:app"
GROUP = "telemetry-collector"
ALERT_CHANNEL = "admin:alerts"
SSE_PATH = re.compile(
    r"/api/v1/(?:events/[^/]+/seat-stream|seat-imports/[^/]+/stream|"
    r"admin/(?:metrics|alerts)/stream)/?\Z"
)


def is_stream(path: str) -> bool:
    return SSE_PATH.fullmatch(path) is not None


def second(ts: datetime) -> datetime:
    return ts.astimezone(UTC).replace(microsecond=0)
