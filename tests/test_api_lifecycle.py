"""Renewing and upgrading a paid subscription: the orders (slice 3, Task 2).

A paid hospital's admin raises RENEWAL and UPGRADE orders through the same
UPI pipeline as a new signup. Prices come from the plans table and the
period maths; nobody else at the hospital may raise them.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import utc_now
from app.modules.identity.models import User
from app.modules.tenancy.models import Hospital
from tests.conftest import auth, login
from tests.payments_support import pending_user
from tests.subscriptions_support import set_plan

ORDERS = "/api/v1/subscriptions/orders"


async def _paid(session: AsyncSession, hospital: Hospital, plan: str, days_left: float) -> None:
    await set_plan(session, hospital, plan, period_end=utc_now() + timedelta(days=days_left))
    await session.commit()


async def test_admin_raises_a_renewal_at_the_plan_price(
    api: AsyncClient, session: AsyncSession, hospital: Hospital, hospital_admin: User
) -> None:
    await _paid(session, hospital, "standard", 5)
    response = await api.post(
        ORDERS, json={"purpose": "RENEWAL"}, headers=auth(await login(api, hospital_admin))
    )
    assert response.status_code == 201, response.text
    assert response.json()["purpose"] == "RENEWAL"
    assert response.json()["plan_code"] == "standard"
    assert response.json()["amount_inr"] == 3000


async def test_renewal_after_downgrade_is_priced_lower(
    api: AsyncClient, session: AsyncSession, hospital: Hospital, hospital_admin: User
) -> None:
    await _paid(session, hospital, "professional", 5)
    headers = auth(await login(api, hospital_admin))
    changed = await api.post(
        "/api/v1/subscriptions/change-plan", json={"plan_code": "standard"}, headers=headers
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["pending_plan_code"] == "standard"
    renewal = await api.post(ORDERS, json={"purpose": "RENEWAL"}, headers=headers)
    assert renewal.json()["plan_code"] == "standard"
    assert renewal.json()["amount_inr"] == 3000


async def test_upgrade_order_is_pro_rated(
    api: AsyncClient, session: AsyncSession, hospital: Hospital, hospital_admin: User
) -> None:
    await _paid(session, hospital, "standard", 15)
    headers = auth(await login(api, hospital_admin))
    quote = await api.get(
        "/api/v1/subscriptions/upgrade-quote", params={"plan_code": "professional"}, headers=headers
    )
    assert quote.status_code == 200, quote.text
    # 15 days (less a few seconds) of a 30-day period: about half of 5999.
    assert 2999 <= quote.json()["amount_inr"] <= 3000
    order = await api.post(
        ORDERS, json={"purpose": "UPGRADE", "plan_code": "professional"}, headers=headers
    )
    assert order.status_code == 201, order.text
    assert order.json()["amount_inr"] == quote.json()["amount_inr"]


async def test_upgrade_in_grace_is_refused(
    api: AsyncClient, session: AsyncSession, hospital: Hospital, hospital_admin: User
) -> None:
    await _paid(session, hospital, "standard", -2)
    response = await api.post(
        ORDERS,
        json={"purpose": "UPGRADE", "plan_code": "professional"},
        headers=auth(await login(api, hospital_admin)),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "upgrade_unavailable"


async def test_upgrade_to_the_same_plan_is_refused(
    api: AsyncClient, session: AsyncSession, hospital: Hospital, hospital_admin: User
) -> None:
    await _paid(session, hospital, "professional", 10)
    response = await api.post(
        ORDERS,
        json={"purpose": "UPGRADE", "plan_code": "professional"},
        headers=auth(await login(api, hospital_admin)),
    )
    assert response.json()["error"]["code"] == "upgrade_unavailable"


async def test_receptionist_cannot_raise_a_renewal(
    api: AsyncClient, session: AsyncSession, hospital: Hospital, receptionist: User
) -> None:
    await _paid(session, hospital, "standard", 5)
    response = await api.post(
        ORDERS, json={"purpose": "RENEWAL"}, headers=auth(await login(api, receptionist))
    )
    assert response.status_code == 403


async def test_pending_user_cannot_raise_a_renewal(api: AsyncClient, session: AsyncSession) -> None:
    response = await api.post(
        ORDERS,
        json={"purpose": "RENEWAL"},
        headers=auth(await login(api, await pending_user(session))),
    )
    assert response.status_code == 403


async def test_complimentary_hospital_cannot_renew(api: AsyncClient, hospital_admin: User) -> None:
    # The harness gives every test hospital a complimentary Professional plan.
    response = await api.post(
        ORDERS, json={"purpose": "RENEWAL"}, headers=auth(await login(api, hospital_admin))
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "subscription_complimentary"


async def test_history_lists_the_hospitals_orders_only(
    api: AsyncClient,
    session: AsyncSession,
    hospital: Hospital,
    hospital_admin: User,
) -> None:
    await _paid(session, hospital, "standard", 5)
    headers = auth(await login(api, hospital_admin))
    await api.post(ORDERS, json={"purpose": "RENEWAL"}, headers=headers)
    # Someone else's signup order must not appear.
    stranger = auth(await login(api, await pending_user(session)))
    await api.post(ORDERS, json={"purpose": "NEW"}, headers=stranger)

    history = await api.get("/api/v1/subscriptions/history", headers=headers)
    assert history.status_code == 200
    assert [row["purpose"] for row in history.json()["items"]] == ["RENEWAL"]


@pytest.mark.parametrize("days_left", [-2, -30])
async def test_lapsed_hospital_reaches_the_order_routes(
    api: AsyncClient,
    session: AsyncSession,
    hospital: Hospital,
    hospital_admin: User,
    days_left: int,
) -> None:
    await _paid(session, hospital, "standard", days_left)
    headers = auth(await login(api, hospital_admin))
    if days_left < -7:
        assert (await api.get("/api/v1/patients", headers=headers)).status_code == 402
    response = await api.post(ORDERS, json={"purpose": "RENEWAL"}, headers=headers)
    assert response.status_code == 201, response.text
