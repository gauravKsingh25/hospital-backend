"""A hospital cannot give itself the platform owner's powers (security review C1).

A hospital administrator may define custom roles. Before this fix a custom
role could carry any permission in the catalogue, `platform:payment_review`
and `platform:subscription_manage` included, so a paying hospital could
approve its own renewals on any 12 digits, extend itself for free, and read
every payer's screenshot. Two locks now: such a role cannot be created, and a
platform-only permission counts for nobody but platform staff, so a role that
somehow carries one (an old row, a hand edit) still opens nothing.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.rbac_registry import platform_only_permissions
from app.modules.identity.models import Permission, Role, RolePermission, User, UserRole
from app.modules.tenancy.models import Hospital
from tests.conftest import auth, login

pytestmark = pytest.mark.integration

API = "/api/v1"


def test_platform_only_permissions_are_the_platform_set() -> None:
    codes = platform_only_permissions()
    assert "platform:payment_review" in codes
    assert "platform:subscription_manage" in codes
    assert "hospital:create" in codes
    assert "patient:read" not in codes
    assert "role:create" not in codes


async def test_a_hospital_cannot_create_a_role_with_platform_powers(
    api: AsyncClient, hospital_admin: User
) -> None:
    response = await api.post(
        f"{API}/roles",
        json={
            "code": "OPS",
            "name": "Operations",
            "permission_codes": ["platform:payment_review", "platform:subscription_manage"],
        },
        headers=auth(await login(api, hospital_admin)),
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "permission_not_grantable"


async def _smuggled_role(session: AsyncSession, hospital: Hospital, user: User) -> None:
    """A tenant role holding platform permissions, written straight to the table."""
    async with db.system_context(session):
        role = Role(hospital_id=hospital.id, code="SMUGGLED", name="Smuggled", is_system=False)
        session.add(role)
        await session.flush()
        permissions = (
            (
                await session.execute(
                    sa.select(Permission).where(
                        Permission.code.in_(  # type: ignore[attr-defined]
                            [
                                "platform:console",
                                "platform:payment_read",
                                "platform:payment_review",
                                "platform:subscription_manage",
                                "platform:hospital_read",
                                "hospital:create",
                            ]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        for permission in permissions:
            session.add(RolePermission(role_id=role.id, permission_id=permission.id))
        session.add(UserRole(user_id=user.id, role_id=role.id))
        await session.flush()
    await session.commit()


async def test_a_platform_permission_opens_nothing_for_hospital_staff(
    api: AsyncClient, session: AsyncSession, hospital: Hospital, hospital_admin: User
) -> None:
    await _smuggled_role(session, hospital, hospital_admin)
    headers = auth(await login(api, hospital_admin))

    for method, path, body in (
        ("GET", f"{API}/platform/payments", None),
        ("GET", f"{API}/platform/subscriptions", None),
        ("POST", f"{API}/platform/payments/01a1dcca-0000-7000-8000-000000000000/approve", {}),
        (
            "POST",
            f"{API}/platform/subscriptions/01a1dcca-0000-7000-8000-000000000000/extend",
            {"days": 30, "reason": "free month"},
        ),
        ("GET", f"{API}/platform/hospitals", None),
        ("POST", f"{API}/hospitals", {"code": "EVIL", "name": "Evil Hospital"}),
    ):
        response = await api.request(method, path, json=body, headers=headers)
        assert response.status_code == 403, f"{method} {path}: {response.status_code}"
