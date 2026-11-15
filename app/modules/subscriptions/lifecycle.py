"""Changing a paid subscription: plan changes, cancel and resume, extensions.

Every change goes through `service.write_subscription`, which drops the cached
row, so the hospital's next request sees it. Every change is audited.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.config import settings
from app.core.database import system_context
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.ids import new_id
from app.core.models import utc_now
from app.core.pagination import PageParams
from app.modules.identity.models import PlatformEmailStatus, User
from app.modules.identity.rbac import Roles
from app.modules.identity.service import record_audit, users_with_role
from app.modules.subscriptions import emails, periods
from app.modules.subscriptions.entitlements import effective_status
from app.modules.subscriptions.models import Plan, Subscription, SubscriptionReminder
from app.modules.subscriptions.service import write_subscription
from app.modules.tenancy import service as tenancy_service

logger = logging.getLogger(__name__)

__all__ = [
    "cancel",
    "extend",
    "list_subscriptions",
    "locked_subscription",
    "resume",
    "schedule_change",
    "send_due_reminders",
]


async def locked_subscription(session: AsyncSession, hospital_id: uuid.UUID) -> Subscription:
    subscription: Subscription | None = (
        (
            await session.execute(
                select(Subscription)
                .where(col(Subscription.hospital_id) == hospital_id)
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )
    if subscription is None:
        raise NotFoundError("This hospital has no subscription.", code="subscription_not_found")
    if subscription.current_period_end is None:
        raise ConflictError(
            "This hospital's plan is complimentary and cannot be changed here.",
            code="subscription_complimentary",
        )
    return subscription


async def schedule_change(
    session: AsyncSession, *, hospital_id: uuid.UUID, plan_code: str, actor: User
) -> Subscription:
    """Move to a lower plan at the next renewal; choosing the current plan undoes it.

    Nothing changes now: the hospital keeps what it paid for until the month
    ends. A move up is an upgrade, which is paid for, so it is refused here.
    """
    subscription = await locked_subscription(session, hospital_id)
    current = await session.get(Plan, subscription.plan_code)
    target = await session.get(Plan, plan_code)
    if current is None or target is None or not target.is_active:
        raise ValidationError("That plan does not exist.", code="plan_unknown")
    if subscription.pending_plan_effective_at is not None:
        raise ConflictError(
            "The next month is already paid for on a plan. Change it at the renewal after.",
            code="downgrade_unavailable",
        )
    if target.rank > current.rank:
        raise ConflictError(
            "Moving to a higher plan is an upgrade. Use Upgrade.", code="downgrade_unavailable"
        )
    before = subscription.pending_plan_code
    subscription.pending_plan_code = None if target.code == current.code else target.code
    saved = await write_subscription(session, subscription)
    await record_audit(
        session,
        action="subscription.schedule_change",
        actor=actor,
        hospital_id=hospital_id,
        resource_type="subscription",
        resource_id=saved.id,
        changes={"pending_plan": {"before": before, "after": saved.pending_plan_code}},
    )
    return saved


async def _set_cancel(
    session: AsyncSession, *, hospital_id: uuid.UUID, actor: User, cancel: bool
) -> Subscription:
    subscription = await locked_subscription(session, hospital_id)
    now = utc_now()
    if (
        not cancel
        and subscription.current_period_end is not None
        and (subscription.current_period_end <= now)
    ):
        raise ConflictError(
            "The paid month has already ended. Renew instead.", code="renewal_unavailable"
        )
    subscription.cancel_at_period_end = cancel
    saved = await write_subscription(session, subscription)
    await record_audit(
        session,
        action="subscription.cancel" if cancel else "subscription.resume",
        actor=actor,
        hospital_id=hospital_id,
        resource_type="subscription",
        resource_id=saved.id,
        changes={"cancel_at_period_end": cancel},
    )
    return saved


async def cancel(session: AsyncSession, *, hospital_id: uuid.UUID, actor: User) -> Subscription:
    """Stop at the end of the paid month. No refund; access runs to the end."""
    return await _set_cancel(session, hospital_id=hospital_id, actor=actor, cancel=True)


async def resume(session: AsyncSession, *, hospital_id: uuid.UUID, actor: User) -> Subscription:
    """Undo a cancellation while the paid month is still running."""
    return await _set_cancel(session, hospital_id=hospital_id, actor=actor, cancel=False)


async def extend(
    session: AsyncSession, *, subscription_id: uuid.UUID, days: int, reason: str, actor: User
) -> Subscription:
    """The platform owner adds days by hand: a delayed payment, a goodwill gesture.

    Counted from the later of the period end and now, so extending a lapsed
    hospital gives it the days from today, not days already gone.
    """
    async with system_context(session):
        subscription: Subscription | None = (
            (
                await session.execute(
                    select(Subscription)
                    .where(col(Subscription.id) == subscription_id)
                    .with_for_update()
                )
            )
            .scalars()
            .first()
        )
        if subscription is None:
            raise NotFoundError("Subscription not found.", code="subscription_not_found")
        if subscription.current_period_end is None:
            raise ConflictError(
                "This subscription is complimentary and never ends.",
                code="subscription_complimentary",
            )
        before = subscription.current_period_end
        subscription.current_period_end = max(before, utc_now()) + timedelta(days=days)
        saved = await write_subscription(session, subscription)
        await record_audit(
            session,
            action="subscription.extend",
            actor=actor,
            hospital_id=saved.hospital_id,
            resource_type="subscription",
            resource_id=saved.id,
            changes={
                "days": days,
                "reason": reason,
                "period_end": {
                    "before": before.isoformat(),
                    "after": saved.current_period_end.isoformat()
                    if saved.current_period_end
                    else None,
                },
            },
        )
    return saved


async def list_subscriptions(
    session: AsyncSession, *, params: PageParams
) -> tuple[list[Subscription], int]:
    """Every hospital's subscription, the soonest to end first; complimentary last."""
    async with system_context(session):
        total = (await session.execute(select(func.count()).select_from(Subscription))).scalar_one()
        rows = (
            (
                await session.execute(
                    select(Subscription)
                    .order_by(col(Subscription.current_period_end).asc().nulls_last())
                    .limit(params.limit)
                    .offset(params.offset)
                )
            )
            .scalars()
            .all()
        )
    return list(rows), int(total)


