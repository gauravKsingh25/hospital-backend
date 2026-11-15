"""Email codes for signup and password reset (spec §8 "OTP").

Six digits, keyed hash at rest, ten minutes, five attempts, single use, a
minute between sends and five an hour. Every refusal is the same refusal.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import DomainError
from app.core.mail import MailMessage, MailResult
from app.core.models import utc_now
from app.modules.identity import otp
from app.modules.identity.models import EmailOtp, OtpPurpose, PlatformEmail

EMAIL = "new.owner@example.com"
SIGNUP = OtpPurpose.SIGNUP


async def _issue(session: AsyncSession, *, minutes_ago: float = 0) -> str:
    code = await otp.issue(
        session,
        email=EMAIL,
        purpose=SIGNUP,
        ip=None,
        now=utc_now() - timedelta(minutes=minutes_ago),
    )
    assert code is not None
    await session.commit()
    return code


async def _refused(session: AsyncSession, code: str, **kwargs: object) -> None:
    with pytest.raises(DomainError) as caught:
        await otp.verify(session, email=EMAIL, purpose=SIGNUP, code=code, **kwargs)  # type: ignore[arg-type]
    assert caught.value.code == "otp_invalid"
    assert caught.value.status_code == 400


async def test_verify_accepts_the_code_once(session: AsyncSession) -> None:
    code = await _issue(session)
    await otp.verify(session, email=EMAIL, purpose=SIGNUP, code=code)
    await session.commit()
    await _refused(session, code)


async def test_codes_are_six_digits(session: AsyncSession) -> None:
    code = await _issue(session)
    assert len(code) == 6 and code.isdigit()


async def test_expired_code_is_refused(session: AsyncSession) -> None:
    code = await _issue(session)
    await _refused(session, code, now=utc_now() + timedelta(minutes=11))


async def test_fifth_wrong_attempt_locks_the_code(session: AsyncSession) -> None:
    code = await _issue(session)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        await _refused(session, wrong)
    await _refused(session, code)


async def test_a_wrong_attempt_is_counted_even_though_the_request_fails(
    session: AsyncSession,
) -> None:
    code = await _issue(session)
    wrong = "000000" if code != "000000" else "111111"
    await _refused(session, wrong)
    await session.rollback()
    attempts = (await session.execute(sa.select(EmailOtp.attempts))).scalar_one()
    assert attempts == 1


async def test_resend_within_60s_is_refused(session: AsyncSession) -> None:
    await _issue(session)
    assert await otp.issue(session, email=EMAIL, purpose=SIGNUP, ip=None) is None


async def test_resend_after_a_minute_is_allowed(session: AsyncSession) -> None:
    await _issue(session, minutes_ago=2)
    assert await otp.issue(session, email=EMAIL, purpose=SIGNUP, ip=None) is not None


async def test_sixth_code_in_an_hour_is_refused(session: AsyncSession) -> None:
    for minutes_ago in (50, 40, 30, 20, 10):
        await _issue(session, minutes_ago=minutes_ago)
    assert await otp.issue(session, email=EMAIL, purpose=SIGNUP, ip=None) is None


async def test_new_code_kills_the_old_one(session: AsyncSession) -> None:
    old = await _issue(session, minutes_ago=2)
    new = await _issue(session)
    if old != new:
        await _refused(session, old)
    await otp.verify(session, email=EMAIL, purpose=SIGNUP, code=new)


async def test_a_reset_code_does_not_verify_a_signup(session: AsyncSession) -> None:
    code = await otp.issue(session, email=EMAIL, purpose=OtpPurpose.RESET, ip=None)
    assert code is not None
    await session.commit()
    await _refused(session, code)


async def test_code_hash_is_not_the_code(session: AsyncSession) -> None:
    code = await _issue(session)
    stored = (await session.execute(sa.select(EmailOtp.code_hash))).scalar_one()
    assert stored != code and code not in stored
    assert len(stored) == 64


async def test_email_is_matched_case_insensitively(session: AsyncSession) -> None:
    code = await _issue(session)
    await otp.verify(session, email=EMAIL.upper(), purpose=SIGNUP, code=code)


async def test_logged_email_body_has_no_code(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[MailMessage] = []

    class Capture:
        async def send(self, message: MailMessage) -> MailResult:
            sent.append(message)
            return MailResult(ok=True, message_id="<m@test>")

    monkeypatch.setattr(otp, "get_mailer", lambda: Capture())
    await otp.send_code_email(session, email=EMAIL, code="482913", purpose=SIGNUP)

    assert "482913" in sent[0].text
    logged = (await session.execute(sa.select(PlatformEmail))).scalars().one()
    # Nowhere in the stored row: support staff read this log, and a working
    # code in it is a working account takeover.
    for column in ("subject", "body_text", "error", "to_email"):
        assert "482913" not in (getattr(logged, column) or ""), column
    assert logged.kind.value == "OTP"


async def test_wrong_guesses_are_capped_across_codes_for_a_day(session: AsyncSession) -> None:
    """Five guesses a code and five codes an hour made 600 guesses a day at one
    reset code: enough to take an account in about a year (security review M1).
    Ten wrong guesses in a day close the address for codes until they age out."""
    for minutes_ago in (180, 120):
        code = await _issue(session, minutes_ago=minutes_ago)
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(otp.MAX_ATTEMPTS):
            await _refused(session, wrong, now=utc_now() - timedelta(minutes=minutes_ago - 1))

    assert await otp.issue(session, email=EMAIL, purpose=SIGNUP, ip=None) is None
    tomorrow = utc_now() + timedelta(hours=25)
    assert await otp.issue(session, email=EMAIL, purpose=SIGNUP, ip=None, now=tomorrow)
