"""Every email about a payment, sent after the change it announces is committed.

Each is logged in `platform_emails` like an invite, so "did the receipt go
out?" is answered from the console. A failed send is recorded and can be
retried; it never undoes the payment decision it was announcing.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.config import settings
from app.core.database import system_context
from app.core.exceptions import NotFoundError
from app.core.mail import MailAttachment, MailMessage, get_mailer
from app.core.models import utc_now
from app.modules.identity.email_templates import format_expiry
from app.modules.identity.models import (
    PlatformEmail,
    PlatformEmailKind,
    PlatformEmailStatus,
    User,
)
from app.modules.subscriptions.features import PLAN_NAMES
from app.modules.subscriptions.models import PaymentOrder, PaymentOrderPurpose, PaymentReceipt

__all__ = [
    "send_approved",
    "send_lifecycle",
    "send_receipt",
    "send_rejected",
    "send_rejected_for",
    "send_submitted",
]


def _rupees(amount: int) -> str:
    """₹8,999 — Indian grouping is the same as Western below a lakh."""
    if amount < 100_000:
        return f"₹{amount:,}"
    head, tail = str(amount)[:-3], str(amount)[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    groups.insert(0, head)
    return "₹" + ",".join(groups) + "," + tail


def _plan(order: PaymentOrder) -> str:
    return PLAN_NAMES.get(order.plan_code, order.plan_code)


async def _send(
    session: AsyncSession,
    *,
    to: str,
    kind: PlatformEmailKind,
    subject: str,
    text: str,
    hospital_id: uuid.UUID | None,
    sent_by: User | None,
    attachments: tuple[MailAttachment, ...] = (),
) -> PlatformEmail:
    result = await get_mailer().send(
        MailMessage(to=to, subject=subject, text=text, attachments=attachments)
    )
    email = PlatformEmail(
        kind=kind,
        to_email=to,
        from_email=settings.MAIL_FROM,
        subject=subject,
        body_text=text,
        hospital_id=hospital_id,
        status=PlatformEmailStatus.SENT if result.ok else PlatformEmailStatus.FAILED,
        error=None if result.ok else result.error,
        provider_message_id=result.message_id,
        sent_by_id=sent_by.id if sent_by else None,
        sent_at=utc_now(),
    )
    session.add(email)
    await session.flush()
    return email


async def send_submitted(session: AsyncSession, *, order: PaymentOrder, payer: User) -> None:
    """To the payer: we have it. To the inbox: a payment is waiting."""
    amount = _rupees(order.amount_inr)
    hours = settings.PAYMENT_REVIEW_HOURS
    await _send(
        session,
        to=payer.email,
        kind=PlatformEmailKind.PAYMENT_SUBMITTED,
        subject=f"We received your payment details — {order.reference_code}",
        text=(
            f"Hello {payer.full_name},\n\n"
            f"Thank you. We have your payment details for the {_plan(order)} plan:\n\n"
            f"  Reference: {order.reference_code}\n"
            f"  Amount:    {amount}\n"
            f"  UTR:       {order.utr}\n\n"
            f"We check every payment by hand against our bank account, usually within "
            f"{hours} hours. You'll get an email as soon as it's confirmed, and your "
            f"hospital account will be ready to use.\n\n"
            f"Questions? Write to {settings.PLATFORM_SUPPORT_CONTACT}.\n"
        ),
        hospital_id=None,
        sent_by=payer,
    )
    await _send(
        session,
        to=settings.contact_inbox,
        kind=PlatformEmailKind.PAYMENT_ALERT,
        subject=f"Payment to check: {amount} — {order.reference_code}",
        text=(
            f"A payment is waiting for review.\n\n"
            f"  Reference: {order.reference_code}\n"
            f"  Amount:    {amount}\n"
            f"  UTR:       {order.utr}\n"
            f"  Payer:     {payer.full_name} <{payer.email}>\n"
            f"  Hospital:  {order.hospital_name or '—'}\n"
            f"  Plan:      {_plan(order)}\n\n"
            f"Check the bank app for this amount and UTR, then approve or reject it:\n"
            f"{settings.APP_PUBLIC_URL}/platform/payments\n"
        ),
        hospital_id=None,
        sent_by=payer,
    )


async def send_approved(
    session: AsyncSession,
    *,
    order: PaymentOrder,
    payer: User,
    receipt: PaymentReceipt,
    sent_by: User | None,
) -> PlatformEmail:
    """To the payer: confirmed, with the receipt attached. Worded by purpose."""
    hospital = order.hospital_name or "your hospital"
    paid = f"Your payment of {_rupees(order.amount_inr)} ({order.reference_code}) is confirmed."
    if order.purpose == PaymentOrderPurpose.RENEWAL:
        subject = f"Subscription renewed — {hospital}"
        what = f"{hospital} is renewed on the {_plan(order)} plan."
        next_step = (
            f"Nothing else to do. Your subscription: {settings.APP_PUBLIC_URL}/admin/subscription"
        )
    elif order.purpose == PaymentOrderPurpose.UPGRADE:
        subject = f"Upgraded to {_plan(order)} — {hospital}"
        what = f"{hospital} is now on the {_plan(order)} plan. The new features are on now."
        next_step = f"Your subscription: {settings.APP_PUBLIC_URL}/admin/subscription"
    else:
        subject = f"Payment confirmed — {hospital} is ready"
        what = f"{hospital} is now on the {_plan(order)} plan, and you are its administrator."
        next_step = f"Sign in to finish setting up your hospital:\n{settings.APP_PUBLIC_URL}/login"
    return await _send(
        session,
        to=payer.email,
        kind=PlatformEmailKind.PAYMENT_APPROVED,
        subject=subject,
        text=(
            f"Hello {payer.full_name},\n\n{paid} {what}\n\n{next_step}\n\n"
            f"Your receipt, {receipt.receipt_number}, is attached. The amount includes GST.\n\n"
            f"Questions? Write to {settings.PLATFORM_SUPPORT_CONTACT}.\n"
        ),
        hospital_id=order.hospital_id,
        sent_by=sent_by,
        attachments=(
            MailAttachment(
                filename=f"{receipt.receipt_number}.pdf",
                content_type="application/pdf",
                data=receipt.pdf,
            ),
        ),
    )


async def send_rejected(
    session: AsyncSession, *, order: PaymentOrder, payer: User, sent_by: User
) -> None:
    """To the payer: why, and how to put it right."""
    await _send(
        session,
        to=payer.email,
        kind=PlatformEmailKind.PAYMENT_REJECTED,
        subject=f"We couldn't confirm your payment — {order.reference_code}",
        text=(
            f"Hello {payer.full_name},\n\n"
            f"We couldn't match your payment for {order.reference_code} "
            f"({_rupees(order.amount_inr)}) in our bank account.\n\n"
            f"Reason: {order.rejection_reason}\n\n"
            f"Sign in to check the UTR and send it again:\n"
            f"{settings.APP_PUBLIC_URL}/get-started\n\n"
            f"If you've been charged and think this is a mistake, reply with a screenshot "
            f"from your UPI app, or write to {settings.PLATFORM_SUPPORT_CONTACT}.\n"
        ),
        hospital_id=None,
        sent_by=sent_by,
    )


async def send_receipt(
    session: AsyncSession, *, order_id: uuid.UUID, sent_by: User | None
) -> PlatformEmail:
    """The approval email with its receipt: sent on approval, and again on request."""
    async with system_context(session):
        order = await session.get(PaymentOrder, order_id)
        receipt: PaymentReceipt | None = (
            (
                await session.execute(
                    select(PaymentReceipt).where(col(PaymentReceipt.payment_order_id) == order_id)
                )
            )
            .scalars()
            .first()
        )
        payer = await session.get(User, order.user_id) if order else None
        if order is None or receipt is None or payer is None:
            raise NotFoundError("There is no receipt for this payment.", code="receipt_not_found")
        return await send_approved(
            session, order=order, payer=payer, receipt=receipt, sent_by=sent_by
        )


async def send_rejected_for(session: AsyncSession, *, order_id: uuid.UUID, sent_by: User) -> None:
    async with system_context(session):
        order = await session.get(PaymentOrder, order_id)
        payer = await session.get(User, order.user_id) if order else None
        if order is None or payer is None:
            raise NotFoundError("Payment not found.", code="order_not_found")
        await send_rejected(session, order=order, payer=payer, sent_by=sent_by)


async def send_lifecycle(
    session: AsyncSession,
    *,
    to: User,
    hospital_id: uuid.UUID,
    hospital_name: str,
    plan_code: str,
    period_end: datetime,
    bucket: int,
) -> PlatformEmail:
    """A reminder before the month ends, the grace notice, or the "ended" notice."""
    plan = PLAN_NAMES.get(plan_code, plan_code)
    link = f"{settings.APP_PUBLIC_URL}/admin/subscription"
    ends = format_expiry(period_end)
    if bucket > 0:
        days = "1 day" if bucket == 1 else f"{bucket} days"
        kind = PlatformEmailKind.SUBSCRIPTION_REMINDER
        subject = f"{hospital_name}: your Nirogya plan ends in {days}"
        lead = (
            f"{hospital_name}'s {plan} plan is paid until {ends}. Renew now so nobody at "
            f"the hospital is interrupted. Renewing early adds a full month from that date."
        )
    elif bucket == 0:
        grace_end = format_expiry(period_end + timedelta(days=settings.GRACE_PERIOD_DAYS))
        kind = PlatformEmailKind.SUBSCRIPTION_GRACE
        subject = f"{hospital_name}: your Nirogya plan has ended — renew by {grace_end}"
        lead = (
            f"{hospital_name}'s {plan} plan ended on {ends}. Everything keeps working until "
            f"{grace_end}; after that only the subscription page opens until you renew."
        )
    else:
        kind = PlatformEmailKind.SUBSCRIPTION_EXPIRED
        subject = f"{hospital_name}: Nirogya is paused until you renew"
        lead = (
            f"{hospital_name}'s {plan} plan has ended and the grace period is over. Your "
            f"records are kept safe; renew to open the app again."
        )
    return await _send(
        session,
        to=to.email,
        kind=kind,
        subject=subject,
        text=(
            f"Hello {to.full_name},\n\n{lead}\n\nRenew here:\n{link}\n\n"
            f"Questions? Write to {settings.PLATFORM_SUPPORT_CONTACT}.\n"
        ),
        hospital_id=hospital_id,
        sent_by=None,
    )
