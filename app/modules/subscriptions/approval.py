"""The platform owner's decision on a payment.

Approving a new signup's payment is the moment a hospital comes into being
(spec §4 A): the hospital row, its administrator (the person who signed up
and paid), its paid subscription and the receipt are created in one
transaction, with the order row locked so two clicks — or two owners — make
one hospital. A second approval returns the first result unchanged.

Emails go out after the commit (the router sends them): a mail server that is
down must never undo a payment that was checked and accepted.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.config import settings
from app.core.database import system_context
from app.core.exceptions import ConflictError, NotFoundError
from app.core.models import utc_now
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.identity.service import (
    assign_roles,
    record_audit,
    revoke_all_refresh_tokens,
)
from app.modules.subscriptions import periods, receipts
from app.modules.subscriptions.entitlements import effective_status
from app.modules.subscriptions.features import PLAN_NAMES
from app.modules.subscriptions.models import (
    PaymentOrder,
    PaymentOrderPurpose,
    PaymentOrderStatus,
    PaymentReceipt,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.modules.subscriptions.service import write_subscription
from app.modules.tenancy import service as tenancy_service
from app.modules.tenancy.models import Hospital
from app.modules.tenancy.schemas import HospitalCreate

__all__ = ["approve", "approve_once", "get_receipt", "reject", "unique_hospital_code"]

_IST = ZoneInfo("Asia/Kolkata")
_MAX_CODE = 16


def _not_reviewable(order: PaymentOrder) -> ConflictError:
    return ConflictError(
        f"This payment is {order.status.value.lower().replace('_', ' ')} and cannot be decided.",
        code="order_not_payable",
    )


async def _locked_order(session: AsyncSession, order_id: uuid.UUID) -> PaymentOrder:
    order: PaymentOrder | None = (
        (
            await session.execute(
                select(PaymentOrder).where(col(PaymentOrder.id) == order_id).with_for_update()
            )
        )
        .scalars()
        .first()
    )
    if order is None:
        raise NotFoundError("Payment not found.", code="order_not_found")
    return order


def _code_stem(name: str) -> str:
    words: list[str] = re.findall(r"[A-Za-z]+", name)
    if not words:
        return "HOSP"
    if len(words) == 1:
        return words[0][:4].upper().ljust(2, "X")
    return "".join(word[0] for word in words)[:6].upper()


async def unique_hospital_code(session: AsyncSession, name: str) -> str:
    """`City Care Hospital` → `CCH`, then `CCH2`, `CCH3`… if taken.

    The code is printed on patient cards and cannot change, so it is short
    and made of the name the hospital chose for itself.
    """
    stem = _code_stem(name)
    async with system_context(session):
        taken = set(
            (
                await session.execute(
                    select(col(Hospital.code)).where(col(Hospital.code).startswith(stem))
                )
            )
            .scalars()
            .all()
        )
    if stem not in taken:
        return stem
    suffix = 2
    while True:
        candidate = f"{stem[: _MAX_CODE - len(str(suffix))]}{suffix}"
        if candidate not in taken:
            return candidate
        suffix += 1


async def _issue_receipt(
    session: AsyncSession,
    *,
    order: PaymentOrder,
    payer: User,
    period_start: datetime,
    period_end: datetime,
) -> PaymentReceipt:
    now = utc_now()
    sequence = (await session.execute(text("SELECT nextval('payment_receipt_seq')"))).scalar_one()
    number = f"NIR-{now.astimezone(_IST).year}-{int(sequence):06d}"
    pdf = receipts.render_pdf(
        receipt_number=number,
        issued_at=now,
        hospital_name=order.hospital_name or "",
        payer_name=payer.full_name,
        payer_email=payer.email,
        plan_name=PLAN_NAMES.get(order.plan_code, order.plan_code),
        amount_inr=order.amount_inr,
        reference_code=order.reference_code,
        utr=order.utr or "",
        period_start=period_start,
        period_end=period_end,
        seller_name="Nirogya",
        seller_contact=settings.PLATFORM_SUPPORT_CONTACT,
    )
    receipt = PaymentReceipt(
        payment_order_id=order.id, receipt_number=number, issued_at=now, pdf=pdf
    )
    session.add(receipt)
    await session.flush()
    return receipt


async def approve(session: AsyncSession, *, order_id: uuid.UUID, actor: User) -> PaymentOrder:
    """Accept a checked payment. Idempotent: a second call changes nothing."""
    order, _ = await approve_once(session, order_id=order_id, actor=actor)
    return order


async def approve_once(
    session: AsyncSession, *, order_id: uuid.UUID, actor: User
) -> tuple[PaymentOrder, bool]:
    """`approve`, also saying whether this call was the one that approved it
    — so the approval email goes out once, however many times it is clicked."""
    async with system_context(session):
        order = await _locked_order(session, order_id)
        if order.status == PaymentOrderStatus.APPROVED:
            return order, False
        if order.status != PaymentOrderStatus.SUBMITTED:
            raise _not_reviewable(order)
        if order.purpose != PaymentOrderPurpose.NEW:
            return await _approve_paid_change(session, order=order, actor=actor), True

        # Locked: one person may have two orders under review (two first
        # requests that raced), and two approvals at once must not both see
        # "no hospital yet" and make two. The second waits here, then finds it.
        payer = (
            (
                await session.execute(
                    select(User)
                    .where(col(User.id) == order.user_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .first()
        )
        if payer is None:
            raise NotFoundError("The payer's account no longer exists.", code="user_not_found")
        if payer.hospital_id is not None:
            raise ConflictError("This person already runs a hospital.", code="already_has_hospital")
        plan = await session.get(Plan, order.plan_code)
        assert plan is not None  # FK

        name = order.hospital_name or f"{payer.full_name}'s hospital"
        hospital = await tenancy_service.create_hospital(
            session,
            HospitalCreate(
                name=name,
                code=await unique_hospital_code(session, name),
                email=payer.email,
                phone=payer.phone,
            ),
        )
        payer.hospital_id = hospital.id
        payer.updated_at = utc_now()
        session.add(payer)
        await session.flush()
        await assign_roles(
            session, payer, [Roles.HOSPITAL_ADMIN], actor=actor, allow_privileged_roles=True
        )
        # The session they paid from was opened with a password alone (or the
        # signup hand-off). HOSPITAL_ADMIN may require two-step sign-in, which
        # only sign-in enforces, so that session must not carry the new role:
        # they sign in again, and enrol there. The access token still in hand
        # lapses within ACCESS_TOKEN_TTL_MINUTES.
        await revoke_all_refresh_tokens(session, payer.id, reason="role_granted")

        now = utc_now()
        subscription = await write_subscription(
            session,
            Subscription(
                hospital_id=hospital.id,
                plan_code=plan.code,
                status=SubscriptionStatus.ACTIVE,
                current_period_start=now,
                current_period_end=now + timedelta(days=plan.interval_days),
                complimentary=False,
            ),
        )

        order.hospital_id = hospital.id
        order.status = PaymentOrderStatus.APPROVED
        order.reviewed_by_id = actor.id
        order.reviewed_at = now
        order.updated_at = now
        session.add(order)
        await session.flush()
        assert subscription.current_period_end is not None
        receipt = await _issue_receipt(
            session,
            order=order,
            payer=payer,
            period_start=subscription.current_period_start,
            period_end=subscription.current_period_end,
        )

        await record_audit(
            session,
            action="hospital.create",
            actor=actor,
            hospital_id=hospital.id,
            resource_type="hospital",
            resource_id=hospital.id,
            changes={"code": hospital.code, "name": hospital.name, "via": "signup"},
        )
        await record_audit(
            session,
            action="payment.approve",
            actor=actor,
            hospital_id=hospital.id,
            resource_type="payment_order",
            resource_id=order.id,
            changes={
                "reference": order.reference_code,
                "amount_inr": order.amount_inr,
                "utr": order.utr,
                "receipt": receipt.receipt_number,
                "admin": payer.email,
            },
        )
    return order, True


async def _approve_paid_change(
    session: AsyncSession, *, order: PaymentOrder, actor: User
) -> PaymentOrder:
    """A renewal or an upgrade for a hospital that already exists.

    The amount stands as ordered: it is what the payer was shown and what the
    owner found in the bank, even if a day passed in between. A renewal runs
    from the later of the old end and now (no unpaid days are charged for); an
    upgrade switches the plan and keeps the period.
    """
    assert order.hospital_id is not None
    subscription = (
        (
            await session.execute(
                select(Subscription)
                .where(col(Subscription.hospital_id) == order.hospital_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    plan = await session.get(Plan, order.plan_code)
    payer = await session.get(User, order.user_id)
    if subscription is None or plan is None or payer is None:
        raise NotFoundError("This payment's subscription is gone.", code="subscription_not_found")

    now = utc_now()
    before = {
        "plan": subscription.plan_code,
        "period_end": subscription.current_period_end.isoformat()
        if subscription.current_period_end
        else None,
    }
    if order.purpose == PaymentOrderPurpose.RENEWAL:
        start, end = periods.renewal_period(
            current_end=subscription.current_period_end,
            approved_at=now,
            interval_days=plan.interval_days,
        )
        subscription.current_period_start = start
        subscription.current_period_end = end
        subscription.cancel_at_period_end = False
        if start > now and plan.code != subscription.plan_code:
            # Renewed early onto another plan: the month already paid for
            # keeps its plan; the switch happens when the new month starts.
            subscription.pending_plan_code = plan.code
            subscription.pending_plan_effective_at = start
        else:
            subscription.plan_code = plan.code
            subscription.pending_plan_code = None
            subscription.pending_plan_effective_at = None
        paid_from, paid_to = start, end
    else:
        # The quote assumed the period as it stood when it was raised: refuse
        # if a renewal moved it, the month lapsed, or the plan is no longer
        # below the one ordered. The owner sorts the money out by hand.
        current = await session.get(Plan, subscription.plan_code)
        status = effective_status(
            period_end=subscription.current_period_end,
            cancel_at_period_end=subscription.cancel_at_period_end,
            now=now,
            grace_days=settings.GRACE_PERIOD_DAYS,
        )
        if (
            current is None
            or plan.rank <= current.rank
            or status != SubscriptionStatus.ACTIVE
            or subscription.current_period_start > order.created_at
        ):
            raise ConflictError(
                "The hospital's subscription changed after this upgrade was priced "
                "(renewed, lapsed or already upgraded). Reject it, and settle the "
                "payment by hand.",
                code="subscription_changed",
            )
        subscription.plan_code = plan.code
        subscription.pending_plan_code = None
        subscription.pending_plan_effective_at = None
        paid_from = now
        paid_to = subscription.current_period_end or now
    subscription.complimentary = False
    subscription = await write_subscription(session, subscription)

    order.status = PaymentOrderStatus.APPROVED
    order.reviewed_by_id = actor.id
    order.reviewed_at = now
    order.updated_at = now
    session.add(order)
    await session.flush()
    receipt = await _issue_receipt(
        session, order=order, payer=payer, period_start=paid_from, period_end=paid_to
    )
    await record_audit(
        session,
        action="payment.approve",
        actor=actor,
        hospital_id=order.hospital_id,
        resource_type="payment_order",
        resource_id=order.id,
        changes={
            "purpose": order.purpose.value,
            "reference": order.reference_code,
            "amount_inr": order.amount_inr,
            "utr": order.utr,
            "receipt": receipt.receipt_number,
            "before": before,
            "after": {
                "plan": subscription.plan_code,
                "period_end": subscription.current_period_end.isoformat()
                if subscription.current_period_end
                else None,
            },
        },
    )
    return order


async def reject(
    session: AsyncSession, *, order_id: uuid.UUID, actor: User, reason: str
) -> PaymentOrder:
    """Turn a payment down with a reason the payer will read.

    The UTR is released: the payer may have mistyped it, and the right number
    must be usable on this same order when they resubmit.
    """
    async with system_context(session):
        order = await _locked_order(session, order_id)
        if order.status != PaymentOrderStatus.SUBMITTED:
            raise _not_reviewable(order)
        now = utc_now()
        rejected_utr = order.utr
        order.status = PaymentOrderStatus.REJECTED
        order.rejection_reason = reason.strip()
        order.utr = None
        order.reviewed_by_id = actor.id
        order.reviewed_at = now
        order.updated_at = now
        # A rejected order is payable again: give it a fresh window.
        order.expires_at = now + timedelta(hours=settings.PAYMENT_ORDER_TTL_HOURS)
        session.add(order)
        await session.flush()
        await record_audit(
            session,
            action="payment.reject",
            actor=actor,
            resource_type="payment_order",
            resource_id=order.id,
            changes={
                "reference": order.reference_code,
                "utr": rejected_utr,
                "reason": order.rejection_reason,
            },
        )
    return order


async def get_receipt(session: AsyncSession, order_id: uuid.UUID) -> PaymentReceipt | None:
    async with system_context(session):
        row: PaymentReceipt | None = (
            (
                await session.execute(
                    select(PaymentReceipt).where(col(PaymentReceipt.payment_order_id) == order_id)
                )
            )
            .scalars()
            .first()
        )
    return row
