"""What the platform console knows about hospitals, their administrators and
the platform team.

Everything here is cross-tenant, which is why it lives in `identity` (with
`tenancy`, the only modules allowed to be): the people and their roles are
identity's, the hospital rows are read through `tenancy.service`, and the
usage counts come from `reporting`. Nothing here reads a patient.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal

from sqlalchemy import ColumnElement, and_, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from app.core.database import system_context
from app.core.exceptions import ConflictError, NotFoundError
from app.core.models import utc_now
from app.core.pagination import MAX_PAGE_SIZE, PageParams
from app.modules.identity.invites import latest_emails
from app.modules.identity.models import (
    AuditLog,
    Invite,
    InviteStatus,
    PlatformEmail,
    PlatformEmailStatus,
    Role,
    User,
    UserRole,
)
from app.modules.identity.rbac import PLATFORM_ROLES, Roles
from app.modules.identity.service import get_user, set_user_active
from app.modules.tenancy import service as tenancy_service

if TYPE_CHECKING:
    from app.modules.tenancy.models import Hospital

__all__ = [
    "AttentionItem",
    "HospitalRow",
    "HospitalStatus",
    "deactivate_platform_user",
    "hospital_admins",
    "hospital_status",
    "list_platform_activity",
    "list_platform_hospitals",
    "list_platform_team",
    "overview",
    "staff_counts",
]

HospitalStatus = Literal["active", "suspended", "invite_pending"]

# An administrator who has not signed in for this long is worth a phone call:
# either the hospital stopped using the system, or someone else is using
# their login.
STALE_ADMIN_DAYS = 14
# "Expiring soon" for an invite — inside a day there is still time to nudge.
EXPIRING_SOON = timedelta(hours=24)
_ACTIVITY_PREFIXES = ("invite.", "hospital.", "email.")
# What the activity log may show. Isolation is lifted for platform users, so
# this filter is what keeps a hospital's clinical trail (patient searches,
# recorded deaths) out of the platform team's view: platform actions, sign-ins,
# and account changes to the platform team itself — nothing else.
_VISIBLE_PREFIXES = (*_ACTIVITY_PREFIXES, "auth.")


@dataclass(frozen=True, slots=True)
class HospitalRow:
    hospital: Hospital
    status: HospitalStatus
    admin_name: str | None
    staff_count: int
    last_admin_login_at: datetime | None


@dataclass(frozen=True, slots=True)
class AttentionItem:
    kind: Literal["invite_expiring", "email_failed", "no_admin", "no_admin_login"]
    hospital_id: uuid.UUID | None
    hospital_name: str | None
    invite_id: uuid.UUID | None = None
    email: str | None = None
    at: datetime | None = None


# ---------------------------------------------------------------------------
# Facts about hospitals that identity owns
# ---------------------------------------------------------------------------
async def hospital_admins(
    session: AsyncSession, hospital_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, list[User]]:
    """Each hospital's active administrators, longest-serving first."""
    if not hospital_ids:
        return {}
    async with system_context(session):
        users = (
            (
                await session.execute(
                    select(User)
                    .join(UserRole, col(UserRole.user_id) == col(User.id))
                    .join(Role, col(Role.id) == col(UserRole.role_id))
                    .where(
                        col(Role.code) == Roles.HOSPITAL_ADMIN,
                        col(User.hospital_id).in_(set(hospital_ids)),
                        col(User.is_active).is_(True),
                        col(User.deleted_at).is_(None),
                    )
                    .order_by(col(User.created_at))
                )
            )
            .scalars()
            .unique()
            .all()
        )
    admins: dict[uuid.UUID, list[User]] = {}
    for user in users:
        if user.hospital_id is not None:
            admins.setdefault(user.hospital_id, []).append(user)
    return admins


async def staff_counts(
    session: AsyncSession, hospital_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, int]:
    if not hospital_ids:
        return {}
    async with system_context(session):
        rows = (
            await session.execute(
                select(User.hospital_id, func.count())
                .where(
                    col(User.hospital_id).in_(set(hospital_ids)),
                    col(User.is_active).is_(True),
                    col(User.deleted_at).is_(None),
                )
                .group_by(col(User.hospital_id))
            )
        ).all()
    return {row[0]: int(row[1]) for row in rows}


async def _pending_invite_hospitals(session: AsyncSession) -> set[uuid.UUID]:
    rows = (
        await session.execute(
            select(Invite.hospital_id)
            .where(
                col(Invite.status) == InviteStatus.PENDING,
                col(Invite.expires_at) > utc_now(),
                col(Invite.hospital_id).is_not(None),
            )
            .distinct()
        )
    ).all()
    return {row[0] for row in rows}


