from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import asyncpg
import httpx
import pytest
from redis.asyncio import Redis
from starlette.types import ASGIApp, Message, Scope

from tests.support import create_user, login
from ticketing.auth.sessions import REVOKE_CHANNEL, revoke_session
from ticketing.core.service import ServiceContext
from ticketing.core.settings import Settings
from ticketing.realtime import reader, relay, revocation, stream
from ticketing.realtime.client_model import apply_delta
from ticketing.realtime.outbox import emit_layout_changed, emit_seat_changes
from ticketing.realtime.reader import EventReader
from ticketing.realtime.snapshot import SnapshotCache
from ticketing.telemetry import business
from ticketing.telemetry.app_metrics import parse_batch
from ticketing.telemetry.common import STREAM

pytestmark = pytest.mark.integration

Observe = Callable[[bytes], Awaitable[None]]
WAIT_S = 10


@pytest.fixture
def settings_overrides(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    # Test assertions synchronize on stream and recheck events. Leave room for a slow database.
    monkeypatch.setattr(revocation, "RECHECK_ACQUIRE_TIMEOUT_S", 3.0)
    monkeypatch.setattr(revocation, "RECHECK_QUERY_TIMEOUT_MS", 5000)
    monkeypatch.setattr(stream, "TERMINAL_WRITE_TIMEOUT_S", 3.0)
    return {"db_pool_acquire_timeout_s": 5.0, "db_statement_timeout_ms": 8000}


class Body(httpx.AsyncByteStream):
    def __init__(self, queue: asyncio.Queue[bytes | None], task: asyncio.Task[None]) -> None:
        self.queue = queue
        self.task = task

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while (chunk := await self.queue.get()) is not None:
            yield chunk

    async def aclose(self) -> None:
        await asyncio.gather(self.task, return_exceptions=True)


class StreamingTransport(httpx.AsyncBaseTransport):
    """Adapt GET to ASGI with a bounded pipe. HTTPX ASGITransport buffers bodies to EOF."""

    def __init__(self, app: ASGIApp, observe: Observe | None = None) -> None:
        self.app = app
        self.observe = observe

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=1)
        started: asyncio.Future[Message] = asyncio.get_running_loop().create_future()
        disconnected = asyncio.Event()

        async def receive() -> Message:
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            if message["type"] == "http.response.start":
                started.set_result(message)
            elif message["type"] == "http.response.body":
                data = message.get("body", b"")
                if data:
                    if self.observe is not None:
                        await self.observe(data)
                    await queue.put(data)
                if not message.get("more_body", False):
                    await queue.put(None)

        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": request.method,
            "headers": [(name.lower(), value) for name, value in request.headers.raw],
            "scheme": request.url.scheme,
            "path": request.url.path,
            "raw_path": request.url.raw_path.split(b"?", 1)[0],
            "query_string": request.url.query,
            "server": (request.url.host, request.url.port),
            "client": ("127.0.0.1", 1234),
            "root_path": "",
        }

        async def run() -> None:
            try:
                await self.app(scope, receive, send)
            except Exception as exc:
                if not started.done():
                    started.set_exception(exc)
                raise

        task = asyncio.create_task(run())

        class ResponseBody(Body):
            async def aclose(self) -> None:
                disconnected.set()
                # The simulated socket may be full when its client disconnects.
                self.task.cancel()
                await super().aclose()

        try:
            async with asyncio.timeout(WAIT_S):
                header = await started
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        return httpx.Response(
            header["status"], headers=header["headers"], stream=ResponseBody(queue, task)
        )


@dataclass
class Event:
    id: int
    order: int

    @property
    def seats(self) -> str:
        return f"/api/v1/events/{self.id}/seats"

    @property
    def stream(self) -> str:
        return f"/api/v1/events/{self.id}/seat-stream"


@pytest.fixture
async def event(db: asyncpg.Connection, client: httpx.AsyncClient) -> Event:
    user = await create_user(db, "owner@example.com")
    await login(client, "owner@example.com")
    event_id = await db.fetchval(
        """
        INSERT INTO events (owner_id, title, category, venue, sales_start_at, sales_end_at,
                            status, published_at)
        VALUES ($1, 'Concert', 'Music', 'Arena', now(), now() + interval '1 day',
                'published', now())
        RETURNING id
        """,
        user,
    )
    zone = await db.fetchval(
        "INSERT INTO zones (event_id, code, name, price_twd) "
        "VALUES ($1,'A','Floor',1000) RETURNING id",
        event_id,
    )
    await db.execute(
        """
        INSERT INTO seats (event_id, zone_id, row_label, seat_number, norm_key, sort_key, seat_idx)
        VALUES ($1,$2,'R','1','A:R:1',0,0), ($1,$2,'R','2','A:R:2',1,7)
        """,
        event_id,
        zone,
    )
    order = await db.fetchval(
        "INSERT INTO orders (event_id, user_id, quantity) VALUES ($1,$2,1) RETURNING id",
        event_id,
        user,
    )
    return Event(event_id, order)


