"""ASGI SSE response with one cancellable write and a shared ring cursor."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from uuid import UUID

from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from ticketing.core.errors import ProblemError
from ticketing.core.logging import get_logger
from ticketing.realtime.client_model import SequenceGap
from ticketing.realtime.frames import encode
from ticketing.realtime.reader import EventReader, Readers
from ticketing.realtime.revocation import Revocations

log = get_logger(__name__)

WRITE_TIMEOUT_S = 5.0
TERMINAL_WRITE_TIMEOUT_S = 0.1
HEARTBEAT_S = 15.0
REPLAY_TIMEOUT_S = 5.0
_HEARTBEAT = b": heartbeat\n\n"


class WriteTimeout(TimeoutError):
    """The transport did not accept the frame before the write deadline."""


async def _write(send: Send, data: bytes, *, more: bool = True) -> None:
    try:
        async with asyncio.timeout(WRITE_TIMEOUT_S):
            await send({"type": "http.response.body", "body": data, "more_body": more})
    except TimeoutError as exc:
        raise WriteTimeout from exc


async def _disconnect(receive: Receive) -> None:
    while (await receive())["type"] != "http.disconnect":
        pass


async def _catch_up(reader: EventReader, cursor: int, limit: int, send: Send) -> int:
    """On a ring miss, replay through the captured head before trying live delivery again."""
    while cursor < limit:
        if reader.broken:
            raise SequenceGap("Reader lost sequence continuity")
        frame = reader.ring.after(cursor)
        if frame is None or frame.end > limit:
            break
        await _write(send, frame.body)
        cursor = frame.end

    if cursor >= limit:
        return cursor
    async with asyncio.timeout(REPLAY_TIMEOUT_S):
        while cursor < limit:
            if reader.broken:
                raise SequenceGap("Reader lost sequence continuity")
            frames = await reader.replays.read(reader.event_id, cursor, limit)
            for frame in frames:
                await _write(send, frame.body)
                cursor = frame.end
    return cursor


async def _stream(reader: EventReader, resume: int | None, send: Send) -> None:
    async with asyncio.timeout(REPLAY_TIMEOUT_S):
        await reader.ready.wait()
    live_head = reader.head
    if live_head is None or reader.broken:
        raise SequenceGap("Reader is unavailable")
    cursor = live_head if resume is None else resume
    if cursor > live_head and cursor > await reader.assigned_head():
        raise SequenceGap("Resume sequence is beyond the assigned head")
    await _write(send, b": connected\n\n")
    cursor = await _catch_up(reader, cursor, live_head, send)

    loop = asyncio.get_running_loop()
    heartbeat_at = loop.time() + HEARTBEAT_S
    previous_progress: bytes | None = None
    while True:
        changed = reader.changed
        if reader.broken:
            raise SequenceGap("Reader lost sequence continuity")
        live_head = reader.head
        assert live_head is not None
        if cursor < live_head:
            cursor = await _catch_up(reader, cursor, live_head, send)
        progress = reader.progress
        if progress is not None and progress is not previous_progress:
            await _write(send, progress)
            previous_progress = progress
        now = loop.time()
        if now >= heartbeat_at:
            await _write(send, _HEARTBEAT)
            heartbeat_at = now + HEARTBEAT_S
        # The reader may advance and evict frames while replay or a write is pending.
        if changed.is_set():
            continue
        with suppress(TimeoutError):
            async with asyncio.timeout_at(heartbeat_at):
                await changed.wait()


class SeatStream(Response):
    media_type = "text/event-stream"

    def __init__(
        self,
        readers: Readers,
        revocations: Revocations,
        event_id: int,
        session_id: UUID,
        resume: int | None,
    ) -> None:
        super().__init__(
            headers={"Cache-Control": "no-cache, no-store, no-transform", "X-Accel-Buffering": "no"}
        )
        del self.headers["content-length"]
        self.readers = readers
        self.revocations = revocations
        self.event_id = event_id
        self.session_id = session_id
        self.resume = resume

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Admission must succeed before sending SSE headers so capacity errors remain HTTP 503s.
        with self.readers.subscribe(self.event_id) as reader:
            await self._serve(reader, receive, send)

    async def _serve(self, reader: EventReader, receive: Receive, send: Send) -> None:
        started = False

        async def run() -> None:
            nonlocal started
            async with asyncio.timeout(WRITE_TIMEOUT_S):
                await send(
                    {
                        "type": "http.response.start",
                        "status": self.status_code,
                        "headers": self.raw_headers,
                    }
                )
            started = True
            await _stream(reader, self.resume, send)

        writer = asyncio.create_task(run())
        disconnect = asyncio.create_task(_disconnect(receive))
        terminal = b""
        with self.revocations.watch(self.session_id, writer):
            try:
                await asyncio.wait((writer, disconnect), return_when=asyncio.FIRST_COMPLETED)
                if disconnect.done():
                    return
                if not writer.cancelled():
                    writer.result()
            except WriteTimeout:
                terminal = encode("resync", {"reason": "write-timeout"})
            except (SequenceGap, TimeoutError, ProblemError):
                terminal = encode("resync", {"reason": "replay-unavailable"})
            except OSError:
                return
            except Exception as exc:
                log.warning("seat_stream.closed", error=type(exc).__name__)
                terminal = encode("resync", {"reason": "reader-unavailable"})
            finally:
                writer.cancel()
                disconnect.cancel()
                await asyncio.gather(writer, disconnect, return_exceptions=True)
            if started:
                # A stalled transport may reject even this last frame. Cancel and close promptly.
                with suppress(TimeoutError, OSError):
                    async with asyncio.timeout(TERMINAL_WRITE_TIMEOUT_S):
                        await send(
                            {"type": "http.response.body", "body": terminal, "more_body": False}
                        )
