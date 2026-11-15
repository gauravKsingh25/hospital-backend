"""UPI payment orders: raising one, taking its proof, expiring the unpaid.

An order fixes what is owed (the amount comes from `plans`, never the
request), who owes it, and the reference code the payer types or the QR
carries. The payer pays in their UPI app, then tells us the UTR (and may add a
screenshot); the platform owner checks the bank app and decides
(`approval.py`). Nothing here grants access: a submitted UTR is a claim, not
a payment.

Slice 2 raises orders for new signups only. Renewals and upgrades share the
table and arrive with slice 3.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

from sqlalchemy import and_, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import col

from app.core.config import settings
from app.core.database import system_context
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    PermissionDeniedError,
    ServiceUnavailableError,
    ValidationError,
)
from app.core.models import utc_now
from app.core.pagination import PageParams
from app.modules.identity import signup
from app.modules.identity.models import User
from app.modules.identity.service import record_audit
from app.modules.subscriptions import periods, upi
from app.modules.subscriptions import service as subscriptions_service
from app.modules.subscriptions.entitlements import effective_status
from app.modules.subscriptions.models import (
    PaymentOrder,
    PaymentOrderPurpose,
    PaymentOrderStatus,
    PaymentProof,
    Plan,
    Subscription,
    SubscriptionStatus,
)
from app.modules.tenancy import service as tenancy_service

__all__ = [
    "MAX_PROOF_BYTES",
    "REFERENCE_ALPHABET",
    "attach_proof",
    "create_order",
    "expire_unpaid",
    "get_order_for_receipt",
    "get_own_order",
    "get_payable_order",
    "has_proof",
    "list_hospital_orders",
    "list_own_orders",
    "new_reference_code",
    "submit_payment",
    "upgrade_quote",
]

# No 0/O, 1/I/L: read aloud over the phone or copied off a screen, these
# get confused. 31 symbols to the sixth is ~887 million codes.
REFERENCE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
MAX_PROOF_BYTES = 5 * 1024 * 1024
_LIVE = (
    PaymentOrderStatus.AWAITING_PAYMENT,
    PaymentOrderStatus.SUBMITTED,
    PaymentOrderStatus.REJECTED,
)
_PAYABLE = (PaymentOrderStatus.AWAITING_PAYMENT, PaymentOrderStatus.REJECTED)
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"%PDF-", "application/pdf"),
)


def _already_has_hospital() -> PermissionDeniedError:
    return PermissionDeniedError(
        "Your account already belongs to a hospital.", code="already_has_hospital"
    )


def new_reference_code() -> str:
    return "NIR" + "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(6))


def _expired_by_clock(order: PaymentOrder, now: datetime) -> bool:
    # A rejected order gets a fresh window on rejection; past it, it is dead
    # too, so a price quoted weeks ago is never paid.
    return order.status in _PAYABLE and order.expires_at <= now


async def _unique_reference(session: AsyncSession) -> str:
    for _ in range(10):
        code = new_reference_code()
        taken = (
            await session.execute(
                select(func.count())
                .select_from(PaymentOrder)
                .where(col(PaymentOrder.reference_code) == code)
            )
        ).scalar_one()
        if not taken:
            return code
    raise ServiceUnavailableError("Could not allocate a payment reference. Try again.")


async def create_order(
    session: AsyncSession,
    *,
    user: User,
    plan_code: str | None,
    purpose: PaymentOrderPurpose,
) -> PaymentOrder:
    """Raise an order, or return the matching one already in flight.

    The same plan again returns the existing unpaid (or rejected) order: a
    payer who refreshes the page must see the reference they may already
    have paid against, not a new one. Another plan retires the old order.
    A payment under review blocks a new order of the same kind.

    `NEW` is for someone who signed up and has no hospital yet. `RENEWAL` and
    `UPGRADE` are for a paying hospital; the caller has checked the person
    may manage its subscription.
    """
    if not settings.UPI_VPA:
        raise ServiceUnavailableError(
            "Payments are not set up yet. Please contact us.", code="payments_not_configured"
        )
    if purpose == PaymentOrderPurpose.NEW:
        return await _create_new(session, user=user, plan_code=plan_code)
    return await _create_for_hospital(session, user=user, plan_code=plan_code, purpose=purpose)


async def _create_new(session: AsyncSession, *, user: User, plan_code: str | None) -> PaymentOrder:
    if user.hospital_id is not None:
        raise _already_has_hospital()
    intent = await signup.latest_completed_intent(session, user.id)
    if intent is None:
        raise NotFoundError("We could not find your signup details.", code="signup_not_found")
    plan = await _active_plan(session, plan_code or intent.plan_code)

    # One order decision per person at a time: lock their user row, so two
    # first requests (a double click, a double render) cannot both find no
    # live order and both insert one.
    async with system_context(session):
        await session.execute(
            select(User.id).where(col(User.id) == user.id).with_for_update()  # type: ignore[call-overload]
        )
    return await _live_or_new(
        session,
        user=user,
        purpose=PaymentOrderPurpose.NEW,
        plan=plan,
        amount_inr=plan.price_inr,
        hospital_id=None,
        hospital_name=intent.hospital_name,
        scope=col(PaymentOrder.user_id) == user.id,
    )


def _upgrade_unavailable() -> ConflictError:
    return ConflictError(
        "An upgrade is possible only to a higher plan while the paid month is running. "
        "Renew on the higher plan instead.",
        code="upgrade_unavailable",
    )


def _complimentary() -> ConflictError:
    return ConflictError(
        "This hospital's plan is complimentary and never needs renewing.",
        code="subscription_complimentary",
    )


async def _price_upgrade(
    session: AsyncSession, subscription: Subscription, plan_code: str, now: datetime
) -> tuple[Plan, int]:
    """The plan to upgrade to and what it costs now, or `upgrade_unavailable`."""
    assert subscription.current_period_end is not None
    current = await _active_plan(session, subscription.plan_code)
    plan = await _active_plan(session, plan_code)
    status = effective_status(
        period_end=subscription.current_period_end,
        cancel_at_period_end=subscription.cancel_at_period_end,
        now=now,
        grace_days=settings.GRACE_PERIOD_DAYS,
    )
    renewed_ahead = subscription.current_period_start > now
    if plan.rank <= current.rank or status != SubscriptionStatus.ACTIVE or renewed_ahead:
        # Renewed ahead: the next month is already paid at a price; upgrade
        # once it starts, or the quote would mix two periods.
        raise _upgrade_unavailable()
    amount = periods.upgrade_amount(
        old_price=current.price_inr,
        new_price=plan.price_inr,
        now=now,
        period_end=subscription.current_period_end,
        interval_days=current.interval_days,
    )
    return plan, amount


async def _create_for_hospital(
    session: AsyncSession, *, user: User, plan_code: str | None, purpose: PaymentOrderPurpose
) -> PaymentOrder:
    hospital_id = user.hospital_id
    if hospital_id is None:
        raise PermissionDeniedError(
            "Only a hospital's administrator can renew or upgrade.", code="tenant_required"
        )
    # Locked for the same reason as a new signup's user row: one decision at a
    # time per hospital.
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
    if subscription is None or subscription.current_period_end is None:
        raise _complimentary()

    if purpose == PaymentOrderPurpose.RENEWAL:
        # The plan asked for, else a scheduled downgrade, else the current
        # one. Any plan: a lapsed hospital moves up by renewing on it (§7).
        plan = await _active_plan(
            session, plan_code or subscription.pending_plan_code or subscription.plan_code
        )
        amount = plan.price_inr
    else:
        if plan_code is None:
            raise ValidationError("Choose the plan to upgrade to.", code="plan_unknown")
        plan, amount = await _price_upgrade(session, subscription, plan_code, utc_now())

    # The caller's own hospital; read without depending on a bound tenant.
    names = await tenancy_service.get_hospital_names(session, {hospital_id})
    return await _live_or_new(
        session,
        user=user,
        purpose=purpose,
        plan=plan,
        amount_inr=amount,
        hospital_id=hospital_id,
        hospital_name=names.get(hospital_id),
        # One payment in flight per hospital, whatever it is for: a renewal
        # and an upgrade approved in either order would otherwise give away a
        # month or take back a paid upgrade.
        scope=and_(
            col(PaymentOrder.hospital_id) == hospital_id,
            col(PaymentOrder.purpose) != PaymentOrderPurpose.NEW,
        ),
    )


async def _active_plan(session: AsyncSession, code: str) -> Plan:
    plan = await session.get(Plan, code)
    if plan is None or not plan.is_active:
        raise ValidationError("That plan does not exist.", code="plan_unknown")
    return plan


async def _live_or_new(
    session: AsyncSession,
    *,
    user: User,
    purpose: PaymentOrderPurpose,
    plan: Plan,
    amount_inr: int,
    hospital_id: uuid.UUID | None,
    hospital_name: str | None,
    scope: ColumnElement[bool],
) -> PaymentOrder:
    now = utc_now()
    live = (
        (
            await session.execute(
                select(PaymentOrder)
                .where(scope, col(PaymentOrder.status).in_(_LIVE))
                .order_by(col(PaymentOrder.created_at).desc())
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for order in live:
        if order.status == PaymentOrderStatus.SUBMITTED:
            raise ConflictError(
                "Your payment is being checked. You can't start another one until it is.",
                code="order_under_review",
            )
    for order in live:
        if _expired_by_clock(order, now):
            order.status = PaymentOrderStatus.EXPIRED
        elif (
            order.plan_code == plan.code
            and order.purpose == purpose
            and order.amount_inr == amount_inr
        ):
            # Same plan, same price: the reference they may already have paid.
            return order
        else:
            order.status = PaymentOrderStatus.EXPIRED
        order.updated_at = now
        session.add(order)

    reference = await _unique_reference(session)
    vpa, payee = settings.UPI_VPA, settings.UPI_PAYEE_NAME
    order = PaymentOrder(
        reference_code=reference,
        purpose=purpose,
        user_id=user.id,
        hospital_id=hospital_id,
        plan_code=plan.code,
        amount_inr=amount_inr,
        upi_vpa=vpa,
        payee_name=payee,
        upi_uri=upi.build_uri(vpa=vpa, payee=payee, amount_inr=amount_inr, reference=reference),
        status=PaymentOrderStatus.AWAITING_PAYMENT,
        expires_at=now + timedelta(hours=settings.PAYMENT_ORDER_TTL_HOURS),
        hospital_name=hospital_name,
    )
    session.add(order)
    await session.flush()
    await record_audit(
        session,
        action="payment.order_create",
        actor=user,
        hospital_id=hospital_id,
        resource_type="payment_order",
        resource_id=order.id,
        changes={
            "reference": reference,
            "purpose": purpose.value,
            "plan": plan.code,
            "amount_inr": amount_inr,
        },
    )
    return order


async def list_hospital_orders(
    session: AsyncSession, *, hospital_id: uuid.UUID, params: PageParams
) -> tuple[list[PaymentOrder], int]:
    """A hospital's payments, newest first: its subscription history."""
    scope = col(PaymentOrder.hospital_id) == hospital_id
    total = (
        await session.execute(select(func.count()).select_from(PaymentOrder).where(scope))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(PaymentOrder)
                .where(scope)
                .order_by(col(PaymentOrder.created_at).desc())
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)


