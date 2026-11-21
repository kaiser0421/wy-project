"""Claimed reconciler rows that never finish are handed back, never silently dropped."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from ticketing.payments import reconciler


def _ctx() -> Any:
    return SimpleNamespace(stop=asyncio.Event())


def _queue(rows: list[dict[str, int]]) -> Any:
    async def claim(limit: int) -> list[dict[str, int]]:
        taken = rows[:limit]
        del rows[:limit]
        return taken

    return claim


async def test_completed_rows_are_not_handed_back() -> None:
    handed: list[Any] = []

    async def process(row: Any) -> None:
        return None

    async def unclaim(rows: list[Any]) -> None:
        handed.extend(rows)

    await reconciler._run_phase(
        _ctx(), "t", _queue([{"id": i} for i in range(5)]), process, 10, 2, unclaim
    )
    assert handed == []


async def test_shutdown_before_work_hands_rows_back() -> None:
    ctx = _ctx()
    handed: list[Any] = []

    async def claim(limit: int) -> list[dict[str, int]]:
        ctx.stop.set()  # shutdown arrives while the claim is committing
        return [{"id": 1}, {"id": 2}][:limit]

    async def process(row: Any) -> None:
        raise AssertionError("no provider work after shutdown")

    async def unclaim(rows: list[Any]) -> None:
        handed.extend(rows)

    await reconciler._run_phase(ctx, "t", claim, process, 10, 2, unclaim)
    assert handed == [{"id": 1}, {"id": 2}]


async def test_phase_timeout_hands_back_only_unfinished_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reconciler, "_PHASE_TIMEOUT_S", 0.2)
    handed: list[Any] = []

    async def process(row: Any) -> None:
        if row["id"] == 2:
            await asyncio.sleep(5)  # stalls past the phase deadline

    async def unclaim(rows: list[Any]) -> None:
        handed.extend(rows)

    await reconciler._run_phase(
        _ctx(), "t", _queue([{"id": 1}, {"id": 2}]), process, 10, 2, unclaim
    )
    assert handed == [{"id": 2}]
