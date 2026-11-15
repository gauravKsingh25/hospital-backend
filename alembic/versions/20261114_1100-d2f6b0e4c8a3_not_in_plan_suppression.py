"""notifications: NOT_IN_PLAN suppression reason

Revision ID: d2f6b0e4c8a3
Revises: c1e5a9d3b7f2
Create Date: 2026-11-14 11:00:00.000000+00:00

A hospital on a plan without patient messaging still has its messages
recorded, as SUPPRESSED with this reason, rather than dropped: the outbox
should say why nothing went out.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d2f6b0e4c8a3"
down_revision: str | None = "c1e5a9d3b7f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Postgres 12+ accepts this inside the migration's transaction; the value
    # is not used until a later one.
    op.execute("ALTER TYPE suppressionreason ADD VALUE IF NOT EXISTS 'NOT_IN_PLAN'")


def downgrade() -> None:
    # The label stays: Postgres cannot drop an enum value, and an unused one
    # is harmless. Rows that carry it are reworded so the older code, which
    # does not know the reason, can still read them.
    op.execute(
        "UPDATE notifications SET suppression_reason = 'DISABLED' "
        "WHERE suppression_reason = 'NOT_IN_PLAN'"
    )