async def upgrade_quote(
    session: AsyncSession, *, hospital_id: uuid.UUID, plan_code: str
) -> tuple[int, datetime]:
    """What an upgrade would cost now, and the date the period still ends."""
    subscription = await subscriptions_service.get_subscription(session, hospital_id)
    if subscription is None or subscription.current_period_end is None:
        raise _complimentary()
    _, amount = await _price_upgrade(session, subscription, plan_code, utc_now())
    return amount, subscription.current_period_end


async def get_payable_order(
    session: AsyncSession,
    *,
    user: User,
    order_id: uuid.UUID,
    may_manage: bool,
    lock: bool = False,
) -> PaymentOrder:
    """An order this person may see and pay: one they raised, or — for a
    hospital's renewal or upgrade — any order of their hospital, if they
    manage its subscription. Everyone else gets 404."""
    statement = select(PaymentOrder).where(col(PaymentOrder.id) == order_id)
    if lock:
        statement = statement.with_for_update()
    async with system_context(session):
        order: PaymentOrder | None = (await session.execute(statement)).scalars().first()
    allowed = order is not None and (
        order.user_id == user.id
        or (may_manage and order.hospital_id is not None and order.hospital_id == user.hospital_id)
    )
    if order is None or not allowed:
        raise NotFoundError("Payment not found.", code="order_not_found")
    return order


