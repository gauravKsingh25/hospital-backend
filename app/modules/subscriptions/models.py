"""Plans and hospital subscriptions.

Platform tables: no `hospital_id` row-level-security policy, like `hospitals`
and `invites`. A subscription is read to decide whether a hospital may act at
all, which happens before there is a tenant context to filter by.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, LargeBinary, UniqueConstraint, text
from sqlmodel import Field, SQLModel

from app.core.models import IdentifiedModel, TimestampMixin

__all__ = [
    "PaymentOrder",
    "PaymentOrderPurpose",
    "PaymentOrderStatus",
    "PaymentProof",
    "PaymentReceipt",
    "Plan",
    "PlanFeature",
    "Subscription",
    "SubscriptionReminder",
    "SubscriptionStatus",
]


class Plan(TimestampMixin, table=True):
    """Something a hospital can be on. Prices are whole rupees, GST included."""

    __tablename__ = "plans"

    code: str = Field(primary_key=True, max_length=32)
    name: str = Field(max_length=80)
    price_inr: int = Field(ge=0)
    interval_days: int = Field(ge=1)
    # Orders the plans: a higher rank is an upgrade.
    rank: int = Field(default=0)
    is_active: bool = Field(default=True)


class PlanFeature(SQLModel, table=True):
    """A feature a plan includes. Presence is the grant."""

    __tablename__ = "plan_features"

    plan_code: str = Field(
        primary_key=True, foreign_key="plans.code", ondelete="CASCADE", max_length=32
    )
    feature_key: str = Field(primary_key=True, max_length=32)


class SubscriptionStatus(enum.StrEnum):
    ACTIVE = "ACTIVE"
    # Period ended unpaid; full access for the grace window.
    GRACE = "GRACE"
    EXPIRED = "EXPIRED"
    # The hospital chose to stop; the paid period has run out.
    CANCELLED = "CANCELLED"


class Subscription(IdentifiedModel, table=True):
    """A hospital's plan and paid period. One per hospital.

    `status` is a stored hint for lists and reports. Whether a hospital may
    act is always computed from the period and the clock
    (`entitlements.effective_status`), so it cannot go stale between sweeps.
    """

    __tablename__ = "subscriptions"

    hospital_id: uuid.UUID = Field(
        foreign_key="hospitals.id", ondelete="CASCADE", unique=True, index=True
    )
    plan_code: str = Field(foreign_key="plans.code", ondelete="RESTRICT", max_length=32, index=True)
    status: SubscriptionStatus = Field(default=SubscriptionStatus.ACTIVE, index=True)
    current_period_start: datetime = Field(
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    # NULL: complimentary, never lapses.
    current_period_end: datetime | None = Field(
        default=None,
        index=True,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    # A downgrade waiting for the next renewal.
    pending_plan_code: str | None = Field(
        default=None, foreign_key="plans.code", ondelete="RESTRICT", max_length=32
    )
    cancel_at_period_end: bool = Field(default=False)
    complimentary: bool = Field(default=False)
    # Set when a renewal has been paid for a different plan, starting at the
    # end of the month already paid: the plan switches then, not at approval.
    # NULL with a `pending_plan_code` is a downgrade waiting for a renewal.
    pending_plan_effective_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )


# ---------------------------------------------------------------------------
# Paying by UPI (spec §6). Money is whole rupees; the amount is copied from
# `plans` when the order is raised, never taken from the request.
# ---------------------------------------------------------------------------
class PaymentOrderPurpose(enum.StrEnum):
    NEW = "NEW"
    RENEWAL = "RENEWAL"
    UPGRADE = "UPGRADE"


class PaymentOrderStatus(enum.StrEnum):
    AWAITING_PAYMENT = "AWAITING_PAYMENT"
    # A UTR is in; the platform owner checks the bank app and decides.
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    # The payer is told why and may resubmit on the same order.
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class PaymentOrder(IdentifiedModel, table=True):
    """One request to pay one amount, identified by its reference code."""

    __tablename__ = "payment_orders"
    __table_args__ = (
        Index("ix_payment_orders_status_submitted_at", "status", "submitted_at"),
        # A UTR identifies one bank transfer: it can settle one order only.
        Index(
            "uq_payment_orders_utr",
            "utr",
            unique=True,
            postgresql_where=text("utr IS NOT NULL"),
        ),
    )

    reference_code: str = Field(max_length=9, unique=True, index=True)
    purpose: PaymentOrderPurpose
    user_id: uuid.UUID = Field(foreign_key="users.id", ondelete="RESTRICT", index=True)
    # NULL for a NEW order until it is approved and the hospital exists.
    hospital_id: uuid.UUID | None = Field(
        default=None, foreign_key="hospitals.id", ondelete="RESTRICT", nullable=True, index=True
    )
    plan_code: str = Field(foreign_key="plans.code", ondelete="RESTRICT", max_length=32)
    amount_inr: int = Field(ge=1)
    # Snapshots: what the payer was shown, whatever the settings say later.
    upi_vpa: str = Field(max_length=100)
    payee_name: str = Field(max_length=100)
    upi_uri: str = Field(max_length=500)
    status: PaymentOrderStatus = Field(default=PaymentOrderStatus.AWAITING_PAYMENT, index=True)
    utr: str | None = Field(default=None, max_length=12)
    payer_upi_id: str | None = Field(default=None, max_length=100)
    submitted_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    reviewed_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="users.id", ondelete="RESTRICT", nullable=True, index=True
    )
    reviewed_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),  # type: ignore[call-overload]
    )
    rejection_reason: str | None = Field(default=None, max_length=500)
    expires_at: datetime = Field(sa_type=DateTime(timezone=True))  # type: ignore[call-overload]
    hospital_name: str | None = Field(default=None, max_length=200)


class PaymentProof(IdentifiedModel, table=True):
    """The payer's screenshot. Apart from the order so lists never load it."""

    __tablename__ = "payment_proofs"

    payment_order_id: uuid.UUID = Field(
        foreign_key="payment_orders.id", ondelete="CASCADE", unique=True, index=True
    )
    content_type: str = Field(max_length=32)
    data: bytes = Field(sa_type=LargeBinary)
    size_bytes: int
    sha256: str = Field(max_length=64)


class PaymentReceipt(IdentifiedModel, table=True):
    """The receipt for an approved order, its PDF kept as issued."""

    __tablename__ = "payment_receipts"

    payment_order_id: uuid.UUID = Field(
        foreign_key="payment_orders.id", ondelete="RESTRICT", unique=True, index=True
    )
    receipt_number: str = Field(max_length=24, unique=True)
    issued_at: datetime = Field(sa_type=DateTime(timezone=True))  # type: ignore[call-overload]
    pdf: bytes = Field(sa_type=LargeBinary)


class SubscriptionReminder(IdentifiedModel, table=True):
    """A reminder already sent for one period: the sweep's memory.

    `days_before` is 7, 3 or 1 before the period ends, 0 for "in grace" and
    -1 for "ended". The unique key makes sending idempotent.
    """

    __tablename__ = "subscription_reminders"
    __table_args__ = (
        UniqueConstraint(
            "subscription_id", "period_end", "days_before", name="uq_subscription_reminders_once"
        ),
    )

    subscription_id: uuid.UUID = Field(
        foreign_key="subscriptions.id", ondelete="CASCADE", index=True
    )
    period_end: datetime = Field(sa_type=DateTime(timezone=True))  # type: ignore[call-overload]
    days_before: int
