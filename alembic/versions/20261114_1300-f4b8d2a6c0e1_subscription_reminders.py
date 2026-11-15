"""subscription lifecycle: reminders sent, and their email kinds

Revision ID: f4b8d2a6c0e1
Revises: e3a7c1f5d9b4
Create Date: 2026-11-14 13:00:00.000000+00:00

One row per reminder sent for a subscription's period: 7, 3 and 1 days
before it ends, then "in grace" (0) and "ended" (-1). The unique key is what
makes the sweep safe to run twice, or on two workers at once.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4b8d2a6c0e1"
down_revision: str | None = "e3a7c1f5d9b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TS = sa.DateTime(timezone=True)
NEW_EMAIL_KINDS: tuple[str, ...] = (
    "SUBSCRIPTION_REMINDER",
    "SUBSCRIPTION_GRACE",
    "SUBSCRIPTION_EXPIRED",
)


def upgrade() -> None:
    op.create_table(
        "subscription_reminders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("subscription_id", sa.Uuid(), nullable=False),
        sa.Column("period_end", _TS, nullable=False),
        sa.Column("days_before", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subscription_id",
            "period_end",
            "days_before",
            name="uq_subscription_reminders_once",
        ),
    )
    op.create_index(
        "ix_subscription_reminders_subscription_id",
        "subscription_reminders",
        ["subscription_id"],
    )
    for kind in NEW_EMAIL_KINDS:
        op.execute(f"ALTER TYPE platformemailkind ADD VALUE IF NOT EXISTS '{kind}'")


def downgrade() -> None:
    # As in e3a7c1f5d9b4: the labels stay, the rows that use them go, so the
    # older code can still load the email log.
    op.get_bind().execute(
        sa.text("DELETE FROM platform_emails WHERE kind::text = ANY(:kinds)"),
        {"kinds": list(NEW_EMAIL_KINDS)},
    )
    op.drop_table("subscription_reminders")
