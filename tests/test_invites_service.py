"""Invitations: the only door to an administrator account.

What is pinned here is the set of promises the invite link makes to the person
holding it and to the platform owner who sent it: it works once, it stops
working when it is replaced, cancelled or old, nobody but the recipient ever
knows the password, and whether the email went out is on record.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.config import settings
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.mail import MailMessage, MailResult
from app.core.models import utc_now
from app.modules.identity import invites
from app.modules.identity.models import (
    Invite,
    InviteStatus,
    PlatformEmail,
    PlatformEmailKind,
    PlatformEmailStatus,
    User,
)
from app.modules.identity.rbac import Roles
from app.modules.identity.service import TwoFactorChallenge, resolve_permissions
from app.modules.tenancy.models import Hospital

pytestmark = pytest.mark.integration

PASSWORD = "a-perfectly-reasonable-passphrase"


class FakeMailer:
    def __init__(self, result: MailResult | None = None) -> None:
        self.sent: list[MailMessage] = []
        self.result = result or MailResult(ok=True, message_id="<m1@example>")

    async def send(self, message: MailMessage) -> MailResult:
        self.sent.append(message)
        return self.result


@pytest.fixture
def mailer(monkeypatch: pytest.MonkeyPatch) -> FakeMailer:
    fake = FakeMailer()
    monkeypatch.setattr(invites, "get_mailer", lambda: fake)
    return fake


async def _invite(
    session: AsyncSession,
    hospital: Hospital,
    actor: User,
    *,
    email: str = "new.admin@example.com",
) -> tuple[Invite, str]:
    invite, token = await invites.create_invite(
        session,
        email=email,
        full_name="New Admin",
        phone=None,
        hospital_id=hospital.id,
        role_codes=[Roles.HOSPITAL_ADMIN],
        actor=actor,
    )
    await session.commit()
    return invite, token


async def _users_named(session: AsyncSession, email: str) -> int:
    # `users` is under row-level security; an unbound session sees nobody.
    async with db.system_context(session):
        return int(
            (
                await session.execute(
                    sa.text("SELECT count(*) FROM users WHERE email = :email"), {"email": email}
                )
            ).scalar_one()
        )


async def _suspend(session: AsyncSession, hospital: Hospital) -> None:
    async with db.system_context(session):
        await session.execute(
            sa.update(Hospital).where(Hospital.id == hospital.id).values(is_active=False)
        )
    await session.commit()


class TestCreate:
    async def test_create_invite_stores_hash_not_token(
        self, session: AsyncSession, hospital: Hospital, platform_admin: User
    ) -> None:
        invite, token = await _invite(session, hospital, platform_admin)

        assert invite.token_hash == hashlib.sha256(token.encode()).hexdigest()
        row = (
            (
                await session.execute(
                    sa.text("SELECT * FROM invites WHERE id = :id"), {"id": invite.id}
                )
            )
            .mappings()
            .one()
        )
        assert all(token not in str(value) for value in row.values())
        ttl = invite.expires_at - utc_now()
        assert timedelta(hours=47, minutes=59) < ttl <= timedelta(hours=48)
        assert invite.status == InviteStatus.PENDING

    async def test_duplicate_pending_invite_conflicts(
        self, session: AsyncSession, hospital: Hospital, platform_admin: User
    ) -> None:
        await _invite(session, hospital, platform_admin, email="dup@example.com")
        with pytest.raises(ConflictError) as excinfo:
            await _invite(session, hospital, platform_admin, email="DUP@example.com")
        assert excinfo.value.code == "invite_pending"

    async def test_invite_for_existing_user_conflicts(
        self, session: AsyncSession, hospital: Hospital, platform_admin: User, receptionist: User
    ) -> None:
        with pytest.raises(ConflictError) as excinfo:
            await _invite(session, hospital, platform_admin, email=receptionist.email)
        assert excinfo.value.code == "email_taken"

    async def test_invite_to_suspended_hospital_refused(
        self, session: AsyncSession, hospital: Hospital, platform_admin: User
    ) -> None:
        await _suspend(session, hospital)
        with pytest.raises(PermissionDeniedError) as excinfo:
            await _invite(session, hospital, platform_admin)
        assert excinfo.value.code == "hospital_suspended"

    async def test_team_invite_requires_platform_role_and_no_hospital(
        self, session: AsyncSession, platform_admin: User
    ) -> None:
        with pytest.raises(ValidationError):
            await invites.create_invite(
                session,
                email="x@example.com",
                full_name="X",
                phone=None,
                hospital_id=None,
                role_codes=[Roles.DOCTOR],
                actor=platform_admin,
            )


class TestSend:
    async def test_send_records_email_with_redacted_link(
        self,
        session: AsyncSession,
        hospital: Hospital,
        platform_admin: User,
        mailer: FakeMailer,
    ) -> None:
        invite, token = await _invite(session, hospital, platform_admin)

        email = await invites.send_invite_email(
            session, invite, token, kind=PlatformEmailKind.INVITE, actor=platform_admin
        )
        await session.commit()

        assert email.status == PlatformEmailStatus.SENT
        assert email.provider_message_id == "<m1@example>"
        assert email.to_email == "new.admin@example.com"
        assert email.hospital_id == hospital.id
        assert token in mailer.sent[0].text
        assert token not in email.body_text
        assert "/invite/••••" in email.body_text
        assert hospital.name in email.subject
        assert invite.send_count == 1
        assert invite.last_sent_at is not None

    async def test_smtp_failure_recorded_without_secret(
        self,
        session: AsyncSession,
        hospital: Hospital,
        platform_admin: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake = FakeMailer(MailResult(ok=False, error="SMTPAuthenticationError: (535, b'no')"))
        monkeypatch.setattr(invites, "get_mailer", lambda: fake)
        invite, token = await _invite(session, hospital, platform_admin)

        email = await invites.send_invite_email(
            session, invite, token, kind=PlatformEmailKind.INVITE, actor=platform_admin
        )
        await session.commit()

        assert email.status == PlatformEmailStatus.FAILED
        assert email.error is not None
        assert email.error.startswith("SMTPAuthenticationError")
        assert invite.status == InviteStatus.PENDING


class TestAccept:
    async def test_accept_creates_signed_in_hospital_admin(
        self, session: AsyncSession, hospital: Hospital, platform_admin: User
    ) -> None:
        invite, token = await _invite(session, hospital, platform_admin)

        user, access_token, _, _ = await invites.accept_invite(
            session, token, password=PASSWORD, ip_address=None, user_agent=None
        )
        await session.commit()

        assert access_token
        assert user.hospital_id == hospital.id
        assert user.must_change_password is False
        roles, _ = await resolve_permissions(session, user)
        assert roles == {Roles.HOSPITAL_ADMIN}
        await session.refresh(invite)
        assert invite.status == InviteStatus.ACCEPTED
        assert invite.accepted_user_id == user.id

    async def test_accept_twice_refused(
        self, session: AsyncSession, hospital: Hospital, platform_admin: User
    ) -> None:
        _, token = await _invite(session, hospital, platform_admin)
        await invites.accept_invite(
            session, token, password=PASSWORD, ip_address=None, user_agent=None
        )
        await session.commit()

        with pytest.raises(ConflictError) as excinfo:
            await invites.accept_invite(
                session, token, password=PASSWORD, ip_address=None, user_agent=None
            )
        assert excinfo.value.code == "invite_used"

    async def test_accept_expired_refused(
        self, session: AsyncSession, hospital: Hospital, platform_admin: User
    ) -> None:
        invite, token = await _invite(session, hospital, platform_admin)
        invite.expires_at = utc_now() - timedelta(minutes=1)
        session.add(invite)
        await session.commit()

        with pytest.raises(ConflictError) as excinfo:
            await invites.accept_invite(
                session, token, password=PASSWORD, ip_address=None, user_agent=None
            )
        await session.commit()
        assert excinfo.value.code == "invite_expired"
        await session.refresh(invite)
        assert invite.status == InviteStatus.EXPIRED

    async def test_accept_revoked_refused(
        self, session: AsyncSession, hospital: Hospital, platform_admin: User
    ) -> None:
        invite, token = await _invite(session, hospital, platform_admin)
        await invites.revoke_invite(session, invite.id, actor=platform_admin)
        await session.commit()

        with pytest.raises(ConflictError) as excinfo:
            await invites.accept_invite(
                session, token, password=PASSWORD, ip_address=None, user_agent=None
            )
        assert excinfo.value.code == "invite_revoked"

    async def test_accept_refused_when_hospital_suspended(
        self, session: AsyncSession, hospital: Hospital, platform_admin: User
    ) -> None:
        _, token = await _invite(session, hospital, platform_admin)
        await _suspend(session, hospital)

        with pytest.raises(PermissionDeniedError) as excinfo:
            await invites.accept_invite(
                session, token, password=PASSWORD, ip_address=None, user_agent=None
            )
        assert excinfo.value.code == "hospital_suspended"
        await session.rollback()
        count = await _users_named(session, "new.admin@example.com")
        assert count == 0

    async def test_accept_weak_password_refused(
        self, session: AsyncSession, hospital: Hospital, platform_admin: User
    ) -> None:
        invite, token = await _invite(session, hospital, platform_admin)

        with pytest.raises(ValidationError) as excinfo:
            await invites.accept_invite(
                session, token, password="short", ip_address=None, user_agent=None
            )
        assert excinfo.value.code == "weak_password"
        await session.rollback()
        await session.refresh(invite)
        assert invite.status == InviteStatus.PENDING

    async def test_unknown_token_is_invalid(self, session: AsyncSession) -> None:
        with pytest.raises(NotFoundError) as excinfo:
            await invites.preview_invite(session, "no-such-token")
        assert excinfo.value.code == "invite_invalid"

    async def test_preview_names_the_hospital(
        self, session: AsyncSession, hospital: Hospital, platform_admin: User
    ) -> None:
        _, token = await _invite(session, hospital, platform_admin)

        preview = await invites.preview_invite(session, token)

        assert preview.full_name == "New Admin"
        assert preview.email == "new.admin@example.com"
        assert preview.hospital_name == hospital.name
        assert preview.status == InviteStatus.PENDING

    async def test_resend_kills_old_token(
        self,
        session: AsyncSession,
        hospital: Hospital,
        platform_admin: User,
        mailer: FakeMailer,
    ) -> None:
        old, old_token = await _invite(session, hospital, platform_admin)

        new, new_token = await invites.resend_invite(session, old.id, actor=platform_admin)
        await session.commit()

        await session.refresh(old)
        assert old.status == InviteStatus.REVOKED
        assert old.superseded_by_id == new.id
        assert new.status == InviteStatus.PENDING
        with pytest.raises(ConflictError) as excinfo:
            await invites.accept_invite(
                session, old_token, password=PASSWORD, ip_address=None, user_agent=None
            )
        assert excinfo.value.code == "invite_revoked"
        await session.rollback()
        user, *_ = await invites.accept_invite(
            session, new_token, password=PASSWORD, ip_address=None, user_agent=None
        )
        assert user.email == "new.admin@example.com"

    async def test_concurrent_accept_creates_one_user(
        self, session: AsyncSession, hospital: Hospital, platform_admin: User
    ) -> None:
        _, token = await _invite(session, hospital, platform_admin)

        async def attempt() -> object:
            async with db.get_sessionmaker()() as own:
                try:
                    result = await invites.accept_invite(
                        own, token, password=PASSWORD, ip_address=None, user_agent=None
                    )
                    await own.commit()
                    return result
                except ConflictError as error:
                    await own.rollback()
                    return error

        outcomes = await asyncio.gather(attempt(), attempt())

        assert sum(isinstance(outcome, ConflictError) for outcome in outcomes) == 1
        count = await _users_named(session, "new.admin@example.com")
        assert count == 1

    async def test_accept_with_two_factor_required_raises_challenge(
        self,
        session: AsyncSession,
        hospital: Hospital,
        platform_admin: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "TWO_FACTOR_REQUIRED_ROLES", [Roles.HOSPITAL_ADMIN])
        invite, token = await _invite(session, hospital, platform_admin)

        with pytest.raises(TwoFactorChallenge):
            await invites.accept_invite(
                session, token, password=PASSWORD, ip_address=None, user_agent=None
            )

        await session.refresh(invite)
        assert invite.status == InviteStatus.ACCEPTED


class TestSweep:
    async def test_expire_due_invites_marks_only_past_pending(
        self, session: AsyncSession, hospital: Hospital, platform_admin: User
    ) -> None:
        past, _ = await _invite(session, hospital, platform_admin, email="past@example.com")
        future, _ = await _invite(session, hospital, platform_admin, email="future@example.com")
        done, token = await _invite(session, hospital, platform_admin, email="done@example.com")
        await invites.accept_invite(
            session, token, password=PASSWORD, ip_address=None, user_agent=None
        )
        for invite in (past, done):
            invite.expires_at = utc_now() - timedelta(hours=1)
            session.add(invite)
        await session.commit()

        assert await invites.expire_due_invites(session) == 1
        await session.commit()

        for invite in (past, future, done):
            await session.refresh(invite)
        assert past.status == InviteStatus.EXPIRED
        assert future.status == InviteStatus.PENDING
        assert done.status == InviteStatus.ACCEPTED


async def test_platform_email_rows_are_listed_newest_first(
    session: AsyncSession, hospital: Hospital, platform_admin: User, mailer: FakeMailer
) -> None:
    from app.core.pagination import PageParams

    for address in ("a@example.com", "b@example.com"):
        invite, token = await _invite(session, hospital, platform_admin, email=address)
        await invites.send_invite_email(
            session, invite, token, kind=PlatformEmailKind.INVITE, actor=platform_admin
        )
    await session.commit()

    rows, total = await invites.list_platform_emails(session, PageParams(limit=10, offset=0))

    assert total == 2
    assert [row.to_email for row in rows] == ["b@example.com", "a@example.com"]
    assert all(isinstance(row, PlatformEmail) for row in rows)
