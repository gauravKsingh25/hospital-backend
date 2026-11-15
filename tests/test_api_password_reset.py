"""Forgotten password by emailed code (spec §2, §8)."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity import otp
from app.modules.identity.models import EmailOtp, PlatformEmail, User
from tests.conftest import TEST_PASSWORD

PUBLIC = "/api/v1/public"
NEW_PASSWORD = "brand-new-pass-2026"


@pytest.fixture
def codes(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    sent: dict[str, str] = {}
    real = otp.send_code_email

    async def capture(session: AsyncSession, *, email: str, code: str, purpose: object) -> None:
        sent[email.lower()] = code
        await real(session, email=email, code=code, purpose=purpose)  # type: ignore[arg-type]

    monkeypatch.setattr(otp, "send_code_email", capture)
    return sent


async def _login(api: AsyncClient, email: str, password: str) -> int:
    response = await api.post("/api/v1/auth/login", json={"email": email, "password": password})
    return response.status_code


async def test_reset_sets_password_and_revokes_sessions(
    api: AsyncClient, codes: dict[str, str], receptionist: User
) -> None:
    signed_in = await api.post(
        "/api/v1/auth/login", json={"email": receptionist.email, "password": TEST_PASSWORD}
    )
    old_refresh = signed_in.json()["refresh_token"]

    forgot = await api.post(f"{PUBLIC}/password/forgot", json={"email": receptionist.email})
    assert forgot.status_code == 202
    reset = await api.post(
        f"{PUBLIC}/password/reset",
        json={
            "email": receptionist.email,
            "code": codes[receptionist.email],
            "password": NEW_PASSWORD,
        },
    )
    assert reset.status_code == 204, reset.text

    assert await _login(api, receptionist.email, TEST_PASSWORD) == 401
    assert await _login(api, receptionist.email, NEW_PASSWORD) == 200
    refreshed = await api.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert refreshed.status_code == 401


async def test_forgot_for_unknown_email_is_202_and_sends_nothing(
    api: AsyncClient, codes: dict[str, str], session: AsyncSession
) -> None:
    response = await api.post(f"{PUBLIC}/password/forgot", json={"email": "nobody@example.com"})
    assert response.status_code == 202
    assert response.json() == {"status": "sent"}
    assert codes == {}
    assert (
        await session.execute(sa.select(sa.func.count()).select_from(PlatformEmail))
    ).scalar_one() == 0


async def test_reset_with_wrong_code_keeps_the_old_password(
    api: AsyncClient, codes: dict[str, str], receptionist: User
) -> None:
    await api.post(f"{PUBLIC}/password/forgot", json={"email": receptionist.email})
    wrong = "000000" if codes[receptionist.email] != "000000" else "111111"
    response = await api.post(
        f"{PUBLIC}/password/reset",
        json={"email": receptionist.email, "code": wrong, "password": NEW_PASSWORD},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "otp_invalid"
    assert await _login(api, receptionist.email, TEST_PASSWORD) == 200


async def test_weak_password_does_not_spend_the_code(
    api: AsyncClient, codes: dict[str, str], receptionist: User
) -> None:
    await api.post(f"{PUBLIC}/password/forgot", json={"email": receptionist.email})
    code = codes[receptionist.email]
    weak = await api.post(
        f"{PUBLIC}/password/reset",
        json={"email": receptionist.email, "code": code, "password": "short"},
    )
    assert weak.status_code == 422
    ok = await api.post(
        f"{PUBLIC}/password/reset",
        json={"email": receptionist.email, "code": code, "password": NEW_PASSWORD},
    )
    assert ok.status_code == 204


async def test_the_request_does_the_same_work_for_known_and_unknown_addresses(
    session: AsyncSession, receptionist: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mail send takes hundreds of milliseconds and happened only for real
    accounts, so the answer's timing told who has one (security review M2).
    The request now only records the code; the email goes after the answer."""
    from app.modules.identity import signup

    async def no_mail_in_the_request(*_: object, **__: object) -> None:
        raise AssertionError("the reset email was sent inside the request")

    monkeypatch.setattr(otp, "send_code_email", no_mail_in_the_request)
    pending = await signup.request_password_reset(session, email=receptionist.email, ip=None)
    assert pending is not None and pending.email == receptionist.email.lower()
    assert len(pending.code) == 6
    assert await signup.request_password_reset(session, email="nobody@example.com", ip=None) is None


async def test_forgot_answers_before_looking_the_address_up(
    api: AsyncClient, session: AsyncSession, receptionist: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even the lookup and the code row differ between a real account and an
    unknown one (a few database round trips). The route hands the address to a
    task that runs after the answer and does nothing else."""
    from app.modules.identity import signup

    handed: list[str] = []

    async def later(email: str, ip: str | None) -> None:
        handed.append(email)

    monkeypatch.setattr(signup, "forgot_password_later", later)
    response = await api.post(f"{PUBLIC}/password/forgot", json={"email": receptionist.email})
    assert response.status_code == 202
    assert handed == [receptionist.email.lower()]
    rows = (await session.execute(sa.select(sa.func.count()).select_from(EmailOtp))).scalar_one()
    assert rows == 0
