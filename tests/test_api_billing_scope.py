"""Who reads which bills.

`invoice:read` is the counter's: the visit at the desk. A patient's bills across
visits need `patient_bills:read`, and every patient's need `invoice:read_all`.
Reception collects today's fee and reads nothing wider; a doctor sees the
patient's dues, not the hospital's; the accountant marks a payment received.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.tenancy.models import Hospital
from tests import accounts_support as support
from tests.conftest import _make_user, auth
from tests.test_api_accounts import ACCOUNTS, INVOICES, _token

pytestmark = pytest.mark.integration

PAYMENTS = "/api/v1/billing/payments"
PATIENTS = "/api/v1/billing/patients"


@pytest.fixture
async def tenant(session: AsyncSession, hospital: Hospital) -> Hospital:
    await db.set_tenant_context(session, hospital.id)
    await support.price_list(session, hospital.id)
    return hospital


@pytest.fixture
async def admin_user(session: AsyncSession, tenant: Hospital) -> User:
    user = await _make_user(session, role_code=Roles.HOSPITAL_ADMIN, hospital_id=tenant.id)
    await db.set_tenant_context(session, tenant.id)
    return user


@pytest.fixture
async def billed(
    api: AsyncClient, session: AsyncSession, tenant: Hospital, admin_user: User
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """An OPD visit whose ₹500 bill is issued and unpaid: (patient, visit, invoice)."""
    patient = await support.register(session, tenant.id)
    visit = await support.open_visit(session, tenant.id, patient, admin_user)
    token = await _token(api, session, tenant, Roles.CASHIER)
    draft = await api.post(INVOICES, json={"encounter_id": str(visit.id)}, headers=auth(token))
    assert draft.status_code == 201, draft.text
    invoice_id = draft.json()["id"]
    issued = await api.post(f"{INVOICES}/{invoice_id}/issue", json={}, headers=auth(token))
    assert issued.status_code == 200, issued.text
    return patient.id, visit.id, invoice_id


async def test_reception_reads_the_visit_at_the_desk_and_nothing_wider(
    api: AsyncClient,
    session: AsyncSession,
    tenant: Hospital,
    billed: tuple[uuid.UUID, uuid.UUID, str],
) -> None:
    patient_id, visit_id, invoice_id = billed
    token = await _token(api, session, tenant, Roles.RECEPTIONIST)

    at_desk = await api.get(
        INVOICES, params={"encounter_id": str(visit_id)}, headers=auth(token)
    )
    assert at_desk.status_code == 200, at_desk.text
    assert at_desk.json()["total"] == 1
    receipts = await api.get(PAYMENTS, params={"invoice_id": invoice_id}, headers=auth(token))
    assert receipts.status_code == 200, receipts.text

    refused = [
        await api.get(INVOICES, params={"patient_id": str(patient_id)}, headers=auth(token)),
        await api.get(INVOICES, headers=auth(token)),
        await api.get(PAYMENTS, params={"patient_id": str(patient_id)}, headers=auth(token)),
        await api.get(PAYMENTS, headers=auth(token)),
        await api.get(ACCOUNTS, headers=auth(token)),
        await api.get(f"{PATIENTS}/{patient_id}/history", headers=auth(token)),
    ]
    for response in refused:
        assert response.status_code == 403, (response.request.url, response.text)


async def test_the_counter_board_is_not_for_clinical_staff(
    api: AsyncClient, session: AsyncSession, tenant: Hospital
) -> None:
    for role in (Roles.NURSE, Roles.DOCTOR, Roles.ADMISSION_DESK, Roles.RECORDS_OFFICER):
        token = await _token(api, session, tenant, role)
        response = await api.get(ACCOUNTS, headers=auth(token))
        assert response.status_code == 403, (role, response.text)


async def test_a_doctor_sees_the_patients_dues_with_the_phone_masked(
    api: AsyncClient,
    session: AsyncSession,
    tenant: Hospital,
    billed: tuple[uuid.UUID, uuid.UUID, str],
) -> None:
    patient_id, _, _ = billed
    token = await _token(api, session, tenant, Roles.DOCTOR)

    history = await api.get(f"{PATIENTS}/{patient_id}/history", headers=auth(token))
    assert history.status_code == 200, history.text
    body = history.json()
    assert body["lifetime_due"] == "500.00"
    assert body["patient"]["phone"] != "9812345670"
    assert body["patient"]["phone"].endswith("670")

    by_patient = await api.get(
        INVOICES, params={"patient_id": str(patient_id)}, headers=auth(token)
    )
    assert by_patient.status_code == 200, by_patient.text
    everyone = await api.get(INVOICES, headers=auth(token))
    assert everyone.status_code == 403, everyone.text


async def test_the_accountant_marks_a_payment_received(
    api: AsyncClient,
    session: AsyncSession,
    tenant: Hospital,
    billed: tuple[uuid.UUID, uuid.UUID, str],
) -> None:
    patient_id, _, invoice_id = billed
    token = await _token(api, session, tenant, Roles.ACCOUNTANT)

    history = await api.get(f"{PATIENTS}/{patient_id}/history", headers=auth(token))
    assert history.json()["patient"]["phone"] == "9812345670"

    paid = await api.post(
        f"{INVOICES}/{invoice_id}/payments",
        json={"amount": "500.00", "method": "UPI", "reference": "UTR998877"},
        headers=auth(token),
    )
    assert paid.status_code == 201, paid.text

    after = (await api.get(f"{PATIENTS}/{patient_id}/history", headers=auth(token))).json()
    assert after["lifetime_due"] == "0.00"

    # Taking money is not giving it back.
    refund = await api.post(
        f"{PAYMENTS}/{paid.json()['id']}/reverse",
        json={"reason": "Entered twice by mistake."},
        headers=auth(token),
    )
    assert refund.status_code == 403, refund.text