async def change(db: asyncpg.Connection, event: Event, status: str, idx: int = 0) -> None:
    async with db.transaction():
        row = await db.fetchrow(
            """
            UPDATE seats SET status=$3::seat_status, order_id=$4, version=version+1
            WHERE event_id=$1 AND seat_idx=$2
            RETURNING version
            """,
            event.id,
            idx,
            status,
            None if status == "available" else event.order,
        )
        await emit_seat_changes(db, event.id, [(idx, status, row["version"])])


@dataclass
class Received:
    kind: str
    seq: int | None
    data: dict[str, Any]


async def frames(response: httpx.Response) -> AsyncIterator[Received]:
    fields: dict[str, str] = {}
    async for line in response.aiter_lines():
        if not line:
            if "event" in fields:
                yield Received(
                    fields["event"],
                    int(fields["id"]) if "id" in fields else None,
                    json.loads(fields["data"]),
                )
            fields.clear()
        elif not line.startswith(":"):
            name, _, value = line.partition(":")
            fields[name] = value.lstrip(" ")


async def receive_kind(events: AsyncIterator[Received], kind: str) -> Received:
    async with asyncio.timeout(WAIT_S):
        async for frame in events:
            if frame.kind == kind:
                return frame
            assert frame.kind == "progress", frame
    raise AssertionError("Stream closed before receiving " + kind)


@asynccontextmanager
async def open_stream(
    app: ASGIApp,
    client: httpx.AsyncClient,
    event: Event,
    resume: int | None = None,
    observe: Observe | None = None,
) -> AsyncIterator[AsyncIterator[Received]]:
    headers = {} if resume is None else {"Last-Event-ID": str(resume)}
    async with (
        httpx.AsyncClient(
            transport=StreamingTransport(app, observe),
            base_url=str(client.base_url),
            cookies=client.cookies,
        ) as streaming,
        streaming.stream("GET", event.stream, headers=headers) as response,
    ):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        yield frames(response)


async def test_snapshot_and_delta_resume_end_to_end(
    db: asyncpg.Connection,
    client: httpx.AsyncClient,
    running_app: ASGIApp,
    event: Event,
    relay_context: ServiceContext,
) -> None:
    snapshot = (await client.get(event.seats)).json()
    compact = (await client.get(event.seats, params={"format": "compact"})).json()
    assert snapshot["last_seq"] == compact["last_seq"] == 0
    assert compact["status"] == "AA"
    assert compact["versions"] == [0, 0]
    assert [seat["idx"] for seat in compact["seats"]] == [0, 7]
    assert snapshot["zones"] == [{"code": "A", "name": "Floor", "price_twd": 1000}]
    async with open_stream(running_app, client, event, snapshot["last_seq"]) as events:
        assert (await receive_kind(events, "progress")).data == {
            "head": 0,
            "oldest_unassigned_age_ms": 0,
        }
        await change(db, event, "held")
        await relay.tick(relay_context)
        held = await receive_kind(events, "delta")
        assert held.seq == held.data["from"] == held.data["to"] == 1
        assert held.data["changes"] == [[0, "held", 1]]
        assert held.data["ts"]
    await change(db, event, "available")
    await relay.tick(relay_context)
    async with open_stream(running_app, client, event, held.seq) as events:
        released = await receive_kind(events, "delta")
        assert released.seq == released.data["from"] == released.data["to"] == 2
        assert released.data["changes"] == [[0, "available", 2]]
        await change(db, event, "held")
        await relay.tick(relay_context)
        assert (await receive_kind(events, "delta")).seq == 3


