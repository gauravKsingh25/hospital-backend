"""The doctor page's data (Docs/superpowers/specs/2026-11-15-doctor-page-and-involvement-design.md).

"Patients seen" is a doctor's visit history: only the admin and that doctor
may read it — reception has no need of a doctor's whole patient list. The
timings section needs to remove what it can add: weekly hours and days off.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.tenancy.models import Hospital
from tests.conftest import _make_user, auth, login

pytestmark = pytest.mark.integration

PATIENTS = "/api/v1/patients"
DOCTORS = "/api/v1/doctors"
QUEUE = "/api/v1/queue"
ENCOUNTERS = "/api/v1/encounters"


@pytest.fixture
async def doctor_user(session: AsyncSession, hospital: Hospital) -> User:
    return await _make_user(session, role_code=Roles.DOCTOR, hospital_id=hospital.id)


@pytest.fixture
async def admin_token(api: AsyncClient, hospital_admin: User) -> str:
    return await login(api, hospital_admin)


@pytest.fixture
async def reception_token(api: AsyncClient, receptionist: User) -> str:
    return await login(api, receptionist)


@pytest.fixture
async def doctor_token(api: AsyncClient, doctor_user: User) -> str:
    return await login(api, doctor_user)


@pytest.fixture
async def doctor_id(api: AsyncClient, admin_token: str, doctor_user: User) -> str:
    response = await api.post(
        DOCTORS,
        json={"user_id": str(doctor_user.id), "specialty": "General Medicine"},
        headers=auth(admin_token),
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def visit(api: AsyncClient, token: str, doctor_id: str, *, name: str, phone: str) -> str:
    patient = await api.post(
        PATIENTS,
        json={"full_name": name, "phone": phone, "gender": "FEMALE", "age_years": 40},
        headers=auth(token),
    )
    assert patient.status_code == 201, patient.text
    opd = await api.post(
        f"{QUEUE}/quick-opd",
        json={"patient_id": patient.json()["id"], "doctor_id": doctor_id},
        headers=auth(token),
    )
    assert opd.status_code == 201, opd.text
    return str(opd.json()["encounter_id"])


async def seen_visits(api: AsyncClient, admin: str, doctor: str, doctor_id: str) -> list[str]:
    """Five visits: three reach the doctor, one is a no-show, one is cancelled."""
    ids = []
    for index, name in enumerate(["Asha", "Bina", "Chitra"]):
        encounter_id = await visit(api, admin, doctor_id, name=name, phone=f"98123{index:05d}")
        started = await api.post(f"{ENCOUNTERS}/{encounter_id}/start", headers=auth(doctor))
        assert started.status_code == 200, started.text
        ids.append(encounter_id)
    for no_show, name in ((True, "Deepa"), (False, "Esha")):
        encounter_id = await visit(
            api, admin, doctor_id, name=name, phone=f"98124{int(no_show)}0000"
        )
        cancelled = await api.post(
            f"{ENCOUNTERS}/{encounter_id}/cancel",
            json={"reason": "Did not come", "no_show": no_show},
            headers=auth(admin),
        )
        assert cancelled.status_code == 200, cancelled.text
    return ids


class TestPatientsSeen:
    async def test_admin_lists_doctors_seen_visits_newest_first(
        self, api: AsyncClient, admin_token: str, doctor_token: str, doctor_id: str
    ) -> None:
        ids = await seen_visits(api, admin_token, doctor_token, doctor_id)
        response = await api.get(f"{ENCOUNTERS}/by-doctor/{doctor_id}", headers=auth(admin_token))
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["total"] == 3
        assert [row["encounter_id"] for row in page["items"]] == list(reversed(ids))
        assert [row["patient_name"] for row in page["items"]] == ["Chitra", "Bina", "Asha"]
        assert all(row["uhid"] for row in page["items"])
        assert {row["status"] for row in page["items"]} == {"IN_CONSULTATION"}

    async def test_doctor_sees_own_list(
        self, api: AsyncClient, admin_token: str, doctor_token: str, doctor_id: str
    ) -> None:
        await seen_visits(api, admin_token, doctor_token, doctor_id)
        response = await api.get(f"{ENCOUNTERS}/by-doctor/{doctor_id}", headers=auth(doctor_token))
        assert response.status_code == 200, response.text
        assert response.json()["total"] == 3

    async def test_other_doctor_and_reception_are_refused(
        self,
        api: AsyncClient,
        session: AsyncSession,
        hospital: Hospital,
        reception_token: str,
        doctor_id: str,
    ) -> None:
        other = await _make_user(session, role_code=Roles.DOCTOR, hospital_id=hospital.id)
        other_token = await login(api, other)
        for token in (other_token, reception_token):
            response = await api.get(f"{ENCOUNTERS}/by-doctor/{doctor_id}", headers=auth(token))
            assert response.status_code == 403, response.text
            assert response.json()["error"]["code"] == "doctor_visits_forbidden"

    async def test_visits_are_paginated_and_date_filtered(
        self, api: AsyncClient, admin_token: str, doctor_token: str, doctor_id: str
    ) -> None:
        await seen_visits(api, admin_token, doctor_token, doctor_id)
        paged = await api.get(
            f"{ENCOUNTERS}/by-doctor/{doctor_id}",
            params={"limit": 2, "offset": 0},
            headers=auth(admin_token),
        )
        assert paged.status_code == 200, paged.text
        assert len(paged.json()["items"]) == 2
        assert paged.json()["total"] == 3

        tomorrow = (date.today() + timedelta(days=2)).isoformat()
        later = await api.get(
            f"{ENCOUNTERS}/by-doctor/{doctor_id}",
            params={"date_from": tomorrow},
            headers=auth(admin_token),
        )
        assert later.status_code == 200, later.text
        assert later.json()["total"] == 0


class TestTimings:
    async def test_remove_weekly_session(
        self, api: AsyncClient, admin_token: str, doctor_id: str
    ) -> None:
        added = await api.post(
            f"{DOCTORS}/{doctor_id}/availability",
            json={"weekday": 0, "start_time": "09:00", "end_time": "10:00"},
            headers=auth(admin_token),
        )
        assert added.status_code == 201, added.text
        removed = await api.delete(
            f"{DOCTORS}/{doctor_id}/availability/{added.json()['id']}", headers=auth(admin_token)
        )
        assert removed.status_code == 204, removed.text
        listed = await api.get(f"{DOCTORS}/{doctor_id}/availability", headers=auth(admin_token))
        assert listed.json() == []

    async def test_list_and_remove_day_off(
        self, api: AsyncClient, admin_token: str, reception_token: str, doctor_id: str
    ) -> None:
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        added = await api.post(
            f"{DOCTORS}/{doctor_id}/exceptions",
            json={"exception_date": tomorrow, "reason": "Conference"},
            headers=auth(admin_token),
        )
        assert added.status_code == 201, added.text

        today = date.today().isoformat()
        listed = await api.get(
            f"{DOCTORS}/{doctor_id}/exceptions",
            params={"from_date": today},
            headers=auth(reception_token),
        )
        assert listed.status_code == 200, listed.text
        assert [row["id"] for row in listed.json()] == [added.json()["id"]]

        removed = await api.delete(
            f"{DOCTORS}/{doctor_id}/exceptions/{added.json()['id']}", headers=auth(admin_token)
        )
        assert removed.status_code == 204, removed.text
        again = await api.get(
            f"{DOCTORS}/{doctor_id}/exceptions",
            params={"from_date": today},
            headers=auth(admin_token),
        )
        assert again.json() == []

    async def test_reception_cannot_remove_a_session(
        self, api: AsyncClient, admin_token: str, reception_token: str, doctor_id: str
    ) -> None:
        added = await api.post(
            f"{DOCTORS}/{doctor_id}/availability",
            json={"weekday": 1, "start_time": "09:00", "end_time": "10:00"},
            headers=auth(admin_token),
        )
        assert added.status_code == 201, added.text
        removed = await api.delete(
            f"{DOCTORS}/{doctor_id}/availability/{added.json()['id']}",
            headers=auth(reception_token),
        )
        assert removed.status_code == 403, removed.text
