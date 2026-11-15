"""The platform owner approves a payment, and a hospital comes into being.

These tests create hospitals through approval, so they opt out of the test
harness's automatic subscription: approval writes the subscription itself.
"""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.exceptions import DomainError
from app.core.mail import MailMessage, MailResult
from app.modules.identity.models import (
    PlatformEmail,
    PlatformEmailKind,
    PlatformEmailStatus,
    Role,
    User,
    UserRole,
)
from app.modules.subscriptions import approval, emails
from app.modules.subscriptions.models import (
    PaymentOrder,
    PaymentOrderStatus,
    PaymentReceipt,
    Subscription,
)
from app.modules.tenancy.models import Hospital
from tests.payments_support import pending_user, submitted_order

pytestmark = pytest.mark.no_auto_subscription


async def _roles(session: AsyncSession, user: User) -> set[str]:
    async with db.system_context(session):
        rows = await session.execute(
            sa.select(Role.code)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user.id)
        )
        return set(rows.scalars().all())


async def _count(session: AsyncSession, model: type) -> int:
    async with db.system_context(session):
        return int(
            (await session.execute(sa.select(sa.func.count()).select_from(model))).scalar_one()
        )


async def test_approve_new_creates_hospital_admin_subscription_and_receipt(
    session: AsyncSession, platform_admin: User
) -> None:
    payer = await pending_user(session, plan_code="standard", hospital_name="City Care Hospital")
    order = await submitted_order(session, payer)

    approved = await approval.approve(session, order_id=order.id, actor=platform_admin)
    await session.commit()

    assert approved.status == PaymentOrderStatus.APPROVED
    assert approved.reviewed_by_id == platform_admin.id
    async with db.system_context(session):
        await session.refresh(payer)
        hospital = await session.get(Hospital, payer.hospital_id)
        sub = (
            (
                await session.execute(
                    sa.select(Subscription).where(Subscription.hospital_id == hospital.id)
                )
            )  # type: ignore[union-attr]
            .scalars()
            .one()
        )
        receipt = (await session.execute(sa.select(PaymentReceipt))).scalars().one()
    assert hospital is not None
    assert hospital.name == "City Care Hospital"
    assert hospital.code == "CCH"
    assert approved.hospital_id == hospital.id
    assert await _roles(session, payer) == {"HOSPITAL_ADMIN"}
    assert sub.plan_code == "standard"
    assert sub.complimentary is False
    assert sub.current_period_end is not None
    assert sub.current_period_end - sub.current_period_start == timedelta(days=30)
    assert re.fullmatch(r"NIR-\d{4}-\d{6}", receipt.receipt_number)
    assert receipt.pdf.startswith(b"%PDF-")


async def test_double_approve_is_idempotent(session: AsyncSession, platform_admin: User) -> None:
    order = await submitted_order(session, await pending_user(session))
    await approval.approve(session, order_id=order.id, actor=platform_admin)
    await session.commit()
    again = await approval.approve(session, order_id=order.id, actor=platform_admin)
    await session.commit()
    assert again.status == PaymentOrderStatus.APPROVED
    assert await _count(session, Hospital) == 1
    assert await _count(session, PaymentReceipt) == 1


async def test_concurrent_approve_creates_one_hospital(
    session: AsyncSession, platform_admin: User
) -> None:
    order = await submitted_order(session, await pending_user(session))
    maker = db.get_sessionmaker()

    async def approve_once() -> PaymentOrderStatus:
        async with maker() as own:
            result = await approval.approve(own, order_id=order.id, actor=platform_admin)
            await own.commit()
            return result.status

    statuses = await asyncio.gather(approve_once(), approve_once())
    assert statuses == [PaymentOrderStatus.APPROVED, PaymentOrderStatus.APPROVED]
    assert await _count(session, Hospital) == 1
    assert await _count(session, Subscription) == 1
    assert await _count(session, PaymentReceipt) == 1


async def test_hospital_code_is_unique(session: AsyncSession, platform_admin: User) -> None:
    codes = []
    for utr in ("100000000001", "100000000002"):
        payer = await pending_user(session, hospital_name="City Care Hospital")
        order = await submitted_order(session, payer, utr=utr)
        approved = await approval.approve(session, order_id=order.id, actor=platform_admin)
        await session.commit()
        async with db.system_context(session):
            hospital = await session.get(Hospital, approved.hospital_id)
        assert hospital is not None
        codes.append(hospital.code)
    assert codes == ["CCH", "CCH2"]


@pytest.mark.parametrize(
    ("name", "stem"),
    [("Sunrise", "SUNR"), ("Dr. Mehta's Eye & ENT Clinic", "DMSEEC"), ("123", "HOSP"), ("A", "AX")],
)
def test_code_stem(name: str, stem: str) -> None:
    assert approval._code_stem(name) == stem


