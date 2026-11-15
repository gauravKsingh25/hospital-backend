"""An id in a request body must belong to the caller's hospital.

Row-level security hides another hospital's rows from reads, but a foreign key
is checked by Postgres *without* RLS: an INSERT that names hospital B's patient
or doctor succeeds unless the service loads that row inside the caller's
hospital first. Each create path that stores an id taken from the request is
tried here with hospital B's id, as hospital A (final review of slice 5,
Important 4). Every one must answer "not found".
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.exceptions import DomainError
from app.modules.clinical import service as clinical_service
from app.modules.clinical.schemas import NoteCreate, NoteTemplateCreate
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.ipd import service as ipd_service
from app.modules.ipd.models import BedClass
from app.modules.ipd.schemas import AdmissionCreate, AdmitFromRequest, BedCreate, WardCreate
from app.modules.notifications import service as notifications_service
from app.modules.notifications.schemas import AdhocSend
from app.modules.scheduling import service as scheduling_service
from app.modules.scheduling.schemas import AppointmentBook, DoctorCreate
from app.modules.tenancy import service as tenancy_service
from app.modules.tenancy.schemas import DepartmentCreate
from tests import accounts_support
from tests.conftest import _make_user
from tests.isolation_support import make_hospital

pytestmark = pytest.mark.integration


async def _side(session: AsyncSession, code: str) -> dict[str, Any]:
    """A hospital with a department, a doctor, a patient and an open visit."""
    hospital = await make_hospital(session, code)
    doctor_user: User = await _make_user(session, role_code=Roles.DOCTOR, hospital_id=hospital.id)
    await db.set_tenant_context(session, hospital.id)
    department = await tenancy_service.create_department(
        session, DepartmentCreate(code="GEN", name="General Medicine"), hospital_id=hospital.id
    )
    doctor = await scheduling_service.create_doctor(
        session,
        DoctorCreate(user_id=doctor_user.id, department_id=department.id),
        hospital_id=hospital.id,
        display_name="Dr Side",
    )
    template = await clinical_service.create_template(
        session,
        NoteTemplateCreate(title="Fever", body="Fever for days.", shared=True),
        hospital_id=hospital.id,
        actor=doctor_user,
    )
    await session.commit()
    await db.set_tenant_context(session, hospital.id)
    await accounts_support.price_list(session, hospital.id)
    await db.set_tenant_context(session, hospital.id)
    patient = await accounts_support.register(session, hospital.id, name=f"{code} Patient")
    await db.set_tenant_context(session, hospital.id)
    encounter = await accounts_support.open_visit(session, hospital.id, patient, doctor_user)
    return {
        "hospital": hospital.id,
        "user": doctor_user,
        "department": department.id,
        "doctor": doctor.id,
        "template": template.id,
        "patient": patient.id,
        "encounter": encounter.id,
    }


@pytest.fixture
async def sides(session: AsyncSession) -> tuple[dict[str, Any], dict[str, Any]]:
    a = await _side(session, "BODYA")
    b = await _side(session, "BODYB")
    await db.set_tenant_context(session, a["hospital"])
    return a, b


async def _not_found(call: Any) -> None:
    with pytest.raises(DomainError) as caught:
        await call
    assert caught.value.status_code == 404, caught.value.code


async def test_an_appointment_cannot_name_another_hospitals_patient(
    session: AsyncSession, sides: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    a, b = sides
    await _not_found(
        scheduling_service.book_appointment(
            session,
            AppointmentBook(
                patient_id=b["patient"],
                doctor_id=a["doctor"],
                scheduled_start=datetime.now(UTC),
                override_availability=True,
            ),
            hospital_id=a["hospital"],
        )
    )


async def test_a_ward_cannot_belong_to_another_hospitals_department(
    session: AsyncSession, sides: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    a, b = sides
    await _not_found(
        ipd_service.create_ward(
            session,
            WardCreate(code="XW", name="Cross Ward", department_id=b["department"]),
            hospital_id=a["hospital"],
        )
    )


async def test_a_note_template_cannot_belong_to_another_hospitals_department(
    session: AsyncSession, sides: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    a, b = sides
    await _not_found(
        clinical_service.create_template(
            session,
            NoteTemplateCreate(title="X", body="Y", department_id=b["department"]),
            hospital_id=a["hospital"],
            actor=a["user"],
        )
    )


async def test_a_note_cannot_cite_another_hospitals_template(
    session: AsyncSession, sides: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    a, b = sides
    encounter = await clinical_service.get_encounter(
        session, a["encounter"], hospital_id=a["hospital"]
    )
    await _not_found(
        clinical_service.write_note(
            session,
            encounter,
            NoteCreate(content="Seen.", template_id=b["template"]),
            actor=a["user"],
        )
    )


async def test_a_message_cannot_name_another_hospitals_visit(
    session: AsyncSession, sides: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    a, b = sides
    await _not_found(
        notifications_service.send_adhoc(
            session,
            AdhocSend(
                patient_id=a["patient"], body="Your report is ready.", encounter_id=b["encounter"]
            ),
            hospital_id=a["hospital"],
            actor=a["user"],
        )
    )


async def _bed(session: AsyncSession, hospital_id: uuid.UUID) -> uuid.UUID:
    suffix = uuid.uuid4().hex[:4].upper()
    ward = await ipd_service.create_ward(
        session,
        WardCreate(code=f"W{suffix}", name="Ward", bed_class=BedClass.GENERAL),
        hospital_id=hospital_id,
    )
    bed = await ipd_service.create_bed(
        session,
        BedCreate(ward_id=ward.id, code=f"W{suffix}-1", tariff_item_code="ROOM-GEN"),
        hospital_id=hospital_id,
    )
    return bed.id


@pytest.mark.parametrize("field", ["attending_doctor_id", "department_id"])
async def test_an_admission_cannot_name_another_hospitals_doctor_or_department(
    session: AsyncSession, sides: tuple[dict[str, Any], dict[str, Any]], field: str
) -> None:
    a, b = sides
    foreign = b["doctor"] if field == "attending_doctor_id" else b["department"]
    bed = await _bed(session, a["hospital"])
    await _not_found(
        ipd_service.admit_patient(
            session,
            AdmissionCreate(encounter_id=a["encounter"], bed_id=bed, **{field: foreign}),
            hospital_id=a["hospital"],
            actor=a["user"],
        )
    )


@pytest.mark.parametrize("field", ["attending_doctor_id", "department_id"])
async def test_an_admission_from_a_request_cannot_name_another_hospitals_doctor_or_department(
    session: AsyncSession, sides: tuple[dict[str, Any], dict[str, Any]], field: str
) -> None:
    a, b = sides
    foreign = b["doctor"] if field == "attending_doctor_id" else b["department"]
    bed = await _bed(session, a["hospital"])
    request = await ipd_service.request_admission(
        session, hospital_id=a["hospital"], encounter_id=a["encounter"]
    )
    await _not_found(
        ipd_service.admit_from_request(
            session, request, AdmitFromRequest(bed_id=bed, **{field: foreign}), actor=a["user"]
        )
    )
