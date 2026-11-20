"""Atomic fixed-window rate limits in Redis.

Every counter gets a TTL in the same Lua call, so keys never leak. Login limits fail closed:
if Redis is unreachable the request is rejected with 503 rather than allowed unthrottled.
"""

from __future__ import annotations

from redis.asyncio import Redis
from redis.exceptions import RedisError

from ticketing.core.errors import ProblemError, server_busy

_LUA = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('PEXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('PTTL', KEYS[1])
return {current, ttl}
"""


class RateLimiter:
    def __init__(self, redis: Redis, prefix: str = "rl") -> None:
        self._redis = redis
        self._prefix = prefix
        self._script = redis.register_script(_LUA)

    async def hit(
        self, bucket: str, key: str, limit: int, window_s: int, *, fail_closed: bool = True
    ) -> None:
        """Count one request; raise 429 when over `limit` within the window."""
        redis_key = f"{self._prefix}:{bucket}:{key}"
        try:
            count, ttl_ms = await self._script(keys=[redis_key], args=[window_s * 1000])
        except (RedisError, OSError, TimeoutError) as exc:
            if fail_closed:
                raise server_busy("Rate limiter unavailable, retry shortly.") from exc
            return
        if int(count) > limit:
            retry_after = max(1, (int(ttl_ms) + 999) // 1000)
            raise ProblemError(
                429,
                "rate-limited",
                "Too many requests",
                f"Limit of {limit} per {window_s}s exceeded for {bucket}.",
                retryable=True,
                headers={"Retry-After": str(retry_after)},
            )
