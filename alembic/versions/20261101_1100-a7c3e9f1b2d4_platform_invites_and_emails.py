"""platform invites, platform email log, platform permissions and support role

Revision ID: a7c3e9f1b2d4
Revises: f2b5d8e0a3c1
Create Date: 2026-11-01 11:00:00.000000+00:00

The platform owner's console. Two platform-level tables (no RLS policy, like
`users` and `hospitals`):

- `invites`         — single-use emailed offers of an administrator account;
                      only the SHA-256 of the link token is stored.
- `platform_emails` — every email the platform sent and whether the relay
                      took it; the stored body has the link token blanked.

Nine `platform:*` permissions, all held by PLATFORM_ADMIN, and a new system
role PLATFORM_SUPPORT holding the five read/resend ones. Mirrors
`identity/rbac.py` — `tests/test_rbac_seed.py` fails if the two disagree.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7c3e9f1b2d4"
down_revision: str | None = "f2b5d8e0a3c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE = (
    "PLATFORM_SUPPORT",
    "Platform Support",
    "Looks after hospitals on the platform; read-only plus resending invites.",
)

NEW_PERMISSIONS: tuple[tuple[str, str, str], ...] = (
    ("platform:console", "identity", "Open the platform owner console."),
    ("platform:hospital_read", "identity", "View every hospital and its usage counts."),
    ("platform:hospital_manage", "identity", "Onboard, edit and suspend hospitals."),
    ("platform:invite_read", "identity", "View invites and platform email."),
    ("platform:invite_send", "identity", "Invite a hospital administrator."),
    ("platform:invite_resend", "identity", "Resend an existing invite."),
    ("platform:invite_revoke", "identity", "Cancel an invite."),
    ("platform:team_manage", "identity", "Invite or remove platform staff."),
    ("platform:audit_read", "identity", "Read the platform activity log."),
)
_NEW_CODES = tuple(code for code, _, _ in NEW_PERMISSIONS)

ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "PLATFORM_ADMIN": _NEW_CODES,
    "PLATFORM_SUPPORT": (
        "platform:console",
        "platform:hospital_read",
        "platform:invite_read",
        "platform:invite_resend",
        "platform:audit_read",
    ),
}

_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    invite_status = postgresql.ENUM(
        "PENDING", "ACCEPTED", "REVOKED", "EXPIRED", name="invitestatus"
    )
    email_kind = postgresql.ENUM("INVITE", "INVITE_RESEND", name="platformemailkind")
    email_status = postgresql.ENUM("SENT", "FAILED", name="platformemailstatus")

    op.create_table(
        "invites",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("email", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column("full_name", sqlmodel.sql.sqltypes.AutoString(length=200), nullable=False),
        sa.Column("phone", sqlmodel.sql.sqltypes.AutoString(length=20), nullable=True),
        sa.Column("hospital_id", sa.Uuid(), nullable=True),
        sa.Column("role_codes", postgresql.ARRAY(sa.String(length=64)), nullable=False),
        sa.Column("token_hash", sqlmodel.sql.sqltypes.AutoString(length=64), nullable=False),
        sa.Column("status", invite_status, nullable=False),
        sa.Column("expires_at", _TS, nullable=False),
        sa.Column("accepted_at", _TS, nullable=True),
        sa.Column("revoked_at", _TS, nullable=True),
        sa.Column("last_sent_at", _TS, nullable=True),
        sa.Column("send_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("invited_by_id", sa.Uuid(), nullable=False),
        sa.Column("accepted_user_id", sa.Uuid(), nullable=True),
        sa.Column("revoked_by_id", sa.Uuid(), nullable=True),
        sa.Column("superseded_by_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["hospital_id"], ["hospitals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["invited_by_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["accepted_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["revoked_by_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["superseded_by_id"], ["invites.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "email",
        "hospital_id",
        "status",
        "invited_by_id",
        "accepted_user_id",
        "revoked_by_id",
        "superseded_by_id",
    ):
        op.create_index(f"ix_invites_{column}", "invites", [column])
    op.create_index("ix_invites_token_hash", "invites", ["token_hash"], unique=True)
    op.create_index("ix_invites_status_expires_at", "invites", ["status", "expires_at"])
    op.create_index(
        "uq_invites_pending_email",
        "invites",
        [sa.text("lower(email)")],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )

    op.create_table(
        "platform_emails",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", _TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("kind", email_kind, nullable=False),
        sa.Column("to_email", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column("from_email", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column("subject", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=False),
        sa.Column("body_text", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("invite_id", sa.Uuid(), nullable=True),
        sa.Column("hospital_id", sa.Uuid(), nullable=True),
        sa.Column("status", email_status, nullable=False),
        sa.Column("error", sqlmodel.sql.sqltypes.AutoString(length=500), nullable=True),
        sa.Column(
            "provider_message_id", sqlmodel.sql.sqltypes.AutoString(length=255), nullable=True
        ),
        sa.Column("sent_by_id", sa.Uuid(), nullable=True),
        sa.Column("sent_at", _TS, nullable=False),
        sa.ForeignKeyConstraint(["invite_id"], ["invites.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["hospital_id"], ["hospitals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["sent_by_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("kind", "to_email", "invite_id", "hospital_id", "status", "sent_by_id", "sent_at"):
        op.create_index(f"ix_platform_emails_{column}", "platform_emails", [column])
    op.create_index(
        "ix_platform_emails_status_sent_at", "platform_emails", ["status", "sent_at"]
    )

    # The app's non-owner role reaches these tables through the
    # `ALTER DEFAULT PRIVILEGES` that `scripts/bootstrap_db_role.py` set up.
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

    code, name, description = _ROLE
    connection.execute(
        sa.text(
            """
            INSERT INTO roles
                (id, hospital_id, code, name, description, is_system, created_at, updated_at)
            VALUES (gen_random_uuid(), NULL, :code, :name, :description, true, now(), now())
            ON CONFLICT DO NOTHING
            """
        ),
        {"code": code, "name": name, "description": description},
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
             WHERE role_id IN (SELECT id FROM roles WHERE code = :role AND hospital_id IS NULL)
                OR permission_id IN (SELECT id FROM permissions WHERE code = ANY(:codes))
            """
        ),
        {"role": _ROLE[0], "codes": list(_NEW_CODES)},
    )
    connection.execute(
        sa.text("DELETE FROM roles WHERE code = :role AND hospital_id IS NULL"),
        {"role": _ROLE[0]},
    )
    connection.execute(
        sa.text("DELETE FROM permissions WHERE code = ANY(:codes)"), {"codes": list(_NEW_CODES)}
    )

    op.drop_table("platform_emails")
    op.drop_table("invites")
    for enum_name in ("platformemailstatus", "platformemailkind", "invitestatus"):
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
