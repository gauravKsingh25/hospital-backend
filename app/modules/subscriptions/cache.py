"""Redis cache of a hospital's subscription row.

Read on every authenticated request, changed a few times a year. Only the
stored facts are cached (plan, period end, flags); the status is recomputed
from them against the clock on each read, so a cached row can never keep a
lapsed hospital "active".

Same contract as `identity.cache`: every Redis failure and every undecodable
entry is a miss, never an error. Correctness comes from the explicit
invalidation in `service.write_subscription`; the TTL only bounds a write
path nobody thought of.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Final, TypedDict

import redis.exceptions

from app.core.redis import get_redis

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS: Final[int] = 60
_PREFIX: Final[str] = "sub:row"


class CachedSubscription(TypedDict):
    plan_code: str
    period_end: datetime | None
    cancel_at_period_end: bool
    complimentary: bool


def _key(hospital_id: uuid.UUID) -> str:
    return f"{_PREFIX}:{hospital_id}"


async def get(hospital_id: uuid.UUID) -> CachedSubscription | None:
    try:
        raw = await get_redis().get(_key(hospital_id))
    except (redis.exceptions.RedisError, OSError) as exc:
        logger.debug("subscription cache read failed, using the database: %s", exc)
        return None
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
        end = payload["period_end"]
        period_end = datetime.fromisoformat(end) if end is not None else None
        if period_end is not None and period_end.tzinfo is None:
            # Comparable with nothing: a naive time against the aware clock
            # raises, and a cache entry must never fail a request.
            raise ValueError("period_end has no timezone")
        return CachedSubscription(
            plan_code=str(payload["plan_code"]),
            period_end=period_end,
            cancel_at_period_end=bool(payload["cancel_at_period_end"]),
            complimentary=bool(payload["complimentary"]),
        )
    except (ValueError, KeyError, TypeError):
        logger.warning("discarding malformed subscription cache entry for %s", hospital_id)
        return None


async def put(hospital_id: uuid.UUID, row: CachedSubscription) -> None:
    end = row["period_end"]
    payload = json.dumps({**row, "period_end": end.isoformat() if end is not None else None})
    try:
        await get_redis().set(_key(hospital_id), payload, ex=CACHE_TTL_SECONDS)
    except (redis.exceptions.RedisError, OSError) as exc:
        logger.debug("subscription cache write failed: %s", exc)


async def invalidate(hospital_id: uuid.UUID) -> None:
    try:
        await get_redis().delete(_key(hospital_id))
    except (redis.exceptions.RedisError, OSError) as exc:
        # Worst case the entry lives out its TTL.
        logger.warning("could not invalidate subscription cache for %s: %s", hospital_id, exc)