async def test_reject_frees_the_utr_and_records_reason(
    session: AsyncSession, platform_admin: User
) -> None:
    order = await submitted_order(session, await pending_user(session), utr="555566667777")
    rejected = await approval.reject(
        session, order_id=order.id, actor=platform_admin, reason="No payment with this UTR"
    )
    await session.commit()
    assert rejected.status == PaymentOrderStatus.REJECTED
    assert rejected.rejection_reason == "No payment with this UTR"
    assert rejected.utr is None
    assert await _count(session, Hospital) == 0


async def test_cannot_approve_an_awaiting_order(
    session: AsyncSession, platform_admin: User
) -> None:
    from app.modules.subscriptions import orders
    from app.modules.subscriptions.models import PaymentOrderPurpose

    payer = await pending_user(session)
    order = await orders.create_order(
        session, user=payer, plan_code=None, purpose=PaymentOrderPurpose.NEW
    )
    await session.commit()
    with pytest.raises(DomainError) as caught:
        await approval.approve(session, order_id=order.id, actor=platform_admin)
    assert caught.value.code == "order_not_payable"


async def test_failed_approval_email_does_not_undo_approval(
    session: AsyncSession, platform_admin: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Down:
        async def send(self, message: MailMessage) -> MailResult:
            return MailResult(ok=False, error="SMTPServerDisconnected: down")

    class Up:
        def __init__(self) -> None:
            self.sent: list[MailMessage] = []

        async def send(self, message: MailMessage) -> MailResult:
            self.sent.append(message)
            return MailResult(ok=True, message_id="<ok@test>")

    payer = await pending_user(session)
    order = await submitted_order(session, payer)
    await approval.approve(session, order_id=order.id, actor=platform_admin)
    await session.commit()

    monkeypatch.setattr(emails, "get_mailer", lambda: Down())
    first = await emails.send_receipt(session, order_id=order.id, sent_by=platform_admin)
    await session.commit()
    assert first.status == PlatformEmailStatus.FAILED
    async with db.system_context(session):
        assert (await session.get(PaymentOrder, order.id)).status == PaymentOrderStatus.APPROVED  # type: ignore[union-attr]

    up = Up()
    monkeypatch.setattr(emails, "get_mailer", lambda: up)
    second = await emails.send_receipt(session, order_id=order.id, sent_by=platform_admin)
    await session.commit()
    assert second.status == PlatformEmailStatus.SENT
    assert second.kind == PlatformEmailKind.PAYMENT_APPROVED
    attachment = up.sent[0].attachments[0]
    assert attachment.content_type == "application/pdf"
    assert attachment.data.startswith(b"%PDF-")
    async with db.system_context(session):
        logged = (await session.execute(sa.select(PlatformEmail))).scalars().all()
    assert {email.status for email in logged} == {
        PlatformEmailStatus.FAILED,
        PlatformEmailStatus.SENT,
    }


async def test_two_orders_for_one_payer_approved_at_once_make_one_hospital(
    session: AsyncSession, platform_admin: User
) -> None:
    """Two SUBMITTED orders for one person (e.g. two first POSTs that raced),
    approved by two owners at the same moment: one hospital, not two."""
    from app.modules.subscriptions.models import PaymentOrderPurpose

    payer = await pending_user(session)
    first = await submitted_order(session, payer, utr="900000000001")
    second = PaymentOrder(
        reference_code="NIRZZZZZ2",
        purpose=PaymentOrderPurpose.NEW,
        user_id=payer.id,
        plan_code="standard",
        amount_inr=3000,
        upi_vpa=first.upi_vpa,
        payee_name=first.payee_name,
        upi_uri=first.upi_uri,
        status=PaymentOrderStatus.SUBMITTED,
        utr="900000000002",
        expires_at=first.expires_at,
        hospital_name=first.hospital_name,
    )
    session.add(second)
    await session.commit()
    maker = db.get_sessionmaker()

    async def approve(order_id: object) -> str:
        async with maker() as own:
            try:
                await approval.approve(own, order_id=order_id, actor=platform_admin)  # type: ignore[arg-type]
                await own.commit()
                return "approved"
            except DomainError as error:
                await own.rollback()
                return error.code

    outcomes = sorted(await asyncio.gather(approve(first.id), approve(second.id)))
    assert outcomes == ["already_has_hospital", "approved"]
    assert await _count(session, Hospital) == 1
    assert await _count(session, Subscription) == 1


async def test_racing_first_orders_create_one_order(session: AsyncSession) -> None:
    """Two first POSTs to /subscriptions/orders at once (a double render or a
    double click) leave the payer with one order, not two."""
    from app.modules.subscriptions import orders
    from app.modules.subscriptions.models import PaymentOrderPurpose

    payer = await pending_user(session)
    maker = db.get_sessionmaker()

    async def create() -> object:
        async with maker() as own:
            order = await orders.create_order(
                own, user=payer, plan_code=None, purpose=PaymentOrderPurpose.NEW
            )
            await own.commit()
            return order.id

    ids = await asyncio.gather(create(), create())
    assert ids[0] == ids[1]
    assert await _count(session, PaymentOrder) == 1
