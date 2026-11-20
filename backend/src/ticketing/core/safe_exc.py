"""Exception summaries that are safe to persist in logs.

Exception messages and local variables may contain user data or secrets, so persistent logs only
get the exception class and the stack locations (file, line, function).
"""

from __future__ import annotations

import traceback
from typing import Any


def exception_summary(exc: BaseException, limit: int = 20) -> dict[str, Any]:
    frames = traceback.extract_tb(exc.__traceback__, limit=limit)
    return {
        "error_type": type(exc).__name__,
        "stack": [f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno}:{f.name}" for f in frames],
    }
