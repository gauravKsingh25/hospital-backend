"""Doctors involvement: a per-doctor switch.

Spec: Docs/superpowers/specs/2026-11-15-doctor-page-and-involvement-design.md.

On, the doctor records and closes their own visits, exactly as before. Off,
reception and nurses mark the doctor's patients seen and the doctor can only
look. Only the hospital admin flips it, and every flip is audited, because it
changes who may close a clinical visit.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.modules.identity.models import AuditLog, User
from app.modules.identity.rbac import Roles
from app.modules.tenancy.models import Hospital
from tests.conftest import _make_user, auth, login

pytestmark = pytest.mark.integration

PATIENTS = "/api/v1/patients"
DOCTORS = "/api/v1/doctors"
QUEUE = "/api/v1/queue"


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
async def doctor_id(api: AsyncClient, admin_token: str, doctor_user: User) -> str:
    response = await api.post(
        DOCTORS,
        json={"user_id": str(doctor_user.id), "specialty": "General Medicine"},
        headers=auth(admin_token),
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def set_involvement(api: AsyncClient, token: str, doctor_id: str, *, enabled: bool) -> None:
    response = await api.put(
        f"{DOCTORS}/{doctor_id}/involvement", json={"enabled": enabled}, headers=auth(token)
    )
    assert response.status_code == 200, response.text


async def queue_patient(api: AsyncClient, token: str, doctor_id: str, *, phone: str) -> dict:
    patient = await api.post(
        PATIENTS,
        json={"full_name": "Sunita Devi", "phone": phone, "gender": "FEMALE", "age_years": 34},
        headers=auth(token),
    )
    assert patient.status_code == 201, patient.text
    response = await api.post(
        f"{QUEUE}/quick-opd",
        json={"patient_id": patient.json()["id"], "doctor_id": doctor_id},
        headers=auth(token),
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


class TestTheSwitch:
    async def test_new_doctor_is_involved_by_default(
        self, api: AsyncClient, admin_token: str, doctor_id: str
    ) -> None:
        response = await api.get(f"{DOCTORS}/{doctor_id}", headers=auth(admin_token))
        assert response.status_code == 200, response.text
        assert response.json()["involvement_enabled"] is True

    async def test_admin_turns_involvement_off_and_it_is_audited(
        self, api: AsyncClient, session: AsyncSession, admin_token: str, doctor_id: str
    ) -> None:
        response = await api.put(
            f"{DOCTORS}/{doctor_id}/involvement", json={"enabled": False}, headers=auth(admin_token)
        )
        assert response.status_code == 200, response.text
        assert response.json()["involvement_enabled"] is False

        async with db.system_context(session):
            rows = (
                (
                    await session.execute(
                        sa.select(AuditLog).where(AuditLog.action == "doctor.involvement_changed")
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1
        assert rows[0].changes["before"] == {"enabled": True}
        assert rows[0].changes["after"] == {"enabled": False}

    async def test_receptionist_cannot_flip_involvement(
        self, api: AsyncClient, reception_token: str, doctor_id: str
    ) -> None:
        response = await api.put(
            f"{DOCTORS}/{doctor_id}/involvement",
            json={"enabled": False},
            headers=auth(reception_token),
        )
        assert response.status_code == 403, response.text

    async def test_involvement_rejects_unknown_fields(
        self, api: AsyncClient, admin_token: str, doctor_id: str
    ) -> None:
        response = await api.put(
            f"{DOCTORS}/{doctor_id}/involvement",
            json={"enabled": False, "x": 1},
            headers=auth(admin_token),
        )
        assert response.status_code == 422, response.text

    async def test_board_column_carries_involvement(
        self, api: AsyncClient, admin_token: str, reception_token: str, doctor_id: str
    ) -> None:
        await queue_patient(api, reception_token, doctor_id, phone="9876543210")
        await set_involvement(api, admin_token, doctor_id, enabled=False)

        response = await api.get(f"{QUEUE}/board", headers=auth(reception_token))
        assert response.status_code == 200, response.text
        column = next(c for c in response.json() if c["doctor_id"] == doctor_id)
        assert column["involvement_enabled"] is False


ENCOUNTERS = "/api/v1/encounters"
ORDERS = "/api/v1/orders"


@pytest.fixture
async def doctor_token(api: AsyncClient, doctor_user: User) -> str:
    return await login(api, doctor_user)


@pytest.fixture
async def nurse_token(api: AsyncClient, session: AsyncSession, hospital: Hospital) -> str:
    nurse = await _make_user(session, role_code=Roles.NURSE, hospital_id=hospital.id)
    return await login(api, nurse)


@pytest.fixture
async def pharmacy_token(api: AsyncClient, session: AsyncSession, hospital: Hospital) -> str:
    pharmacist = await _make_user(session, role_code=Roles.PHARMACIST, hospital_id=hospital.id)
    return await login(api, pharmacist)


def refused(response: Response, code: str = "doctor_entry_off") -> None:
    body = response.json()
    assert response.status_code == 409, body
    assert body["error"]["code"] == code, body


DOCTOR_WRITES: dict[str, tuple[str, str, dict[str, object] | None]] = {
    "note": ("post", "/notes", {"content": "Chest clear."}),
    "diagnosis": ("post", "/diagnoses", {"description": "Viral fever"}),
    "order": ("post", "/orders", {"order_type": "PHARMACY", "item_name": "Paracetamol 650mg"}),
    "start": ("post", "/start", None),
    "complete": ("post", "/complete", {}),
    "follow_up": ("patch", "", {"follow_up_instructions": "Review in a week"}),
}


async def place_order(api: AsyncClient, token: str, encounter_id: str, kind: str) -> str:
    item = "Complete blood count" if kind == "LAB" else "Paracetamol 650mg"
    response = await api.post(
        f"{ENCOUNTERS}/{encounter_id}/orders",
        json={"order_type": kind, "item_name": item},
        headers=auth(token),
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def status_of(api: AsyncClient, token: str, encounter_id: str) -> str:
    response = await api.get(f"{ENCOUNTERS}/{encounter_id}", headers=auth(token))
    assert response.status_code == 200, response.text
    return str(response.json()["status"])


async def mark_seen(api: AsyncClient, token: str, entry_id: str) -> Response:
    return await api.post(f"{QUEUE}/{entry_id}/seen", headers=auth(token))


class TestTheGuard:
    @pytest.mark.parametrize("write", sorted(DOCTOR_WRITES))
    async def test_off_doctor_refuses_each_doctor_write(
        self,
        api: AsyncClient,
        admin_token: str,
        reception_token: str,
        doctor_token: str,
        doctor_id: str,
        write: str,
    ) -> None:
        visit = await queue_patient(api, reception_token, doctor_id, phone="9876500001")
        await set_involvement(api, admin_token, doctor_id, enabled=False)

        method, suffix, body = DOCTOR_WRITES[write]
        url = f"{ENCOUNTERS}/{visit['encounter_id']}{suffix}"
        if body is None:
            response = await api.request(method.upper(), url, headers=auth(doctor_token))
        else:
            response = await api.request(method.upper(), url, json=body, headers=auth(doctor_token))
        refused(response)

    async def test_off_doctor_refuses_sign_note_and_cancel_order(
        self,
        api: AsyncClient,
        admin_token: str,
        reception_token: str,
        doctor_token: str,
        doctor_id: str,
    ) -> None:
        visit = await queue_patient(api, reception_token, doctor_id, phone="9876500002")
        note = await api.post(
            f"{ENCOUNTERS}/{visit['encounter_id']}/notes",
            json={"content": "Draft", "sign": False},
            headers=auth(doctor_token),
        )
        assert note.status_code == 201, note.text
        order_id = await place_order(api, doctor_token, visit["encounter_id"], "PHARMACY")

        await set_involvement(api, admin_token, doctor_id, enabled=False)

        note_id = note.json()["id"]
        refused(await api.post(f"{ENCOUNTERS}/notes/{note_id}/sign", headers=auth(doctor_token)))
        refused(
            await api.post(
                f"{ORDERS}/{order_id}/cancel",
                json={"reason": "Not needed"},
                headers=auth(doctor_token),
            )
        )

    async def test_admin_is_refused_too(
        self, api: AsyncClient, admin_token: str, reception_token: str, doctor_id: str
    ) -> None:
        visit = await queue_patient(api, reception_token, doctor_id, phone="9876500003")
        await set_involvement(api, admin_token, doctor_id, enabled=False)
        refused(
            await api.post(
                f"{ENCOUNTERS}/{visit['encounter_id']}/notes",
                json={"content": "Admin note"},
                headers=auth(admin_token),
            )
        )

    async def test_vitals_still_recorded_for_off_doctor(
        self,
        api: AsyncClient,
        admin_token: str,
        reception_token: str,
        nurse_token: str,
        doctor_id: str,
    ) -> None:
        visit = await queue_patient(api, reception_token, doctor_id, phone="9876500004")
        await set_involvement(api, admin_token, doctor_id, enabled=False)
        response = await api.post(
            f"{ENCOUNTERS}/{visit['encounter_id']}/vitals",
            json={"pulse_bpm": 82},
            headers=auth(nurse_token),
        )
        assert response.status_code == 201, response.text

    async def test_reassigning_the_visit_is_allowed(
        self, api: AsyncClient, admin_token: str, reception_token: str, doctor_id: str
    ) -> None:
        visit = await queue_patient(api, reception_token, doctor_id, phone="9876500005")
        await set_involvement(api, admin_token, doctor_id, enabled=False)
        response = await api.patch(
            f"{ENCOUNTERS}/{visit['encounter_id']}",
            json={"doctor_id": doctor_id},
            headers=auth(admin_token),
        )
        assert response.status_code == 200, response.text

    async def test_switch_back_on_restores_entry(
        self,
        api: AsyncClient,
        admin_token: str,
        reception_token: str,
        doctor_token: str,
        doctor_id: str,
    ) -> None:
        visit = await queue_patient(api, reception_token, doctor_id, phone="9876500006")
        await set_involvement(api, admin_token, doctor_id, enabled=False)
        await set_involvement(api, admin_token, doctor_id, enabled=True)
        response = await api.post(
            f"{ENCOUNTERS}/{visit['encounter_id']}/notes",
            json={"content": "Chest clear."},
            headers=auth(doctor_token),
        )
        assert response.status_code == 201, response.text

    async def test_admitted_visit_is_outside_the_switch(
        self,
        api: AsyncClient,
        admin_token: str,
        reception_token: str,
        doctor_token: str,
        doctor_id: str,
    ) -> None:
        visit = await queue_patient(api, reception_token, doctor_id, phone="9876500007")
        encounter = f"{ENCOUNTERS}/{visit['encounter_id']}"
        await api.post(f"{encounter}/start", headers=auth(doctor_token))
        admitted = await api.post(f"{encounter}/admit", json={}, headers=auth(doctor_token))
        assert admitted.status_code == 200, admitted.text
        assert admitted.json()["status"] == "ADMITTED"

        await set_involvement(api, admin_token, doctor_id, enabled=False)
        note = await api.post(
            f"{encounter}/notes", json={"content": "Ward round."}, headers=auth(doctor_token)
        )
        assert note.status_code == 201, note.text
        await place_order(api, doctor_token, visit["encounter_id"], "PHARMACY")


class TestSeenClosesTheVisit:
    async def test_seen_completes_off_doctors_visit(
        self, api: AsyncClient, admin_token: str, reception_token: str, doctor_id: str
    ) -> None:
        visit = await queue_patient(api, reception_token, doctor_id, phone="9876500011")
        await set_involvement(api, admin_token, doctor_id, enabled=False)
        seen = await mark_seen(api, reception_token, visit["queue_entry"]["id"])
        assert seen.status_code == 200, seen.text
        assert await status_of(api, admin_token, visit["encounter_id"]) == "COMPLETED"

    async def test_seen_with_pending_lab_order_stays_open(
        self,
        api: AsyncClient,
        admin_token: str,
        reception_token: str,
        doctor_token: str,
        doctor_id: str,
    ) -> None:
        visit = await queue_patient(api, reception_token, doctor_id, phone="9876500012")
        await place_order(api, doctor_token, visit["encounter_id"], "LAB")
        await set_involvement(api, admin_token, doctor_id, enabled=False)
        seen = await mark_seen(api, reception_token, visit["queue_entry"]["id"])
        assert seen.status_code == 200, seen.text
        # Held open by the lab order, exactly where the doctor's own "complete
        # visit" would have left it: Seen shares that close.
        assert await status_of(api, admin_token, visit["encounter_id"]) in {
            "AWAITING_RESULTS",
            "PENDING_CLEARANCE",
        }

    async def test_seen_with_pending_medicine_goes_to_clearance(
        self,
        api: AsyncClient,
        admin_token: str,
        reception_token: str,
        doctor_token: str,
        doctor_id: str,
    ) -> None:
        visit = await queue_patient(api, reception_token, doctor_id, phone="9876500013")
        await place_order(api, doctor_token, visit["encounter_id"], "PHARMACY")
        await set_involvement(api, admin_token, doctor_id, enabled=False)
        seen = await mark_seen(api, reception_token, visit["queue_entry"]["id"])
        assert seen.status_code == 200, seen.text
        assert await status_of(api, admin_token, visit["encounter_id"]) == "PENDING_CLEARANCE"

    async def test_off_doctors_visit_still_clears_at_the_pharmacy(
        self,
        api: AsyncClient,
        admin_token: str,
        reception_token: str,
        doctor_token: str,
        pharmacy_token: str,
        doctor_id: str,
    ) -> None:
        visit = await queue_patient(api, reception_token, doctor_id, phone="9876500014")
        order_id = await place_order(api, doctor_token, visit["encounter_id"], "PHARMACY")
        await set_involvement(api, admin_token, doctor_id, enabled=False)
        seen = await mark_seen(api, reception_token, visit["queue_entry"]["id"])
        assert seen.status_code == 200, seen.text

        cleared = await api.post(f"{ORDERS}/{order_id}/complete", headers=auth(pharmacy_token))
        assert cleared.status_code == 200, cleared.text
        assert await status_of(api, admin_token, visit["encounter_id"]) == "COMPLETED"


class TestQueueRules:
    async def test_seen_refused_while_doctor_involved(
        self, api: AsyncClient, reception_token: str, doctor_id: str
    ) -> None:
        visit = await queue_patient(api, reception_token, doctor_id, phone="9876500021")
        entry_id = visit["queue_entry"]["id"]
        refused(await mark_seen(api, reception_token, entry_id), code="doctor_involved")

        board = await api.get(f"{QUEUE}/board", headers=auth(reception_token))
        column = next(c for c in board.json() if c["doctor_id"] == doctor_id)
        assert [e["id"] for e in column["entries"]] == [entry_id]

    async def test_reception_and_nurse_mark_seen_for_off_doctor(
        self,
        api: AsyncClient,
        admin_token: str,
        reception_token: str,
        nurse_token: str,
        doctor_id: str,
    ) -> None:
        first = await queue_patient(api, reception_token, doctor_id, phone="9876500022")
        second = await queue_patient(api, reception_token, doctor_id, phone="9876500023")
        await set_involvement(api, admin_token, doctor_id, enabled=False)

        by_reception = await mark_seen(api, reception_token, first["queue_entry"]["id"])
        assert by_reception.status_code == 200, by_reception.text
        by_nurse = await mark_seen(api, nurse_token, second["queue_entry"]["id"])
        assert by_nurse.status_code == 200, by_nurse.text

        assert await status_of(api, admin_token, first["encounter_id"]) == "COMPLETED"
        assert await status_of(api, admin_token, second["encounter_id"]) == "COMPLETED"

    async def test_queue_complete_refused_for_off_doctor(
        self,
        api: AsyncClient,
        admin_token: str,
        reception_token: str,
        doctor_token: str,
        doctor_id: str,
    ) -> None:
        visit = await queue_patient(api, reception_token, doctor_id, phone="9876500024")
        entry_id = visit["queue_entry"]["id"]
        started = await api.post(f"{QUEUE}/{entry_id}/start", headers=auth(reception_token))
        assert started.status_code == 200, started.text
        await set_involvement(api, admin_token, doctor_id, enabled=False)
        refused(await api.post(f"{QUEUE}/{entry_id}/complete", headers=auth(doctor_token)))

    async def test_reception_still_calls_and_starts_for_off_doctor(
        self, api: AsyncClient, admin_token: str, reception_token: str, doctor_id: str
    ) -> None:
        visit = await queue_patient(api, reception_token, doctor_id, phone="9876500025")
        await set_involvement(api, admin_token, doctor_id, enabled=False)
        called = await api.post(
            f"{QUEUE}/call-next", params={"doctor_id": doctor_id}, headers=auth(reception_token)
        )
        assert called.status_code == 200, called.text
        started = await api.post(
            f"{QUEUE}/{visit['queue_entry']['id']}/start", headers=auth(reception_token)
        )
        assert started.status_code == 200, started.text


class TestTheOffDoctorOnlyLooks:
    async def test_off_doctor_cannot_mark_their_own_patient_seen(
        self,
        api: AsyncClient,
        admin_token: str,
        reception_token: str,
        doctor_token: str,
        doctor_id: str,
    ) -> None:
        """"Doctor can only see, not edit": Seen closes the visit, so the
        doctor whose entry is off may not press it on their own patients."""
        visit = await queue_patient(api, reception_token, doctor_id, phone="9876500031")
        await set_involvement(api, admin_token, doctor_id, enabled=False)
        refused(
            await mark_seen(api, doctor_token, visit["queue_entry"]["id"]),
            code="doctor_entry_off",
        )
        assert await status_of(api, admin_token, visit["encounter_id"]) == "REGISTERED"
