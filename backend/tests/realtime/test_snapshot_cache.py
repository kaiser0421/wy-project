from __future__ import annotations

import asyncio

import pytest

from ticketing.realtime.snapshot import FullSnapshot, SnapshotCache


async def test_overlapping_refreshes_share_work_and_survive_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    joined = asyncio.Event()
    reads = 0
    cache = SnapshotCache(None, 1.0)
    shield = asyncio.shield

    async def read(_: int) -> FullSnapshot:
        nonlocal reads
        reads += 1
        started.set()
        await release.wait()
        return FullSnapshot(event_id=1, layout_ver=2, last_seq=3, seats=[], zones=[])

    def join(task: asyncio.Task) -> asyncio.Future:
        joined.set()
        return shield(task)

    monkeypatch.setattr(cache, "_read", read)
    first = asyncio.create_task(cache.get(1, 0))
    async with asyncio.timeout(2):
        await started.wait()
        # The first caller already joined the refresh. Observe a second join before cancelling it.
        monkeypatch.setattr(asyncio, "shield", join)
        second = asyncio.create_task(cache.get(1, 2))
        await joined.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert (await second).snapshot.last_seq == 3
    assert reads == 1
    await cache.close()
