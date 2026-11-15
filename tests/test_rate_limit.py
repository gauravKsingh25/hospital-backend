"""The public signup and contact routes are guarded by a small Redis limiter.

It must refuse past the limit, keep separate keys apart, and — the part that
is easy to get wrong — let traffic through when Redis itself is down, because
a Redis blip must not take signup offline (spec §8).
"""

from __future__ import annotations

import uuid

import pytest
import redis.exceptions
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.core import rate_limit
from app.core.exceptions import register_exception_handlers


def _bucket() -> str:
    # A fresh bucket per test: counters live in the shared Redis.
    return f"test-{uuid.uuid4().hex[:8]}"


async def test_allows_up_to_limit_then_refuses() -> None:
    bucket = _bucket()
    results = [
        await rate_limit.hit(bucket, "1.2.3.4", limit=3, window_seconds=60) for _ in range(4)
    ]
    assert results == [True, True, True, False]


async def test_separate_keys_do_not_share_a_budget() -> None:
    bucket = _bucket()
    assert await rate_limit.hit(bucket, "a", limit=1, window_seconds=60) is True
    assert await rate_limit.hit(bucket, "b", limit=1, window_seconds=60) is True
    assert await rate_limit.hit(bucket, "a", limit=1, window_seconds=60) is False


async def test_fails_open_when_redis_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        def pipeline(self, *args: object, **kwargs: object) -> object:
            raise redis.exceptions.ConnectionError("down")

        async def incr(self, *args: object, **kwargs: object) -> int:
            raise redis.exceptions.ConnectionError("down")

    monkeypatch.setattr(rate_limit, "get_redis", lambda: Broken())
    assert await rate_limit.hit(_bucket(), "x", limit=1, window_seconds=60) is True
    assert await rate_limit.hit(_bucket(), "x", limit=1, window_seconds=60) is True


async def test_dependency_returns_429_with_retry_after() -> None:
    app = FastAPI()
    register_exception_handlers(app)
    limiter = rate_limit.limit_by_ip(_bucket(), limit=1, window_seconds=60)

    # `dependencies=` rather than a parameter: under postponed annotations
    # FastAPI cannot resolve a dependency that is a local variable.
    @app.get("/ping", dependencies=[Depends(limiter)])
    async def ping() -> dict[str, str]:
        return {"ok": "yes"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        first = await client.get("/ping")
        second = await client.get("/ping")

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limited"
    assert int(second.headers["retry-after"]) > 0
