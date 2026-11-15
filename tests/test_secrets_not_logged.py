"""No secret reaches the logs on the road from signup to an approved hospital.

Spec §2 anti-goal: "The UPI ID, SMTP password or OTP secret in code, git or
logs." Logs are read by support staff and shipped to log services; a code, a
token or a password in one is a key handed to whoever reads it. The whole
flow runs with every logger at DEBUG and every formatted record is searched.
"""

from __future__ import annotations

import logging

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.identity import otp
from app.modules.identity.models import User
from tests.conftest import auth, login
from tests.payments_support import PNG

API = "/api/v1"
PASSWORD = "lotus-secret-pass-2026"


@pytest.mark.no_auto_subscription
async def test_signup_and_approval_log_no_secret(
    api: AsyncClient,
    platform_admin: User,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    codes: list[str] = []
    real = otp.send_code_email

    async def capture(session: AsyncSession, *, email: str, code: str, purpose: object) -> None:
        codes.append(code)
        await real(session, email=email, code=code, purpose=purpose)  # type: ignore[arg-type]

    monkeypatch.setattr(otp, "send_code_email", capture)
    email = "secret.check@lotusclinic.example"

    await api.post(
        f"{API}/public/signup/start",
        json={
            "full_name": "Dr Secret Check",
            "email": email,
            "hospital_name": "Secret Clinic",
            "plan_code": "standard",
        },
    )
    verified = (
        await api.post(f"{API}/public/signup/verify", json={"email": email, "code": codes[-1]})
    ).json()
    completed = (
        await api.post(
            f"{API}/public/signup/complete",
            json={"signup_token": verified["signup_token"], "password": PASSWORD},
        )
    ).json()
    tokens = (
        await api.post(f"{API}/public/signup/handoff", json={"code": completed["handoff_code"]})
    ).json()
    headers = auth(tokens["access_token"])
    order = (await api.post(f"{API}/subscriptions/orders", json={}, headers=headers)).json()
    await api.put(f"{API}/subscriptions/orders/{order['id']}/proof", content=PNG, headers=headers)
    await api.post(
        f"{API}/subscriptions/orders/{order['id']}/submit",
        json={"utr": "987654321777"},
        headers=headers,
    )
    approved = await api.post(
        f"{API}/platform/payments/{order['id']}/approve",
        headers=auth(await login(api, platform_admin)),
    )
    assert approved.status_code == 200

    secrets = {
        "OTP code": codes[-1],
        "signup token": verified["signup_token"],
        "handoff code": completed["handoff_code"],
        "access token": tokens["access_token"],
        "refresh token": tokens["refresh_token"],
        "password": PASSWORD,
        "UPI ID": settings.UPI_VPA,
        "OTP secret": settings.OTP_HMAC_SECRET.get_secret_value(),
        "SMTP password": settings.SMTP_PASSWORD.get_secret_value(),
        "SECRET_KEY": settings.SECRET_KEY,
    }
    # The console mailer prints whole emails on purpose: it is the developer's
    # stand-in for a mail server, and settings refuse it in production (the
    # SMTP mailer logs no message text). Every other record must be clean.
    logged = "\n".join(
        record.getMessage()
        for record in caplog.records
        if not record.getMessage().startswith("Email (console)")
    )
    assert logged, "nothing was logged: the check would prove nothing"
    leaked = [name for name, value in secrets.items() if value and value in logged]
    assert leaked == []
