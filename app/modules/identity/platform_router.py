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
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.config import settings
from app.core.deps import SessionDep, get_client_ip, require
from app.core.pagination import Page, PageParams, page_params
from app.modules.identity import invites, service
from app.modules.identity.models import (
    Invite,
    InviteStatus,
    PlatformEmail,
    PlatformEmailKind,
    PlatformEmailStatus,
)
from app.modules.identity.rbac import Permissions, Roles
from app.modules.identity.schemas import (
    HospitalAdminInviteCreate,
    InviteAccept,
    InvitePreviewRead,
    InviteRead,
    PlatformEmailRead,
    TokenPair,
    TwoFactorChallengeRead,
)
from app.modules.identity.service import AuthContext
from app.modules.tenancy import service as tenancy_service

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
@invites_router.get("/{token}", response_model=InvitePreviewRead, summary="Preview an invite")
async def preview_invite(token: str, session: SessionDep) -> InvitePreviewRead:
    preview = await invites.preview_invite(session, token)
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
    "/{token}/accept",
    response_model=TokenPair | TwoFactorChallengeRead,
    summary="Accept an invite: set a password and sign in",
)
async def accept_invite(
    token: str, payload: InviteAccept, session: SessionDep, request: Request
) -> TokenPair | TwoFactorChallengeRead:
    try:
        user, access_token, refresh_token, expires_in = await invites.accept_invite(
            session,
            token,
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
