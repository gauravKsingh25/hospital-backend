"""Self-service signup, the hand-off to the app, password reset, contact inbox.

Signup is three requests (spec §5): details (a code is emailed), the code (a
short-lived signup token comes back), and a password (the account is
created). The account has no hospital and no role, so it can do nothing but
pay; the platform owner's approval of that payment is what makes it a
hospital administrator.

Nothing here tells a caller whether an email address has an account: `start`
and `forgot` answer the same for every address, and only the inbox's owner
learns the difference.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col

from app.core.config import settings
from app.core.database import session_scope, system_context
from app.core.exceptions import AuthenticationError, DomainError, ValidationError
from app.core.mail import MailMessage, get_mailer
from app.core.models import utc_now
from app.core.pagination import PageParams
from app.core.security import (
    SIGNUP,
    create_signup_token,
    decode_token,
    fingerprint,
    generate_opaque_token,
    hash_password,
    validate_password_strength,
)
from app.modules.identity import otp
from app.modules.identity.models import (
    ContactMessage,
    OtpPurpose,
    PlatformEmail,
    PlatformEmailKind,
    PlatformEmailStatus,
    SignupIntent,
    SignupIntentStatus,
    User,
)
from app.modules.identity.schemas import UserCreate
from app.modules.identity.service import (
    create_user,
    get_user_by_email,
    issue_token_pair,
    record_audit,
    revoke_all_refresh_tokens,
)
from app.modules.subscriptions import service as subscriptions_service

logger = logging.getLogger(__name__)

__all__ = [
    "HANDOFF_SECONDS",
    "PendingCode",
    "complete",
    "create_contact_message",
    "exchange_handoff",
    "expire_stale_intents",
    "forgot_password_later",
    "latest_completed_intent",
    "list_contact_messages",
    "request_password_reset",
    "resend",
    "reset_password",
    "send_password_reset",
    "start",
    "verify",
]

HANDOFF_SECONDS = 120
INTENT_LIFETIME = timedelta(hours=24)


class SignupTokenInvalidError(DomainError):
    code = "signup_token_invalid"
    message = "This signup link has expired or was already used. Please start again."


class HandoffInvalidError(DomainError):
    code = "handoff_invalid"
    message = "This sign-in link has expired or was already used. Please sign in."


async def _latest_intent(
    session: AsyncSession, email: str, status: SignupIntentStatus
) -> SignupIntent | None:
    row: SignupIntent | None = (
        (
            await session.execute(
                select(SignupIntent)
                .where(col(SignupIntent.email) == email, col(SignupIntent.status) == status)
                .order_by(col(SignupIntent.created_at).desc())
                .limit(1)
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )
    return row


async def start(
    session: AsyncSession,
    *,
    email: str,
    full_name: str,
    hospital_name: str,
    plan_code: str,
    phone: str | None,
    ip: str | None,
) -> None:
    """Record the details and email a code — or, for an address that already
    has an account, an email saying so. The caller answers the same either way."""
    if plan_code not in {
        plan.code for plan in await subscriptions_service.list_active_plans(session)
    }:
        raise ValidationError("That plan does not exist.", code="plan_unknown")

    if await get_user_by_email(session, email) is not None:
        # The OTP cooldown and hourly cap space these emails out too: the code
        # itself is never sent, and verifying it finds no signup to verify.
        if await otp.issue(session, email=email, purpose=OtpPurpose.SIGNUP, ip=ip) is not None:
            await otp.send_signup_exists_email(session, email=email)
        return

    # One signup in flight per address: the newest details win.
    await session.execute(
        update(SignupIntent)
        .where(
            col(SignupIntent.email) == email,
            col(SignupIntent.status).in_([SignupIntentStatus.STARTED, SignupIntentStatus.VERIFIED]),
        )
        .values(status=SignupIntentStatus.EXPIRED, updated_at=utc_now())
    )
    session.add(
        SignupIntent(
            email=email,
            full_name=full_name,
            hospital_name=hospital_name,
            plan_code=plan_code,
            phone=phone,
        )
    )
    await session.flush()
    code = await otp.issue(session, email=email, purpose=OtpPurpose.SIGNUP, ip=ip)
    if code is not None:
        await otp.send_code_email(session, email=email, code=code, purpose=OtpPurpose.SIGNUP)


async def resend(session: AsyncSession, *, email: str, ip: str | None) -> None:
    """Send a fresh code for a signup in progress. Silent otherwise."""
    if await _latest_intent(session, email, SignupIntentStatus.STARTED) is None:
        return
    if await get_user_by_email(session, email) is not None:
        return
    code = await otp.issue(session, email=email, purpose=OtpPurpose.SIGNUP, ip=ip)
    if code is not None:
        await otp.send_code_email(session, email=email, code=code, purpose=OtpPurpose.SIGNUP)


async def verify(session: AsyncSession, *, email: str, code: str) -> tuple[str, int]:
    """Check the code; return a signup token for the password step."""
    await otp.verify(session, email=email, purpose=OtpPurpose.SIGNUP, code=code)
    intent = await _latest_intent(session, email, SignupIntentStatus.STARTED)
    if intent is None:
        # A code with no signup behind it (the address already had an
        # account): indistinguishable from a wrong code, by design.
        raise otp.OtpInvalidError()
    now = utc_now()
    intent.status = SignupIntentStatus.VERIFIED
    intent.verified_at = now
    intent.updated_at = now
    session.add(intent)
    await session.flush()
    token, claims = create_signup_token(intent.id)
    return token, int((claims.expires_at - claims.issued_at).total_seconds())


def _intent_id(signup_token: str) -> uuid.UUID:
    try:
        return decode_token(signup_token, expected_type=SIGNUP).subject
    except AuthenticationError as exc:
        raise SignupTokenInvalidError() from exc


async def complete(
    session: AsyncSession,
    *,
    signup_token: str,
    password: str,
    ip: str | None,
    user_agent: str | None,
) -> str:
    """Create the account. Returns the one-time code that signs it in to the app."""
    intent_id = _intent_id(signup_token)
    # Before the token is spent: a weak password should cost a retry, not a
    # whole new signup.
    validate_password_strength(password)
    intent: SignupIntent | None = (
        (
            await session.execute(
                select(SignupIntent).where(col(SignupIntent.id) == intent_id).with_for_update()
            )
        )
        .scalars()
        .first()
    )
    if intent is None or intent.status != SignupIntentStatus.VERIFIED:
        raise SignupTokenInvalidError()
    if await get_user_by_email(session, intent.email) is not None:
        raise SignupTokenInvalidError()

    user = await create_user(
        session,
        UserCreate(
            email=intent.email,
            full_name=intent.full_name,
            phone=intent.phone,
            password=password,
            role_codes=[],
        ),
        hospital_id=None,
        must_change_password=False,
    )

    handoff = generate_opaque_token()
    now = utc_now()
    intent.status = SignupIntentStatus.COMPLETED
    intent.user_id = user.id
    intent.handoff_code_hash = fingerprint(handoff)
    intent.handoff_expires_at = now + timedelta(seconds=HANDOFF_SECONDS)
    intent.updated_at = now
    session.add(intent)
    await session.flush()
    await record_audit(
        session,
        action="signup.complete",
        actor=user,
        resource_type="user",
        resource_id=user.id,
        ip_address=ip,
        user_agent=user_agent,
        changes={"hospital_name": intent.hospital_name, "plan": intent.plan_code},
    )
    return handoff


async def exchange_handoff(
    session: AsyncSession, *, code: str, ip: str | None, user_agent: str | None
) -> tuple[User, str, str, int]:
    """Turn the one-time code into a session. Works once, for two minutes."""
    intent: SignupIntent | None = (
        (
            await session.execute(
                select(SignupIntent)
                .where(col(SignupIntent.handoff_code_hash) == fingerprint(code))
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )
    now = utc_now()
    if (
        intent is None
        or intent.user_id is None
        or intent.handoff_expires_at is None
        or intent.handoff_expires_at <= now
    ):
        raise HandoffInvalidError()
    intent.handoff_code_hash = None
    intent.handoff_expires_at = None
    intent.updated_at = now
    session.add(intent)

    async with system_context(session):
        user = await session.get(User, intent.user_id)
    if user is None or not user.is_active or user.deleted_at is not None:
        raise HandoffInvalidError()

    user.last_login_at = now
    async with system_context(session):
        session.add(user)
        await session.flush()
    access, refresh, expires_in = await issue_token_pair(
        session, user, ip_address=ip, user_agent=user_agent
    )
    await record_audit(
        session,
        action="auth.login",
        actor=user,
        resource_type="user",
        resource_id=user.id,
        ip_address=ip,
        user_agent=user_agent,
        changes={"via": "signup_handoff"},
    )
    return user, access, refresh, expires_in


async def latest_completed_intent(session: AsyncSession, user_id: uuid.UUID) -> SignupIntent | None:
    """The details a signed-up user gave: their hospital's name and plan."""
    row: SignupIntent | None = (
        (
            await session.execute(
                select(SignupIntent)
                .where(
                    col(SignupIntent.user_id) == user_id,
                    col(SignupIntent.status) == SignupIntentStatus.COMPLETED,
                )
                .order_by(col(SignupIntent.created_at).desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    return row


async def expire_stale_intents(session: AsyncSession, now: datetime | None = None) -> int:
    """Signups abandoned for a day stop being finishable."""
    cutoff = (now or utc_now()) - INTENT_LIFETIME
    result = await session.execute(
        update(SignupIntent)
        .where(
            col(SignupIntent.status).in_([SignupIntentStatus.STARTED, SignupIntentStatus.VERIFIED]),
            col(SignupIntent.created_at) < cutoff,
        )
        .values(status=SignupIntentStatus.EXPIRED, updated_at=utc_now())
    )
    await session.flush()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Forgotten password
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PendingCode:
    """A reset code recorded in the request, to be emailed after the answer."""

    email: str
    code: str


async def request_password_reset(
    session: AsyncSession, *, email: str, ip: str | None
) -> PendingCode | None:
    """Record a reset code if an active account has this address.

    The email is **not** sent here. A mail send takes hundreds of milliseconds
    and would happen only for real accounts, so the answer's timing would say
    who has one (security review M2). The caller commits, answers, and then
    hands the returned code to `send_password_reset`.
    """
    user = await get_user_by_email(session, email)
    if user is None or not user.is_active:
        return None
    code = await otp.issue(session, email=email, purpose=OtpPurpose.RESET, ip=ip)
    if code is None:
        return None
    return PendingCode(email=email.strip().lower(), code=code)


async def forgot_password_later(email: str, ip: str | None) -> None:
    """The whole forgot-password step, run after the answer has gone.

    The route does nothing that depends on the address — not even the lookup
    — so a real account and an unknown one answer in the same time (security
    review M2). Own session; failures are logged, never raised.
    """
    try:
        async with session_scope() as session:
            pending = await request_password_reset(session, email=email, ip=ip)
            await session.commit()
        if pending is not None:
            await send_password_reset(pending)
    except Exception:
        logger.exception("password reset request could not be processed")


async def send_password_reset(pending: PendingCode) -> None:
    """Email a recorded reset code, after the request has been answered.

    Runs with its own session: the request's is closed by then. A failure is
    logged, never raised, since nobody is waiting for this answer; the email
    log in the console shows the failed send.
    """
    try:
        async with session_scope() as session:
            await otp.send_code_email(
                session, email=pending.email, code=pending.code, purpose=OtpPurpose.RESET
            )
    except Exception:
        logger.exception("password reset email could not be sent")


async def reset_password(
    session: AsyncSession,
    *,
    email: str,
    code: str,
    password: str,
    ip: str | None,
    user_agent: str | None,
) -> None:
    """Set a new password with an emailed code. Ends every existing session."""
    # Checked first so a weak password does not burn the code.
    validate_password_strength(password)
    await otp.verify(session, email=email, purpose=OtpPurpose.RESET, code=code)
    user = await get_user_by_email(session, email)
    if user is None or not user.is_active:
        raise otp.OtpInvalidError()

    now = utc_now()
    user.password_hash = hash_password(password)
    user.must_change_password = False
    user.password_changed_at = now
    user.failed_login_attempts = 0
    user.locked_until = None
    user.updated_at = now
    async with system_context(session):
        session.add(user)
        await session.flush()
    await revoke_all_refresh_tokens(session, user.id, reason="password_reset")
    await record_audit(
        session,
        action="auth.password_reset",
        actor=user,
        hospital_id=user.hospital_id,
        resource_type="user",
        resource_id=user.id,
        ip_address=ip,
        user_agent=user_agent,
    )


# ---------------------------------------------------------------------------
# Contact form
# ---------------------------------------------------------------------------
async def create_contact_message(
    session: AsyncSession,
    *,
    name: str,
    email: str,
    phone: str | None,
    hospital_name: str | None,
    message: str,
    ip: str | None,
) -> ContactMessage:
    """Keep the message and tell the inbox. The email is a nudge; the row is the record."""
    row = ContactMessage(
        name=name.strip(),
        email=email,
        phone=phone,
        hospital_name=hospital_name.strip() if hospital_name else None,
        message=message.strip(),
        created_ip=ip,
    )
    session.add(row)
    await session.flush()

    inbox = settings.contact_inbox
    subject = f"Website enquiry from {row.name}"
    body = (
        f"From: {row.name} <{row.email}>\n"
        f"Phone: {row.phone or '—'}\n"
        f"Hospital: {row.hospital_name or '—'}\n\n"
        f"{row.message}\n"
    )
    result = await get_mailer().send(MailMessage(to=inbox, subject=subject, text=body))
    session.add(
        PlatformEmail(
            kind=PlatformEmailKind.CONTACT_ALERT,
            to_email=inbox,
            from_email=settings.MAIL_FROM,
            subject=subject,
            body_text=body,
            status=PlatformEmailStatus.SENT if result.ok else PlatformEmailStatus.FAILED,
            error=None if result.ok else result.error,
            provider_message_id=result.message_id,
            sent_at=utc_now(),
        )
    )
    await session.flush()
    return row


async def list_contact_messages(
    session: AsyncSession, params: PageParams
) -> tuple[list[ContactMessage], int]:
    total = (await session.execute(select(func.count()).select_from(ContactMessage))).scalar_one()
    rows = (
        (
            await session.execute(
                select(ContactMessage)
                .order_by(col(ContactMessage.created_at).desc())
                .limit(params.limit)
                .offset(params.offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)
