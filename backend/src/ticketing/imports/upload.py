"""Read multipart data directly from the ASGI stream and durably store only the file part."""

import hashlib
import os
from collections.abc import AsyncIterable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast
from uuid import uuid4

from anyio import to_thread
from streaming_form_data import StreamingFormDataParser  # type: ignore[import-untyped]
from streaming_form_data.parser import (  # type: ignore[import-untyped]
    ParseFailedException,
    parse_content_boundary,
)
from streaming_form_data.targets import BaseTarget  # type: ignore[import-untyped]

from ticketing.core.errors import ProblemError
from ticketing.imports.models import Format, Mode

_MAX_FILE_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Upload:
    path: Path
    digest: bytes
    size: int
    format: Format
    mode: Mode


def _malformed() -> ProblemError:
    return ProblemError(400, "invalid-multipart", "Send one file part and an optional mode part")


def _too_large() -> ProblemError:
    return ProblemError(413, "payload-too-large", "Seat import exceeds the upload size limit")


class _Part(BaseTarget):  # type: ignore[misc]
    def __init__(self) -> None:
        super().__init__()
        self.seen = False
        self.finished = False

    def on_start(self) -> None:
        if self.seen:
            raise _malformed()
        self.seen = True

    def on_finish(self) -> None:
        self.finished = True


class _File(_Part):
    def __init__(self, output: BinaryIO, max_bytes: int) -> None:
        super().__init__()
        self.output = output
        self.limit = max_bytes
        self.digest = hashlib.sha256()
        self.size = 0
        self.format: Format | None = None

    def on_start(self) -> None:
        super().on_start()
        filename = self.multipart_filename
        ext = filename.rsplit(".", 1)[-1].lower() if filename and "." in filename else ""
        if ext not in ("csv", "json"):
            raise ProblemError(415, "unsupported-media-type", "Upload a .csv or .json file")
        self.format = cast(Format, ext)

    def on_data_received(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if self.size > self.limit:
            raise _too_large()
        self.digest.update(chunk)
        self.output.write(chunk)


class _Mode(_Part):
    def __init__(self) -> None:
        super().__init__()
        self.data = bytearray()

    def on_data_received(self, chunk: bytes) -> None:
        if len(self.data) + len(chunk) > len("all_or_nothing"):
            raise ProblemError(422, "validation-failed", "Invalid seat import mode")
        self.data.extend(chunk)

    def value(self) -> Mode:
        if not self.seen or self.data == b"all_or_nothing":
            return "all_or_nothing"
        if self.data == b"skip_invalid":
            return "skip_invalid"
        raise ProblemError(422, "validation-failed", "Invalid seat import mode")


def _persist(output: BinaryIO, temporary: Path, final: Path) -> None:
    output.flush()
    os.fsync(output.fileno())
    temporary.replace(final)
    directory = os.open(final.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


async def store_upload(
    stream: AsyncIterable[bytes], content_type: str, folder: str, max_bytes: int
) -> Upload:
    if content_type.partition(";")[0].strip().lower() != "multipart/form-data":
        raise ProblemError(415, "unsupported-media-type", "Use multipart/form-data")
    max_bytes = min(max_bytes, _MAX_FILE_BYTES)
    directory = await to_thread.run_sync(Path(folder).resolve)
    await to_thread.run_sync(lambda: directory.mkdir(mode=0o750, parents=True, exist_ok=True))
    key = uuid4().hex
    temporary = directory / f".{key}.part"
    final = directory / f"{key}.upload"
    saved = False
    try:
        with os.fdopen(
            os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
        ) as output:
            headers = {"Content-Type": content_type}
            boundary = parse_content_boundary(headers)
            if not boundary.isascii() or len(boundary) > 70:
                raise _malformed()
            end = b"\r\n--" + boundary + b"--"
            parser = StreamingFormDataParser(headers, strict=True)
            file, mode = _File(output, max_bytes), _Mode()
            parser.register("file", file)
            parser.register("mode", mode)
            received = 0
            tail = b""
            closing_boundary = False
            async for chunk in stream:
                for offset in range(0, len(chunk), 64 * 1024):
                    block = chunk[offset : offset + 64 * 1024]
                    received += len(block)
                    if received > max_bytes + 16 * 1024:
                        raise _too_large()
                    await to_thread.run_sync(parser.data_received, block)
                    if received - file.size > 16 * 1024:
                        raise _too_large()
                    if not closing_boundary:
                        combined = tail + block
                        closing_boundary = end in combined
                        tail = combined[-len(end) :]
            if (
                not closing_boundary
                or not file.finished
                or not file.format
                or (mode.seen and not mode.finished)
            ):
                raise _malformed()
            result = Upload(final, file.digest.digest(), file.size, file.format, mode.value())
            await to_thread.run_sync(_persist, output, temporary, final)
        saved = True
        return result
    except (ParseFailedException, UnicodeError) as exc:
        raise _malformed() from exc
    finally:
        if not saved:
            temporary.unlink(missing_ok=True)
            final.unlink(missing_ok=True)
