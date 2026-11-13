"""Platform console routes and the public invite routes.

Two audiences. `/platform/*` is the platform team's: every route gated on a
`platform:*` permission, which no hospital role holds. `/invites/*` is the
invited person's, reached from an email before they have an account, so it is
unauthenticated — the link token is the credential, and it works once.

Invite writes commit the invite *before* emailing it, then commit the email
record: a relay that refuses the message must not take the invite with it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.config import settings
from app.core.deps import SessionDep, get_client_ip, require
from app.core.pagination import Page, PageParams, page_params
from app.modules.identity import invites, platform, service
from app.modules.identity.models import (
    Invite,
    InviteStatus,
    PlatformEmail,
    PlatformEmailKind,
    PlatformEmailStatus,
    User,
)
from app.modules.identity.rbac import Permissions, Roles
from app.modules.identity.schemas import (
    AttentionItemRead,
    AuditLogRead,
    HospitalAdminInviteCreate,
    HospitalUsageRead,
    HospitalWithAdminCreate,
    InviteAccept,
    InvitePreviewRead,
    InviteRead,
    InviteToken,
    PlatformAdminRow,
    PlatformEmailRead,
    PlatformHospitalDetail,
    PlatformHospitalRow,
    PlatformOverviewRead,
    PlatformTeamRow,
    TeamInviteCreate,
    TokenPair,
    TwoFactorChallengeRead,
)
from app.modules.identity.service import AuthContext
from app.modules.reporting import service as reporting_service
from app.modules.tenancy import service as tenancy_service
from app.modules.tenancy.schemas import HospitalRead

invites_router = APIRouter(prefix="/invites", tags=["invites"])
platform_router = APIRouter(prefix="/platform", tags=["platform"])

CanSendInvite = Annotated[AuthContext, Depends(require(Permissions.PLATFORM_INVITE_SEND))]
CanResendInvite = Annotated[
    AuthContext,
    Depends(require(Permissions.PLATFORM_INVITE_RESEND, Permissions.PLATFORM_INVITE_SEND)),
]
CanRevokeInvite = Annotated[AuthContext, Depends(require(Permissions.PLATFORM_INVITE_REVOKE))]
CanReadInvites = Annotated[AuthContext, Depends(require(Permissions.PLATFORM_INVITE_READ))]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
async def render_invites(session: SessionDep, rows: Sequence[Invite]) -> list[InviteRead]:
    """Invites with the names and email outcome a list row shows — three
    lookups for the page, not three per row."""
    hospitals = await tenancy_service.get_hospital_names(
        session, {row.hospital_id for row in rows if row.hospital_id is not None}
    )
    people = await service.get_user_names(session, {row.invited_by_id for row in rows})
    emails = await invites.latest_emails(session, [row.id for row in rows])
    rendered: list[InviteRead] = []
    for row in rows:
        email = emails.get(row.id)
        rendered.append(
            InviteRead.model_validate(
                {
                    **row.model_dump(exclude={"token_hash"}),
                    "hospital_name": hospitals.get(row.hospital_id) if row.hospital_id else None,
                    "invited_by_name": people.get(row.invited_by_id),
                    "last_email_status": email.status if email else None,
                    "last_email_error": email.error if email else None,
                }
            )
        )
    return rendered


async def _render_emails(
    session: SessionDep, rows: Sequence[PlatformEmail]
) -> list[PlatformEmailRead]:
    hospitals = await tenancy_service.get_hospital_names(
        session, {row.hospital_id for row in rows if row.hospital_id is not None}
    )
    people = await service.get_user_names(
        session, {row.sent_by_id for row in rows if row.sent_by_id is not None}
    )
    return [
        PlatformEmailRead.model_validate(
            {
                **row.model_dump(),
                "hospital_name": hospitals.get(row.hospital_id) if row.hospital_id else None,
                "sent_by_name": people.get(row.sent_by_id) if row.sent_by_id else None,
            }
        )
        for row in rows
    ]


async def issue_and_send(
    session: SessionDep,
    invite: Invite,
    raw_token: str,
    *,
    kind: PlatformEmailKind,
    context: AuthContext,
) -> InviteRead:
    """Commit the invite, email it, commit the email record, render."""
    await session.commit()
    await invites.send_invite_email(session, invite, raw_token, kind=kind, actor=context.user)
    await session.commit()
    return (await render_invites(session, [invite]))[0]


# ---------------------------------------------------------------------------
# The invited person (unauthenticated)
# ---------------------------------------------------------------------------
# POST with the token in the body, for both: a token in the path would be
# written to the access log on every page view.
@invites_router.post("/preview", response_model=InvitePreviewRead, summary="Preview an invite")
async def preview_invite(payload: InviteToken, session: SessionDep) -> InvitePreviewRead:
    preview = await invites.preview_invite(session, payload.token)
    await session.commit()
    return InvitePreviewRead(
        full_name=preview.full_name,
        email=preview.email,
        hospital_name=preview.hospital_name,
        role_codes=preview.role_codes,
        status=preview.status,
        expires_at=preview.expires_at,
        support_contact=settings.PLATFORM_SUPPORT_CONTACT,
    )


@invites_router.post(
    "/accept",
    response_model=TokenPair | TwoFactorChallengeRead,
    summary="Accept an invite: set a password and sign in",
)
async def accept_invite(
    payload: InviteAccept, session: SessionDep, request: Request
) -> TokenPair | TwoFactorChallengeRead:
    try:
        user, access_token, refresh_token, expires_in = await invites.accept_invite(
            session,
            payload.token,
            password=payload.password,
            ip_address=await get_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except service.TwoFactorChallenge as challenge:
        return TwoFactorChallengeRead(
            enrol=challenge.enrol,
            challenge_token=challenge.token,
            expires_in=challenge.expires_in,
        )
    await session.commit()
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        must_change_password=user.must_change_password,
    )


# ---------------------------------------------------------------------------
# The platform team: invites and email
# ---------------------------------------------------------------------------
@platform_router.post(
    "/hospitals/{hospital_id}/invites",
    response_model=InviteRead,
    status_code=status.HTTP_201_CREATED,
    summary="Invite an administrator to a hospital",
)
async def invite_hospital_admin(
    hospital_id: uuid.UUID,
    payload: HospitalAdminInviteCreate,
    session: SessionDep,
    context: CanSendInvite,
) -> InviteRead:
    await tenancy_service.get_hospital(session, hospital_id)  # 404 before 'suspended'
    invite, raw_token = await invites.create_invite(
        session,
        email=payload.email,
        full_name=payload.full_name,
        phone=payload.phone,
        hospital_id=hospital_id,
        role_codes=[Roles.HOSPITAL_ADMIN],
        actor=context.user,
    )
    return await issue_and_send(
        session, invite, raw_token, kind=PlatformEmailKind.INVITE, context=context
    )


@platform_router.get("/invites", response_model=Page[InviteRead], summary="List invites")
async def list_invites(
    session: SessionDep,
    context: CanReadInvites,
    params: Annotated[PageParams, Depends(page_params)],
    status_filter: Annotated[InviteStatus | None, Query(alias="status")] = None,
    hospital_id: uuid.UUID | None = None,
) -> Page[InviteRead]:
    rows, total = await invites.list_invites(
        session, params, status=status_filter, hospital_id=hospital_id
    )
    return Page.build(await render_invites(session, rows), total=total, params=params)


@platform_router.post(
    "/invites/{invite_id}/resend", response_model=InviteRead, summary="Resend an invite"
)
async def resend_invite(
    invite_id: uuid.UUID, session: SessionDep, context: CanResendInvite
) -> InviteRead:
    invite, raw_token = await invites.resend_invite(session, invite_id, actor=context.user)
    return await issue_and_send(
        session, invite, raw_token, kind=PlatformEmailKind.INVITE_RESEND, context=context
    )


@platform_router.post(
    "/invites/{invite_id}/revoke", response_model=InviteRead, summary="Cancel an invite"
)
async def revoke_invite(
    invite_id: uuid.UUID, session: SessionDep, context: CanRevokeInvite
) -> InviteRead:
    invite = await invites.revoke_invite(session, invite_id, actor=context.user)
    await session.commit()
    return (await render_invites(session, [invite]))[0]


@platform_router.get(
    "/emails", response_model=Page[PlatformEmailRead], summary="Emails the platform sent"
)
async def list_emails(
    session: SessionDep,
    context: CanReadInvites,
    params: Annotated[PageParams, Depends(page_params)],
    status_filter: Annotated[PlatformEmailStatus | None, Query(alias="status")] = None,
    kind: PlatformEmailKind | None = None,
    hospital_id: uuid.UUID | None = None,
    to: Annotated[str | None, Query(max_length=255)] = None,
) -> Page[PlatformEmailRead]:
    rows, total = await invites.list_platform_emails(
        session, params, status=status_filter, kind=kind, hospital_id=hospital_id, to=to
    )
    return Page.build(await _render_emails(session, rows), total=total, params=params)


# ---------------------------------------------------------------------------
# The platform team: hospitals, overview, team, activity
# ---------------------------------------------------------------------------
CanUseConsole = Annotated[AuthContext, Depends(require(Permissions.PLATFORM_CONSOLE))]
CanReadHospitals = Annotated[AuthContext, Depends(require(Permissions.PLATFORM_HOSPITAL_READ))]
CanManageHospitals = Annotated[AuthContext, Depends(require(Permissions.PLATFORM_HOSPITAL_MANAGE))]
CanManageTeam = Annotated[AuthContext, Depends(require(Permissions.PLATFORM_TEAM_MANAGE))]
CanReadActivity = Annotated[AuthContext, Depends(require(Permissions.PLATFORM_AUDIT_READ))]


def _team_row(user: User, roles: list[str]) -> PlatformTeamRow:
    return PlatformTeamRow.model_validate({**user.model_dump(), "roles": roles})


async def _hospital_detail(session: SessionDep, hospital_id: uuid.UUID) -> PlatformHospitalDetail:
    hospital = await tenancy_service.get_hospital(session, hospital_id)
    status_ = await platform.hospital_status(session, hospital)
    admins = (await platform.hospital_admins(session, [hospital.id])).get(hospital.id, [])
    invite_rows, _ = await invites.list_invites(
        session, PageParams(limit=20, offset=0), hospital_id=hospital.id
    )
    staff = (await platform.staff_counts(session, [hospital.id])).get(hospital.id, 0)
    usage = await reporting_service.hospital_usage(session, hospital.id)
    return PlatformHospitalDetail(
        hospital=HospitalRead.model_validate(hospital),
        status=status_,
        admins=[PlatformAdminRow.model_validate(admin) for admin in admins],
        invites=await render_invites(session, invite_rows),
        usage=HospitalUsageRead(staff=staff, **usage),
    )


@platform_router.get("/overview", response_model=PlatformOverviewRead, summary="Console overview")
async def get_overview(session: SessionDep, context: CanUseConsole) -> PlatformOverviewRead:
    summary = await platform.overview(session)
    return PlatformOverviewRead(
        active_hospitals=summary.active_hospitals,
        suspended_hospitals=summary.suspended_hospitals,
        pending_invites=summary.pending_invites,
        staff_accounts=summary.staff_accounts,
        emails_failed_7d=summary.emails_failed_7d,
        attention=[AttentionItemRead.model_validate(asdict(item)) for item in summary.attention],
        recent_activity=[AuditLogRead.model_validate(row) for row in summary.recent_activity],
    )


@platform_router.get(
    "/hospitals", response_model=Page[PlatformHospitalRow], summary="Every hospital"
)
async def list_hospitals(
    session: SessionDep,
    context: CanReadHospitals,
    params: Annotated[PageParams, Depends(page_params)],
    q: Annotated[str | None, Query(max_length=100)] = None,
    status_filter: Annotated[
        Literal["active", "suspended", "invite_pending"] | None, Query(alias="status")
    ] = None,
) -> Page[PlatformHospitalRow]:
    rows, total = await platform.list_platform_hospitals(
        session, params, search=q, status=status_filter
    )
    items = [
        PlatformHospitalRow(
            id=row.hospital.id,
            code=row.hospital.code,
            name=row.hospital.name,
            city=row.hospital.city,
            is_active=row.hospital.is_active,
            status=row.status,
            admin_name=row.admin_name,
            staff_count=row.staff_count,
            last_admin_login_at=row.last_admin_login_at,
            created_at=row.hospital.created_at,
        )
        for row in rows
    ]
    return Page.build(items, total=total, params=params)


@platform_router.post(
    "/hospitals",
    response_model=PlatformHospitalDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Onboard a hospital and invite its first administrator",
)
async def create_hospital_with_admin(
    payload: HospitalWithAdminCreate,
    session: SessionDep,
    request: Request,
    context: CanManageHospitals,
) -> PlatformHospitalDetail:
    # One transaction for both: if the invite is refused (the address already
    # has an account), the hospital is not left behind half-onboarded.
    hospital = await tenancy_service.create_hospital(session, payload.hospital)
    await service.record_audit(
        session,
        action="hospital.create",
        actor=context.user,
        hospital_id=hospital.id,
        resource_type="hospital",
        resource_id=hospital.id,
        changes={"code": hospital.code, "name": hospital.name},
        ip_address=await get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    invite, raw_token = await invites.create_invite(
        session,
        email=payload.admin.email,
        full_name=payload.admin.full_name,
        phone=payload.admin.phone,
        hospital_id=hospital.id,
        role_codes=[Roles.HOSPITAL_ADMIN],
        actor=context.user,
    )
    await issue_and_send(session, invite, raw_token, kind=PlatformEmailKind.INVITE, context=context)
    return await _hospital_detail(session, hospital.id)


@platform_router.get(
    "/hospitals/{hospital_id}", response_model=PlatformHospitalDetail, summary="One hospital"
)
async def get_hospital(
    hospital_id: uuid.UUID, session: SessionDep, context: CanReadHospitals
) -> PlatformHospitalDetail:
    return await _hospital_detail(session, hospital_id)


@platform_router.get("/team", response_model=Page[PlatformTeamRow], summary="The platform team")
async def list_team(
    session: SessionDep,
    context: CanUseConsole,
    params: Annotated[PageParams, Depends(page_params)],
) -> Page[PlatformTeamRow]:
    users, total = await platform.list_platform_team(session, params)
    roles = await service.get_roles_for_users(session, [user.id for user in users])
    return Page.build(
        [_team_row(user, roles.get(user.id, [])) for user in users], total=total, params=params
    )


@platform_router.post(
    "/team/invites",
    response_model=InviteRead,
    status_code=status.HTTP_201_CREATED,
    summary="Invite someone to the platform team",
)
async def invite_team_member(
    payload: TeamInviteCreate, session: SessionDep, context: CanManageTeam
) -> InviteRead:
    invite, raw_token = await invites.create_invite(
        session,
        email=payload.email,
        full_name=payload.full_name,
        phone=None,
        hospital_id=None,
        role_codes=[payload.role],
        actor=context.user,
    )
    return await issue_and_send(
        session, invite, raw_token, kind=PlatformEmailKind.INVITE, context=context
    )


@platform_router.post(
    "/team/{user_id}/deactivate",
    response_model=PlatformTeamRow,
    summary="Remove someone from the platform team",
)
async def deactivate_team_member(
    user_id: uuid.UUID, session: SessionDep, context: CanManageTeam
) -> PlatformTeamRow:
    user = await platform.deactivate_platform_user(session, user_id, actor=context.user)
    await session.commit()
    roles = await service.get_roles_for_users(session, [user.id])
    return _team_row(user, roles.get(user.id, []))


@platform_router.get(
    "/audit", response_model=Page[AuditLogRead], summary="Activity across the platform"
)
async def list_activity(
    session: SessionDep,
    context: CanReadActivity,
    params: Annotated[PageParams, Depends(page_params)],
    hospital_id: uuid.UUID | None = None,
    action: Annotated[str | None, Query(max_length=100)] = None,
    actor_user_id: uuid.UUID | None = None,
) -> Page[AuditLogRead]:
    entries, total = await platform.list_platform_activity(
        session, params, hospital_id=hospital_id, action=action, actor_user_id=actor_user_id
    )
    return Page.build(
        [AuditLogRead.model_validate(entry) for entry in entries], total=total, params=params
    )
