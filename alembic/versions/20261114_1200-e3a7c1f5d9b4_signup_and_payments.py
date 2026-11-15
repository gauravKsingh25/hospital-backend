"""signup and payments: OTPs, signup intents, contact messages, UPI orders

Revision ID: e3a7c1f5d9b4
Revises: d2f6b0e4c8a3
Create Date: 2026-11-14 12:00:00.000000+00:00

Everything a hospital needs to arrive on its own: an emailed code, the
details it signed up with, a UPI payment order with its proof and receipt,
and the website's contact inbox. Platform tables, like `invites`: a person
signing up belongs to no hospital yet, so there is no tenant policy.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e3a7c1f5d9b4"
down_revision: str | None = "d2f6b0e4c8a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TS = sa.DateTime(timezone=True)


def _s(length: int) -> sqlmodel.sql.sqltypes.AutoString:
    return sqlmodel.sql.sqltypes.AutoString(length=length)


NEW_EMAIL_KINDS: tuple[str, ...] = (
    "OTP",
    "SIGNUP_EXISTS",
    "PASSWORD_RESET",
    "PAYMENT_SUBMITTED",
    "PAYMENT_ALERT",
    "PAYMENT_APPROVED",
    "PAYMENT_REJECTED",
    "CONTACT_ALERT",
)

NEW_PERMISSIONS: tuple[tuple[str, str, str], ...] = (
    ("platform:payment_read", "subscriptions", "View payments awaiting review and their proof."),
    ("platform:payment_review", "subscriptions", "Approve or reject a hospital's payment."),
)
_NEW_CODES = tuple(code for code, _, _ in NEW_PERMISSIONS)

ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "PLATFORM_ADMIN": _NEW_CODES,
    "PLATFORM_SUPPORT": ("platform:payment_read",),
}


def _base_columns() -> list[sa.Column[object]]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", _TS, server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    otp_purpose = postgresql.ENUM("SIGNUP", "RESET", name="otppurpose")
    intent_status = postgresql.ENUM(
        "STARTED", "VERIFIED", "COMPLETED", "EXPIRED", name="signupintentstatus"
    )
    order_purpose = postgresql.ENUM("NEW", "RENEWAL", "UPGRADE", name="paymentorderpurpose")
    order_status = postgresql.ENUM(
        "AWAITING_PAYMENT",
        "SUBMITTED",
        "APPROVED",
        "REJECTED",
        "EXPIRED",
        name="paymentorderstatus",
    )

    op.create_table(
        "email_otps",
        *_base_columns(),
        sa.Column("email", _s(255), nullable=False),
        sa.Column("purpose", otp_purpose, nullable=False),
        sa.Column("code_hash", _s(64), nullable=False),
        sa.Column("expires_at", _TS, nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consumed_at", _TS, nullable=True),
        sa.Column("created_ip", _s(45), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_email_otps_email", "email_otps", ["email"])
    op.create_index(
        "ix_email_otps_email_purpose_created", "email_otps", ["email", "purpose", "created_at"]
    )

    op.create_table(
        "signup_intents",
        *_base_columns(),
        sa.Column("email", _s(255), nullable=False),
        sa.Column("full_name", _s(200), nullable=False),
        sa.Column("hospital_name", _s(200), nullable=False),
        sa.Column("plan_code", _s(32), nullable=False),
        sa.Column("phone", _s(20), nullable=True),
        sa.Column("status", intent_status, nullable=False),
        sa.Column("verified_at", _TS, nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("handoff_code_hash", _s(64), nullable=True),
        sa.Column("handoff_expires_at", _TS, nullable=True),
        sa.ForeignKeyConstraint(["plan_code"], ["plans.code"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("handoff_code_hash"),
    )
    op.create_index("ix_signup_intents_email", "signup_intents", ["email"])
    op.create_index("ix_signup_intents_status", "signup_intents", ["status"])
    op.create_index("ix_signup_intents_user_id", "signup_intents", ["user_id"])

    op.create_table(
        "contact_messages",
        *_base_columns(),
        sa.Column("name", _s(200), nullable=False),
        sa.Column("email", _s(255), nullable=False),
        sa.Column("phone", _s(20), nullable=True),
        sa.Column("hospital_name", _s(200), nullable=True),
        sa.Column("message", _s(4000), nullable=False),
        sa.Column("created_ip", _s(45), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_contact_messages_email", "contact_messages", ["email"])

    op.create_table(
        "payment_orders",
        *_base_columns(),
        sa.Column("reference_code", _s(9), nullable=False),
        sa.Column("purpose", order_purpose, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("hospital_id", sa.Uuid(), nullable=True),
        sa.Column("plan_code", _s(32), nullable=False),
        sa.Column("amount_inr", sa.Integer(), nullable=False),
        sa.Column("upi_vpa", _s(100), nullable=False),
        sa.Column("payee_name", _s(100), nullable=False),
        sa.Column("upi_uri", _s(500), nullable=False),
        sa.Column("status", order_status, nullable=False),
        sa.Column("utr", _s(12), nullable=True),
        sa.Column("payer_upi_id", _s(100), nullable=True),
        sa.Column("submitted_at", _TS, nullable=True),
        sa.Column("reviewed_by_id", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", _TS, nullable=True),
        sa.Column("rejection_reason", _s(500), nullable=True),
        sa.Column("expires_at", _TS, nullable=False),
        sa.Column("hospital_name", _s(200), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["hospital_id"], ["hospitals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["plan_code"], ["plans.code"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reviewed_by_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("amount_inr >= 1", name="ck_payment_orders_amount_positive"),
    )
    op.create_index(
        "ix_payment_orders_reference_code", "payment_orders", ["reference_code"], unique=True
    )
    op.create_index("ix_payment_orders_user_id", "payment_orders", ["user_id"])
    op.create_index("ix_payment_orders_hospital_id", "payment_orders", ["hospital_id"])
    op.create_index("ix_payment_orders_reviewed_by_id", "payment_orders", ["reviewed_by_id"])
    op.create_index("ix_payment_orders_status", "payment_orders", ["status"])
    op.create_index(
        "ix_payment_orders_status_submitted_at", "payment_orders", ["status", "submitted_at"]
    )
    op.create_index(
        "uq_payment_orders_utr",
        "payment_orders",
        ["utr"],
        unique=True,
        postgresql_where=sa.text("utr IS NOT NULL"),
    )

    op.create_table(
        "payment_proofs",
        *_base_columns(),
        sa.Column("payment_order_id", sa.Uuid(), nullable=False),
        sa.Column("content_type", _s(32), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", _s(64), nullable=False),
        sa.ForeignKeyConstraint(["payment_order_id"], ["payment_orders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_payment_proofs_payment_order_id", "payment_proofs", ["payment_order_id"], unique=True
    )

    op.create_table(
        "payment_receipts",
        *_base_columns(),
        sa.Column("payment_order_id", sa.Uuid(), nullable=False),
        sa.Column("receipt_number", _s(24), nullable=False),
        sa.Column("issued_at", _TS, nullable=False),
        sa.Column("pdf", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(["payment_order_id"], ["payment_orders.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("receipt_number"),
    )
    op.create_index(
        "ix_payment_receipts_payment_order_id",
        "payment_receipts",
        ["payment_order_id"],
        unique=True,
    )
    op.execute("CREATE SEQUENCE IF NOT EXISTS payment_receipt_seq START 1")

    for kind in NEW_EMAIL_KINDS:
        op.execute(f"ALTER TYPE platformemailkind ADD VALUE IF NOT EXISTS '{kind}'")

    connection = op.get_bind()
    for code, module, description in NEW_PERMISSIONS:
        connection.execute(
            sa.text(
                """
                INSERT INTO permissions (id, code, module, description, created_at, updated_at)
                VALUES (gen_random_uuid(), :code, :module, :description, now(), now())
                ON CONFLICT (code) DO NOTHING
                """
            ),
            {"code": code, "module": module, "description": description},
        )
    for role_code, permission_codes in ROLE_PERMISSIONS.items():
        for permission_code in permission_codes:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO role_permissions (role_id, permission_id, created_at)
                    SELECT r.id, p.id, now()
                      FROM roles r, permissions p
                     WHERE r.code = :role_code
                       AND r.hospital_id IS NULL
                       AND p.code = :permission_code
                    ON CONFLICT DO NOTHING
                    """
                ),
                {"role_code": role_code, "permission_code": permission_code},
            )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            DELETE FROM role_permissions
             WHERE permission_id IN (SELECT id FROM permissions WHERE code = ANY(:codes))
            """
        ),
        {"codes": list(_NEW_CODES)},
    )
    connection.execute(
        sa.text("DELETE FROM permissions WHERE code = ANY(:codes)"), {"codes": list(_NEW_CODES)}
    )
    # The email kind labels stay (Postgres cannot drop one), but the rows that
    # use them go: the older code maps the column to a two-value enum and
    # would fail to load the email log at all. They describe signups and
    # payments whose tables this downgrade drops anyway.
    connection.execute(
        sa.text("DELETE FROM platform_emails WHERE kind::text = ANY(:kinds)"),
        {"kinds": list(NEW_EMAIL_KINDS)},
    )
    op.execute("DROP SEQUENCE IF EXISTS payment_receipt_seq")
    op.drop_table("payment_receipts")
    op.drop_table("payment_proofs")
    op.drop_table("payment_orders")
    op.drop_table("contact_messages")
    op.drop_table("signup_intents")
    op.drop_table("email_otps")
    bind = op.get_bind()
    for name in ("paymentorderstatus", "paymentorderpurpose", "signupintentstatus", "otppurpose"):
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)
