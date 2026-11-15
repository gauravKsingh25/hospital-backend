"""Renewal reminders and the status sweep (slice 3, Task 4).

Reminders go to every active administrator, once per bucket per period:
7, 3 and 1 days before the end, then "in grace", then "ended". The sweep may
run any number of times, and two workers may run it at once.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.models import utc_now
from app.modules.identity.models import PlatformEmail, PlatformEmailKind, User
from app.modules.identity.rbac import Roles
from app.modules.subscriptions import lifecycle, service
from app.modules.subscriptions.models import SubscriptionStatus
from app.modules.tenancy.models import Hospital
from app.workers import tasks
from tests.conftest import _make_user
from tests.subscriptions_support import set_plan


async def _ends_in(
    session: AsyncSession, hospital: Hospital, days: float, cancel: bool = False
) -> None:
    await set_plan(
        session,
        hospital,
        "standard",
        period_end=utc_now() + timedelta(days=days),
        cancel=cancel,
    )
    await session.commit()


async def _sent(session: AsyncSession) -> list[PlatformEmail]:
    async with db.system_context(session):
        return list((await session.execute(sa.select(PlatformEmail))).scalars().all())


async def test_seven_day_reminder_goes_to_every_admin_once(
    session: AsyncSession, hospital: Hospital, hospital_admin: User
) -> None:
    second = await _make_user(session, role_code=Roles.HOSPITAL_ADMIN, hospital_id=hospital.id)
    await _make_user(session, role_code=Roles.RECEPTIONIST, hospital_id=hospital.id)
    await _ends_in(session, hospital, 6)

    result = await lifecycle.send_due_reminders(session)
    await session.commit()

    sent = await _sent(session)
    assert result["reminders_sent"] == 1
    assert {email.to_email for email in sent} == {hospital_admin.email, second.email}
    assert {email.kind for email in sent} == {PlatformEmailKind.SUBSCRIPTION_REMINDER}
    assert "7 days" in sent[0].subject


async def test_running_twice_sends_once(
    session: AsyncSession, hospital: Hospital, hospital_admin: User
) -> None:
    await _ends_in(session, hospital, 2)
    maker = db.get_sessionmaker()

    async def sweep() -> None:
        async with maker() as own:
            await lifecycle.send_due_reminders(own)
            await own.commit()

    await asyncio.gather(sweep(), sweep())
    await sweep()
    assert len(await _sent(session)) == 1


async def test_cancelled_hospital_gets_no_reminder(
    session: AsyncSession, hospital: Hospital, hospital_admin: User
) -> None:
    await _ends_in(session, hospital, 2, cancel=True)
    await lifecycle.send_due_reminders(session)
    await session.commit()
    assert await _sent(session) == []


async def test_complimentary_hospital_gets_no_reminder(
    session: AsyncSession, hospital: Hospital, hospital_admin: User
) -> None:
    # The harness's default subscription: complimentary, no end date.
    await lifecycle.send_due_reminders(session)
    await session.commit()
    assert await _sent(session) == []


@pytest.mark.parametrize(
    ("days", "kind"),
    [(-1, PlatformEmailKind.SUBSCRIPTION_GRACE), (-8, PlatformEmailKind.SUBSCRIPTION_EXPIRED)],
)
async def test_grace_and_expired_emails(
    session: AsyncSession,
    hospital: Hospital,
    hospital_admin: User,
    days: int,
    kind: PlatformEmailKind,
) -> None:
    await _ends_in(session, hospital, days)
    await lifecycle.send_due_reminders(session)
    await session.commit()
    [email] = await _sent(session)
    assert email.kind == kind
    assert email.to_email == hospital_admin.email


async def test_stored_status_follows_the_clock(
    session: AsyncSession, hospital: Hospital, hospital_admin: User
) -> None:
    await _ends_in(session, hospital, -2)
    async with db.system_context(session):
        sub = await service.get_subscription(session, hospital.id)
        assert sub is not None
        # As if written while still paid up; the clock has since moved on.
        sub.status = SubscriptionStatus.ACTIVE
        session.add(sub)
        await session.commit()

    result = await lifecycle.send_due_reminders(session)
    await session.commit()
    async with db.system_context(session):
        sub = await service.get_subscription(session, hospital.id)
        assert sub is not None
        await session.refresh(sub)
    assert sub.status == SubscriptionStatus.GRACE
    assert result["statuses_updated"] == 1


async def test_run_sweeps_includes_reminders(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake() -> dict[str, int]:
        return {"reminders_sent": 4, "statuses_updated": 1}

    monkeypatch.setattr(tasks, "sweep_subscriptions", fake)
    totals = await tasks.run_sweeps()
    assert totals["reminders_sent"] == 4
