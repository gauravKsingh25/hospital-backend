"""billing read scope, and the accountant records payments

Revision ID: b9d4e1f3a6c8
Revises: a7c3e9f1b2d4
Create Date: 2026-11-01 12:00:00.000000+00:00

Who sees money was decided by `invoice:read` alone, which the front desk holds
to collect today's fee. So a receptionist opened every patient's billing
history on the patient record, and anyone with `charge:read` — nurses, doctors,
the admission desk — opened the hospital-wide board of who owes what.

Two narrower reads now carry that:

- `patient_bills:read` — one patient's bills across visits (the record's
  Billing tab): cashier, billing staff, accountant, auditor, doctor, admins.
- `invoice:read_all` — every patient's bills (the counter board, worklists):
  the same, less the doctor.

Reception keeps `invoice:read` and collects at the desk as before. The
accountant also gains `payment:record`, to mark a bill's payment received from
the patient's account. Data only: no schema change.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b9d4e1f3a6c8"
down_revision: str | None = "a7c3e9f1b2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_PERMISSIONS: tuple[tuple[str, str, str], ...] = (
    (
        "invoice:read_all",
        "billing",
        "View every patient's bills and dues: the counter board and billing worklists.",
    ),
    (
        "patient_bills:read",
        "billing",
        "View a patient's bills, payments and dues across all their visits.",
    ),
)
_NEW_CODES = tuple(code for code, _, _ in NEW_PERMISSIONS)
_BOTH = ("invoice:read_all", "patient_bills:read")

ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "PLATFORM_ADMIN": _BOTH,
    "HOSPITAL_ADMIN": _BOTH,
    "CASHIER": _BOTH,
    "BILLING_STAFF": _BOTH,
    "AUDITOR": _BOTH,
    "ACCOUNTANT": (*_BOTH, "payment:record"),
    "DOCTOR": ("patient_bills:read",),
}


def upgrade() -> None:
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
             WHERE role_id IN (SELECT id FROM roles WHERE code = 'ACCOUNTANT' AND hospital_id IS NULL)
               AND permission_id IN (SELECT id FROM permissions WHERE code = 'payment:record')
            """
        )
    )
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
