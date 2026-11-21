"""Measure the per-request session check in isolation, inside a running API container.

Usage:
  read -rs TOKEN && export TOKEN   # paste the session cookie value; it is not echoed
  docker compose exec -T -e TOKEN api python - < scripts/measure_auth_cost.py

On one database connection it times N calls of the real authenticate() against N calls of
SELECT 1 (the round-trip baseline), then prints EXPLAIN ANALYZE for the same valid token.
"""

import asyncio
import os
import re
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


async def timed(call: Callable[[], Awaitable[object]], out: list[float]) -> object:
    start = time.perf_counter_ns()
    result = await call()
    out.append((time.perf_counter_ns() - start) / 1e6)
    return result


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
    for i in range(ROUNDS * N):  # alternate the order every call so both share the same drift
        first, second = (baseline, base), (check, auth)
        for call, sink in (first, second) if i % 2 == 0 else (second, first):
            if await timed(call, sink) is None:
                raise SystemExit("the session stopped authenticating during the run; no result")
    print(summary("SELECT 1 (round trip)", base))
    print(summary("authenticate()", auth))
    diff = statistics.median(auth) - statistics.median(base)
    print(f"isolated cost, p50 difference = {diff:.3f} ms")
    sessions = await conn.fetchval("SELECT count(*) FROM sessions")
    users = await conn.fetchval("SELECT count(*) FROM users")
    print(f"sessions rows = {sessions} users rows = {users}")
    plan = await conn.fetch("EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) " + _AUTH_SQL, hash_token(token))
    for row in plan:  # the token hash is not a usable cookie, but keep it out of shared output
        print(re.sub(r"'\\x[0-9a-f]+'", "'<token hash>'", row[0]))
    await conn.close()


asyncio.run(main())
