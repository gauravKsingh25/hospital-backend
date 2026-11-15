"""The whole road, over HTTP: a stranger becomes a hospital's administrator.

Signup → code → password → hand-off → order → screenshot → UTR → the owner
approves → the same session now runs a hospital on the plan it paid for.
And the sweep that tidies up the ones who never finish.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import utc_now
from app.modules.identity import otp
from app.modules.identity.models import SignupIntent, SignupIntentStatus, User
from app.modules.subscriptions import orders
from app.modules.subscriptions.models import (
    PaymentOrder,
    PaymentOrderPurpose,
    PaymentOrderStatus,
)
from app.workers import tasks
from tests.conftest import auth, login
from tests.payments_support import PNG, pending_user

API = "/api/v1"


@pytest.mark.no_auto_subscription
async def test_signup_to_working_hospital(
    api: AsyncClient, platform_admin: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    codes: dict[str, str] = {}
    real = otp.send_code_email

    async def capture(session: AsyncSession, *, email: str, code: str, purpose: object) -> None:
        codes[email] = code
        await real(session, email=email, code=code, purpose=purpose)  # type: ignore[arg-type]

    monkeypatch.setattr(otp, "send_code_email", capture)
    email = "dr.rao@lotusclinic.example"

    started = await api.post(
        f"{API}/public/signup/start",
        json={
            "full_name": "Dr Meera Rao",
            "email": email,
            "hospital_name": "Lotus Clinic",
            "plan_code": "standard",
        },
    )
    assert started.status_code == 202
    verified = await api.post(
        f"{API}/public/signup/verify", json={"email": email, "code": codes[email]}
    )
    completed = await api.post(
        f"{API}/public/signup/complete",
        json={"signup_token": verified.json()["signup_token"], "password": "lotus-clinic-2026"},
    )
    handoff = await api.post(
        f"{API}/public/signup/handoff", json={"code": completed.json()["handoff_code"]}
    )
    headers = auth(handoff.json()["access_token"])

    order = (await api.post(f"{API}/subscriptions/orders", json={}, headers=headers)).json()
    assert order["amount_inr"] == 3000
    await api.put(f"{API}/subscriptions/orders/{order['id']}/proof", content=PNG, headers=headers)
    submitted = await api.post(
        f"{API}/subscriptions/orders/{order['id']}/submit",
        json={"utr": "987654321098"},
        headers=headers,
    )
    assert submitted.json()["status"] == "SUBMITTED"
    assert (await api.get(f"{API}/patients", headers=headers)).status_code == 403

    approved = await api.post(
        f"{API}/platform/payments/{order['id']}/approve",
        headers=auth(await login(api, platform_admin)),
    )
    assert approved.status_code == 200

    me = (await api.get(f"{API}/auth/me", headers=headers)).json()
    assert me["roles"] == ["HOSPITAL_ADMIN"]
    current = (await api.get(f"{API}/subscriptions/current", headers=headers)).json()
    assert current["plan_code"] == "standard"
    assert current["features"] == ["opd"]
    assert (await api.get(f"{API}/patients", headers=headers)).status_code == 200
    # Standard is OPD only.
    wards = await api.get(f"{API}/ipd/wards", headers=headers)
    assert wards.status_code == 403
    assert wards.json()["error"]["code"] == "feature_not_in_plan"


async def test_sweep_expires_orders_and_intents(session: AsyncSession) -> None:
    payer = await pending_user(session)
    order = await orders.create_order(
        session, user=payer, plan_code=None, purpose=PaymentOrderPurpose.NEW
    )
    order.expires_at = utc_now() - timedelta(hours=1)
    session.add(order)
    stale = SignupIntent(
        email="gone@example.com",
        full_name="Gone",
        hospital_name="Gone Hospital",
        plan_code="standard",
        status=SignupIntentStatus.STARTED,
    )
    session.add(stale)
    await session.flush()
    await session.execute(
        sa.update(SignupIntent)
        .where(SignupIntent.id == stale.id)
        .values(created_at=utc_now() - timedelta(hours=25))
    )
    await session.commit()

    result = await tasks.sweep_signups_and_orders()
    assert result == {"orders_expired": 1, "intents_expired": 1}

    await session.refresh(order)
    await session.refresh(stale)
    assert order.status == PaymentOrderStatus.EXPIRED
    assert stale.status == SignupIntentStatus.EXPIRED
    assert (await session.get(PaymentOrder, order.id)) is not None


async def test_run_sweeps_includes_the_signup_sweep(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake() -> dict[str, int]:
        return {"orders_expired": 2, "intents_expired": 3}

    monkeypatch.setattr(tasks, "sweep_signups_and_orders", fake)
    totals = await tasks.run_sweeps()
    assert totals["orders_expired"] == 2
    assert totals["intents_expired"] == 3
