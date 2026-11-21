"""Write paymock events in the collector's service log format."""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import TextIO
from uuid import uuid4

import orjson


class ServiceLog:
    def __init__(self, boot_uuid: str, file: TextIO | None) -> None:
        self.boot_uuid = boot_uuid
        self.file = file
        self.sequence = count(1)

    def write(
        self, event: str, *, level: str = "info", request_id: str | None = None, **attrs: object
    ) -> None:
        line = orjson.dumps(
            {
                "ts": datetime.now(UTC).isoformat(),
                "service": "paymock",
                "level": level,
                "event": event,
                "boot_uuid": self.boot_uuid,
                "local_seq": next(self.sequence),
                "request_id": request_id,
                "attrs": attrs,
            }
        ).decode()
        if self.file is not None:
            self.file.write(line + "\n")
        print(line, flush=True)


@contextmanager
def open_service_log(log_dir: str | None) -> Iterator[ServiceLog]:
    boot_uuid = uuid4().hex
    output: AbstractContextManager[TextIO | None]
    if log_dir:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"paymock-{boot_uuid}.json"
        output = path.open("a", buffering=1, encoding="utf-8")
    else:
        output = nullcontext()
    with output as file:
        yield ServiceLog(boot_uuid, file)
