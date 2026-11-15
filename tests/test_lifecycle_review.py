"""Slice 3 review findings, each pinned by a test (C1, C2, I1 to I5, M2)."""

from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.exceptions import DomainError
from app.core.mail import MailMessage, MailResult
from app.core.models import utc_now
from app.modules.identity.models import PlatformEmail, User
from app.modules.identity.rbac import Roles
from app.modules.subscriptions import approval, emails, lifecycle, orders, service
from app.modules.subscriptions.models import (
    PaymentOrder,
    PaymentOrderPurpose,
    PaymentOrderStatus,
    Subscription,
)
from app.modules.tenancy.models import Hospital
from tests.conftest import _make_user, auth, login
from tests.payments_support import PNG
from tests.subscriptions_support import set_plan

ORDERS = "/api/v1/subscriptions/orders"
RENEWAL = PaymentOrderPurpose.RENEWAL
UPGRADE = PaymentOrderPurpose.UPGRADE
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


async def _submitted(
    session: AsyncSession, admin: User, purpose: PaymentOrderPurpose, plan: str | None, utr: str
) -> PaymentOrder:
    order = await orders.create_order(session, user=admin, plan_code=plan, purpose=purpose)
    await orders.submit_payment(session, user=admin, order_id=order.id, utr=utr, payer_upi_id=None)
    await session.commit()
    return order


