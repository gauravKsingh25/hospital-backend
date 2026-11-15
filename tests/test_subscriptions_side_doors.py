"""A Standard hospital cannot start locked work from an open screen.

Locking the lab's own routes is not enough if the consultation screen can
still order a lab test: the order would sit in a worklist nobody at that
hospital can open. Same for sending a patient to a ward that is not in the
plan, and for patient messages.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.core.exceptions import FeatureNotInPlanError
from app.core.models import utc_now
from app.modules.clinical import service as clinical_service
from app.modules.clinical.models import Encounter, EncounterStatus, Order, OrderType
from app.modules.clinical.schemas import DeathRecord, OrderCreate
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.ipd import service as ipd_service
from app.modules.notifications import service as notifications
from app.modules.notifications.gateways import GatewayResult, OutboundMessage
from app.modules.notifications.models import (
    NotificationChannel,
    NotificationStatus,
    RecipientType,
    SuppressionReason,
)
from app.modules.notifications.service import Recipient
from app.modules.notifications.templates import TemplateCode
from app.modules.patients import service as patients_service
from app.modules.patients.models import Gender, Patient
from app.modules.patients.schemas import PatientRegister
from app.modules.tenancy.models import Hospital
from tests.conftest import _make_user
from tests.subscriptions_support import set_plan

pytestmark = pytest.mark.integration

CONTEXT = {"hospital_name": "Sunrise", "patient_name": "Ram Prasad"}


class RecordingGateway:
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    def supports(self, channel: NotificationChannel) -> bool:
        return True

    async def send(self, message: OutboundMessage) -> GatewayResult:
        self.sent.append(message)
        return GatewayResult.ok(self.name)


async def _to_plan(session: AsyncSession, hospital: Hospital, plan: str) -> None:
    await set_plan(session, hospital, plan, period_end=utc_now() + timedelta(days=10))
    await session.commit()
    await db.set_tenant_context(session, hospital.id)


@pytest.fixture
async def tenant(session: AsyncSession, hospital: Hospital) -> Hospital:
    """A hospital on Standard, with the session bound to it."""
    await _to_plan(session, hospital, "standard")
    return hospital


@pytest.fixture
async def doctor_user(session: AsyncSession, tenant: Hospital) -> User:
    user = await _make_user(session, role_code=Roles.DOCTOR, hospital_id=tenant.id)
    await db.set_tenant_context(session, tenant.id)
    return user


@pytest.fixture
async def patient(session: AsyncSession, tenant: Hospital) -> Patient:
    record, _ = await patients_service.register_patient(
        session,
        PatientRegister(
            full_name="Ram Prasad", phone="9812345670", gender=Gender.MALE, age_years=71
        ),
        hospital_id=tenant.id,
    )
    await session.commit()
    return record


@pytest.fixture
def recipient(patient: Patient) -> Recipient:
    return Recipient(
        recipient_type=RecipientType.PATIENT,
        name=patient.full_name,
        phone=patient.phone,
        email="ram@example.com",
        language="en",
        patient_id=patient.id,
    )


@pytest.fixture
async def encounter(
    session: AsyncSession, tenant: Hospital, patient: Patient, doctor_user: User
) -> Encounter:
    record = await clinical_service.open_encounter(
        session, hospital_id=tenant.id, patient_id=patient.id, actor=doctor_user
    )
    await session.commit()
    return record


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("order_type", [OrderType.LAB, OrderType.RADIOLOGY])
async def test_standard_cannot_order_a_test(
    session: AsyncSession, encounter: Encounter, doctor_user: User, order_type: OrderType
) -> None:
    with pytest.raises(FeatureNotInPlanError) as refused:
        await clinical_service.place_order(
            session,
            encounter,
            OrderCreate(order_type=order_type, item_name="CBC"),
            actor=doctor_user,
        )

    assert refused.value.details == {"feature": "diagnostics", "plan": "standard"}
    orders = (await session.execute(select(Order))).scalars().all()
    assert orders == []


async def test_standard_can_still_prescribe_on_an_opd_visit(
    session: AsyncSession, encounter: Encounter, doctor_user: User
) -> None:
    order = await clinical_service.place_order(
        session,
        encounter,
        OrderCreate(order_type=OrderType.PHARMACY, item_name="Paracetamol 650mg"),
        actor=doctor_user,
    )

    assert order.id is not None


async def test_professional_orders_a_test(
    session: AsyncSession, tenant: Hospital, encounter: Encounter, doctor_user: User
) -> None:
    await _to_plan(session, tenant, "professional")

    order = await clinical_service.place_order(
        session,
        encounter,
        OrderCreate(order_type=OrderType.LAB, item_name="CBC"),
        actor=doctor_user,
    )

    assert order.order_type is OrderType.LAB


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------
async def test_standard_cannot_request_admission(
    session: AsyncSession, tenant: Hospital, encounter: Encounter, doctor_user: User
) -> None:
    with pytest.raises(FeatureNotInPlanError) as refused:
        await ipd_service.request_admission(
            session, hospital_id=tenant.id, encounter_id=encounter.id, actor=doctor_user
        )

    assert refused.value.details == {"feature": "ipd", "plan": "standard"}


async def test_standard_cannot_convert_a_visit_to_an_admission(
    session: AsyncSession, encounter: Encounter, doctor_user: User
) -> None:
    before = encounter.status

    with pytest.raises(FeatureNotInPlanError):
        await clinical_service.admit(session, encounter, actor=doctor_user)

    await session.refresh(encounter)
    assert encounter.status is before
    assert encounter.status is not EncounterStatus.ADMITTED


# ---------------------------------------------------------------------------
# Patient messages
# ---------------------------------------------------------------------------
async def test_standard_messages_are_recorded_as_not_in_plan(
    session: AsyncSession, tenant: Hospital, recipient: Recipient
) -> None:
    gateway = RecordingGateway()

    notification = await notifications.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=recipient,
        context=CONTEXT,
        gateway=gateway,
    )
    await session.commit()

    assert notification.status is NotificationStatus.SUPPRESSED
    assert notification.suppression_reason is SuppressionReason.NOT_IN_PLAN
    assert gateway.sent == []


async def test_message_queued_on_professional_is_not_sent_after_downgrade(
    session: AsyncSession, tenant: Hospital, recipient: Recipient
) -> None:
    await _to_plan(session, tenant, "professional")
    queued = await notifications.enqueue(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=recipient,
        context=CONTEXT,
    )
    await session.commit()
    assert queued.status is not NotificationStatus.SUPPRESSED

    await _to_plan(session, tenant, "standard")
    gateway = RecordingGateway()
    result = await notifications.dispatch(session, queued, gateway=gateway)
    await session.commit()

    assert result.status is NotificationStatus.SUPPRESSED
    assert result.suppression_reason is SuppressionReason.NOT_IN_PLAN
    assert gateway.sent == []


async def test_deceased_still_wins_over_not_in_plan(
    session: AsyncSession,
    tenant: Hospital,
    encounter: Encounter,
    doctor_user: User,
    recipient: Recipient,
) -> None:
    await clinical_service.record_death(
        session,
        encounter,
        DeathRecord(
            deceased_at=utc_now(),
            death_certified_by_id=doctor_user.id,
            cause_of_death="Cardiac arrest.",
        ),
        actor=doctor_user,
    )
    await session.commit()

    notification = await notifications.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=recipient,
        context=CONTEXT,
        gateway=RecordingGateway(),
    )

    assert notification.suppression_reason is SuppressionReason.DECEASED


async def test_staff_are_still_messaged_on_standard(
    session: AsyncSession, tenant: Hospital, doctor_user: User
) -> None:
    """The lock is on messaging patients. A message to the hospital's own
    staff (a bill that needs settling, an alert) is not what the plan sells."""
    gateway = RecordingGateway()

    notification = await notifications.notify(
        session,
        hospital_id=tenant.id,
        code=TemplateCode.FOLLOW_UP_REMINDER,
        recipient=Recipient(
            recipient_type=RecipientType.STAFF,
            name=doctor_user.full_name,
            phone="9800000001",
            email=doctor_user.email,
            language="en",
            user_id=doctor_user.id,
        ),
        context=CONTEXT,
        gateway=gateway,
    )

    assert notification.suppression_reason is not SuppressionReason.NOT_IN_PLAN
