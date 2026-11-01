"""Suspending a hospital locks its people out — at once, not at next login.

Until this was enforced, suspension flipped a flag that nothing read: the
hospital's staff signed in and worked as before. A "Suspend" button that does
not suspend is worse than none, because the platform owner believes it worked.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.modules.identity.models import User
from app.modules.tenancy.models import Hospital
from tests.conftest import TEST_PASSWORD, auth, login

pytestmark = pytest.mark.integration

ME = "/api/v1/auth/me"


async def _set_active(api: AsyncClient, owner: User, hospital: Hospital, *, active: bool) -> None:
    token = await login(api, owner)
    verb = "activate" if active else "deactivate"
    response = await api.post(f"/api/v1/hospitals/{hospital.id}/{verb}", headers=auth(token))
    assert response.status_code == 200, response.text


async def test_suspended_hospital_login_refused(
    api: AsyncClient, hospital: Hospital, platform_admin: User, hospital_admin: User
) -> None:
    await _set_active(api, platform_admin, hospital, active=False)

    response = await api.post(
        "/api/v1/auth/login", json={"email": hospital_admin.email, "password": TEST_PASSWORD}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "hospital_suspended"


async def test_suspended_hospital_live_token_refused(
    api: AsyncClient, hospital: Hospital, platform_admin: User, hospital_admin: User
) -> None:
    token = await login(api, hospital_admin)
    assert (await api.get(ME, headers=auth(token))).status_code == 200

    await _set_active(api, platform_admin, hospital, active=False)

    response = await api.get(ME, headers=auth(token))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "hospital_suspended"


async def test_suspended_hospital_refresh_revoked(
    api: AsyncClient, hospital: Hospital, platform_admin: User, hospital_admin: User
) -> None:
    pair = (
        await api.post(
            "/api/v1/auth/login", json={"email": hospital_admin.email, "password": TEST_PASSWORD}
        )
    ).json()

    await _set_active(api, platform_admin, hospital, active=False)

    response = await api.post("/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert response.status_code == 401


async def test_reactivated_hospital_can_sign_in(
    api: AsyncClient, hospital: Hospital, platform_admin: User, hospital_admin: User
) -> None:
    await _set_active(api, platform_admin, hospital, active=False)
    await _set_active(api, platform_admin, hospital, active=True)

    token = await login(api, hospital_admin)
    assert (await api.get(ME, headers=auth(token))).status_code == 200


async def test_platform_admin_unaffected_by_hospital_suspension(
    api: AsyncClient, hospital: Hospital, platform_admin: User
) -> None:
    await _set_active(api, platform_admin, hospital, active=False)

    token = await login(api, platform_admin)
    assert (await api.get(ME, headers=auth(token))).status_code == 200
