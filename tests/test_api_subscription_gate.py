"""A hospital whose subscription has lapsed is refused — everywhere but the
few routes it needs to see that and put it right."""

from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.models import utc_now
from app.modules.identity.models import User
from app.modules.tenancy.models import Hospital
from tests.conftest import auth, login
from tests.subscriptions_support import set_plan

pytestmark = pytest.mark.integration

PATIENTS = "/api/v1/patients"
ME = "/api/v1/auth/me"
CURRENT = "/api/v1/subscriptions/current"


async def test_active_hospital_is_let_through_and_me_reports_the_plan(
    api: AsyncClient, hospital: Hospital, hospital_admin: User
) -> None:
    token = await login(api, hospital_admin)

    assert (await api.get(PATIENTS, headers=auth(token))).status_code == 200
    # The app draws from /auth/me: an API that lets the hospital through
    # while the app still shows "subscription ended" unlocks nothing.
    entitlements = (await api.get(ME, headers=auth(token))).json()["entitlements"]
    assert entitlements["status"] == "ACTIVE"
    assert "ipd" in entitlements["features"]
    entitlements = (await api.get(ME, headers=auth(token))).json()["entitlements"]
    assert entitlements["plan_code"] == "professional"
    assert entitlements["plan_name"] == "Professional"
    assert entitlements["status"] == "ACTIVE"
    assert entitlements["days_left"] is None
    assert entitlements["complimentary"] is True
    assert "ipd" in entitlements["features"]


async def test_grace_is_let_through(
    api: AsyncClient, session: AsyncSession, hospital: Hospital, hospital_admin: User
) -> None:
    await set_plan(session, hospital, "standard", period_end=utc_now() - timedelta(days=3))
    await session.commit()
    token = await login(api, hospital_admin)

    assert (await api.get(PATIENTS, headers=auth(token))).status_code == 200
    entitlements = (await api.get(ME, headers=auth(token))).json()["entitlements"]
    assert entitlements["status"] == "GRACE"
    assert entitlements["features"] == ["opd"]


@pytest.mark.parametrize(
    ("days_past", "cancel", "status"),
    [(8, False, "EXPIRED"), (1, True, "CANCELLED")],
)
async def test_lapsed_hospital_gets_402_but_can_still_see_itself(
    api: AsyncClient,
    session: AsyncSession,
    hospital: Hospital,
    hospital_admin: User,
    receptionist: User,
    days_past: int,
    cancel: bool,
    status: str,
) -> None:
    await set_plan(
        session,
        hospital,
        "standard",
        period_end=utc_now() - timedelta(days=days_past),
        cancel=cancel,
    )
    await session.commit()

    for user in (hospital_admin, receptionist):
        token = await login(api, user)
        refused = await api.get(PATIENTS, headers=auth(token))
        assert refused.status_code == 402, refused.text
        assert refused.json()["error"]["code"] == "subscription_inactive"
        assert refused.json()["error"]["details"]["status"] == status
        me = await api.get(ME, headers=auth(token))
        assert me.status_code == 200
        assert me.json()["entitlements"]["status"] == status

    admin_token = await login(api, hospital_admin)
    current = await api.get(CURRENT, headers=auth(admin_token))
    assert current.status_code == 200, current.text
    assert current.json()["status"] == status


@pytest.mark.no_auto_subscription
async def test_hospital_without_any_subscription_row_gets_402(
    api: AsyncClient, hospital: Hospital, hospital_admin: User
) -> None:
    token = await login(api, hospital_admin)

    refused = await api.get(PATIENTS, headers=auth(token))

    assert refused.status_code == 402
    assert refused.json()["error"]["details"]["status"] == "NONE"
    assert (await api.get(ME, headers=auth(token))).json()["entitlements"] is None
    missing = await api.get(CURRENT, headers=auth(token))
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "subscription_not_found"


async def test_platform_admin_is_never_gated(api: AsyncClient, platform_admin: User) -> None:
    token = await login(api, platform_admin)

    assert (await api.get("/api/v1/platform/overview", headers=auth(token))).status_code == 200
    assert (await api.get(ME, headers=auth(token))).json()["entitlements"] is None


async def test_receptionist_cannot_read_the_subscription_details(
    api: AsyncClient, hospital: Hospital, receptionist: User
) -> None:
    token = await login(api, receptionist)

    assert (await api.get(CURRENT, headers=auth(token))).status_code == 403


async def test_gate_can_be_switched_off(
    api: AsyncClient,
    session: AsyncSession,
    hospital: Hospital,
    hospital_admin: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await set_plan(session, hospital, "standard", period_end=utc_now() - timedelta(days=30))
    await session.commit()
    monkeypatch.setattr(settings, "SUBSCRIPTIONS_ENFORCED", False)

    token = await login(api, hospital_admin)

    assert (await api.get(PATIENTS, headers=auth(token))).status_code == 200
    # The app draws from /auth/me: an API that lets the hospital through
    # while the app still shows "subscription ended" unlocks nothing.
    entitlements = (await api.get(ME, headers=auth(token))).json()["entitlements"]
    assert entitlements["status"] == "ACTIVE"
    assert "ipd" in entitlements["features"]