async def test_replay_live_handoff_during_writes(
    db: asyncpg.Connection,
    client: httpx.AsyncClient,
    running_app: ASGIApp,
    event: Event,
    relay_context: ServiceContext,
) -> None:
    await change(db, event, "held")
    await relay.tick(relay_context)
    replay_started = asyncio.Event()
    resume_writes = asyncio.Event()

    async def pause_replay(data: bytes) -> None:
        if data.startswith(b"id: 1\n"):
            replay_started.set()
            await resume_writes.wait()

    async with open_stream(running_app, client, event, 0, pause_replay) as events:
        async with asyncio.timeout(3):
            await replay_started.wait()
        async with relay_context.db.acquire() as conn:
            await change(conn, event, "available")
        await relay.tick(relay_context)
        async with db.transaction():
            await db.execute("UPDATE events SET layout_ver=2 WHERE id=$1", event.id)
            await emit_layout_changed(db, event.id, 2)
        await relay.tick(relay_context)
        resume_writes.set()
        assert (await receive_kind(events, "delta")).seq == 1
        assert (await receive_kind(events, "delta")).seq == 2
        layout = await receive_kind(events, "layout")
        assert layout.seq == 3
        assert layout.data == {"layout_ver": 2}
        latest = (await client.get(event.seats, params={"min_layout_ver": 2})).json()
        assert latest["layout_ver"] == 2
        assert latest["last_seq"] == 3


