"""Shared helpers for the subscription tests."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.models import utc_now
from app.modules.subscriptions import service
from app.modules.subscriptions.models import Subscription, SubscriptionStatus
from app.modules.tenancy.models import Hospital


async def set_plan(
    session: AsyncSession,
    hospital: Hospital,
    plan_code: str,
    *,
    period_end: datetime | None = None,
    cancel: bool = False,
) -> Subscription:
    """Put a hospital on a plan, replacing whatever it had."""
    async with db.system_context(session):
        existing = (
            (
                await session.execute(
                    select(Subscription).where(Subscription.hospital_id == hospital.id)
                )
            )
            .scalars()
            .first()
        )
        sub = existing or Subscription(
            hospital_id=hospital.id,
            plan_code=plan_code,
            status=SubscriptionStatus.ACTIVE,
            current_period_start=utc_now(),
        )
        sub.plan_code = plan_code
        sub.current_period_end = period_end
        sub.cancel_at_period_end = cancel
        sub.complimentary = period_end is None
        return await service.write_subscription(session, sub)
