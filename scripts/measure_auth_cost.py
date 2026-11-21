"""Measure the per-request session check in isolation, inside a running API container.

Usage:
  TOKEN=<value of the session cookie after logging in>
  docker compose exec -T -e TOKEN="$TOKEN" api python - < scripts/measure_auth_cost.py

On one database connection it times N calls of the real authenticate() against N calls of
SELECT 1 (the round-trip baseline), then prints EXPLAIN ANALYZE for the same valid token.
"""

import asyncio
import os
import statistics
import time
from collections.abc import Awaitable, Callable

import asyncpg

from ticketing.auth.sessions import _AUTH_SQL, authenticate
from ticketing.auth.tokens import hash_token

N = 2000
ROUNDS = 3


def pct(xs: list[float], p: float) -> float:
    ordered = sorted(xs)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


async def timed(call: Callable[[], Awaitable[object]]) -> list[float]:
    out = []
    for _ in range(N):
        start = time.perf_counter_ns()
        await call()
        out.append((time.perf_counter_ns() - start) / 1e6)
    return out


def summary(name: str, xs: list[float]) -> str:
    return (
        f"{name:22s} n={len(xs)} p50={statistics.median(xs):.3f} p95={pct(xs, 0.95):.3f} "
        f"p99={pct(xs, 0.99):.3f} mean={statistics.fmean(xs):.3f} ms"
    )


async def main() -> None:
    token = os.environ["TOKEN"]
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    principal = await authenticate(conn, token)
    if principal is None:
        raise SystemExit("TOKEN did not authenticate; log in again and pass the session cookie")
    print(f"valid session: user_id={principal.user_id} role={principal.role}")

    async def baseline() -> object:
        return await conn.fetchval("SELECT 1")

    async def check() -> object:
        return await authenticate(conn, token)

    for _ in range(200):
        await baseline()
        await check()
    base: list[float] = []
    auth: list[float] = []
    for _ in range(ROUNDS):  # interleave so both share the same drift
        base += await timed(baseline)
        auth += await timed(check)
    print(summary("SELECT 1 (round trip)", base))
    print(summary("authenticate()", auth))
    diff = statistics.median(auth) - statistics.median(base)
    print(f"isolated cost, p50 difference = {diff:.3f} ms")
    sessions = await conn.fetchval("SELECT count(*) FROM sessions")
    users = await conn.fetchval("SELECT count(*) FROM users")
    print(f"sessions rows = {sessions} users rows = {users}")
    plan = await conn.fetch("EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) " + _AUTH_SQL, hash_token(token))
    for row in plan:
        print(row[0])
    await conn.close()


asyncio.run(main())
