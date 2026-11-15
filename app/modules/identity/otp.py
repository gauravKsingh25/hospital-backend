"""One-time codes sent by email: signup verification and password reset.

Six digits from `secrets`, stored only as an HMAC keyed with
`OTP_HMAC_SECRET` (a leaked table is not a list of working codes, and a
million candidates cannot be hashed offline without the key). A code lives
ten minutes, survives five wrong guesses, works once, and is replaced by the
next one sent. Sends are spaced a minute apart and capped at five an hour per
address — those limits live here in Postgres, so they hold even when the
Redis rate limiter is down.

Every failure — wrong, expired, used, locked, never sent — raises the same
error with the same message: telling them apart would tell a guesser which
addresses have a code outstanding.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import col

from app.core.config import settings
from app.core.exceptions import DomainError
from app.core.mail import MailMessage, get_mailer
from app.core.models import utc_now
from app.modules.identity.models import (
    EmailOtp,
    OtpPurpose,
    PlatformEmail,
    PlatformEmailKind,
    PlatformEmailStatus,
)

__all__ = [
    "DAILY_FAILURE_CAP",
    "MAX_ATTEMPTS",
    "OtpInvalidError",
    "issue",
    "send_code_email",
    "send_signup_exists_email",
    "verify",
]

MAX_ATTEMPTS = 5
RESEND_COOLDOWN = timedelta(seconds=60)
HOURLY_CAP = 5
# Wrong guesses allowed per address and purpose in a rolling day, across all
# its codes. Without it, five guesses a code and five codes an hour gave an
# attacker 600 guesses a day at a reset code (security review M1); with it,
# ten a day: about one chance in 270 a year of guessing a six-digit code.
DAILY_FAILURE_CAP = 10
_REDACTED = "••••••"


class OtpInvalidError(DomainError):
    code = "otp_invalid"
    message = "That code is not valid. Check it, or ask for a new one."


def _secret() -> bytes:
    secret = settings.OTP_HMAC_SECRET.get_secret_value()
    # Production refuses to start without one (config); a developer's machine
    # falls back to the signing key rather than to nothing.
    return (secret or f"otp:{settings.SECRET_KEY}").encode()


async def _failures_today(
    session: AsyncSession, scope: tuple[ColumnElement[bool], ...], moment: datetime
) -> int:
    total = (
        await session.execute(
            select(func.coalesce(func.sum(EmailOtp.attempts), 0)).where(
                *scope, col(EmailOtp.created_at) > moment - timedelta(days=1)
            )
        )
    ).scalar_one()
    return int(total)


def _normalise(email: str) -> str:
    return email.strip().lower()


def _hash(email: str, purpose: OtpPurpose, code: str) -> str:
    message = f"{purpose.value}:{email}:{code}".encode()
    return hmac.new(_secret(), message, hashlib.sha256).hexdigest()


async def issue(
    session: AsyncSession,
    *,
    email: str,
    purpose: OtpPurpose,
    ip: str | None,
    now: datetime | None = None,
) -> str | None:
    """Create a code and return it, or None if this address must wait.

    The caller answers the same either way. Any earlier live code for the
    same address and purpose stops working.
    """
    moment = now or utc_now()
    address = _normalise(email)
    scope = (col(EmailOtp.email) == address, col(EmailOtp.purpose) == purpose)

    latest = (
        await session.execute(select(func.max(EmailOtp.created_at)).where(*scope))
    ).scalar_one_or_none()
    if latest is not None and moment - latest < RESEND_COOLDOWN:
        return None
    sent_last_hour = (
        await session.execute(
            select(func.count())
            .select_from(EmailOtp)
            .where(*scope, col(EmailOtp.created_at) > moment - timedelta(hours=1))
        )
    ).scalar_one()
    if sent_last_hour >= HOURLY_CAP:
        return None
    if await _failures_today(session, scope, moment) >= DAILY_FAILURE_CAP:
        return None

    await session.execute(
        update(EmailOtp)
        .where(*scope, col(EmailOtp.consumed_at).is_(None))
        .values(consumed_at=moment, updated_at=moment)
    )
    code = f"{secrets.randbelow(1_000_000):06d}"
    session.add(
        EmailOtp(
            email=address,
            purpose=purpose,
            code_hash=_hash(address, purpose, code),
            expires_at=moment + timedelta(minutes=settings.OTP_TTL_MINUTES),
            created_ip=ip,
            created_at=moment,
            updated_at=moment,
        )
    )
    await session.flush()
    return code


async def verify(
    session: AsyncSession,
    *,
    email: str,
    purpose: OtpPurpose,
    code: str,
    now: datetime | None = None,
) -> None:
    """Accept the code once, or raise `OtpInvalidError`.

    A wrong guess is committed before the error is raised: the request's
    session rolls back on the way out, and an attempt counter that rolls back
    with it counts nothing.
    """
    moment = now or utc_now()
    address = _normalise(email)
    row = (
        (
            await session.execute(
                select(EmailOtp)
                .where(
                    col(EmailOtp.email) == address,
                    col(EmailOtp.purpose) == purpose,
                    col(EmailOtp.consumed_at).is_(None),
                )
                .order_by(col(EmailOtp.created_at).desc())
                .limit(1)
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )
    if row is None or row.expires_at <= moment or row.attempts >= MAX_ATTEMPTS:
        raise OtpInvalidError()
    scope = (col(EmailOtp.email) == address, col(EmailOtp.purpose) == purpose)
    if await _failures_today(session, scope, moment) >= DAILY_FAILURE_CAP:
        raise OtpInvalidError()

    candidate = _hash(address, purpose, code.strip())
    if not hmac.compare_digest(candidate, row.code_hash):
        row.attempts += 1
        row.updated_at = moment
        session.add(row)
        await session.commit()
        raise OtpInvalidError()

    row.consumed_at = moment
    row.updated_at = moment
    session.add(row)
    await session.flush()


async def _send(
    session: AsyncSession,
    *,
    to: str,
    kind: PlatformEmailKind,
    subject: str,
    text: str,
    logged_text: str,
    logged_subject: str,
) -> None:
    """Send, then log what was sent with the code blanked out of every column."""
    result = await get_mailer().send(MailMessage(to=to, subject=subject, text=text))
    session.add(
        PlatformEmail(
            kind=kind,
            to_email=to,
            from_email=settings.MAIL_FROM,
            subject=logged_subject,
            body_text=logged_text,
            status=PlatformEmailStatus.SENT if result.ok else PlatformEmailStatus.FAILED,
            error=None if result.ok else result.error,
            provider_message_id=result.message_id,
            sent_at=utc_now(),
        )
    )
    await session.flush()


async def send_code_email(
    session: AsyncSession, *, email: str, code: str, purpose: OtpPurpose
) -> None:
    """Email the code. The logged copy has it blanked."""
    address = _normalise(email)
    minutes = settings.OTP_TTL_MINUTES
    if purpose == OtpPurpose.SIGNUP:
        kind = PlatformEmailKind.OTP
        subject = f"{code} is your Nirogya verification code"
        lead = "Use this code to verify your email address and finish signing up:"
    else:
        kind = PlatformEmailKind.PASSWORD_RESET
        subject = f"{code} is your Nirogya password reset code"
        lead = "Use this code to choose a new password:"
    body = (
        f"{lead}\n\n    {code}\n\n"
        f"It works once and expires in {minutes} minutes.\n"
        "If you didn't ask for this, you can ignore this email — nothing changes.\n"
    )
    await _send(
        session,
        to=address,
        kind=kind,
        subject=subject,
        text=body,
        logged_text=body.replace(code, _REDACTED),
        # The code is in the subject too, so it shows on a phone's lock screen.
        logged_subject=subject.replace(code, _REDACTED),
    )


async def send_signup_exists_email(session: AsyncSession, *, email: str) -> None:
    """Sent instead of a code when the address already has an account.

    The page answers identically either way; only the owner of the inbox
    learns that the account exists.
    """
    address = _normalise(email)
    body = (
        "Someone, probably you, tried to sign up for Nirogya with this email address.\n\n"
        "You already have an account. Sign in here:\n"
        f"{settings.APP_PUBLIC_URL}/login\n\n"
        "Forgotten your password? Reset it here:\n"
        f"{settings.APP_PUBLIC_URL}/forgot-password\n\n"
        "If this wasn't you, you can ignore this email.\n"
    )
    await _send(
        session,
        to=address,
        kind=PlatformEmailKind.SIGNUP_EXISTS,
        subject="You already have a Nirogya account",
        text=body,
        logged_text=body,
        logged_subject="You already have a Nirogya account",
    )
