from __future__ import annotations

import asyncio
from typing import cast

import asyncpg
import pytest

from ticketing.core.errors import ProblemError
from ticketing.realtime import reader
from ticketing.realtime.frames import Frame
from ticketing.realtime.reader import Replays


async def test_replay_capacity_counts_shielded_tasks_across_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reader, "MAX_REPLAY_TASKS", 1)
    replays = Replays(cast(asyncpg.Pool, None), timeout=1.0)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[tuple[int, int, int]] = []
    result = [Frame(1, 1, b"frame")]

    async def read(event_id: int, cursor: int, end: int) -> list[Frame]:
        calls.append((event_id, cursor, end))
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(replays, "_read", read)
    first = asyncio.create_task(replays.read(1, 0, 1))
    try:
        async with asyncio.timeout(10):
            await entered.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        for args in ((1, 1, 2), (2, 0, 1)):
            with pytest.raises(ProblemError) as denied:
                await replays.read(*args)
            assert (denied.value.status, denied.value.code) == (503, "server-busy")

        joining = asyncio.Event()

        async def join() -> list[Frame]:
            joining.set()
            return await replays.read(1, 0, 1)

        shared = asyncio.create_task(join())
        async with asyncio.timeout(10):
            await joining.wait()
            release.set()
            assert await shared is result
        assert calls == [(1, 0, 1)]
        assert await replays.read(2, 0, 1) is result
        assert calls == [(1, 0, 1), (2, 0, 1)]
    finally:
        await replays.close()
        await asyncio.gather(first, return_exceptions=True)