async def get_own_order(
    session: AsyncSession, *, user: User, order_id: uuid.UUID, lock: bool = False
) -> PaymentOrder:
    statement = select(PaymentOrder).where(
        col(PaymentOrder.id) == order_id, col(PaymentOrder.user_id) == user.id
    )
    if lock:
        statement = statement.with_for_update()
    order: PaymentOrder | None = (await session.execute(statement)).scalars().first()
    if order is None:
        raise NotFoundError("Payment not found.", code="order_not_found")
    return order


async def get_order_for_receipt(
    session: AsyncSession,
    *,
    order_id: uuid.UUID,
    user_id: uuid.UUID,
    hospital_id: uuid.UUID | None,
    may_manage: bool,
) -> PaymentOrder:
    """An order whose receipt this person may download: one they paid, or one
    paid for their hospital if they manage its subscription."""
    async with system_context(session):
        order = await session.get(PaymentOrder, order_id)
    if order is None or not (
        order.user_id == user_id
        or (may_manage and hospital_id is not None and order.hospital_id == hospital_id)
    ):
        raise NotFoundError("Payment not found.", code="order_not_found")
    return order


async def list_own_orders(
    session: AsyncSession, *, user: User, params: PageParams
) -> tuple[list[PaymentOrder], int]:
    scope = col(PaymentOrder.user_id) == user.id
    total = (
        await session.execute(select(func.count()).select_from(PaymentOrder).where(scope))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(PaymentOrder)
                .where(scope)
                .order_by(col(PaymentOrder.created_at).desc())
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)


