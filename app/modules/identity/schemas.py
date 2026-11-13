"""Request/response DTOs for `identity`.

Note what is absent: no schema anywhere exposes `password_hash`, `totp_secret`
or the raw refresh token. Read models are explicit allow-lists, not
`model_config = from_attributes` over the whole table.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.modules.identity.models import InviteStatus, PlatformEmailKind, PlatformEmailStatus
from app.modules.tenancy.schemas import HospitalCreate, HospitalRead

__all__ = [
    "AttentionItemRead",
    "AuditLogRead",
    "ChangePasswordRequest",
    "HospitalAdminInviteCreate",
    "HospitalUsageRead",
    "HospitalWithAdminCreate",
    "InviteAccept",
    "InvitePreviewRead",
    "InviteRead",
    "InviteToken",
    "LoginRequest",
    "PermissionRead",
    "PlatformAdminRow",
    "PlatformEmailRead",
    "PlatformHospitalDetail",
    "PlatformHospitalRow",
    "PlatformOverviewRead",
    "PlatformTeamRow",
    "RefreshRequest",
    "RoleAssignmentRequest",
    "RoleCreate",
    "RoleRead",
    "TeamInviteCreate",
    "TokenPair",
    "UserCreate",
    "UserRead",
    "UserUpdate",
]


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    email: EmailStr
    password: Annotated[str, Field(min_length=1, max_length=256)]
    # SEC-05: the token from "remember this device", if this device has one.
    device_token: Annotated[str | None, Field(max_length=2048)] = None


class RefreshRequest(BaseModel):
    refresh_token: Annotated[str, Field(min_length=1, max_length=512)]


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth token type, not a secret
    expires_in: int = Field(description="Access token lifetime in seconds.")
    must_change_password: bool = Field(
        default=False,
        description="When true the client must send the user to a password change screen.",
    )
    device_token: str | None = Field(
        default=None,
        description="Set when the person asked to remember this device (SEC-05).",
    )


class TwoFactorChallengeRead(BaseModel):
    """The password was right; a code from the authenticator app is next.

    `enrol` is true when the person's role requires two-step sign-in and they
    have not set the app up yet.
    """

    two_factor_required: bool = True
    enrol: bool
    challenge_token: str
    expires_in: int


class TwoFactorVerify(BaseModel):
    challenge_token: Annotated[str, Field(min_length=1, max_length=2048)]
    code: Annotated[str, Field(min_length=6, max_length=12)]
    remember_device: bool = False


class TwoFactorEnrolStart(BaseModel):
    challenge_token: Annotated[str, Field(min_length=1, max_length=2048)]


class TwoFactorSecretRead(BaseModel):
    """What the authenticator app needs: scan the URI as a QR code, or type
    the secret."""

    secret: str
    otpauth_uri: str


class TwoFactorCode(BaseModel):
    code: Annotated[str, Field(min_length=6, max_length=12)]


class TwoFactorDisable(BaseModel):
    password: Annotated[str, Field(min_length=1, max_length=256)]
    code: Annotated[str, Field(min_length=6, max_length=12)]


class TwoFactorStatusRead(BaseModel):
    enabled: bool
    required: bool


class ChangePasswordRequest(BaseModel):
    current_password: Annotated[str, Field(min_length=1, max_length=256)]
    new_password: Annotated[str, Field(min_length=1, max_length=256)]


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
class UserCreate(BaseModel):
    email: EmailStr
    full_name: Annotated[str, Field(min_length=1, max_length=200)]
    phone: Annotated[str | None, Field(max_length=20)] = None
    password: Annotated[str, Field(min_length=1, max_length=256)]
    role_codes: Annotated[
        list[str],
        Field(default_factory=list, description="System or hospital role codes to assign."),
    ]
    # Platform admins may create a user for any hospital; a hospital admin's
    # own tenant is forced by the service regardless of what is sent.
    hospital_id: uuid.UUID | None = None

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, value: str) -> str:
        return value.strip().lower()


class UserUpdate(BaseModel):
    # SEC-08: a field this update cannot change is refused, not ignored.
    model_config = ConfigDict(extra="forbid")

    full_name: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    phone: Annotated[str | None, Field(max_length=20)] = None
    is_active: bool | None = None


class RoleAssignmentRequest(BaseModel):
    role_codes: Annotated[
        list[str],
        Field(description="The user's complete role set after this call."),
    ]


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    hospital_id: uuid.UUID | None
    email: str
    full_name: str
    phone: str | None
    is_active: bool
    must_change_password: bool
    two_factor_enabled: bool
    last_login_at: datetime | None
    created_at: datetime

    # The roles live in a join table, so `from_attributes` cannot reach them and
    # the router has to supply them. Without this the staff screen cannot answer
    # the question it exists to answer — who is a doctor here — and role
    # assignment returns a body that does not show what it just changed.
    #
    # **Required, not defaulted.** A default would let a call site forget, and a
    # `roles: []` that means "nobody bothered to load them" is worse than an
    # error: it renders as an account with no access at all. Being required
    # makes `model_validate(user)` fail loudly, which is why every route in this
    # module goes through `_render` instead.
    roles: list[str]


class CurrentUserRead(UserRead):
    """`/auth/me` — adds the resolved authorisation context.

    The client uses `permissions` to decide which controls to render. It is
    never the authorisation decision itself; that is re-checked server-side on
    every request.
    """

    roles: list[str]
    permissions: list[str]
    # SEC-05: the person's role makes two-step sign-in compulsory.
    two_factor_required: bool = False


# ---------------------------------------------------------------------------
# Roles & permissions
# ---------------------------------------------------------------------------
class PermissionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    module: str
    description: str


class RoleCreate(BaseModel):
    code: Annotated[str, Field(min_length=2, max_length=64)]
    name: Annotated[str, Field(min_length=2, max_length=128)]
    description: Annotated[str | None, Field(max_length=500)] = None
    permission_codes: Annotated[list[str], Field(default_factory=list)]

    @field_validator("code")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper().replace(" ", "_")


class RoleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    hospital_id: uuid.UUID | None
    code: str
    name: str
    description: str | None
    is_system: bool


class RoleDetail(RoleRead):
    permissions: list[str]


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------
class AuditLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    hospital_id: uuid.UUID | None
    actor_user_id: uuid.UUID | None
    actor_email: str | None
    action: str
    resource_type: str | None
    resource_id: uuid.UUID | None
    changes: dict[str, Any] | None
    succeeded: bool
    ip_address: str | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Invitations and platform email
# ---------------------------------------------------------------------------
class InviteToken(BaseModel):
    """The link token, in a request body — never a URL path, which every
    access log would record."""

    model_config = ConfigDict(extra="forbid")

    token: Annotated[str, Field(min_length=1, max_length=256)]


class InviteAccept(InviteToken):
    password: Annotated[str, Field(min_length=1, max_length=256)]


class InvitePreviewRead(BaseModel):
    """What the set-password page shows. No token, no ids."""

    full_name: str
    email: str
    hospital_name: str | None
    role_codes: list[str]
    status: InviteStatus
    expires_at: datetime
    support_contact: str


class HospitalAdminInviteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: Annotated[str, Field(min_length=1, max_length=200)]
    email: EmailStr
    phone: Annotated[str | None, Field(max_length=20)] = None

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, value: str) -> str:
        return value.strip().lower()


class InviteRead(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    phone: str | None
    hospital_id: uuid.UUID | None
    hospital_name: str | None
    role_codes: list[str]
    status: InviteStatus
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None
    last_sent_at: datetime | None
    send_count: int
    invited_by_name: str | None
    # The newest email for this invite: "did it go out?" on the row itself.
    last_email_status: PlatformEmailStatus | None
    last_email_error: str | None
    created_at: datetime


class PlatformEmailRead(BaseModel):
    id: uuid.UUID
    kind: PlatformEmailKind
    to_email: str
    from_email: str
    subject: str
    body_text: str
    hospital_id: uuid.UUID | None
    hospital_name: str | None
    invite_id: uuid.UUID | None
    status: PlatformEmailStatus
    error: str | None
    provider_message_id: str | None
    sent_by_name: str | None
    sent_at: datetime


# ---------------------------------------------------------------------------
# Platform console
# ---------------------------------------------------------------------------
class PlatformHospitalRow(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    city: str | None
    is_active: bool
    status: Literal["active", "suspended", "invite_pending"]
    admin_name: str | None
    staff_count: int
    last_admin_login_at: datetime | None
    created_at: datetime


class PlatformAdminRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str
    email: str
    phone: str | None
    is_active: bool
    two_factor_enabled: bool
    last_login_at: datetime | None
    created_at: datetime


class HospitalUsageRead(BaseModel):
    staff: int
    patients: int
    visits_last_7_days: int


class PlatformHospitalDetail(BaseModel):
    hospital: HospitalRead
    status: Literal["active", "suspended", "invite_pending"]
    admins: list[PlatformAdminRow]
    invites: list[InviteRead]
    usage: HospitalUsageRead


class HospitalWithAdminCreate(BaseModel):
    """A new hospital and the invite for its first administrator, together."""

    model_config = ConfigDict(extra="forbid")

    hospital: HospitalCreate
    admin: HospitalAdminInviteCreate


class AttentionItemRead(BaseModel):
    kind: Literal["invite_expiring", "email_failed", "no_admin", "no_admin_login"]
    hospital_id: uuid.UUID | None
    hospital_name: str | None
    invite_id: uuid.UUID | None
    email: str | None
    at: datetime | None


class PlatformOverviewRead(BaseModel):
    active_hospitals: int
    suspended_hospitals: int
    pending_invites: int
    staff_accounts: int
    emails_failed_7d: int
    attention: list[AttentionItemRead]
    recent_activity: list[AuditLogRead]


class PlatformTeamRow(BaseModel):
    id: uuid.UUID
    full_name: str
    email: str
    roles: list[str]
    is_active: bool
    two_factor_enabled: bool
    last_login_at: datetime | None
    created_at: datetime


class TeamInviteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: Annotated[str, Field(min_length=1, max_length=200)]
    email: EmailStr
    role: Literal["PLATFORM_ADMIN", "PLATFORM_SUPPORT"]

    @field_validator("email")
    @classmethod
    def _normalise_email(cls, value: str) -> str:
        return value.strip().lower()
