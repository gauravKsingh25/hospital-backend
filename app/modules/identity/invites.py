"""Invitations: the only door to an administrator account.

A hospital administrator — or a member of the platform team — never has a
password chosen for them. The platform owner sends an invite; the person opens
a single-use link and picks their own. These functions are that lifecycle:

    create ─► (email) ─► PENDING ─► accept ─► ACCEPTED (account exists)
                            │
                            ├─► resend ─► REVOKED, superseded by a new PENDING
                            ├─► revoke ─► REVOKED
                            └─► time   ─► EXPIRED

The link token is shown exactly once — returned from `create_invite` /
`resend_invite` so the caller can email it — and stored only as SHA-256, the
same treatment refresh tokens get. `expires_at` is checked on every accept; the
EXPIRED status is bookkeeping for lists, never what makes a link stop working.

Kept out of `service.py`, which is already the largest file in the module, and
imported directly by the platform router.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from app.core.config import settings
from app.core.database import system_context
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.mail import MailMessage, get_mailer
from app.core.models import utc_now
from app.core.pagination import PageParams
from app.modules.identity.email_templates import render_invite
from app.modules.identity.models import (
    Invite,
    InviteStatus,
    PlatformEmail,
    PlatformEmailKind,
    PlatformEmailStatus,
    User,
)
from app.modules.identity.rbac import PLATFORM_ROLES, SYSTEM_ROLES, Roles
from app.modules.identity.schemas import UserCreate
from app.modules.identity.service import create_user, get_user_by_email, login, record_audit
from app.modules.tenancy import service as tenancy_service

__all__ = [
    "InvitePreview",
    "accept_invite",
    "create_invite",
    "expire_due_invites",
    "get_invite",
    "hash_token",
    "invite_url",
    "latest_emails",
    "list_invites",
    "list_platform_emails",
    "preview_invite",
    "resend_invite",
    "revoke_invite",
    "send_invite_email",
]

_ROLE_NAMES = {role.code: role.name for role in SYSTEM_ROLES}
_REDACTED = "••••"


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def invite_url(raw_token: str) -> str:
    return f"{settings.APP_PUBLIC_URL.rstrip('/')}/invite/{raw_token}"


@dataclass(frozen=True, slots=True)
class InvitePreview:
    full_name: str
    email: str
    hospital_name: str | None
    role_codes: list[str]
    status: InviteStatus
    expires_at: datetime


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------
async def get_invite(session: AsyncSession, invite_id: uuid.UUID, *, lock: bool = False) -> Invite:
    """One invite. `lock` for a write: a resend or cancel that arrives while the
    link is being accepted waits for the accept, then sees ACCEPTED — instead of
    overwriting it and showing "cancelled" for an account that now exists."""
    statement = select(Invite).where(Invite.id == invite_id)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    invite: Invite | None = (await session.execute(statement)).scalars().first()
    if invite is None:
        raise NotFoundError("Invite not found.", code="invite_not_found")
    return invite


async def _by_token(session: AsyncSession, raw_token: str, *, lock: bool = False) -> Invite:
    statement = select(Invite).where(Invite.token_hash == hash_token(raw_token))
    if lock:
        # Two tabs submitting the same link: the second waits here, then sees
        # ACCEPTED and is refused — never a second account.
        statement = statement.with_for_update()
    invite: Invite | None = (await session.execute(statement)).scalars().first()
    if invite is None:
        raise NotFoundError("This invite link is not valid.", code="invite_invalid")
    return invite


def _is_expired(invite: Invite) -> bool:
    return invite.expires_at <= utc_now()


async def _mark_expired_if_due(session: AsyncSession, invite: Invite) -> None:
    if invite.status == InviteStatus.PENDING and _is_expired(invite):
        invite.status = InviteStatus.EXPIRED
        invite.updated_at = utc_now()
        session.add(invite)
        await session.flush()


async def _hospital_name(session: AsyncSession, hospital_id: uuid.UUID | None) -> str | None:
    if hospital_id is None:
        return None
    async with system_context(session):
        hospital = await tenancy_service.get_hospital(session, hospital_id)
    return hospital.name


# ---------------------------------------------------------------------------
# Create / resend / revoke
# ---------------------------------------------------------------------------
def _validate_roles(hospital_id: uuid.UUID | None, role_codes: list[str]) -> None:
    if hospital_id is not None:
        if role_codes != [Roles.HOSPITAL_ADMIN]:
            raise ValidationError(
                "A hospital invite grants the hospital administrator role only.",
                code="invalid_invite_roles",
            )
        return
    if len(role_codes) != 1 or role_codes[0] not in PLATFORM_ROLES:
        raise ValidationError(
            "A platform team invite grants exactly one platform role.",
            code="invalid_invite_roles",
        )


async def _new_invite(
    session: AsyncSession,
    *,
    email: str,
    full_name: str,
    phone: str | None,
    hospital_id: uuid.UUID | None,
    role_codes: list[str],
    actor: User,
) -> tuple[Invite, str]:
    raw_token = secrets.token_urlsafe(32)
    invite = Invite(
        email=email,
        full_name=full_name.strip(),
        phone=phone,
        hospital_id=hospital_id,
        role_codes=list(role_codes),
        token_hash=hash_token(raw_token),
        status=InviteStatus.PENDING,
        expires_at=utc_now() + timedelta(hours=settings.INVITE_TTL_HOURS),
        invited_by_id=actor.id,
    )
    session.add(invite)
    try:
        await session.flush()
    except IntegrityError as error:
        # Two submits for one address at once: the partial unique index lets
        # one through. The other is the same conflict the pre-check reports.
        raise ConflictError(
            "This address already has an invite waiting.", code="invite_pending"
        ) from error
    await session.refresh(invite)
    return invite, raw_token


async def _live_invite_for(session: AsyncSession, email: str) -> Invite | None:
    """The PENDING invite for an address, if any (expired or not)."""
    return (
        (
            await session.execute(
                select(Invite).where(
                    func.lower(Invite.email) == email.strip().lower(),
                    Invite.status == InviteStatus.PENDING,
                )
            )
        )
        .scalars()
        .first()
    )


async def create_invite(
    session: AsyncSession,
    *,
    email: str,
    full_name: str,
    phone: str | None,
    hospital_id: uuid.UUID | None,
    role_codes: list[str],
    actor: User,
) -> tuple[Invite, str]:
    """Create a PENDING invite and return it with its raw link token.

    Refused up front — before anybody is emailed a link that cannot work — when
    the address already has an account, already has a live invite, or the
    hospital is suspended.
    """
    _validate_roles(hospital_id, role_codes)
    normalised = email.strip().lower()

    if hospital_id is not None and not await tenancy_service.is_hospital_active(
        session, hospital_id
    ):
        raise PermissionDeniedError(
            "This hospital is suspended; reactivate it before inviting anyone.",
            code="hospital_suspended",
        )
    if await get_user_by_email(session, normalised) is not None:
        raise ConflictError(
            "An account with that email address already exists.", code="email_taken"
        )

    existing = await _live_invite_for(session, normalised)
    if existing is not None:
        if not _is_expired(existing):
            raise ConflictError(
                "This address already has an invite waiting. Resend or cancel it instead.",
                code="invite_pending",
                details={"invite_id": str(existing.id)},
            )
        # A stale PENDING row would trip the one-live-invite-per-address index.
        await _mark_expired_if_due(session, existing)

    invite, raw_token = await _new_invite(
        session,
        email=normalised,
        full_name=full_name,
        phone=phone,
        hospital_id=hospital_id,
        role_codes=role_codes,
        actor=actor,
    )
    await record_audit(
        session,
        action="invite.create",
        actor=actor,
        hospital_id=hospital_id,
        resource_type="invite",
        resource_id=invite.id,
        changes={"email": invite.email, "roles": invite.role_codes},
    )
    return invite, raw_token


async def resend_invite(
    session: AsyncSession, invite_id: uuid.UUID, *, actor: User
) -> tuple[Invite, str]:
    """Replace an invite with a fresh one: new link, new expiry.

    The old link dies here rather than living on beside the new one — an inbox
    holding two working links to an administrator account is one too many.
    """
    old = await get_invite(session, invite_id, lock=True)
    if old.status not in (InviteStatus.PENDING, InviteStatus.EXPIRED):
        raise ConflictError(
            f"This invite is {old.status.lower()} and cannot be resent.",
            code="invite_not_pending",
        )
    if old.hospital_id is not None and not await tenancy_service.is_hospital_active(
        session, old.hospital_id
    ):
        raise PermissionDeniedError(
            "This hospital is suspended; reactivate it before inviting anyone.",
            code="hospital_suspended",
        )
    if await get_user_by_email(session, old.email) is not None:
        raise ConflictError(
            "An account with that email address already exists.", code="email_taken"
        )
    newer = await _live_invite_for(session, old.email)
    if newer is not None and newer.id != old.id:
        raise ConflictError(
            "A newer invite is already waiting for this address. Resend that one instead.",
            code="invite_pending",
            details={"invite_id": str(newer.id)},
        )

    now = utc_now()
    old.status = InviteStatus.REVOKED
    old.revoked_at = now
    old.revoked_by_id = actor.id
    old.updated_at = now
    session.add(old)
    await session.flush()

    new, raw_token = await _new_invite(
        session,
        email=old.email,
        full_name=old.full_name,
        phone=old.phone,
        hospital_id=old.hospital_id,
        role_codes=old.role_codes,
        actor=actor,
    )
    old.superseded_by_id = new.id
    session.add(old)
    await session.flush()

    await record_audit(
        session,
        action="invite.resend",
        actor=actor,
        hospital_id=new.hospital_id,
        resource_type="invite",
        resource_id=new.id,
        changes={"email": new.email, "replaces": str(old.id)},
    )
    return new, raw_token


async def revoke_invite(session: AsyncSession, invite_id: uuid.UUID, *, actor: User) -> Invite:
    invite = await get_invite(session, invite_id, lock=True)
    if invite.status != InviteStatus.PENDING:
        raise ConflictError(
            f"This invite is {invite.status.lower()} and cannot be cancelled.",
            code="invite_not_pending",
        )
    now = utc_now()
    invite.status = InviteStatus.REVOKED
    invite.revoked_at = now
    invite.revoked_by_id = actor.id
    invite.updated_at = now
    session.add(invite)
    await session.flush()
    await record_audit(
        session,
        action="invite.revoke",
        actor=actor,
        hospital_id=invite.hospital_id,
        resource_type="invite",
        resource_id=invite.id,
        changes={"email": invite.email},
    )
    return invite


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
async def send_invite_email(
    session: AsyncSession,
    invite: Invite,
    raw_token: str,
    *,
    kind: PlatformEmailKind,
    actor: User,
) -> PlatformEmail:
    """Email the link and record what happened.

    Called after the invite has been committed, so a relay that refuses the
    message never takes the invite down with it: the owner sees FAILED, with
    the reason, beside a Resend button.
    """
    hospital_name = await _hospital_name(session, invite.hospital_id)
    role_label = (
        "administrator"
        if invite.hospital_id is not None
        else _ROLE_NAMES.get(invite.role_codes[0], invite.role_codes[0])
    )
    url = invite_url(raw_token)
    subject, text, html = render_invite(
        full_name=invite.full_name,
        hospital_name=hospital_name,
        role_label=role_label,
        url=url,
        expires_at=invite.expires_at,
        support_contact=settings.PLATFORM_SUPPORT_CONTACT,
    )

    result = await get_mailer().send(
        MailMessage(to=invite.email, subject=subject, text=text, html=html)
    )

    # The log records that a link went out, never the link: support staff read
    # it, and a working token in a table is a working token in a backup.
    redacted = text.replace(raw_token, _REDACTED)
    email = PlatformEmail(
        kind=kind,
        to_email=invite.email,
        from_email=settings.MAIL_FROM,
        subject=subject,
        body_text=redacted,
        invite_id=invite.id,
        hospital_id=invite.hospital_id,
        status=PlatformEmailStatus.SENT if result.ok else PlatformEmailStatus.FAILED,
        error=None if result.ok else result.error,
        provider_message_id=result.message_id,
        sent_by_id=actor.id,
        sent_at=utc_now(),
    )
    session.add(email)

    invite.last_sent_at = email.sent_at
    invite.send_count += 1
    invite.updated_at = utc_now()
    session.add(invite)
    await session.flush()

    await record_audit(
        session,
        action="email.send",
        actor=actor,
        hospital_id=invite.hospital_id,
        resource_type="platform_email",
        resource_id=email.id,
        succeeded=result.ok,
        changes={"kind": kind.value, "to": invite.email, "status": email.status.value},
    )
    return email


# ---------------------------------------------------------------------------
# The invited person's side
# ---------------------------------------------------------------------------
async def preview_invite(session: AsyncSession, raw_token: str) -> InvitePreview:
    """What the set-password page shows before anything is submitted."""
    invite = await _by_token(session, raw_token)
    await _mark_expired_if_due(session, invite)
    return InvitePreview(
        full_name=invite.full_name,
        email=invite.email,
        hospital_name=await _hospital_name(session, invite.hospital_id),
        role_codes=list(invite.role_codes),
        status=invite.status,
        expires_at=invite.expires_at,
    )


async def _refuse(
    session: AsyncSession,
    invite: Invite | None,
    error: ConflictError | PermissionDeniedError | NotFoundError,
    *,
    ip_address: str | None,
    user_agent: str | None,
) -> None:
    """Audit a refused accept, keep the record, and raise.

    Committed before raising, as a failed login is: the request's session rolls
    back on the way out, and an attempt on an administrator account that leaves
    no trace is not one anybody can investigate.
    """
    await record_audit(
        session,
        action="invite.accept_failed",
        hospital_id=invite.hospital_id if invite else None,
        resource_type="invite",
        resource_id=invite.id if invite else None,
        succeeded=False,
        ip_address=ip_address,
        user_agent=user_agent,
        changes={"reason": error.code},
    )
    await session.commit()
    raise error


async def accept_invite(
    session: AsyncSession,
    raw_token: str,
    *,
    password: str,
    ip_address: str | None,
    user_agent: str | None,
) -> tuple[User, str, str, int]:
    """Create the account the invite offers and sign its owner in.

    Returns exactly what `service.login` does — and, like it, raises
    `TwoFactorChallenge` when the new account's role needs an authenticator,
    in which case the account and the ACCEPTED invite are already committed
    and the person continues at enrolment.
    """
    try:
        invite = await _by_token(session, raw_token, lock=True)
    except NotFoundError as error:
        await _refuse(session, None, error, ip_address=ip_address, user_agent=user_agent)
        raise  # unreachable; `_refuse` raises

    refusal: ConflictError | PermissionDeniedError | None = None
    if invite.status == InviteStatus.ACCEPTED:
        refusal = ConflictError("This invite has already been used.", code="invite_used")
    elif invite.status == InviteStatus.REVOKED:
        refusal = ConflictError("This invite has been cancelled.", code="invite_revoked")
    elif invite.status == InviteStatus.EXPIRED or _is_expired(invite):
        await _mark_expired_if_due(session, invite)
        refusal = ConflictError("This invite has expired.", code="invite_expired")
    elif invite.hospital_id is not None and not await tenancy_service.is_hospital_active(
        session, invite.hospital_id
    ):
        refusal = PermissionDeniedError(
            f"This hospital's access is suspended. Contact {settings.PLATFORM_SUPPORT_CONTACT}.",
            code="hospital_suspended",
        )
    if refusal is not None:
        await _refuse(session, invite, refusal, ip_address=ip_address, user_agent=user_agent)

    user = await create_user(
        session,
        UserCreate(
            email=invite.email,
            full_name=invite.full_name,
            phone=invite.phone,
            password=password,
            role_codes=list(invite.role_codes),
            hospital_id=invite.hospital_id,
        ),
        hospital_id=invite.hospital_id,
        must_change_password=False,
        allow_privileged_roles=True,
    )

    now = utc_now()
    invite.status = InviteStatus.ACCEPTED
    invite.accepted_at = now
    invite.accepted_user_id = user.id
    invite.updated_at = now
    session.add(invite)
    await session.flush()
    await record_audit(
        session,
        action="invite.accept",
        actor=user,
        hospital_id=invite.hospital_id,
        resource_type="invite",
        resource_id=invite.id,
        ip_address=ip_address,
        user_agent=user_agent,
        changes={"roles": invite.role_codes},
    )

    return await login(
        session,
        email=invite.email,
        password=password,
        ip_address=ip_address,
        user_agent=user_agent,
    )


# ---------------------------------------------------------------------------
# Lists and the sweep
# ---------------------------------------------------------------------------
async def list_invites(
    session: AsyncSession,
    params: PageParams,
    *,
    status: InviteStatus | None = None,
    hospital_id: uuid.UUID | None = None,
) -> tuple[list[Invite], int]:
    filters: list[ColumnElement[bool]] = []
    if status is not None:
        filters.append(col(Invite.status) == status)
    if hospital_id is not None:
        filters.append(col(Invite.hospital_id) == hospital_id)

    total = (
        await session.execute(select(func.count()).select_from(Invite).where(*filters))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(Invite)
                .where(*filters)
                .order_by(col(Invite.created_at).desc())
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def list_platform_emails(
    session: AsyncSession,
    params: PageParams,
    *,
    status: PlatformEmailStatus | None = None,
    kind: PlatformEmailKind | None = None,
    hospital_id: uuid.UUID | None = None,
    to: str | None = None,
) -> tuple[list[PlatformEmail], int]:
    filters: list[ColumnElement[bool]] = []
    if status is not None:
        filters.append(col(PlatformEmail.status) == status)
    if kind is not None:
        filters.append(col(PlatformEmail.kind) == kind)
    if hospital_id is not None:
        filters.append(col(PlatformEmail.hospital_id) == hospital_id)
    if to:
        filters.append(col(PlatformEmail.to_email).ilike(f"%{to.strip()}%"))

    total = (
        await session.execute(select(func.count()).select_from(PlatformEmail).where(*filters))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(PlatformEmail)
                .where(*filters)
                .order_by(col(PlatformEmail.sent_at).desc(), col(PlatformEmail.id).desc())
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), total


async def latest_emails(
    session: AsyncSession, invite_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, PlatformEmail]:
    """The newest email for each invite — one query for a page of invites."""
    if not invite_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(PlatformEmail)
                .where(col(PlatformEmail.invite_id).in_(set(invite_ids)))
                .distinct(col(PlatformEmail.invite_id))
                .order_by(col(PlatformEmail.invite_id), col(PlatformEmail.sent_at).desc())
            )
        )
        .scalars()
        .all()
    )
    return {row.invite_id: row for row in rows if row.invite_id is not None}


async def expire_due_invites(session: AsyncSession) -> int:
    """Mark PENDING invites past their expiry as EXPIRED. Bookkeeping only."""
    result = await session.execute(
        update(Invite)
        .where(col(Invite.status) == InviteStatus.PENDING, col(Invite.expires_at) <= utc_now())
        .values(status=InviteStatus.EXPIRED, updated_at=utc_now())
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
