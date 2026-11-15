"""Request/response shapes for `subscriptions`."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.models import utc_now
from app.modules.subscriptions.entitlements import Entitlements, effective_status
from app.modules.subscriptions.features import PLAN_NAMES, PLAN_PROFESSIONAL, FeatureKey
from app.modules.subscriptions.models import (
    PaymentOrder,
    PaymentOrderPurpose,
    PaymentOrderStatus,
    Subscription,
    SubscriptionStatus,
)

__all__ = [
    "ContactMessageRead",
    "EmailOutcome",
    "EntitlementsRead",
    "PaymentOrderCreate",
    "PaymentOrderDetail",
    "PaymentOrderRead",
    "PaymentReject",
    "PaymentSubmit",
    "PlanChange",
    "PlatformPaymentRow",
    "PlatformSubscriptionRow",
    "SubscriptionDetail",
    "SubscriptionExtend",
    "UpgradeQuote",
]


class EntitlementsRead(BaseModel):
    """A hospital's plan and what it unlocks, as the app needs it.

    Like `permissions` on `/auth/me`, this decides what to *render*. The API
    re-checks the subscription and the feature on every request.
    """

    plan_code: str
    plan_name: str
    status: SubscriptionStatus
    period_end: datetime | None
    # Whole days of the paid period left; null for a plan with no end date.
    days_left: int | None
    complimentary: bool
    features: list[str]

    @classmethod
    def unenforced(cls, entitlements: Entitlements | None) -> EntitlementsRead:
        """What the app is told while `SUBSCRIPTIONS_ENFORCED` is off.

        The API lets every hospital through and unlocks every feature; the app
        draws from this, so it has to be told the same or the escape hatch
        opens the API and leaves the screens locked.
        """
        plan_code = entitlements.plan_code if entitlements else PLAN_PROFESSIONAL
        return cls(
            plan_code=plan_code,
            plan_name=PLAN_NAMES.get(plan_code, plan_code),
            status=SubscriptionStatus.ACTIVE,
            period_end=entitlements.period_end if entitlements else None,
            days_left=None,
            complimentary=entitlements.complimentary if entitlements else True,
            features=sorted(feature.value for feature in FeatureKey),
        )

    @classmethod
    def for_hospital(cls, entitlements: Entitlements | None) -> EntitlementsRead | None:
        """A hospital user's entitlements as `/auth/me` reports them."""
        if not settings.SUBSCRIPTIONS_ENFORCED:
            return cls.unenforced(entitlements)
        return cls.from_entitlements(entitlements) if entitlements is not None else None

    @classmethod
    def from_entitlements(
        cls, entitlements: Entitlements, *, now: datetime | None = None
    ) -> EntitlementsRead:
        return cls(
            plan_code=entitlements.plan_code,
            plan_name=PLAN_NAMES.get(entitlements.plan_code, entitlements.plan_code),
            status=entitlements.status,
            period_end=entitlements.period_end,
            days_left=entitlements.days_left(now or utc_now()),
            complimentary=entitlements.complimentary,
            # A lapsed hospital's plan still lists its features in the registry;
            # what it may use right now is nothing.
            features=sorted(entitlements.features) if entitlements.usable else [],
        )


class PaymentOrderCreate(BaseModel):
    """What the payer chooses. The amount is never theirs to say."""

    purpose: PaymentOrderPurpose = PaymentOrderPurpose.NEW
    # Omitted: the plan picked at signup.
    plan_code: Annotated[str | None, Field(max_length=32)] = None


class PaymentSubmit(BaseModel):
    utr: Annotated[
        str,
        Field(
            pattern=r"^\d{12}$",
            description="The 12-digit UTR (UPI reference number) from the payer's UPI app.",
        ),
    ]
    payer_upi_id: Annotated[str | None, Field(max_length=100)] = None


class PaymentOrderRead(BaseModel):
    id: uuid.UUID
    reference_code: str
    purpose: PaymentOrderPurpose
    plan_code: str
    plan_name: str
    amount_inr: int
    status: PaymentOrderStatus
    utr: str | None
    submitted_at: datetime | None
    rejection_reason: str | None
    expires_at: datetime
    created_at: datetime

    @classmethod
    def from_order(cls, order: PaymentOrder) -> PaymentOrderRead:
        return cls(
            id=order.id,
            reference_code=order.reference_code,
            purpose=order.purpose,
            plan_code=order.plan_code,
            plan_name=PLAN_NAMES.get(order.plan_code, order.plan_code),
            amount_inr=order.amount_inr,
            status=order.status,
            utr=order.utr,
            submitted_at=order.submitted_at,
            rejection_reason=order.rejection_reason,
            expires_at=order.expires_at,
            created_at=order.created_at,
        )


class PaymentOrderDetail(PaymentOrderRead):
    """One order, with what the payment screen needs to show."""

    upi_uri: str
    upi_vpa: str
    payee_name: str
    # A standalone SVG; the app shows it as an image, never as markup.
    qr_svg: str
    review_hours: int
    has_proof: bool
    receipt_number: str | None = None


class PlatformPaymentRow(PaymentOrderRead):
    """A payment as the platform owner reviews it."""

    payer_name: str | None
    payer_email: str | None
    payer_upi_id: str | None
    hospital_name: str | None
    hospital_id: uuid.UUID | None
    reviewed_at: datetime | None
    has_proof: bool
    receipt_number: str | None


class PaymentReject(BaseModel):
    reason: Annotated[
        str, Field(min_length=5, max_length=500, description="Shown to the payer, in an email.")
    ]


class EmailOutcome(BaseModel):
    status: str
    error: str | None


class ContactMessageRead(BaseModel):
    id: uuid.UUID
    name: str
    email: str
    phone: str | None
    hospital_name: str | None
    message: str
    created_at: datetime


class SubscriptionDetail(BaseModel):
    """A hospital's subscription as its administrator manages it."""

    plan_code: str
    plan_name: str
    status: SubscriptionStatus
    current_period_start: datetime
    current_period_end: datetime | None
    pending_plan_code: str | None
    cancel_at_period_end: bool
    complimentary: bool

    @classmethod
    def from_subscription(cls, subscription: Subscription, *, now: datetime) -> SubscriptionDetail:
        return cls(
            plan_code=subscription.plan_code,
            plan_name=PLAN_NAMES.get(subscription.plan_code, subscription.plan_code),
            status=effective_status(
                period_end=subscription.current_period_end,
                cancel_at_period_end=subscription.cancel_at_period_end,
                now=now,
                grace_days=settings.GRACE_PERIOD_DAYS,
            ),
            current_period_start=subscription.current_period_start,
            current_period_end=subscription.current_period_end,
            pending_plan_code=subscription.pending_plan_code,
            cancel_at_period_end=subscription.cancel_at_period_end,
            complimentary=subscription.complimentary,
        )


class PlanChange(BaseModel):
    plan_code: Annotated[str, Field(min_length=1, max_length=32)]


class UpgradeQuote(BaseModel):
    plan_code: str
    amount_inr: int
    period_end: datetime


class PlatformSubscriptionRow(SubscriptionDetail):
    id: uuid.UUID
    hospital_id: uuid.UUID
    hospital_name: str | None


class SubscriptionExtend(BaseModel):
    days: Annotated[int, Field(ge=1, le=365)]
    reason: Annotated[str, Field(min_length=3, max_length=300)]
