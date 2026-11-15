"""A small fixed-window rate limiter for the unauthenticated routes.

The public signup, password-reset and contact routes can be reached by
anybody, so each is capped per client address (and, where it matters, per
email address). Counters live in Redis: `INCR` on a key named for the current
window, with an expiry set on the first hit.

**It fails open.** If Redis cannot be reached the request is allowed and a
warning logged (spec §8): a Redis blip must not take signup offline, and the
limits that actually protect an account — OTP attempts and the hourly OTP cap
— are kept in Postgres and hold regardless.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Annotated

import redis.exceptions
from fastapi import Depends

from app.core.deps import get_client_ip
from app.core.exceptions import RateLimitedError
from app.core.redis import get_redis

logger = logging.getLogger(__name__)

__all__ = ["hit", "limit_by_ip", "retry_after"]

_PREFIX = "rl"


def _window(window_seconds: int, now: float) -> int:
    return int(now // window_seconds)


def retry_after(window_seconds: int, now: float | None = None) -> int:
    """Seconds until the current window ends: what `Retry-After` says."""
    moment = time.time() if now is None else now
    return max(1, int(window_seconds - (moment % window_seconds)))


async def hit(bucket: str, key: str, *, limit: int, window_seconds: int) -> bool:
    """Count one request against `bucket`/`key`. True when it is allowed."""
    name = f"{_PREFIX}:{bucket}:{key}:{_window(window_seconds, time.time())}"
    try:
        client = get_redis()
        count = int(await client.incr(name))
        if count == 1:
            await client.expire(name, window_seconds)
    except (redis.exceptions.RedisError, OSError) as exc:
        logger.warning("rate limiter unavailable, allowing request bucket=%s: %s", bucket, exc)
        return True
    return count <= limit


def limit_by_ip(bucket: str, *, limit: int, window_seconds: int) -> Callable[..., Awaitable[None]]:
    """A dependency that refuses a client address past `limit` per window."""

    async def dependency(ip: Annotated[str | None, Depends(get_client_ip)]) -> None:
        if not await hit(bucket, ip or "unknown", limit=limit, window_seconds=window_seconds):
            raise RateLimitedError(retry_after_seconds=retry_after(window_seconds))

    return dependency
