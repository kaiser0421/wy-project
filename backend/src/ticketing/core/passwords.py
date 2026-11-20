"""argon2id password hashing with a bounded CPU budget.

Hashing runs in a dedicated thread pool; at most `argon2_max_concurrency` hashes run per process.
A slot is held until the worker thread finishes even if the request is cancelled, so cancelled
requests cannot pile up extra CPU work. Waiting longer than one second for a slot fails fast with
503 server-busy.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

from argon2 import PasswordHasher as _Argon2
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from ticketing.core.errors import server_busy
from ticketing.core.settings import Settings

MAX_PASSWORD_BYTES = 256


class PasswordHasher:
    def __init__(self, settings: Settings) -> None:
        self._argon = _Argon2(
            time_cost=settings.argon2_time_cost,
            memory_cost=settings.argon2_memory_kib,
            parallelism=settings.argon2_parallelism,
        )
        self._slots = asyncio.Semaphore(settings.argon2_max_concurrency)
        self._pool = ThreadPoolExecutor(
            max_workers=settings.argon2_max_concurrency, thread_name_prefix="argon2"
        )
        # Verified for unknown emails so response time does not reveal whether an account exists.
        self._dummy_hash = self._argon.hash("dummy-password-for-timing")

    async def _run[T](self, fn: object, *args: str) -> T:
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=1.0)
        except TimeoutError as exc:
            raise server_busy("Too many concurrent sign-ins, retry shortly.") from exc
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.run_in_executor(self._pool, fn, *args)  # type: ignore[arg-type]
        future.add_done_callback(lambda _f: self._slots.release())
        result: T = await asyncio.shield(future)
        return result

    async def hash(self, password: str) -> str:
        return await self._run(self._argon.hash, password[:MAX_PASSWORD_BYTES])

    async def verify(self, password_hash: str | None, password: str) -> bool:
        """Constant-shape verification; unknown users verify against a dummy hash."""
        target = password_hash or self._dummy_hash
        ok: bool = await self._run(self._verify_sync, target, password[:MAX_PASSWORD_BYTES])
        return ok and password_hash is not None

    def needs_rehash(self, password_hash: str) -> bool:
        return self._argon.check_needs_rehash(password_hash)

    def _verify_sync(self, password_hash: str, password: str) -> bool:
        try:
            return self._argon.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
