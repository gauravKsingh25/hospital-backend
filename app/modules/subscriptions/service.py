"""Subscriptions — the module's public interface.

Other modules ask two questions here: "what may this hospital use?"
(`get_entitlements`) and "may it use this?" (`ensure_feature`). Nothing
outside this module reads the tables.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.config import settings
from app.core.exceptions import FeatureNotInPlanError
from app.core.models import utc_now
from app.modules.subscriptions import cache
from app.modules.subscriptions.entitlements import Entitlements, effective_status
from app.modules.subscriptions.features import PLAN_FEATURES, PLAN_PROFESSIONAL, FeatureKey
from app.modules.subscriptions.models import (
    PaymentOrder,
    PaymentOrderStatus,
    Plan,
    Subscription,
    SubscriptionStatus,
)

# Held so the after-commit cache clears are not garbage-collected mid-flight.
_pending: set[asyncio.Task[None]] = set()

__all__ = [
    "count_payments_awaiting_review",
    "ensure_feature",
    "get_entitlements",
    "get_subscription",
    "grant_complimentary",
    "hospital_has_feature",
    "list_active_plans",
    "plan_feature_keys",
    "write_subscription",
]


async def get_subscription(session: AsyncSession, hospital_id: uuid.UUID) -> Subscription | None:
    result = await session.execute(
        select(Subscription).where(col(Subscription.hospital_id) == hospital_id)
    )
    row: Subscription | None = result.scalars().first()
    return row


async def write_subscription(session: AsyncSession, sub: Subscription) -> Subscription:
    """Save a subscription. The only place one is written.

    Drops the cached row, so a plan change or an extension is seen by the very
    next request rather than a minute later.
    """
    now = utc_now()
    sub.status = effective_status(
        period_end=sub.current_period_end,
        cancel_at_period_end=sub.cancel_at_period_end,
        now=now,
        grace_days=settings.GRACE_PERIOD_DAYS,
    )
    sub.updated_at = now
    session.add(sub)
    await session.flush()
    await session.refresh(sub)
    await cache.invalidate(sub.hospital_id)
    _invalidate_after_commit(session, sub.hospital_id)
    return sub


def _invalidate_after_commit(session: AsyncSession, hospital_id: uuid.UUID) -> None:
    """Drop the cached row again once this change is committed.

    Clearing it only before the commit leaves a gap: a request from the same
    hospital in that gap misses, reads the row as it still stands, and caches
    it for another minute. The second clear, after the commit, is the one
    that guarantees the next request sees the change.
    """

    def _on_commit(_session: object) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Committed from synchronous code with no loop: the TTL bounds it.
            return
        task = loop.create_task(cache.invalidate(hospital_id))
        _pending.add(task)
        task.add_done_callback(_pending.discard)

    event.listen(session.sync_session, "after_commit", _on_commit, once=True)


async def grant_complimentary(
    session: AsyncSession, hospital_id: uuid.UUID, *, plan_code: str = PLAN_PROFESSIONAL
) -> Subscription:
    """Give a hospital a subscription that never lapses. Idempotent.

    For hospitals the platform owner sets up by hand, and for every hospital
    that existed before plans did.
    """
    existing = await get_subscription(session, hospital_id)
    if existing is not None:
        return existing
    return await write_subscription(
        session,
        Subscription(
            hospital_id=hospital_id,
            plan_code=plan_code,
            status=SubscriptionStatus.ACTIVE,
            current_period_start=utc_now(),
            current_period_end=None,
            complimentary=True,
        ),
    )


async def get_entitlements(
    session: AsyncSession, hospital_id: uuid.UUID, *, now: datetime | None = None
) -> Entitlements | None:
    """What the hospital may use at `now`. None: it has no subscription."""
    row = await cache.get(hospital_id)
    if row is None:
        sub = await get_subscription(session, hospital_id)
        if sub is None:
            return None
        row = cache.CachedSubscription(
            plan_code=sub.plan_code,
            period_end=sub.current_period_end,
            cancel_at_period_end=sub.cancel_at_period_end,
            complimentary=sub.complimentary,
        )
        await cache.put(hospital_id, row)

    status = effective_status(
        period_end=row["period_end"],
        cancel_at_period_end=row["cancel_at_period_end"],
        now=now or utc_now(),
        grace_days=settings.GRACE_PERIOD_DAYS,
    )
    # The registry in code is the authority; an unknown plan grants nothing.
    features = PLAN_FEATURES.get(row["plan_code"], frozenset())
    return Entitlements(
        plan_code=row["plan_code"],
        status=status,
        period_end=row["period_end"],
        complimentary=row["complimentary"],
        features=frozenset(feature.value for feature in features),
    )


async def hospital_has_feature(
    session: AsyncSession, hospital_id: uuid.UUID, feature: FeatureKey
) -> bool:
    if not settings.SUBSCRIPTIONS_ENFORCED:
        return True
    entitlements = await get_entitlements(session, hospital_id)
    return entitlements is not None and entitlements.has(feature.value)


async def ensure_feature(
    session: AsyncSession, hospital_id: uuid.UUID, feature: FeatureKey
) -> None:
    """Raise unless the hospital's plan includes `feature`.

    For services reached through an open route that would start work in a
    locked module: ordering a lab test from a consultation, say.
    """
    if not settings.SUBSCRIPTIONS_ENFORCED:
        return
    entitlements = await get_entitlements(session, hospital_id)
    if entitlements is None or not entitlements.has(feature.value):
        raise FeatureNotInPlanError(
            details={
                "feature": feature.value,
                "plan": entitlements.plan_code if entitlements else None,
            }
        )


async def list_active_plans(session: AsyncSession) -> list[Plan]:
    """The plans on sale, cheapest first. Prices come only from here."""
    rows = await session.execute(
        select(Plan).where(col(Plan.is_active).is_(True)).order_by(col(Plan.rank))
    )
    return list(rows.scalars().all())


def plan_feature_keys(plan_code: str) -> list[str]:
    """What a plan includes, in the registry's order."""
    included = PLAN_FEATURES.get(plan_code, frozenset())
    return [feature.value for feature in FeatureKey if feature in included]


async def count_payments_awaiting_review(session: AsyncSession) -> int:
    """Payments someone has submitted and is waiting on: the console's first number."""
    count = (
        await session.execute(
            select(func.count())
            .select_from(PaymentOrder)
            .where(col(PaymentOrder.status) == PaymentOrderStatus.SUBMITTED)
        )
    ).scalar_one()
    return int(count)