async def _hospitals_with_admin(session: AsyncSession) -> set[uuid.UUID]:
    async with system_context(session):
        rows = (
            await session.execute(
                select(User.hospital_id)
                .join(UserRole, col(UserRole.user_id) == col(User.id))
                .join(Role, col(Role.id) == col(UserRole.role_id))
                .where(
                    col(Role.code) == Roles.HOSPITAL_ADMIN,
                    col(User.is_active).is_(True),
                    col(User.deleted_at).is_(None),
                    col(User.hospital_id).is_not(None),
                )
                .distinct()
            )
        ).all()
    return {row[0] for row in rows}


async def _awaiting_admin(session: AsyncSession) -> set[uuid.UUID]:
    """Hospitals with an invite out and nobody yet in the administrator seat."""
    return await _pending_invite_hospitals(session) - await _hospitals_with_admin(session)


# ---------------------------------------------------------------------------
# The hospital list
# ---------------------------------------------------------------------------
async def list_platform_hospitals(
    session: AsyncSession,
    params: PageParams,
    *,
    search: str | None = None,
    status: HospitalStatus | None = None,
) -> tuple[list[HospitalRow], int]:
    awaiting = await _awaiting_admin(session)
    if status == "suspended":
        hospitals, total = await tenancy_service.list_hospitals(
            session, params, search=search, is_active=False
        )
    elif status == "invite_pending":
        hospitals, total = await tenancy_service.list_hospitals(
            session, params, search=search, is_active=True, only_ids=awaiting
        )
    elif status == "active":
        hospitals, total = await tenancy_service.list_hospitals(
            session, params, search=search, is_active=True, exclude_ids=awaiting
        )
    else:
        hospitals, total = await tenancy_service.list_hospitals(
            session, params, search=search, include_inactive=True
        )

    ids = [hospital.id for hospital in hospitals]
    admins = await hospital_admins(session, ids)
    staff = await staff_counts(session, ids)
    rows: list[HospitalRow] = []
    for hospital in hospitals:
        people = admins.get(hospital.id, [])
        logins = [user.last_login_at for user in people if user.last_login_at is not None]
        row_status: HospitalStatus = (
            "suspended"
            if not hospital.is_active
            else "invite_pending"
            if hospital.id in awaiting
            else "active"
        )
        rows.append(
            HospitalRow(
                hospital=hospital,
                status=row_status,
                admin_name=people[0].full_name if people else None,
                staff_count=staff.get(hospital.id, 0),
                last_admin_login_at=max(logins) if logins else None,
            )
        )
    return rows, total


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Overview:
    active_hospitals: int
    suspended_hospitals: int
    pending_invites: int
    staff_accounts: int
    emails_failed_7d: int
    attention: list[AttentionItem]
    recent_activity: list[AuditLog]


