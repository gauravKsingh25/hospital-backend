"""subscriptions: when a paid-for plan change takes effect

Revision ID: a5c9e3b7d1f2
Revises: f4b8d2a6c0e1
Create Date: 2026-11-14 14:00:00.000000+00:00

An early renewal onto a different plan must not take away (or hand out) the
plan of the month already paid for. The switch is recorded with the moment
it applies — the start of the new month — and the worker sweep makes it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a5c9e3b7d1f2"
down_revision: str | None = "f4b8d2a6c0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column("pending_plan_effective_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subscriptions", "pending_plan_effective_at")
