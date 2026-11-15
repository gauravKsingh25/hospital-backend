"""doctor involvement: a per-doctor switch for the doctor's own data entry

Revision ID: b6d0f4c8e2a3
Revises: a5c9e3b7d1f2
Create Date: 2026-11-15 10:00:00

Every existing doctor starts involved (true), so today's behaviour is
unchanged on deploy.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b6d0f4c8e2a3"
down_revision: str | None = "a5c9e3b7d1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSION = "doctor:involvement:manage"
GRANTED_TO = ("HOSPITAL_ADMIN", "PLATFORM_ADMIN")


def upgrade() -> None:
    op.add_column(
        "doctors",
        sa.Column("involvement_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            INSERT INTO permissions (id, code, module, description, created_at, updated_at)
            VALUES (gen_random_uuid(), :code, 'scheduling',
                    'Turn a doctor''s own data entry on or off.', now(), now())
            ON CONFLICT (code) DO NOTHING
            """
        ),
        {"code": PERMISSION},
    )
    for role_code in GRANTED_TO:
        connection.execute(
            sa.text(
                """
                INSERT INTO role_permissions (role_id, permission_id, created_at)
                SELECT r.id, p.id, now()
                  FROM roles r, permissions p
                 WHERE r.code = :role_code
                   AND r.hospital_id IS NULL
                   AND p.code = :code
                ON CONFLICT DO NOTHING
                """
            ),
            {"role_code": role_code, "code": PERMISSION},
        )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            DELETE FROM role_permissions
             WHERE permission_id IN (SELECT id FROM permissions WHERE code = :code)
            """
        ),
        {"code": PERMISSION},
    )
    connection.execute(sa.text("DELETE FROM permissions WHERE code = :code"), {"code": PERMISSION})
    op.drop_column("doctors", "involvement_enabled")
