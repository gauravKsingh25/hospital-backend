"""The console's payment queue: who may see it, who may decide, and the proof."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.subscriptions import orders
from tests.conftest import _make_user, auth, login
from tests.payments_support import PDF, PNG, chunks, pending_user, submitted_order

# Tests that approve create hospitals, and approval writes the subscription
# itself, so those opt out of the harness granting one.
approving = pytest.mark.no_auto_subscription

PAYMENTS = "/api/v1/platform/payments"


@pytest.fixture
async def platform_support(session: AsyncSession) -> User:
    return await _make_user(session, role_code=Roles.PLATFORM_SUPPORT, hospital_id=None)


async def test_queue_lists_submitted_payments(
    api: AsyncClient, session: AsyncSession, platform_admin: User
) -> None:
    payer = await pending_user(session, hospital_name="Sunrise Hospital")
    order = await submitted_order(session, payer, utr="246824682468")
    response = await api.get(
        PAYMENTS, params={"status": "SUBMITTED"}, headers=auth(await login(api, platform_admin))
    )
    assert response.status_code == 200, response.text
    [row] = response.json()["items"]
    assert row["id"] == str(order.id)
    assert row["reference_code"] == order.reference_code
    assert row["utr"] == "246824682468"
    assert row["payer_email"] == payer.email
    assert row["hospital_name"] == "Sunrise Hospital"
    assert row["amount_inr"] == 3000
    assert row["has_proof"] is False


@approving
async def test_approve_and_reject_over_http(
    api: AsyncClient, session: AsyncSession, platform_admin: User
) -> None:
    headers = auth(await login(api, platform_admin))
    first = await submitted_order(session, await pending_user(session), utr="111111111111")
    second = await submitted_order(session, await pending_user(session), utr="222222222222")

    approved = await api.post(f"{PAYMENTS}/{first.id}/approve", headers=headers)
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"
    assert approved.json()["receipt_number"].startswith("NIR-")

    short = await api.post(f"{PAYMENTS}/{second.id}/reject", json={"reason": "no"}, headers=headers)
    assert short.status_code == 422
    rejected = await api.post(
        f"{PAYMENTS}/{second.id}/reject",
        json={"reason": "No payment with this UTR"},
        headers=headers,
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "REJECTED"


@approving
async def test_approved_user_reaches_tenant_routes(
    api: AsyncClient, session: AsyncSession, platform_admin: User
) -> None:
    payer = await pending_user(session)
    payer_headers = auth(await login(api, payer))
    order = await submitted_order(session, payer)
    assert (await api.get("/api/v1/patients", headers=payer_headers)).status_code == 403

    await api.post(f"{PAYMENTS}/{order.id}/approve", headers=auth(await login(api, platform_admin)))

    # The same session: roles and tenant are read fresh on every request.
    me = await api.get("/api/v1/auth/me", headers=payer_headers)
    assert "HOSPITAL_ADMIN" in me.json()["roles"]
    assert (await api.get("/api/v1/patients", headers=payer_headers)).status_code == 200
    current = await api.get("/api/v1/subscriptions/current", headers=payer_headers)
    assert current.json()["plan_code"] == "standard"

    # The payment screen keeps polling its order: it must see the approval.
    polled = await api.get(f"/api/v1/subscriptions/orders/{order.id}", headers=payer_headers)
    assert polled.status_code == 200
    assert polled.json()["status"] == "APPROVED"
    assert polled.json()["receipt_number"].startswith("NIR-")
    listed = await api.get("/api/v1/subscriptions/orders", headers=payer_headers)
    assert listed.json()["items"][0]["status"] == "APPROVED"

    receipt = await api.get(f"/api/v1/subscriptions/receipts/{order.id}/pdf", headers=payer_headers)
    assert receipt.status_code == 200
    assert receipt.headers["content-type"] == "application/pdf"
    assert receipt.content.startswith(b"%PDF-")


async def test_support_can_read_but_not_approve(
    api: AsyncClient, session: AsyncSession, platform_support: User
) -> None:
    order = await submitted_order(session, await pending_user(session))
    headers = auth(await login(api, platform_support))
    assert (await api.get(PAYMENTS, headers=headers)).status_code == 200
    assert (await api.get(f"{PAYMENTS}/{order.id}", headers=headers)).status_code == 200
    assert (await api.post(f"{PAYMENTS}/{order.id}/approve", headers=headers)).status_code == 403
    rejected = await api.post(
        f"{PAYMENTS}/{order.id}/reject", json={"reason": "Not found in bank"}, headers=headers
    )
    assert rejected.status_code == 403


async def test_hospital_admin_gets_403_on_platform_payments(
    api: AsyncClient, hospital_admin: User
) -> None:
    response = await api.get(PAYMENTS, headers=auth(await login(api, hospital_admin)))
    assert response.status_code == 403


async def test_proof_route_is_platform_only_and_nosniff(
    api: AsyncClient, session: AsyncSession, platform_admin: User
) -> None:
    payer = await pending_user(session)
    order = await orders.create_order(
        session, user=payer, plan_code=None, purpose=orders.PaymentOrderPurpose.NEW
    )
    await orders.attach_proof(session, user=payer, order_id=order.id, body=chunks(PNG))
    await session.commit()

    as_payer = await api.get(f"{PAYMENTS}/{order.id}/proof", headers=auth(await login(api, payer)))
    assert as_payer.status_code == 403

    response = await api.get(
        f"{PAYMENTS}/{order.id}/proof", headers=auth(await login(api, platform_admin))
    )
    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith("inline")


async def test_pdf_proof_is_served_as_an_attachment(
    api: AsyncClient, session: AsyncSession, platform_admin: User
) -> None:
    payer = await pending_user(session)
    order = await orders.create_order(
        session, user=payer, plan_code=None, purpose=orders.PaymentOrderPurpose.NEW
    )
    await orders.attach_proof(session, user=payer, order_id=order.id, body=chunks(PDF))
    await session.commit()
    response = await api.get(
        f"{PAYMENTS}/{order.id}/proof", headers=auth(await login(api, platform_admin))
    )
    assert response.headers["content-disposition"].startswith("attachment")


@approving
async def test_resend_receipt(
    api: AsyncClient, session: AsyncSession, platform_admin: User
) -> None:
    headers = auth(await login(api, platform_admin))
    order = await submitted_order(session, await pending_user(session))
    await api.post(f"{PAYMENTS}/{order.id}/approve", headers=headers)
    response = await api.post(f"{PAYMENTS}/{order.id}/resend-receipt", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "SENT"


async def test_overview_counts_payments_awaiting_review(
    api: AsyncClient, session: AsyncSession, platform_admin: User
) -> None:
    await submitted_order(session, await pending_user(session), utr="333333333333")
    await submitted_order(session, await pending_user(session), utr="444444444444")
    response = await api.get(
        "/api/v1/platform/overview", headers=auth(await login(api, platform_admin))
    )
    assert response.status_code == 200
    assert response.json()["payments_awaiting_review"] == 2


async def test_contact_messages_are_listed_for_the_platform(
    api: AsyncClient, platform_admin: User
) -> None:
    await api.post(
        "/api/v1/public/contact",
        json={
            "name": "Ravi Kumar",
            "email": "ravi@example.com",
            "message": "Please show us the system next week.",
        },
    )
    response = await api.get(
        "/api/v1/platform/contact-messages", headers=auth(await login(api, platform_admin))
    )
    assert response.status_code == 200
    assert response.json()["items"][0]["email"] == "ravi@example.com"


@approving
async def test_approval_ends_the_payers_sessions(
    api: AsyncClient, session: AsyncSession, platform_admin: User
) -> None:
    """The payer becomes HOSPITAL_ADMIN, a role that may require two-step
    sign-in. A session started before (password only, or the signup hand-off)
    must not carry the new role on: they sign in again, and enrol there."""
    from tests.conftest import TEST_PASSWORD

    payer = await pending_user(session)
    signed_in = await api.post(
        "/api/v1/auth/login", json={"email": payer.email, "password": TEST_PASSWORD}
    )
    refresh = signed_in.json()["refresh_token"]
    order = await submitted_order(session, payer)

    await api.post(f"{PAYMENTS}/{order.id}/approve", headers=auth(await login(api, platform_admin)))

    rotated = await api.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert rotated.status_code == 401
