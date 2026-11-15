"""Approving renewals and upgrades; cancel, resume and extend (slice 3, Task 3)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.models import utc_now
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.subscriptions import approval, orders, service
from app.modules.subscriptions.models import (
    PaymentOrderPurpose,
    PaymentOrderStatus,
    Subscription,
)
from app.modules.tenancy.models import Hospital
from tests.conftest import _make_user, auth, login
from tests.subscriptions_support import set_plan

DAY = timedelta(days=1)


async def _paid(session: AsyncSession, hospital: Hospital, plan: str, days_left: float) -> None:
    await set_plan(session, hospital, plan, period_end=utc_now() + timedelta(days=days_left))
    await session.commit()


async def _sub(session: AsyncSession, hospital: Hospital) -> Subscription:
    async with db.system_context(session):
        sub = await service.get_subscription(session, hospital.id)
        assert sub is not None
        await session.refresh(sub)
        return sub


async def _approved(
    session: AsyncSession,
    admin: User,
    platform_admin: User,
    purpose: PaymentOrderPurpose,
    plan_code: str | None = None,
    utr: str = "500000000001",
) -> None:
    order = await orders.create_order(session, user=admin, plan_code=plan_code, purpose=purpose)
    await orders.submit_payment(session, user=admin, order_id=order.id, utr=utr, payer_upi_id=None)
    await session.commit()
    approved = await approval.approve(session, order_id=order.id, actor=platform_admin)
    assert approved.status == PaymentOrderStatus.APPROVED
    await session.commit()


async def test_renewal_in_grace_starts_at_approval_and_restores_access(
    api: AsyncClient,
    session: AsyncSession,
    hospital: Hospital,
    hospital_admin: User,
    platform_admin: User,
) -> None:
    await _paid(session, hospital, "standard", -10)  # past grace: expired
    headers = auth(await login(api, hospital_admin))
    assert (await api.get("/api/v1/patients", headers=headers)).status_code == 402

    before = utc_now()
    await _approved(session, hospital_admin, platform_admin, PaymentOrderPurpose.RENEWAL)

    sub = await _sub(session, hospital)
    assert sub.current_period_start >= before
    assert sub.current_period_end is not None
    assert sub.current_period_end - sub.current_period_start == 30 * DAY
    assert (await api.get("/api/v1/patients", headers=headers)).status_code == 200


async def test_early_renewal_appends_thirty_days(
    session: AsyncSession, hospital: Hospital, hospital_admin: User, platform_admin: User
) -> None:
    await _paid(session, hospital, "standard", 5)
    old_end = (await _sub(session, hospital)).current_period_end
    assert old_end is not None
    await _approved(session, hospital_admin, platform_admin, PaymentOrderPurpose.RENEWAL)
    sub = await _sub(session, hospital)
    assert sub.current_period_start == old_end
    assert sub.current_period_end == old_end + 30 * DAY


async def test_upgrade_switches_features_keeps_period_end(
    api: AsyncClient,
    session: AsyncSession,
    hospital: Hospital,
    hospital_admin: User,
    platform_admin: User,
) -> None:
    await _paid(session, hospital, "standard", 12)
    end = (await _sub(session, hospital)).current_period_end
    headers = auth(await login(api, hospital_admin))
    assert (await api.get("/api/v1/ipd/wards", headers=headers)).status_code == 403

    await _approved(
        session, hospital_admin, platform_admin, PaymentOrderPurpose.UPGRADE, "professional"
    )
    sub = await _sub(session, hospital)
    assert sub.plan_code == "professional"
    assert sub.current_period_end == end
    assert (await api.get("/api/v1/ipd/wards", headers=headers)).status_code == 200


async def test_late_upgrade_approval_keeps_the_ordered_amount(
    session: AsyncSession, hospital: Hospital, hospital_admin: User, platform_admin: User
) -> None:
    await _paid(session, hospital, "standard", 12)
    order = await orders.create_order(
        session, user=hospital_admin, plan_code="professional", purpose=PaymentOrderPurpose.UPGRADE
    )
    amount = order.amount_inr
    await orders.submit_payment(
        session, user=hospital_admin, order_id=order.id, utr="500000000002", payer_upi_id=None
    )
    await session.commit()
    # A day passes before the owner checks the bank app.
    sub = await _sub(session, hospital)
    sub.current_period_end = (sub.current_period_end or utc_now()) - DAY
    await service.write_subscription(session, sub)
    await session.commit()

    approved = await approval.approve(session, order_id=order.id, actor=platform_admin)
    await session.commit()
    assert approved.amount_inr == amount
    assert (await _sub(session, hospital)).plan_code == "professional"


async def test_downgrade_applies_at_a_renewal_from_grace(
    api: AsyncClient,
    session: AsyncSession,
    hospital: Hospital,
    hospital_admin: User,
    platform_admin: User,
) -> None:
    """Renewing in grace starts the new month now, so the scheduled downgrade
    applies at approval. (An early renewal waits for the new month:
    test_lifecycle_review.test_early_renewal_keeps_the_paid_plan_until_the_new_period.)"""
    await _paid(session, hospital, "professional", -2)
    headers = auth(await login(api, hospital_admin))
    await api.post(
        "/api/v1/subscriptions/change-plan", json={"plan_code": "standard"}, headers=headers
    )
    assert (await _sub(session, hospital)).plan_code == "professional"

    await _approved(session, hospital_admin, platform_admin, PaymentOrderPurpose.RENEWAL)
    sub = await _sub(session, hospital)
    assert sub.plan_code == "standard"
    assert sub.pending_plan_code is None


async def test_cancel_then_period_end_is_cancelled_without_grace(
    api: AsyncClient, session: AsyncSession, hospital: Hospital, hospital_admin: User
) -> None:
    await _paid(session, hospital, "standard", 3)
    headers = auth(await login(api, hospital_admin))
    cancelled = await api.post("/api/v1/subscriptions/cancel", headers=headers)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["cancel_at_period_end"] is True
    assert cancelled.json()["status"] == "ACTIVE"

    sub = await _sub(session, hospital)
    sub.current_period_end = utc_now() - timedelta(hours=1)
    await service.write_subscription(session, sub)
    await session.commit()
    assert (await api.get("/api/v1/patients", headers=headers)).status_code == 402


async def test_resume_clears_cancel(
    api: AsyncClient, session: AsyncSession, hospital: Hospital, hospital_admin: User
) -> None:
    await _paid(session, hospital, "standard", 3)
    headers = auth(await login(api, hospital_admin))
    await api.post("/api/v1/subscriptions/cancel", headers=headers)
    resumed = await api.post("/api/v1/subscriptions/resume", headers=headers)
    assert resumed.status_code == 200
    assert resumed.json()["cancel_at_period_end"] is False


async def test_extend_moves_period_end_and_audits(
    api: AsyncClient,
    session: AsyncSession,
    hospital: Hospital,
    platform_admin: User,
) -> None:
    await _paid(session, hospital, "standard", 2)
    sub = await _sub(session, hospital)
    old_end = sub.current_period_end
    assert old_end is not None
    response = await api.post(
        f"/api/v1/platform/subscriptions/{sub.id}/extend",
        json={"days": 7, "reason": "Bank holiday; payment delayed"},
        headers=auth(await login(api, platform_admin)),
    )
    assert response.status_code == 200, response.text
    assert (await _sub(session, hospital)).current_period_end == old_end + 7 * DAY

    listed = await api.get(
        "/api/v1/platform/subscriptions", headers=auth(await login(api, platform_admin))
    )
    row = next(r for r in listed.json()["items"] if r["hospital_id"] == str(hospital.id))
    assert row["plan_code"] == "standard"
    assert row["hospital_name"] == hospital.name


async def test_support_cannot_extend(
    session: AsyncSession, api: AsyncClient, hospital: Hospital
) -> None:
    support = await _make_user(session, role_code=Roles.PLATFORM_SUPPORT, hospital_id=None)
    sub = await _sub(session, hospital)
    response = await api.post(
        f"/api/v1/platform/subscriptions/{sub.id}/extend",
        json={"days": 7, "reason": "Trying"},
        headers=auth(await login(api, support)),
    )
    assert response.status_code == 403


@pytest.mark.parametrize("days", [0, 400])
async def test_extend_days_are_bounded(
    api: AsyncClient, session: AsyncSession, hospital: Hospital, platform_admin: User, days: int
) -> None:
    await _paid(session, hospital, "standard", 2)
    sub = await _sub(session, hospital)
    response = await api.post(
        f"/api/v1/platform/subscriptions/{sub.id}/extend",
        json={"days": days, "reason": "Bounds"},
        headers=auth(await login(api, platform_admin)),
    )
    assert response.status_code == 422


async def test_renewal_email_says_renewed_not_welcome(
    session: AsyncSession, hospital: Hospital, hospital_admin: User, platform_admin: User
) -> None:
    from sqlalchemy import select

    from app.modules.identity.models import PlatformEmail
    from app.modules.subscriptions import emails

    await _paid(session, hospital, "standard", 5)
    order = await orders.create_order(
        session, user=hospital_admin, plan_code=None, purpose=PaymentOrderPurpose.RENEWAL
    )
    await orders.submit_payment(
        session, user=hospital_admin, order_id=order.id, utr="500000000009", payer_upi_id=None
    )
    await session.commit()
    await approval.approve(session, order_id=order.id, actor=platform_admin)
    await session.commit()
    await emails.send_receipt(session, order_id=order.id, sent_by=platform_admin)
    await session.commit()
    async with db.system_context(session):
        sent = (await session.execute(select(PlatformEmail))).scalars().one()
    assert "renewed" in sent.subject.lower()
    assert "administrator" not in sent.body_text