async def has_proof(session: AsyncSession, order_id: uuid.UUID) -> bool:
    count = (
        await session.execute(
            select(func.count())
            .select_from(PaymentProof)
            .where(col(PaymentProof.payment_order_id) == order_id)
        )
    ).scalar_one()
    return bool(count)


def _payable(order: PaymentOrder, now: datetime) -> None:
    if order.status not in _PAYABLE or _expired_by_clock(order, now):
        raise ConflictError(
            "This payment can no longer be updated. Start a new one if you still need to pay.",
            code="order_not_payable",
        )


def _sniff(head: bytes) -> str:
    for signature, content_type in _SIGNATURES:
        if head.startswith(signature):
            return content_type
    raise ValidationError("Upload a screenshot (PNG or JPEG) or a PDF.", code="proof_type_invalid")


async def attach_proof(
    session: AsyncSession,
    *,
    user: User,
    order_id: uuid.UUID,
    body: AsyncIterator[bytes],
    may_manage: bool = False,
) -> PaymentProof:
    """Keep the payer's screenshot, replacing any earlier one.

    The type is decided by the file's first bytes, never its name or the
    header the browser sent; the size limit is enforced while reading, so a
    lying `Content-Length` cannot get a larger file stored.
    """
    order = await get_payable_order(
        session, user=user, order_id=order_id, may_manage=may_manage, lock=True
    )
    _payable(order, utc_now())

    chunks: list[bytes] = []
    size = 0
    async for chunk in body:
        size += len(chunk)
        if size > MAX_PROOF_BYTES:
            raise PayloadTooLargeError(
                "The file is larger than 5 MB. Upload a smaller screenshot.",
                code="proof_too_large",
            )
        chunks.append(chunk)
    data = b"".join(chunks)
    content_type = _sniff(data[:8])

    proof: PaymentProof | None = (
        (
            await session.execute(
                select(PaymentProof).where(col(PaymentProof.payment_order_id) == order.id)
            )
        )
        .scalars()
        .first()
    )
    now = utc_now()
    if proof is None:
        proof = PaymentProof(
            payment_order_id=order.id,
            content_type=content_type,
            data=data,
            size_bytes=size,
            sha256=hashlib.sha256(data).hexdigest(),
        )
    else:
        proof.content_type = content_type
        proof.data = data
        proof.size_bytes = size
        proof.sha256 = hashlib.sha256(data).hexdigest()
        proof.updated_at = now
    session.add(proof)
    await session.flush()
    return proof


async def submit_payment(
    session: AsyncSession,
    *,
    user: User,
    order_id: uuid.UUID,
    utr: str,
    payer_upi_id: str | None,
    may_manage: bool = False,
) -> PaymentOrder:
    """Record the UTR; the order waits for the platform owner's check."""
    order = await get_payable_order(
        session, user=user, order_id=order_id, may_manage=may_manage, lock=True
    )
    now = utc_now()
    _payable(order, now)

    clash = (
        await session.execute(
            select(func.count())
            .select_from(PaymentOrder)
            .where(col(PaymentOrder.utr) == utr, col(PaymentOrder.id) != order.id)
        )
    ).scalar_one()
    if clash:
        raise _utr_taken()

    order.utr = utr
    order.payer_upi_id = payer_upi_id
    order.status = PaymentOrderStatus.SUBMITTED
    order.submitted_at = now
    order.rejection_reason = None
    order.updated_at = now
    session.add(order)
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        # Two orders claimed the same UTR at once; the unique index decided.
        raise _utr_taken() from exc
    await record_audit(
        session,
        action="payment.submit",
        actor=user,
        resource_type="payment_order",
        resource_id=order.id,
        changes={"reference": order.reference_code, "utr": utr},
    )
    return order


def _utr_taken() -> ConflictError:
    return ConflictError(
        "This UTR has already been used for another payment. Check the number in your UPI app.",
        code="utr_taken",
    )


async def expire_unpaid(session: AsyncSession, now: datetime | None = None) -> int:
    """Unpaid orders past their time stop being payable."""
    moment = now or utc_now()
    result = await session.execute(
        update(PaymentOrder)
        .where(
            col(PaymentOrder.status).in_(_PAYABLE),
            col(PaymentOrder.expires_at) <= moment,
        )
        .values(status=PaymentOrderStatus.EXPIRED, updated_at=moment)
    )
    await session.flush()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
