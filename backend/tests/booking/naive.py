"""Intentionally remove seat locking and the guarded write for break-it tests."""

import asyncio

from ticketing.booking.models import SpecificSeats
from ticketing.booking.test_hooks import SeatStrategy


def broken_strategy():
    barrier = asyncio.Barrier(2)

    async def allocate(conn, event_id, selection):
        assert isinstance(selection, SpecificSeats)
        seats = await conn.fetch(
            """SELECT id FROM seats WHERE event_id=$1 AND id=ANY($2::bigint[])
               AND status='available' ORDER BY id""",
            event_id,
            selection.seat_ids,
        )
        assert len(seats) == len(selection.seat_ids)
        # The transactions both observe available before either is allowed to write.
        async with asyncio.timeout(10):
            await barrier.wait()
        return seats

    async def overwrite(conn, order_id, seat_ids):
        return await conn.fetch(
            """UPDATE seats SET status='held',order_id=$1,version=version+1
               WHERE id=ANY($2::bigint[]) RETURNING seat_idx,version""",
            order_id,
            seat_ids,
        )

    return SeatStrategy(allocate, overwrite)
