"""Self-service signup over HTTP (spec §5): details → code → password.

The account that comes out the other end has no hospital and no role, so it
can do nothing but pay; the hospital arrives with the approval (slice 2,
Task 7).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.modules.identity import otp, signup
from app.modules.identity.models import PlatformEmail, PlatformEmailKind, SignupIntent, User
from tests.conftest import TEST_PASSWORD, auth

PUBLIC = "/api/v1/public"
NEW_EMAIL = "owner@cityclinic.example"
PASSWORD = "a-strong-pass-2026"


@pytest.fixture
def codes(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The last code emailed to each address, captured instead of read from a mailbox."""
    sent: dict[str, str] = {}
    real = otp.send_code_email

    async def capture(session: AsyncSession, *, email: str, code: str, purpose: object) -> None:
        sent[email.lower()] = code
        await real(session, email=email, code=code, purpose=purpose)  # type: ignore[arg-type]

    monkeypatch.setattr(otp, "send_code_email", capture)
    return sent


def _details(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "full_name": "Asha Verma",
        "email": NEW_EMAIL,
        "phone": "9876543210",
        "hospital_name": "City Care Clinic",
        "plan_code": "standard",
    }
    body.update(overrides)
    return body


async def _signup(api: AsyncClient, codes: dict[str, str], email: str = NEW_EMAIL) -> str:
    """Run the three steps; return the handoff code."""
    started = await api.post(f"{PUBLIC}/signup/start", json=_details(email=email))
    assert started.status_code == 202, started.text
    verified = await api.post(
        f"{PUBLIC}/signup/verify", json={"email": email, "code": codes[email.lower()]}
    )
    assert verified.status_code == 200, verified.text
    completed = await api.post(
        f"{PUBLIC}/signup/complete",
        json={"signup_token": verified.json()["signup_token"], "password": PASSWORD},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["expires_in"] == 120
    return str(completed.json()["handoff_code"])


async def test_signup_creates_a_user_with_no_hospital_and_no_roles(
    api: AsyncClient, codes: dict[str, str], session: AsyncSession
) -> None:
    await _signup(api, codes)
    async with db.system_context(session):
        user = (
            (await session.execute(sa.select(User).where(User.email == NEW_EMAIL))).scalars().one()
        )
        intent = (await session.execute(sa.select(SignupIntent))).scalars().one()
    assert user.hospital_id is None
    assert user.must_change_password is False
    assert intent.status == "COMPLETED"
    assert intent.user_id == user.id
    assert intent.hospital_name == "City Care Clinic"

    login = await api.post("/api/v1/auth/login", json={"email": NEW_EMAIL, "password": PASSWORD})
    me = await api.get("/api/v1/auth/me", headers=auth(login.json()["access_token"]))
    assert me.json()["roles"] == []


async def test_signed_up_user_gets_403_on_tenant_routes(
    api: AsyncClient, codes: dict[str, str]
) -> None:
    await _signup(api, codes)
    login = await api.post("/api/v1/auth/login", json={"email": NEW_EMAIL, "password": PASSWORD})
    headers = auth(login.json()["access_token"])
    for path in ("/api/v1/patients", "/api/v1/subscriptions/current", "/api/v1/users"):
        response = await api.get(path, headers=headers)
        assert response.status_code == 403, (path, response.text)


async def test_start_for_existing_email_answers_the_same_and_sends_no_code(
    api: AsyncClient, codes: dict[str, str], receptionist: User, session: AsyncSession
) -> None:
    response = await api.post(f"{PUBLIC}/signup/start", json=_details(email=receptionist.email))
    assert response.status_code == 202
    assert response.json() == {"status": "sent"}
    assert receptionist.email not in codes
    kinds = (await session.execute(sa.select(PlatformEmail.kind))).scalars().all()
    assert kinds == [PlatformEmailKind.SIGNUP_EXISTS]


async def test_unknown_plan_is_422(api: AsyncClient) -> None:
    response = await api.post(f"{PUBLIC}/signup/start", json=_details(plan_code="gold"))
    assert response.status_code == 422


async def test_verify_with_wrong_code_is_otp_invalid(
    api: AsyncClient, codes: dict[str, str]
) -> None:
    await api.post(f"{PUBLIC}/signup/start", json=_details())
    wrong = "000000" if codes[NEW_EMAIL] != "000000" else "111111"
    response = await api.post(f"{PUBLIC}/signup/verify", json={"email": NEW_EMAIL, "code": wrong})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "otp_invalid"


async def test_resend_sends_a_fresh_code_after_the_cooldown(
    api: AsyncClient, codes: dict[str, str], session: AsyncSession
) -> None:
    await api.post(f"{PUBLIC}/signup/start", json=_details())
    first = codes[NEW_EMAIL]
    # Move the first code back past the one-minute cooldown.
    await session.execute(
        sa.text("UPDATE email_otps SET created_at = created_at - interval '2 minutes'")
    )
    await session.commit()
    response = await api.post(f"{PUBLIC}/signup/resend", json={"email": NEW_EMAIL})
    assert response.status_code == 202
    verified = await api.post(
        f"{PUBLIC}/signup/verify", json={"email": NEW_EMAIL, "code": codes[NEW_EMAIL]}
    )
    assert verified.status_code == 200
    if codes[NEW_EMAIL] != first:
        stale = await api.post(f"{PUBLIC}/signup/verify", json={"email": NEW_EMAIL, "code": first})
        assert stale.status_code == 400


async def test_complete_rejects_a_reused_signup_token(
    api: AsyncClient, codes: dict[str, str]
) -> None:
    await api.post(f"{PUBLIC}/signup/start", json=_details())
    verified = await api.post(
        f"{PUBLIC}/signup/verify", json={"email": NEW_EMAIL, "code": codes[NEW_EMAIL]}
    )
    token = verified.json()["signup_token"]
    first = await api.post(
        f"{PUBLIC}/signup/complete", json={"signup_token": token, "password": PASSWORD}
    )
    again = await api.post(
        f"{PUBLIC}/signup/complete", json={"signup_token": token, "password": PASSWORD}
    )
    assert first.status_code == 200
    assert again.status_code == 400
    assert again.json()["error"]["code"] == "signup_token_invalid"


async def test_a_forged_signup_token_is_refused(api: AsyncClient) -> None:
    response = await api.post(
        f"{PUBLIC}/signup/complete", json={"signup_token": "not-a-token", "password": PASSWORD}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "signup_token_invalid"


async def test_weak_password_is_refused_and_the_token_still_works(
    api: AsyncClient, codes: dict[str, str]
) -> None:
    await api.post(f"{PUBLIC}/signup/start", json=_details())
    verified = await api.post(
        f"{PUBLIC}/signup/verify", json={"email": NEW_EMAIL, "code": codes[NEW_EMAIL]}
    )
    token = verified.json()["signup_token"]
    weak = await api.post(
        f"{PUBLIC}/signup/complete", json={"signup_token": token, "password": "short"}
    )
    assert weak.status_code == 422
    ok = await api.post(
        f"{PUBLIC}/signup/complete", json={"signup_token": token, "password": PASSWORD}
    )
    assert ok.status_code == 200


async def test_handoff_code_works_once(api: AsyncClient, codes: dict[str, str]) -> None:
    handoff = await _signup(api, codes)
    first = await api.post(f"{PUBLIC}/signup/handoff", json={"code": handoff})
    assert first.status_code == 200, first.text
    me = await api.get("/api/v1/auth/me", headers=auth(first.json()["access_token"]))
    assert me.json()["email"] == NEW_EMAIL
    assert first.json()["refresh_token"]

    second = await api.post(f"{PUBLIC}/signup/handoff", json={"code": handoff})
    assert second.status_code == 400
    assert second.json()["error"]["code"] == "handoff_invalid"


async def test_handoff_code_expires_after_two_minutes(
    api: AsyncClient, codes: dict[str, str], session: AsyncSession
) -> None:
    handoff = await _signup(api, codes)
    await session.execute(
        sa.update(SignupIntent).values(
            handoff_expires_at=SignupIntent.handoff_expires_at - timedelta(minutes=3)  # type: ignore[operator]
        )
    )
    await session.commit()
    response = await api.post(f"{PUBLIC}/signup/handoff", json={"code": handoff})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "handoff_invalid"


async def test_start_is_rate_limited_per_ip(api: AsyncClient, codes: dict[str, str]) -> None:
    statuses = [
        (
            await api.post(f"{PUBLIC}/signup/start", json=_details(email=f"p{i}@example.com"))
        ).status_code
        for i in range(11)
    ]
    assert statuses[:10] == [202] * 10
    assert statuses[10] == 429


async def test_public_plans_lists_both_with_server_prices(api: AsyncClient) -> None:
    response = await api.get(f"{PUBLIC}/plans")
    assert response.status_code == 200
    plans = {plan["code"]: plan for plan in response.json()}
    assert plans["standard"]["price_inr"] == 3000
    assert plans["professional"]["price_inr"] == 8999
    assert plans["standard"]["features"] == ["opd"]
    assert [p["code"] for p in response.json()] == ["standard", "professional"]


async def test_stale_intents_expire(
    api: AsyncClient, codes: dict[str, str], session: AsyncSession
) -> None:
    await api.post(f"{PUBLIC}/signup/start", json=_details())
    await session.execute(
        sa.text("UPDATE signup_intents SET created_at = created_at - interval '25 hours'")
    )
    await session.commit()
    expired = await signup.expire_stale_intents(session)
    assert expired == 1


async def test_existing_staff_password_still_signs_in(api: AsyncClient, receptionist: User) -> None:
    # Guard: nothing here touches ordinary sign-in.
    response = await api.post(
        "/api/v1/auth/login", json={"email": receptionist.email, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200
