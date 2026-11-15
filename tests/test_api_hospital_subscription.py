"""A hospital the platform owner sets up by hand starts with a subscription.

Marked `no_auto_subscription` throughout: the test harness otherwise gives
every inserted hospital one, which would hide a route that forgot to.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.modules.identity.models import User
from app.modules.subscriptions.models import Subscription
from app.modules.tenancy.models import Hospital
from tests.conftest import auth, login

pytestmark = [pytest.mark.integration, pytest.mark.no_auto_subscription]


async def _subscription(session: AsyncSession, code: str) -> Subscription | None:
    async with db.system_context(session):
        result = await session.execute(
            sa.select(Subscription)
            .join(Hospital, Hospital.id == Subscription.hospital_id)  # type: ignore[arg-type]
            .where(Hospital.code == code)
        )
        return result.scalars().first()


def _assert_complimentary(sub: Subscription | None) -> None:
    assert sub is not None
    assert sub.plan_code == "professional"
    assert sub.complimentary is True
    assert sub.current_period_end is None


async def test_console_created_hospital_has_a_complimentary_professional_subscription(
    api: AsyncClient, session: AsyncSession, platform_admin: User
) -> None:
    response = await api.post(
        "/api/v1/platform/hospitals",
        json={
            "hospital": {"code": "SUB01", "name": "Hospital SUB01", "city": "Varanasi"},
            "admin": {"full_name": "First Admin", "email": "first@sub01.example.com"},
        },
        headers=auth(await login(api, platform_admin)),
    )

    assert response.status_code == 201, response.text
    _assert_complimentary(await _subscription(session, "SUB01"))


async def test_hospital_created_through_the_tenancy_route_has_one_too(
    api: AsyncClient, session: AsyncSession, platform_admin: User
) -> None:
    response = await api.post(
        "/api/v1/hospitals",
        json={"code": "SUB02", "name": "Hospital SUB02"},
        headers=auth(await login(api, platform_admin)),
    )

    assert response.status_code == 201, response.text
    _assert_complimentary(await _subscription(session, "SUB02"))