# --- C1 / I2: a hospital's administrators can pay its orders -------------------
async def test_admin_submits_a_renewal_utr_over_http(
    api: AsyncClient, session: AsyncSession, hospital: Hospital, hospital_admin: User
) -> None:
    await _paid(session, hospital, "standard", -10)  # lapsed: the case that matters most
    headers = auth(await login(api, hospital_admin))
    order = (await api.post(ORDERS, json={"purpose": "RENEWAL"}, headers=headers)).json()

    uploaded = await api.put(f"{ORDERS}/{order['id']}/proof", content=PNG, headers=headers)
    assert uploaded.status_code == 204, uploaded.text
    submitted = await api.post(
        f"{ORDERS}/{order['id']}/submit", json={"utr": "610000000001"}, headers=headers
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "SUBMITTED"


async def test_another_admin_can_open_and_pay_the_hospitals_order(
    api: AsyncClient, session: AsyncSession, hospital: Hospital, hospital_admin: User
) -> None:
    await _paid(session, hospital, "standard", 3)
    deputy = await _make_user(session, role_code=Roles.HOSPITAL_ADMIN, hospital_id=hospital.id)
    first = (
        await api.post(
            ORDERS, json={"purpose": "RENEWAL"}, headers=auth(await login(api, hospital_admin))
        )
    ).json()
    headers = auth(await login(api, deputy))
    assert (await api.get(f"{ORDERS}/{first['id']}", headers=headers)).status_code == 200
    submitted = await api.post(
        f"{ORDERS}/{first['id']}/submit", json={"utr": "610000000002"}, headers=headers
    )
    assert submitted.status_code == 200, submitted.text


async def test_staff_and_other_hospitals_cannot_pay_it(
    api: AsyncClient, session: AsyncSession, hospital: Hospital, hospital_admin: User
) -> None:
    await _paid(session, hospital, "standard", 3)
    order = (
        await api.post(
            ORDERS, json={"purpose": "RENEWAL"}, headers=auth(await login(api, hospital_admin))
        )
    ).json()
    clerk = await _make_user(session, role_code=Roles.RECEPTIONIST, hospital_id=hospital.id)
    other = await _make_user(session, role_code=Roles.HOSPITAL_ADMIN, hospital_id=None)
    for user in (clerk, other):
        response = await api.post(
            f"{ORDERS}/{order['id']}/submit",
            json={"utr": "610000000003"},
            headers=auth(await login(api, user)),
        )
        assert response.status_code in (403, 404), (user.email, response.status_code)


# --- C2: one live order per hospital, re-checked at approval ----------------------
async def test_a_renewal_retires_an_unpaid_upgrade(
    session: AsyncSession, hospital: Hospital, hospital_admin: User
) -> None:
    await _paid(session, hospital, "standard", 10)
    upgrade = await orders.create_order(
        session, user=hospital_admin, plan_code="professional", purpose=UPGRADE
    )
    await orders.create_order(session, user=hospital_admin, plan_code=None, purpose=RENEWAL)
    await session.refresh(upgrade)
    assert upgrade.status == PaymentOrderStatus.EXPIRED


async def test_a_submitted_upgrade_blocks_a_renewal(
    session: AsyncSession, hospital: Hospital, hospital_admin: User
) -> None:
    await _paid(session, hospital, "standard", 10)
    await _submitted(session, hospital_admin, UPGRADE, "professional", "620000000001")
    with pytest.raises(DomainError) as caught:
        await orders.create_order(session, user=hospital_admin, plan_code=None, purpose=RENEWAL)
    assert caught.value.code == "order_under_review"


async def test_upgrade_approval_after_the_period_changed_is_refused(
    session: AsyncSession, hospital: Hospital, hospital_admin: User, platform_admin: User
) -> None:
    await _paid(session, hospital, "standard", 1)
    order = await _submitted(session, hospital_admin, UPGRADE, "professional", "620000000002")
    # As an early renewal approved meanwhile would leave it: a new period ahead.
    sub = await _sub(session, hospital)
    sub.current_period_start = sub.current_period_end or utc_now()
    sub.current_period_end = sub.current_period_start + 30 * DAY
    await service.write_subscription(session, sub)
    await session.commit()

    with pytest.raises(DomainError) as caught:
        await approval.approve(session, order_id=order.id, actor=platform_admin)
    assert caught.value.code == "subscription_changed"
    assert (await _sub(session, hospital)).plan_code == "standard"


async def test_upgrade_approval_after_the_month_lapsed_is_refused(
    session: AsyncSession, hospital: Hospital, hospital_admin: User, platform_admin: User
) -> None:
    await _paid(session, hospital, "standard", 2)
    order = await _submitted(session, hospital_admin, UPGRADE, "professional", "620000000003")
    sub = await _sub(session, hospital)
    sub.current_period_end = utc_now() - DAY
    await service.write_subscription(session, sub)
    await session.commit()
    with pytest.raises(DomainError) as caught:
        await approval.approve(session, order_id=order.id, actor=platform_admin)
    assert caught.value.code == "subscription_changed"


# --- I1: rejected orders expire; stale upgrade prices are not reused ---------------
async def test_a_rejected_order_expires_after_its_window(
    session: AsyncSession, hospital: Hospital, hospital_admin: User, platform_admin: User
) -> None:
    await _paid(session, hospital, "standard", 5)
    order = await _submitted(session, hospital_admin, RENEWAL, None, "630000000001")
    await approval.reject(session, order_id=order.id, actor=platform_admin, reason="Not found")
    await session.commit()
    order.expires_at = utc_now() - timedelta(minutes=1)
    session.add(order)
    await session.commit()

    fresh = await orders.create_order(session, user=hospital_admin, plan_code=None, purpose=RENEWAL)
    assert fresh.id != order.id
    assert await orders.expire_unpaid(session) == 0  # already retired by the new order
    await session.refresh(order)
    assert order.status == PaymentOrderStatus.EXPIRED


async def test_a_stale_upgrade_price_is_not_reused(
    session: AsyncSession, hospital: Hospital, hospital_admin: User
) -> None:
    await _paid(session, hospital, "standard", 20)
    first = await orders.create_order(
        session, user=hospital_admin, plan_code="professional", purpose=UPGRADE
    )
    await session.commit()
    sub = await _sub(session, hospital)
    sub.current_period_end = utc_now() + 5 * DAY  # days have passed
    await service.write_subscription(session, sub)
    await session.commit()
    second = await orders.create_order(
        session, user=hospital_admin, plan_code="professional", purpose=UPGRADE
    )
    assert second.id != first.id
    assert second.amount_inr < first.amount_inr


# --- I3: a downgrade waits for the paid month to end -------------------------------
async def test_early_renewal_keeps_the_paid_plan_until_the_new_period(
    api: AsyncClient,
    session: AsyncSession,
    hospital: Hospital,
    hospital_admin: User,
    platform_admin: User,
) -> None:
    await _paid(session, hospital, "professional", 5)
    old_end = (await _sub(session, hospital)).current_period_end
    assert old_end is not None
    await lifecycle.schedule_change(
        session, hospital_id=hospital.id, plan_code="standard", actor=hospital_admin
    )
    await session.commit()
    order = await _submitted(session, hospital_admin, RENEWAL, None, "640000000001")
    assert order.amount_inr == 3000
    await approval.approve(session, order_id=order.id, actor=platform_admin)
    await session.commit()

    sub = await _sub(session, hospital)
    assert sub.plan_code == "professional"
    assert sub.pending_plan_code == "standard"
    assert sub.pending_plan_effective_at == old_end
    headers = auth(await login(api, hospital_admin))
    assert (await api.get("/api/v1/ipd/wards", headers=headers)).status_code == 200

    await lifecycle.send_due_reminders(session, now=old_end - timedelta(hours=1))
    await session.commit()
    assert (await _sub(session, hospital)).plan_code == "professional"

    await lifecycle.send_due_reminders(session, now=old_end + timedelta(minutes=5))
    await session.commit()
    sub = await _sub(session, hospital)
    assert sub.plan_code == "standard"
    assert sub.pending_plan_code is None
    assert sub.pending_plan_effective_at is None


# --- I4: a lapsed hospital can renew onto a higher plan ----------------------------
async def test_a_lapsed_standard_hospital_can_renew_on_professional(
    api: AsyncClient,
    session: AsyncSession,
    hospital: Hospital,
    hospital_admin: User,
    platform_admin: User,
) -> None:
    await _paid(session, hospital, "standard", -10)
    headers = auth(await login(api, hospital_admin))
    created = await api.post(
        ORDERS, json={"purpose": "RENEWAL", "plan_code": "professional"}, headers=headers
    )
    assert created.status_code == 201, created.text
    assert created.json()["amount_inr"] == 8999
    await api.post(
        f"{ORDERS}/{created.json()['id']}/submit", json={"utr": "650000000001"}, headers=headers
    )
    await approval.approve(session, order_id=created.json()["id"], actor=platform_admin)  # type: ignore[arg-type]
    await session.commit()
    assert (await _sub(session, hospital)).plan_code == "professional"
    assert (await api.get("/api/v1/ipd/wards", headers=headers)).status_code == 200


# --- I5: reminders are committed per hospital and retried on failure ---------------
async def test_a_failed_reminder_is_sent_again_next_time(
    session: AsyncSession,
    hospital: Hospital,
    hospital_admin: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _paid(session, hospital, "standard", 2)

    class Down:
        async def send(self, message: MailMessage) -> MailResult:
            return MailResult(ok=False, error="SMTPServerDisconnected")

    monkeypatch.setattr(emails, "get_mailer", lambda: Down())
    first = await lifecycle.send_due_reminders(session)
    assert first["reminders_sent"] == 0

    class Up:
        async def send(self, message: MailMessage) -> MailResult:
            return MailResult(ok=True, message_id="<ok@test>")

    monkeypatch.setattr(emails, "get_mailer", lambda: Up())
    second = await lifecycle.send_due_reminders(session)
    assert second["reminders_sent"] == 1


async def test_one_failing_hospital_does_not_stop_the_others(
    session: AsyncSession,
    hospital: Hospital,
    hospital_admin: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _paid(session, hospital, "standard", 2)
    async with db.system_context(session):
        other = Hospital(code="OTHR", name="Other Hospital")
        session.add(other)
        await session.flush()
    await session.commit()
    await _paid(session, other, "standard", 2)
    await _make_user(session, role_code=Roles.HOSPITAL_ADMIN, hospital_id=other.id)

    real = emails.send_lifecycle
    # Read now: the sweep rolls back the failing hospital, expiring loaded rows.
    failing_id, other_id = hospital.id, other.id

    async def flaky(session: AsyncSession, **kwargs: object) -> PlatformEmail:
        if kwargs["hospital_id"] == failing_id:
            raise RuntimeError("template blew up")
        return await real(session, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(emails, "send_lifecycle", flaky)
    result = await lifecycle.send_due_reminders(session)
    assert result["reminders_sent"] == 1
    async with db.system_context(session):
        sent = (await session.execute(sa.select(PlatformEmail.hospital_id))).scalars().all()
    assert set(sent) == {other_id}
