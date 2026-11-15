"""Set-up for the cross-tenant walk: a user who holds every hospital role, and
a second hospital full of real records whose ids the walk aims at.
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.models import utc_now
from app.core.security import hash_password
from app.modules.clinical import service as clinical_service
from app.modules.clinical.schemas import NoteCreate, NoteTemplateCreate
from app.modules.identity.models import Role, User, UserRole
from app.modules.identity.rbac import PLATFORM_ROLES
from app.modules.notifications import service as notifications_service
from app.modules.notifications.schemas import AdhocSend
from app.modules.scheduling import service as scheduling_service
from app.modules.scheduling.schemas import DoctorCreate
from app.modules.subscriptions import orders
from app.modules.subscriptions.models import PaymentOrderPurpose
from app.modules.tenancy import service as tenancy_service
from app.modules.tenancy.models import Hospital
from app.modules.tenancy.schemas import DepartmentCreate, HospitalCreate
from tests import accounts_support, subscriptions_support
from tests.conftest import TEST_PASSWORD

# (path parameter, a fragment of the path, the table its id lives in). The
# first row whose fragment is in the path wins; "" matches any path.
RESOLVE: list[tuple[str, str, str]] = [
    ("item_id", "/billing/services/", "service_items"),
    ("item_id", "/diagnostics/catalogue/", "test_catalogue_items"),
    ("item_id", "/ipd/medicines/", "medicine_items"),
    ("order_id", "/ipd/medications/", "medication_orders"),
    ("order_id", "/subscriptions/", "payment_orders"),
    ("order_id", "/platform/payments/", "payment_orders"),
    ("order_id", "", "orders"),
    ("request_id", "", "admission_requests"),
    ("summary_id", "", "discharge_summaries"),
    ("template_id", "/note-templates/", "note_templates"),
    ("template_id", "", "notification_templates"),
    ("card_id", "", "rate_cards"),
    ("dose_id", "", "medication_administrations"),
    ("entry_id", "", "queue_entries"),
    ("issue_id", "", "medicine_issues"),
    ("note_id", "", "clinical_notes"),
    ("report_id", "", "diagnostic_reports"),
    ("specimen_id", "", "specimens"),
    ("analyte_id", "", "test_analytes"),
    ("suppression_id", "", "notification_suppressions"),
    ("notification_id", "", "notifications"),
    ("subscription_id", "", "subscriptions"),
    ("admission_id", "", "admissions"),
    ("appointment_id", "", "appointments"),
    ("bed_id", "", "beds"),
    ("ward_id", "", "wards"),
    ("charge_id", "", "charges"),
    ("claim_id", "", "insurance_claims"),
    ("department_id", "", "departments"),
    ("doctor_id", "", "doctors"),
    ("encounter_id", "", "encounters"),
    ("invite_id", "", "invites"),
    ("invoice_id", "", "invoices"),
    ("patient_id", "", "patients"),
    ("payment_id", "", "payments"),
    ("user_id", "", "users"),
]

TABLES = sorted({table for _, _, table in RESOLVE})


def table_for(param: str, path: str) -> str | None:
    for name, fragment, table in RESOLVE:
        if name == param and fragment in path:
            return table
    return None


async def make_hospital(session: AsyncSession, code: str) -> Hospital:
    async with db.system_context(session):
        hospital = await tenancy_service.create_hospital(
            session, HospitalCreate(code=code, name=f"{code} Hospital")
        )
        await session.flush()
    await session.commit()
    return hospital


async def all_roles_user(session: AsyncSession, hospital_id: uuid.UUID) -> User:
    """One account holding every hospital role, so RBAC never answers first.

    Platform roles are left out: they are cross-tenant by design, and the
    service refuses them to an account bound to a hospital.
    """
    async with db.system_context(session):
        user = User(
            hospital_id=hospital_id,
            email=f"all-{uuid.uuid4().hex[:8]}@example.com",
            full_name="Every Role",
            password_hash=hash_password(TEST_PASSWORD),
            must_change_password=False,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        roles = (
            (
                await session.execute(
                    sa.select(Role).where(
                        Role.hospital_id.is_(None),  # type: ignore[union-attr]
                        Role.code.not_in(PLATFORM_ROLES),  # type: ignore[attr-defined]
                    )
                )
            )
            .scalars()
            .all()
        )
        for role in roles:
            session.add(UserRole(user_id=user.id, role_id=role.id))
        await session.flush()
        await session.refresh(user)
    await session.commit()
    return user


async def fill_hospital(session: AsyncSession, hospital: Hospital, actor: User) -> None:
    """Real records of the common kinds: a priced visit, a queue, an admission."""
    hospital_id = hospital.id
    await db.set_tenant_context(session, hospital_id)
    await accounts_support.price_list(session, hospital_id)
    department = await tenancy_service.create_department(
        session, DepartmentCreate(code="GEN", name="General Medicine"), hospital_id=hospital_id
    )
    doctor = await scheduling_service.create_doctor(
        session,
        DoctorCreate(user_id=actor.id, department_id=department.id),
        hospital_id=hospital_id,
        display_name=actor.full_name,
    )
    await session.commit()
    await db.set_tenant_context(session, hospital_id)
    walk_in = await accounts_support.register(session, hospital_id, name="Queue Patient")
    await db.set_tenant_context(session, hospital_id)
    await scheduling_service.quick_opd(
        session, hospital_id=hospital_id, patient_id=walk_in.id, doctor_id=doctor.id
    )
    await session.commit()
    await db.set_tenant_context(session, hospital_id)
    stay = await accounts_support.admitted_stay(session, hospital_id, actor, name="Ward Patient")
    await session.commit()

    # A shared note template, a note written with it, a message to the patient.
    await db.set_tenant_context(session, hospital_id)
    template = await clinical_service.create_template(
        session,
        NoteTemplateCreate(title="Ward round", body="Comfortable overnight.", shared=True),
        hospital_id=hospital_id,
        actor=actor,
    )
    encounter = await clinical_service.get_encounter(
        session, stay.encounter.id, hospital_id=hospital_id
    )
    await clinical_service.write_note(
        session,
        encounter,
        NoteCreate(content="Comfortable overnight.", template_id=template.id),
        actor=actor,
    )
    await notifications_service.send_adhoc(
        session,
        AdhocSend(patient_id=walk_in.id, body="Your token is ready."),
        hospital_id=hospital_id,
        actor=actor,
    )
    await session.commit()

    # A renewal payment order of the hospital's own (a paying hospital: the
    # test default is a complimentary plan, which never renews).
    await subscriptions_support.set_plan(
        session, hospital, "professional", period_end=utc_now() + timedelta(days=10)
    )
    await orders.create_order(
        session, user=actor, plan_code=None, purpose=PaymentOrderPurpose.RENEWAL
    )
    await session.commit()


async def harvest(session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, str]:
    """One id per table, from this hospital only."""
    found: dict[str, str] = {}
    async with db.system_context(session):
        for table in TABLES:
            if not re.fullmatch(r"[a-z_]+", table):
                raise ValueError(table)
            row = (
                await session.execute(
                    sa.text(f"SELECT id FROM {table} WHERE hospital_id = :h LIMIT 1"),  # noqa: S608
                    {"h": hospital_id},
                )
            ).first()
            if row is not None:
                found[table] = str(row[0])
        uhid = (
            await session.execute(
                sa.text("SELECT uhid FROM patients WHERE hospital_id = :h LIMIT 1"),
                {"h": hospital_id},
            )
        ).first()
        if uhid is not None:
            found["uhid"] = str(uhid[0])
    await session.commit()
    return found
