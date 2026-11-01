"""The platform console's API: onboarding, the hospital list, the overview,
the platform team, and the activity log."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.mail import MailMessage, MailResult
from app.core.models import utc_now
from app.modules.clinical import service as clinical_service
from app.modules.identity import invites
from app.modules.identity.models import Invite, User
from app.modules.identity.rbac import Roles
from app.modules.patients import service as patients_service
from app.modules.patients.models import Gender
from app.modules.patients.schemas import PatientRegister
from app.modules.tenancy.models import Hospital
from tests.conftest import _make_user, auth, login

pytestmark = pytest.mark.integration

PLATFORM = "/api/v1/platform"


class OkMailer:
    async def send(self, message: MailMessage) -> MailResult:
        return MailResult(ok=True, message_id="<ok@example>")


@pytest.fixture(autouse=True)
def mailer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(invites, "get_mailer", lambda: OkMailer())


def _new_hospital_body(code: str, email: str) -> dict[str, object]:
    return {
        "hospital": {"code": code, "name": f"Hospital {code}", "city": "Varanasi"},
        "admin": {"full_name": "First Admin", "email": email},
    }


async def _hospital_exists(session: AsyncSession, code: str) -> bool:
    async with db.system_context(session):
        found = (await session.execute(sa.select(Hospital.id).where(Hospital.code == code))).first()
    return found is not None


async def test_create_hospital_with_admin_invite(
    api: AsyncClient, session: AsyncSession, platform_admin: User
) -> None:
    owner = await login(api, platform_admin)

    response = await api.post(
        f"{PLATFORM}/hospitals",
        json=_new_hospital_body("VNS01", "first@vns.example.com"),
        headers=auth(owner),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["hospital"]["code"] == "VNS01"
    assert [invite["status"] for invite in body["invites"]] == ["PENDING"]
    assert body["invites"][0]["last_email_status"] == "SENT"
    assert body["admins"] == []

    listing = (
        await api.get(f"{PLATFORM}/hospitals", params={"q": "VNS01"}, headers=auth(owner))
    ).json()
    assert [row["status"] for row in listing["items"]] == ["invite_pending"]


async def test_create_hospital_rolls_back_when_invite_invalid(
    api: AsyncClient, session: AsyncSession, platform_admin: User, receptionist: User
) -> None:
    owner = await login(api, platform_admin)

    response = await api.post(
        f"{PLATFORM}/hospitals",
        json=_new_hospital_body("DUP01", receptionist.email),
        headers=auth(owner),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_taken"
    assert await _hospital_exists(session, "DUP01") is False


async def test_support_cannot_create_hospital(api: AsyncClient, session: AsyncSession) -> None:
    support = await _make_user(session, role_code=Roles.PLATFORM_SUPPORT, hospital_id=None)
    token = await login(api, support)

    response = await api.post(
        f"{PLATFORM}/hospitals",
        json=_new_hospital_body("SUP01", "x@sup.example.com"),
        headers=auth(token),
    )

    assert response.status_code == 403


async def test_hospital_list_rows_carry_admin_and_staff(
    api: AsyncClient,
    session: AsyncSession,
    hospital: Hospital,
    hospital_admin: User,
    receptionist: User,
    platform_admin: User,
) -> None:
    owner = await login(api, platform_admin)

    response = await api.get(f"{PLATFORM}/hospitals", headers=auth(owner))

    assert response.status_code == 200, response.text
    row = next(item for item in response.json()["items"] if item["id"] == str(hospital.id))
    assert row["status"] == "active"
    assert row["admin_name"] == hospital_admin.full_name
    assert row["staff_count"] == 2


async def test_platform_lists_are_paginated(
    api: AsyncClient, session: AsyncSession, platform_admin: User
) -> None:
    async with db.system_context(session):
        for code in ("PGA", "PGB"):
            session.add(Hospital(code=f"{code}{uuid.uuid4().hex[:4].upper()}", name=code))
        await session.flush()
    await session.commit()
    owner = await login(api, platform_admin)

    body = (await api.get(f"{PLATFORM}/hospitals", params={"limit": 1}, headers=auth(owner))).json()

    assert len(body["items"]) == 1
    assert body["total"] >= 2


async def test_hospital_detail_usage_counts(
    api: AsyncClient,
    session: AsyncSession,
    hospital: Hospital,
    hospital_admin: User,
    receptionist: User,
    platform_admin: User,
) -> None:
    await db.set_tenant_context(session, hospital.id)
    first, _ = await patients_service.register_patient(
        session,
        PatientRegister(
            full_name="Asha Devi", phone="9812300001", gender=Gender.FEMALE, age_years=40
        ),
        hospital_id=hospital.id,
    )
    await patients_service.register_patient(
        session,
        PatientRegister(
            full_name="Ravi Kumar", phone="9812300002", gender=Gender.MALE, age_years=33
        ),
        hospital_id=hospital.id,
    )
    await clinical_service.open_encounter(
        session, hospital_id=hospital.id, patient_id=first.id, actor=receptionist
    )
    await session.commit()
    owner = await login(api, platform_admin)

    response = await api.get(f"{PLATFORM}/hospitals/{hospital.id}", headers=auth(owner))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["usage"] == {"staff": 2, "patients": 2, "visits_last_7_days": 1}
    assert [admin["email"] for admin in body["admins"]] == [hospital_admin.email]


async def test_overview_counts_and_attention(
    api: AsyncClient,
    session: AsyncSession,
    hospital: Hospital,
    hospital_admin: User,
    platform_admin: User,
) -> None:
    async with db.system_context(session):
        await session.execute(
            sa.update(User)
            .where(User.id == hospital_admin.id)
            .values(last_login_at=utc_now() - timedelta(days=20))
        )
    await session.commit()
    owner_user = platform_admin
    owner = await login(api, owner_user)

    created = await api.post(
        f"{PLATFORM}/hospitals",
        json=_new_hospital_body("EXP01", "soon@exp.example.com"),
        headers=auth(owner),
    )
    invite_id = created.json()["invites"][0]["id"]
    await session.execute(
        sa.update(Invite)
        .where(Invite.id == uuid.UUID(invite_id))
        .values(expires_at=utc_now() + timedelta(hours=2))
    )
    await session.commit()

    # The admin's own sign-in above would count as a login; put it back.
    async with db.system_context(session):
        await session.execute(
            sa.update(User)
            .where(User.id == hospital_admin.id)
            .values(last_login_at=utc_now() - timedelta(days=20))
        )
    await session.commit()

    response = await api.get(f"{PLATFORM}/overview", headers=auth(owner))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["active_hospitals"] >= 2
    assert body["pending_invites"] == 1
    kinds = {(item["kind"], item["hospital_name"]) for item in body["attention"]}
    assert ("invite_expiring", "Hospital EXP01") in kinds
    assert ("no_admin_login", hospital.name) in kinds
    assert any(row["action"] == "invite.create" for row in body["recent_activity"])


async def test_cannot_deactivate_last_platform_admin(
    api: AsyncClient, session: AsyncSession, platform_admin: User
) -> None:
    other = await _make_user(session, role_code=Roles.PLATFORM_ADMIN, hospital_id=None)
    owner = await login(api, platform_admin)

    first = await api.post(f"{PLATFORM}/team/{other.id}/deactivate", headers=auth(owner))
    assert first.status_code == 200, first.text
    assert first.json()["is_active"] is False

    # Only `platform_admin` is left, and nobody may remove themselves — so the
    # platform can never be left without an active owner.
    self_ = await api.post(f"{PLATFORM}/team/{platform_admin.id}/deactivate", headers=auth(owner))
    assert self_.status_code == 409
    assert self_.json()["error"]["code"] == "cannot_deactivate_self"


async def test_team_route_cannot_touch_hospital_staff(
    api: AsyncClient, platform_admin: User, hospital_admin: User
) -> None:
    """Hospital staff are their administrator's to manage, not the platform team's."""
    owner = await login(api, platform_admin)

    response = await api.post(
        f"{PLATFORM}/team/{hospital_admin.id}/deactivate", headers=auth(owner)
    )

    assert response.status_code == 404


async def test_team_lists_platform_users_only(
    api: AsyncClient, session: AsyncSession, platform_admin: User, hospital_admin: User
) -> None:
    support = await _make_user(session, role_code=Roles.PLATFORM_SUPPORT, hospital_id=None)
    owner = await login(api, platform_admin)

    body = (await api.get(f"{PLATFORM}/team", headers=auth(owner))).json()

    emails = {row["email"] for row in body["items"]}
    assert {platform_admin.email, support.email} <= emails
    assert hospital_admin.email not in emails


async def test_team_invite_support_role(api: AsyncClient, platform_admin: User) -> None:
    owner = await login(api, platform_admin)

    response = await api.post(
        f"{PLATFORM}/team/invites",
        json={
            "full_name": "Support One",
            "email": "support.one@example.com",
            "role": "PLATFORM_SUPPORT",
        },
        headers=auth(owner),
    )

    assert response.status_code == 201, response.text
    assert response.json()["hospital_id"] is None
    assert response.json()["role_codes"] == ["PLATFORM_SUPPORT"]


async def test_activity_log_filters_by_action(api: AsyncClient, platform_admin: User) -> None:
    owner = await login(api, platform_admin)
    await api.post(
        f"{PLATFORM}/hospitals",
        json=_new_hospital_body("ACT01", "act@example.com"),
        headers=auth(owner),
    )

    body = (
        await api.get(f"{PLATFORM}/audit", params={"action": "invite.create"}, headers=auth(owner))
    ).json()

    assert body["total"] >= 1
    assert {row["action"] for row in body["items"]} == {"invite.create"}