async def test_snapshot_version_beats_older_unassigned_outbox_row(
    db: asyncpg.Connection,
    client: httpx.AsyncClient,
    running_app: ASGIApp,
    event: Event,
    relay_context: ServiceContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await change(db, event, "held")
    await change(db, event, "available")
    snapshot = (await client.get(event.seats)).json()
    state = {seat["idx"]: (seat["status"], seat["version"]) for seat in snapshot["seats"]}
    async with open_stream(running_app, client, event, snapshot["last_seq"]) as events:
        progress = await receive_kind(events, "progress")
        assert progress.data["head"] == 0
        assert progress.data["oldest_unassigned_age_ms"] > 0
        monkeypatch.setattr(relay, "BATCH_SIZE", 1)
        await relay.tick(relay_context)
        delta = await receive_kind(events, "delta")
        assert delta.seq == 1
        assert delta.data["changes"] == [[0, "held", 1]]
        assert (
            apply_delta(
                state,
                0,
                delta.data,
            )
            == 1
        )
        assert state[0] == ("available", 2)


async def wait_for_head(event_reader: EventReader, head: int) -> None:
    async with asyncio.timeout(WAIT_S):
        while event_reader.head != head:
            assert not event_reader.broken
            await event_reader.changed.wait()


@pytest.mark.parametrize("missing", [False, True])
async def test_replay_recovers_from_ring_eviction_or_resyncs(
    db: asyncpg.Connection,
    client: httpx.AsyncClient,
    running_app: ASGIApp,
    asgi_app: ASGIApp,
    event: Event,
    relay_context: ServiceContext,
    monkeypatch: pytest.MonkeyPatch,
    missing: bool,
) -> None:
    await change(db, event, "held")
    await relay.tick(relay_context)
    replay_started = asyncio.Event()
    resume_writes = asyncio.Event()
    readers = asgi_app.fastapi.state.seat_readers  # type: ignore[attr-defined]

    async def pause_first_frame(data: bytes) -> None:
        if data.startswith(b"id: 1\n"):
            replay_started.set()
            await resume_writes.wait()

    monkeypatch.setattr(stream, "REPLAY_TIMEOUT_S", WAIT_S * 2.0)
    monkeypatch.setattr(stream, "WRITE_TIMEOUT_S", WAIT_S * 2.0)
    async with open_stream(running_app, client, event, 0, pause_first_frame) as events:
        async with asyncio.timeout(WAIT_S):
            await replay_started.wait()
        event_reader = readers.events[event.id]
        async with relay_context.db.acquire() as writer:
            await change(writer, event, "available")
        await relay.tick(relay_context)
        await wait_for_head(event_reader, 2)
        assert event_reader.ring.after(1) is not None

        # Consume the last ring slot while the first replay frame is still in flight.
        with monkeypatch.context() as eviction:
            eviction.setattr(reader, "RING_BYTES", 1)
            async with relay_context.db.acquire() as writer:
                await change(writer, event, "held")
            await relay.tick(relay_context)
            await wait_for_head(event_reader, 3)
        assert event_reader.ring.after(1) is None
        if missing:
            await db.execute("DELETE FROM outbox WHERE event_id=$1 AND seq=2", event.id)
        resume_writes.set()

        first = await receive_kind(events, "delta")
        assert (first.data["from"], first.seq) == (1, 1)
        if missing:
            assert (await receive_kind(events, "resync")).data == {"reason": "replay-unavailable"}
            with pytest.raises(StopAsyncIteration):
                await anext(events)
        else:
            recovered = await receive_kind(events, "delta")
            assert (recovered.data["from"], recovered.data["to"], recovered.seq) == (2, 3, 3)
            await change(db, event, "available")
            await relay.tick(relay_context)
            live = await receive_kind(events, "delta")
            assert (live.data["from"], live.data["to"], live.seq) == (4, 4, 4)


async def test_resume_inside_coalesced_ring_frame_replays_only_the_suffix(
    db: asyncpg.Connection,
    client: httpx.AsyncClient,
    running_app: ASGIApp,
    event: Event,
    relay_context: ServiceContext,
) -> None:
    async with open_stream(running_app, client, event, 0) as live:
        await receive_kind(live, "progress")
        await change(db, event, "held", idx=0)
        await change(db, event, "held", idx=7)
        await relay.tick(relay_context)
        coalesced = await receive_kind(live, "delta")
        assert (coalesced.data["from"], coalesced.seq) == (1, 2)
        async with open_stream(running_app, client, event, 1) as resumed:
            suffix = await receive_kind(resumed, "delta")
            assert (suffix.data["from"], suffix.data["to"], suffix.seq) == (2, 2, 2)
            assert suffix.data["changes"] == [[7, "held", 1]]


async def test_event_reader_capacity_rejects_new_events_and_shares_existing_reader(
    db: asyncpg.Connection,
    client: httpx.AsyncClient,
    running_app: ASGIApp,
    event: Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reader, "MAX_EVENT_READERS", 1)
    other_id = await db.fetchval(
        """
        INSERT INTO events (owner_id, title, category, venue, sales_start_at, sales_end_at,
                            status, published_at)
        SELECT owner_id, 'Other concert', category, venue, sales_start_at, sales_end_at,
               status, published_at FROM events WHERE id=$1
        RETURNING id
        """,
        event.id,
    )
    other = Event(other_id, event.order)
    async with open_stream(running_app, client, event) as first:
        await receive_kind(first, "progress")
        denied = await client.get(other.stream)
        assert denied.status_code == 503
        assert denied.headers["content-type"] == "application/problem+json"
        assert denied.json()["code"] == "server-busy"
        assert denied.json()["retryable"] is True
        async with open_stream(running_app, client, event) as shared:
            await receive_kind(shared, "progress")
    async with open_stream(running_app, client, other) as admitted:
        await receive_kind(admitted, "progress")


async def test_slow_connection_resyncs_and_fast_peer_keeps_receiving(
    db: asyncpg.Connection,
    client: httpx.AsyncClient,
    running_app: ASGIApp,
    event: Event,
    relay_context: ServiceContext,
) -> None:
    blocked = asyncio.Event()

    async def stall_data(data: bytes) -> None:
        if data.startswith(b"id:"):
            blocked.set()
            await asyncio.Future()

    async with open_stream(running_app, client, event, 0, stall_data) as slow:
        await receive_kind(slow, "progress")
        async with open_stream(running_app, client, event, 0) as fast:
            await receive_kind(fast, "progress")
            await change(db, event, "held")
            await relay.tick(relay_context)
            assert (await receive_kind(fast, "delta")).seq == 1
            async with asyncio.timeout(WAIT_S):
                await blocked.wait()
            await change(db, event, "available")
            await relay.tick(relay_context)
            assert (await receive_kind(fast, "delta")).seq == 2
            resync = await receive_kind(slow, "resync")
            assert resync.data == {"reason": "write-timeout"}
            with pytest.raises(StopAsyncIteration):
                await anext(slow)


@pytest.mark.parametrize("mode", ["pubsub", "dropped", "expired", "inactive"])
async def test_invalid_session_closes_stream(
    db: asyncpg.Connection,
    client: httpx.AsyncClient,
    running_app: ASGIApp,
    asgi_app: ASGIApp,
    event: Event,
    relay_context: ServiceContext,
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pubsub = mode == "pubsub"
    state = asgi_app.fastapi.state  # type: ignore[attr-defined]
    checked_live = asyncio.Event()
    checked_invalid = asyncio.Event()
    live_sessions = state.seat_revocations._live_sessions

    async def observe(ids: list[Any]) -> set[Any]:
        result = await live_sessions(ids)
        if result:
            checked_live.set()
        else:
            checked_invalid.set()
        return result

    if pubsub:
        # Keep the backstop out of the Pub/Sub test without stopping the listener.
        async def wait_forever(_: list[Any]) -> set[Any]:
            await asyncio.Future()
            raise AssertionError("unreachable")

        monkeypatch.setattr(state.seat_revocations, "_live_sessions", wait_forever)
    else:
        monkeypatch.setattr(state.seat_revocations, "_live_sessions", observe)

    async with open_stream(running_app, client, event) as events:
        await receive_kind(events, "progress")
        assert state.sse_active_connections() == 1
        session_id = await db.fetchval("SELECT id FROM sessions")
        if pubsub:
            async with asyncio.timeout(WAIT_S):
                while (  # noqa: ASYNC110
                    dict(await relay_context.redis.pubsub_numsub(REVOKE_CHANNEL)).get(
                        REVOKE_CHANNEL.encode(), 0
                    )
                    < 1
                ):
                    await asyncio.sleep(0.01)  # Redis subscription can only be observed by polling
        else:
            async with asyncio.timeout(WAIT_S):
                await checked_live.wait()
        if mode == "expired":
            await db.execute(
                "UPDATE sessions SET idle_expires_at=app_now() WHERE id=$1", session_id
            )
        elif mode == "inactive":
            await db.execute("UPDATE users SET is_active=false")
        else:
            assert await revoke_session(db, session_id, "logout")
        if pubsub:
            await relay_context.redis.publish(REVOKE_CHANNEL, json.dumps([str(session_id)]))
        async with asyncio.timeout(WAIT_S):
            async for remaining in events:
                assert remaining.kind == "progress"
        if not pubsub:
            assert checked_invalid.is_set()
    assert state.sse_active_connections() == 0


async def test_snapshot_visibility_and_layout_bypass(
    db: asyncpg.Connection,
    client: httpx.AsyncClient,
    make_client: Callable[..., httpx.AsyncClient],
    event: Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This event can be seeded as draft even though orders exist in the test fixture.
    await db.execute("UPDATE events SET status='draft', published_at=NULL WHERE id=$1", event.id)
    await create_user(db, "viewer@example.com", role="viewer")
    reads = 0
    snapshot_read = SnapshotCache._read

    async def count_read(cache: SnapshotCache, event_id: int) -> Any:
        nonlocal reads
        reads += 1
        return await snapshot_read(cache, event_id)

    monkeypatch.setattr(SnapshotCache, "_read", count_read)
    monkeypatch.setattr("ticketing.realtime.snapshot._CACHE_TTL_S", 60.0)
    assert (await client.get(event.seats)).status_code == 200
    assert (await client.get(event.seats)).status_code == 200
    assert reads == 1
    async with make_client() as other:
        assert (await other.get(event.seats)).status_code == 401
        await login(other, "viewer@example.com")
        assert (await other.get(event.seats)).status_code == 404
        assert (await other.get(event.stream)).status_code == 404
    await db.execute("UPDATE events SET layout_ver=2 WHERE id=$1", event.id)
    updated = await client.get(event.seats, params={"min_layout_ver": 2})
    assert updated.json()["layout_ver"] == 2
    assert reads == 2


async def test_openapi_documents_stream_content_and_errors(client: httpx.AsyncClient) -> None:
    paths = (await client.get("/api/openapi.json")).json()["paths"]
    operation = paths["/api/v1/events/{event_id}/seat-stream"]["get"]
    assert set(operation["responses"]["200"]["content"]) == {"text/event-stream"}
    assert "progress" in operation["description"]
    assert "Last-Event-ID" in operation["description"]
    assert operation["responses"]["404"]["content"]["application/problem+json"]["example"] == {
        "status": 404,
        "code": "not-found",
    }
    assert operation["security"] == [{"sessionCookie": []}]


async def test_missing_replay_row_sends_resync_and_closes(
    db: asyncpg.Connection,
    client: httpx.AsyncClient,
    running_app: ASGIApp,
    event: Event,
    relay_context: ServiceContext,
) -> None:
    await change(db, event, "held")
    await change(db, event, "available")
    await relay.tick(relay_context)
    await db.execute("DELETE FROM outbox WHERE event_id=$1 AND seq=1", event.id)
    async with open_stream(running_app, client, event, 0) as events:
        assert (await receive_kind(events, "resync")).data == {"reason": "replay-unavailable"}
        with pytest.raises(StopAsyncIteration):
            await anext(events)


async def test_repeated_failed_rechecks_close_all_local_streams(
    client: httpx.AsyncClient,
    running_app: ASGIApp,
    asgi_app: ASGIApp,
    event: Event,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revocations = asgi_app.fastapi.state.seat_revocations  # type: ignore[attr-defined]
    failures = 0
    begin_rechecks = asyncio.Event()
    second_started = asyncio.Event()
    release_second = asyncio.Event()

    async def fail(ids: list[Any]) -> set[Any]:
        nonlocal failures
        assert len(ids) == 1
        await begin_rechecks.wait()
        failures += 1
        if failures == 2:
            second_started.set()
            await release_second.wait()
        raise TimeoutError

    monkeypatch.setattr(revocations, "_live_sessions", fail)
    # Shrink the production threshold (5) so the test stays fast; the behaviour is the same.
    monkeypatch.setattr(revocation, "FAIL_CLOSED_AFTER", 2)
    async with (
        open_stream(running_app, client, event) as first,
        open_stream(running_app, client, event) as second,
    ):
        await receive_kind(first, "progress")
        await receive_kind(second, "progress")
        begin_rechecks.set()
        async with asyncio.timeout(WAIT_S):
            await second_started.wait()
            assert revocations.active == 2
            release_second.set()
            for events in (first, second):
                async for remaining in events:
                    assert remaining.kind == "progress"
    assert failures >= 2
    assert revocations.active == 0


async def test_api_flushes_gauge_after_sse_connect_and_disconnect(
    client: httpx.AsyncClient,
    running_app: ASGIApp,
    event: Event,
    settings: Settings,
) -> None:
    business.record(813, "hold_ok")
    assert (await client.get("/healthz/live")).status_code == 200
    async with Redis.from_url(settings.redis_url) as redis:
        async with open_stream(running_app, client, event) as events:
            await receive_kind(events, "progress")
            async with asyncio.timeout(WAIT_S):
                while True:
                    intervals = parse_batch(await redis.xrange(STREAM))
                    if any(item.sse == 1 for item in intervals):
                        break
                    await asyncio.sleep(0.05)
            active_interval = max(item.number for item in intervals if item.sse == 1)
            assert any((813, "hold_ok", 1) in item.outcomes for item in intervals)
            assert (
                sum(
                    item.routes["/healthz/live"].status_classes[2]
                    for item in intervals
                    if "/healthz/live" in item.routes
                )
                == 1
            )
        async with asyncio.timeout(WAIT_S):
            while True:
                intervals = parse_batch(await redis.xrange(STREAM))
                if any(item.number > active_interval and item.sse == 0 for item in intervals):
                    break
                await asyncio.sleep(0.05)


async def test_draft_visibility_matches_events_after_owner_demotion(
    db: asyncpg.Connection,
    client: httpx.AsyncClient,
    running_app: ASGIApp,
    make_client: Callable[[], httpx.AsyncClient],
    event: Event,
) -> None:
    await db.execute("UPDATE events SET status='draft', published_at=NULL WHERE id=$1", event.id)
    detail = f"/api/v1/events/{event.id}"
    assert (await client.get(detail)).status_code == 200
    assert (await client.get(event.seats)).status_code == 200
    async with open_stream(running_app, client, event) as stream:
        await receive_kind(stream, "progress")
    owner_id = await db.fetchval("SELECT owner_id FROM events WHERE id=$1", event.id)
    for role in ("user", "viewer"):
        email = f"other-{role}@example.com"
        await create_user(db, email, role=role)
        async with make_client() as other:
            await login(other, email)
            for url in (detail, event.seats, event.stream):
                assert (await other.get(url)).status_code == 404
    await create_user(db, "admin@example.com", role="admin")
    async with make_client() as admin:
        admin_session = await login(admin, "admin@example.com")
        user_url = f"/api/v1/admin/users/{owner_id}"
        before = await admin.get(user_url)
        changed = await admin.patch(
            user_url,
            headers={**admin_session.headers(), "If-Match": before.headers["ETag"]},
            json={"role": "viewer"},
        )
        assert changed.status_code == 200
        assert (await admin.get(event.seats)).status_code == 200
        async with open_stream(running_app, admin, event) as stream:
            await receive_kind(stream, "progress")
    assert (await client.get(event.seats)).status_code == 401
    await login(client, "owner@example.com")
    for url in (detail, event.seats, event.stream):
        denied = await client.get(url)
        assert denied.status_code == 404
        assert denied.json()["code"] == "not-found"
