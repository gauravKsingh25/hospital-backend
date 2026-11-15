"""subscriptions: plans, plan features and one subscription per hospital

Revision ID: c1e5a9d3b7f2
Revises: b9d4e1f3a6c8
Create Date: 2026-11-14 10:00:00.000000+00:00

Hospitals are now on a plan. Two are seeded: Standard (OPD only) and
Professional (everything), both monthly. Every hospital that already exists
is given a complimentary Professional subscription with no end date, so
nothing that works today stops working.

Platform tables, like `hospitals` and `invites`: no tenant RLS policy.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c1e5a9d3b7f2"
down_revision: str | None = "b9d4e1f3a6c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TS = sa.DateTime(timezone=True)
_STR32 = sqlmodel.sql.sqltypes.AutoString(length=32)

# (code, name, price in rupees, days, rank)
PLANS: tuple[tuple[str, str, int, int, int], ...] = (
    ("standard", "Standard", 3000, 30, 1),
    ("professional", "Professional", 8999, 30, 2),
)

PLAN_FEATURES: dict[str, tuple[str, ...]] = {
    "standard": ("opd",),
    "professional": (
        "opd",
        "ipd",
        "diagnostics",
        "medicines",
        "accounts",
        "messaging",
        "reports_full",
    ),
}

NEW_PERMISSIONS: tuple[tuple[str, str, str], ...] = (
    ("subscription:read", "subscriptions", "View the hospital's plan, status and renewal date."),
    (
        "subscription:manage",
        "subscriptions",
        "Renew, upgrade, downgrade or cancel the hospital's plan.",
    ),
    (
        "platform:subscription_manage",
        "subscriptions",
        "Extend or override any hospital's subscription.",
    ),
)
_NEW_CODES = tuple(code for code, _, _ in NEW_PERMISSIONS)

ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "PLATFORM_ADMIN": _NEW_CODES,
    "HOSPITAL_ADMIN": ("subscription:read", "subscription:manage"),
}


def upgrade() -> None:
    status = postgresql.ENUM("ACTIVE", "GRACE", "EXPIRED", "CANCELLED", name="subscriptionstatus")

    op.create_table(
        "plans",
        sa.Column("created_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("code", _STR32, nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(length=80), nullable=False),
        sa.Column("price_inr", sa.Integer(), nullable=False),
        sa.Column("interval_days", sa.Integer(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.PrimaryKeyConstraint("code"),
        sa.CheckConstraint("price_inr >= 0", name="ck_plans_price_non_negative"),
        sa.CheckConstraint("interval_days >= 1", name="ck_plans_interval_positive"),
    )
    op.create_table(
        "plan_features",
        sa.Column("plan_code", _STR32, nullable=False),
        sa.Column("feature_key", _STR32, nullable=False),
        sa.ForeignKeyConstraint(["plan_code"], ["plans.code"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("plan_code", "feature_key"),
    )
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("hospital_id", sa.Uuid(), nullable=False),
        sa.Column("plan_code", _STR32, nullable=False),
        sa.Column("status", status, nullable=False),
        sa.Column("current_period_start", _TS, nullable=False),
        sa.Column("current_period_end", _TS, nullable=True),
        sa.Column("pending_plan_code", _STR32, nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("complimentary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["hospital_id"], ["hospitals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plan_code"], ["plans.code"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["pending_plan_code"], ["plans.code"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_subscriptions_hospital_id", "subscriptions", ["hospital_id"], unique=True)
    op.create_index("ix_subscriptions_plan_code", "subscriptions", ["plan_code"])
    op.create_index("ix_subscriptions_status", "subscriptions", ["status"])
    op.create_index("ix_subscriptions_current_period_end", "subscriptions", ["current_period_end"])

    connection = op.get_bind()
    for code, name, price, days, rank in PLANS:
        connection.execute(
            sa.text(
                """
                INSERT INTO plans (code, name, price_inr, interval_days, rank, is_active)
                VALUES (:code, :name, :price, :days, :rank, true)
                """
            ),
            {"code": code, "name": name, "price": price, "days": days, "rank": rank},
        )
    for plan_code, features in PLAN_FEATURES.items():
        for feature in features:
            connection.execute(
                sa.text("INSERT INTO plan_features (plan_code, feature_key) VALUES (:p, :f)"),
                {"p": plan_code, "f": feature},
            )

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

    # Nothing that works today stops working.
    connection.execute(
        sa.text(
            """
            INSERT INTO subscriptions (id, hospital_id, plan_code, status,
                                       current_period_start, current_period_end,
                                       complimentary, cancel_at_period_end,
                                       created_at, updated_at)
            SELECT gen_random_uuid(), h.id, 'professional', 'ACTIVE', now(), NULL,
                   true, false, now(), now()
              FROM hospitals h
             WHERE NOT EXISTS (SELECT 1 FROM subscriptions s WHERE s.hospital_id = h.id)
            """
        )
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
    op.drop_table("subscriptions")
    op.drop_table("plan_features")
    op.drop_table("plans")
    postgresql.ENUM(name="subscriptionstatus").drop(op.get_bind(), checkfirst=True)