async def overview(session: AsyncSession) -> Overview:
    now = utc_now()
    one = PageParams(limit=1, offset=0)
    _, active_total = await tenancy_service.list_hospitals(session, one, is_active=True)
    _, suspended_total = await tenancy_service.list_hospitals(session, one, is_active=False)

    pending = list(
        (
            await session.execute(
                select(Invite)
                .where(col(Invite.status) == InviteStatus.PENDING, col(Invite.expires_at) > now)
                .order_by(col(Invite.expires_at))
            )
        )
        .scalars()
        .all()
    )
    async with system_context(session):
        staff_total = (
            await session.execute(
                select(func.count())
                .select_from(User)
                .where(
                    col(User.hospital_id).is_not(None),
                    col(User.is_active).is_(True),
                    col(User.deleted_at).is_(None),
                )
            )
        ).scalar_one()
    failed_total = (
        await session.execute(
            select(func.count())
            .select_from(PlatformEmail)
            .where(
                col(PlatformEmail.status) == PlatformEmailStatus.FAILED,
                col(PlatformEmail.sent_at) >= now - timedelta(days=7),
            )
        )
    ).scalar_one()

    attention = await _attention(session, pending, now=now)

    async with system_context(session):
        activity = list(
            (
                await session.execute(
                    select(AuditLog)
                    .where(or_(*(col(AuditLog.action).startswith(p) for p in _ACTIVITY_PREFIXES)))
                    .order_by(col(AuditLog.created_at).desc())
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )

    return Overview(
        active_hospitals=active_total,
        suspended_hospitals=suspended_total,
        pending_invites=len(pending),
        staff_accounts=int(staff_total),
        emails_failed_7d=int(failed_total),
        attention=attention,
        recent_activity=activity,
    )


async def _attention(
    session: AsyncSession, pending: list[Invite], *, now: datetime
) -> list[AttentionItem]:
    """What the platform owner should do something about, most urgent first."""
    active, _ = await tenancy_service.list_hospitals(
        session, PageParams(limit=MAX_PAGE_SIZE, offset=0), is_active=True
    )
    names = {hospital.id: hospital.name for hospital in active}
    names.update(
        await tenancy_service.get_hospital_names(
            session, {i.hospital_id for i in pending if i.hospital_id is not None}
        )
    )
    items: list[AttentionItem] = []

    for invite in pending:
        if invite.expires_at <= now + EXPIRING_SOON:
            items.append(
                AttentionItem(
                    kind="invite_expiring",
                    hospital_id=invite.hospital_id,
                    hospital_name=names.get(invite.hospital_id) if invite.hospital_id else None,
                    invite_id=invite.id,
                    email=invite.email,
                    at=invite.expires_at,
                )
            )

    if pending:
        latest = await latest_emails(session, [invite.id for invite in pending])
        for invite in pending:
            email = latest.get(invite.id)
            if email is not None and email.status == PlatformEmailStatus.FAILED:
                items.append(
                    AttentionItem(
                        kind="email_failed",
                        hospital_id=invite.hospital_id,
                        hospital_name=names.get(invite.hospital_id) if invite.hospital_id else None,
                        invite_id=invite.id,
                        email=invite.email,
                        at=email.sent_at,
                    )
                )

    admins = await hospital_admins(session, [hospital.id for hospital in active])
    invited = {invite.hospital_id for invite in pending}
    stale_before = now - timedelta(days=STALE_ADMIN_DAYS)
    for hospital in active:
        people = admins.get(hospital.id, [])
        if not people:
            if hospital.id not in invited:
                items.append(
                    AttentionItem(
                        kind="no_admin", hospital_id=hospital.id, hospital_name=hospital.name
                    )
                )
            continue
        last_seen = max(user.last_login_at or user.created_at for user in people)
        if last_seen < stale_before:
            items.append(
                AttentionItem(
                    kind="no_admin_login",
                    hospital_id=hospital.id,
                    hospital_name=hospital.name,
                    at=last_seen,
                )
            )
    return items[:20]


# ---------------------------------------------------------------------------
# The activity log
# ---------------------------------------------------------------------------
def _visible_to_platform() -> ColumnElement[bool]:
    return or_(
        *(col(AuditLog.action).startswith(prefix) for prefix in _VISIBLE_PREFIXES),
        and_(col(AuditLog.action).startswith("user."), col(AuditLog.hospital_id).is_(None)),
    )


async def list_platform_activity(
    session: AsyncSession,
    params: PageParams,
    *,
    hospital_id: uuid.UUID | None = None,
    action: str | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> tuple[list[AuditLog], int]:
    filters: list[ColumnElement[bool]] = [_visible_to_platform()]
    if hospital_id is not None:
        filters.append(col(AuditLog.hospital_id) == hospital_id)
    if action:
        filters.append(col(AuditLog.action) == action)
    if actor_user_id is not None:
        filters.append(col(AuditLog.actor_user_id) == actor_user_id)
    async with system_context(session):
        total = (
            await session.execute(select(func.count()).select_from(AuditLog).where(*filters))
        ).scalar_one()
        rows = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(*filters)
                    .order_by(col(AuditLog.created_at).desc())
                    .limit(params.limit)
                    .offset(params.offset)
                )
            )
            .scalars()
            .all()
        )
    return list(rows), int(total)


async def hospital_status(session: AsyncSession, hospital: Hospital) -> HospitalStatus:
    """This hospital's own status — never inferred from a search that could
    match another hospital first."""
    if not hospital.is_active:
        return "suspended"
    return "invite_pending" if hospital.id in await _awaiting_admin(session) else "active"


# ---------------------------------------------------------------------------
# The platform team
# ---------------------------------------------------------------------------
def _platform_user_filter() -> ColumnElement[bool]:
    return col(User.id).in_(
        select(UserRole.user_id)
        .join(Role, col(Role.id) == col(UserRole.role_id))
        .where(col(Role.code).in_(PLATFORM_ROLES), col(Role.hospital_id).is_(None))
    )


async def list_platform_team(session: AsyncSession, params: PageParams) -> tuple[list[User], int]:
    filters = [_platform_user_filter(), col(User.deleted_at).is_(None)]
    async with system_context(session):
        total = (
            await session.execute(select(func.count()).select_from(User).where(*filters))
        ).scalar_one()
        rows = (
            (
                await session.execute(
                    select(User)
                    .where(*filters)
                    .order_by(col(User.is_active).desc(), col(User.full_name))
                    .limit(params.limit)
                    .offset(params.offset)
                )
            )
            .scalars()
            .all()
        )
    return list(rows), int(total)


async def deactivate_platform_user(
    session: AsyncSession, user_id: uuid.UUID, *, actor: User
) -> User:
    """Remove someone from the platform team.

    Nobody may remove themselves. That alone keeps an active owner in post:
    only an active owner can call this, and they cannot be the one removed.
    """
    if user_id == actor.id:
        raise ConflictError(
            "You cannot deactivate your own account.", code="cannot_deactivate_self"
        )
    async with system_context(session):
        is_platform = (
            await session.execute(
                select(func.count())
                .select_from(User)
                .where(col(User.id) == user_id, _platform_user_filter())
            )
        ).scalar_one()
    if not is_platform:
        raise NotFoundError("Platform team member not found.", code="user_not_found")
    user = await get_user(session, user_id)
    return await set_user_active(session, user, active=False, actor=actor)