async def send_due_reminders(session: AsyncSession, now: datetime | None = None) -> dict[str, int]:
    """The subscription sweep: plan switches due, status hint, reminders.

    One subscription at a time, each in its own transaction, so one bad row
    never stops the rest. A reminder row is committed *before* its email goes
    out (two sweeps at once: the unique key lets one of them send); if every
    send fails, the row is removed again so the next sweep retries. Access
    never waits on this: status is computed from the clock on every request.
    """
    moment = now or utc_now()
    totals = {"reminders_sent": 0, "statuses_updated": 0, "plans_switched": 0}
    async with system_context(session):
        ids = (
            (
                await session.execute(
                    select(Subscription.id).where(  # type: ignore[call-overload]
                        col(Subscription.current_period_end).is_not(None)
                    )
                )
            )
            .scalars()
            .all()
        )
    await session.commit()
    for subscription_id in ids:
        try:
            await _sweep_one(session, subscription_id, moment, totals)
        except Exception:
            await session.rollback()
            logger.exception("subscription sweep failed for %s", subscription_id)
    return totals


async def _sweep_one(
    session: AsyncSession, subscription_id: uuid.UUID, moment: datetime, totals: dict[str, int]
) -> None:
    async with system_context(session):
        subscription: Subscription | None = (
            (
                await session.execute(
                    select(Subscription)
                    .where(col(Subscription.id) == subscription_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .first()
        )
        if subscription is None or subscription.current_period_end is None:
            return

        if (
            subscription.pending_plan_code is not None
            and subscription.pending_plan_effective_at is not None
            and subscription.pending_plan_effective_at <= moment
        ):
            # A renewal paid for another plan, and its month has begun.
            subscription.plan_code = subscription.pending_plan_code
            subscription.pending_plan_code = None
            subscription.pending_plan_effective_at = None
            subscription = await write_subscription(session, subscription)
            totals["plans_switched"] += 1
        else:
            status = effective_status(
                period_end=subscription.current_period_end,
                cancel_at_period_end=subscription.cancel_at_period_end,
                now=moment,
                grace_days=settings.GRACE_PERIOD_DAYS,
            )
            if status != subscription.status:
                subscription = await write_subscription(session, subscription)
                totals["statuses_updated"] += 1

        period_end = subscription.current_period_end
        assert period_end is not None
        bucket = periods.reminder_due(
            period_end=period_end,
            now=moment,
            grace_days=settings.GRACE_PERIOD_DAYS,
            cancelled=subscription.cancel_at_period_end,
        )
        reminder_id = None
        if bucket is not None:
            reminder_id = (
                await session.execute(
                    pg_insert(SubscriptionReminder)
                    .values(
                        id=new_id(),
                        subscription_id=subscription.id,
                        period_end=period_end,
                        days_before=bucket,
                    )
                    .on_conflict_do_nothing(constraint="uq_subscription_reminders_once")
                    .returning(col(SubscriptionReminder.id))
                )
            ).scalar_one_or_none()
        hospital_id, plan_code = subscription.hospital_id, subscription.plan_code
    await session.commit()
    if reminder_id is None or bucket is None:
        return

    # Committed: this sweep owns the reminder. Now tell the administrators.
    async with system_context(session):
        names = await tenancy_service.get_hospital_names(session, {hospital_id})
        admins = await users_with_role(
            session, hospital_id=hospital_id, role_code=Roles.HOSPITAL_ADMIN
        )
        delivered = False
        for admin in admins:
            email = await emails.send_lifecycle(
                session,
                to=admin,
                hospital_id=hospital_id,
                hospital_name=names.get(hospital_id, "your hospital"),
                plan_code=plan_code,
                period_end=period_end,
                bucket=bucket,
            )
            delivered = delivered or email.status == PlatformEmailStatus.SENT
        if not delivered:
            # Nobody got it (mail server down, or no administrator): forget
            # the reminder so the next sweep tries again.
            await session.execute(
                delete(SubscriptionReminder).where(col(SubscriptionReminder.id) == reminder_id)
            )
    await session.commit()
    if delivered:
        totals["reminders_sent"] += 1
