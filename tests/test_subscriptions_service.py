"""Plans, the seeded feature map, and the entitlement lookup."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.models import utc_now
from app.core.redis import get_redis
from app.modules.subscriptions import cache, service
from app.modules.subscriptions.features import PLAN_FEATURES, FeatureKey
from app.modules.subscriptions.models import Plan, PlanFeature
from app.modules.tenancy.models import Hospital
from tests.subscriptions_support import set_plan

pytestmark = pytest.mark.integration


async def test_plans_are_seeded(session: AsyncSession) -> None:
    rows = {p.code: p for p in (await session.execute(select(Plan))).scalars()}
    assert rows["standard"].price_inr == 3000
    assert rows["professional"].price_inr == 8999
    assert rows["standard"].interval_days == rows["professional"].interval_days == 30
    assert rows["standard"].rank < rows["professional"].rank


async def test_seeded_plan_features_match_the_registry(session: AsyncSession) -> None:
    seeded: dict[str, set[str]] = {}
    for pf in (await session.execute(select(PlanFeature))).scalars():
        seeded.setdefault(pf.plan_code, set()).add(pf.feature_key)
    assert seeded == {code: {f.value for f in feats} for code, feats in PLAN_FEATURES.items()}


@pytest.mark.no_auto_subscription
async def test_hospital_without_a_subscription_has_no_entitlements(
    session: AsyncSession, hospital: Hospital
) -> None:
    assert await service.get_entitlements(session, hospital.id) is None
    assert not await service.hospital_has_feature(session, hospital.id, FeatureKey.OPD)


@pytest.mark.no_auto_subscription
async def test_grant_complimentary_is_idempotent(session: AsyncSession, hospital: Hospital) -> None:
    a = await service.grant_complimentary(session, hospital.id)
    b = await service.grant_complimentary(session, hospital.id)
    assert a.id == b.id
    assert a.complimentary
    assert a.current_period_end is None
    e = await service.get_entitlements(session, hospital.id)
    assert e is not None
    assert e.plan_code == "professional"
    assert e.has("ipd")


async def test_standard_has_only_opd(session: AsyncSession, hospital: Hospital) -> None:
    await set_plan(session, hospital, "standard", period_end=utc_now() + timedelta(days=10))
    e = await service.get_entitlements(session, hospital.id)
    assert e is not None
    assert e.features == frozenset({"opd"})


async def test_a_plan_change_is_seen_on_the_next_read(
    session: AsyncSession, hospital: Hospital
) -> None:
    await set_plan(session, hospital, "standard", period_end=utc_now() + timedelta(days=10))
    assert not await service.hospital_has_feature(session, hospital.id, FeatureKey.IPD)
    await set_plan(session, hospital, "professional", period_end=utc_now() + timedelta(days=10))
    assert await service.hospital_has_feature(session, hospital.id, FeatureKey.IPD)


async def test_a_reader_between_write_and_commit_does_not_pin_the_old_plan(
    session: AsyncSession, hospital: Hospital
) -> None:
    """A change is cached away from under itself if the cache is only cleared
    before the commit: another request reads the still-committed old row in
    the gap and re-caches it for a minute."""
    await set_plan(session, hospital, "standard", period_end=utc_now() + timedelta(days=10))
    await session.commit()

    await set_plan(session, hospital, "professional", period_end=utc_now() + timedelta(days=10))
    async with db.get_sessionmaker()() as other:
        in_the_gap = await service.get_entitlements(other, hospital.id)
    assert in_the_gap is not None
    assert in_the_gap.plan_code == "standard"

    await session.commit()
    await asyncio.sleep(0.05)

    async with db.get_sessionmaker()() as other:
        after = await service.get_entitlements(other, hospital.id)
    assert after is not None
    assert after.plan_code == "professional"


async def test_a_cached_period_end_without_a_timezone_is_a_miss(
    session: AsyncSession, hospital: Hospital
) -> None:
    await service.grant_complimentary(session, hospital.id)
    await get_redis().set(
        f"sub:row:{hospital.id}",
        '{"plan_code": "standard", "period_end": "2020-01-01T00:00:00", '
        '"cancel_at_period_end": false, "complimentary": false}',
    )

    entitlements = await service.get_entitlements(session, hospital.id)

    assert entitlements is not None
    assert entitlements.plan_code == "professional"


async def test_cache_garbage_and_redis_errors_fall_back_to_the_database(
    session: AsyncSession, hospital: Hospital, monkeypatch: pytest.MonkeyPatch
) -> None:
    await service.grant_complimentary(session, hospital.id)
    await get_redis().set(f"sub:row:{hospital.id}", "not json")
    first = await service.get_entitlements(session, hospital.id)
    assert first is not None
    assert first.usable

    def _down() -> None:
        raise OSError("redis is down")

    monkeypatch.setattr(cache, "get_redis", _down)
    second = await service.get_entitlements(session, hospital.id)
    assert second is not None
    assert second.usable
