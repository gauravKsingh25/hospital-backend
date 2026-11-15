"""The payment screens' API, for someone who has signed up and not yet paid."""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.models import User
from tests.conftest import auth, login
from tests.payments_support import PNG, pending_user

ORDERS = "/api/v1/subscriptions/orders"


async def _token(api: AsyncClient, session: AsyncSession, **kwargs: str) -> str:
    return await login(api, await pending_user(session, **kwargs))


async def test_pending_user_creates_and_reads_an_order(
    api: AsyncClient, session: AsyncSession
) -> None:
    headers = auth(await _token(api, session))
    created = await api.post(ORDERS, json={"purpose": "NEW"}, headers=headers)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["plan_code"] == "standard"
    assert body["plan_name"] == "Standard"
    assert body["amount_inr"] == 3000
    assert body["status"] == "AWAITING_PAYMENT"
    assert body["reference_code"].startswith("NIR")

    detail = await api.get(f"{ORDERS}/{body['id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["qr_svg"].startswith("<svg")
    assert detail.json()["upi_uri"].startswith("upi://pay?")
    assert detail.json()["review_hours"] == 12
    assert detail.json()["has_proof"] is False

    listed = await api.get(ORDERS, headers=headers)
    assert listed.json()["total"] == 1


async def test_amount_in_the_request_is_ignored(api: AsyncClient, session: AsyncSession) -> None:
    headers = auth(await _token(api, session))
    created = await api.post(
        ORDERS,
        json={"purpose": "NEW", "plan_code": "professional", "amount_inr": 1},
        headers=headers,
    )
    assert created.json()["amount_inr"] == 8999


async def test_other_users_order_is_404(api: AsyncClient, session: AsyncSession) -> None:
    mine = await api.post(ORDERS, json={"purpose": "NEW"}, headers=auth(await _token(api, session)))
    theirs = auth(await _token(api, session))
    response = await api.get(f"{ORDERS}/{mine.json()['id']}", headers=theirs)
    assert response.status_code == 404


async def test_hospital_staff_cannot_create_new_orders(
    api: AsyncClient, hospital_admin: User
) -> None:
    response = await api.post(
        ORDERS, json={"purpose": "NEW"}, headers=auth(await login(api, hospital_admin))
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "already_has_hospital"


async def test_platform_admin_cannot_create_orders(api: AsyncClient, platform_admin: User) -> None:
    response = await api.post(
        ORDERS, json={"purpose": "NEW"}, headers=auth(await login(api, platform_admin))
    )
    assert response.status_code == 403


async def test_upload_then_submit_moves_to_submitted(
    api: AsyncClient, session: AsyncSession
) -> None:
    headers = auth(await _token(api, session))
    order = (await api.post(ORDERS, json={"purpose": "NEW"}, headers=headers)).json()

    uploaded = await api.put(
        f"{ORDERS}/{order['id']}/proof",
        content=PNG,
        headers={**headers, "Content-Type": "application/octet-stream"},
    )
    assert uploaded.status_code == 204, uploaded.text

    submitted = await api.post(
        f"{ORDERS}/{order['id']}/submit",
        json={"utr": "123456789012", "payer_upi_id": "asha@okhdfc"},
        headers=headers,
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "SUBMITTED"
    assert submitted.json()["utr"] == "123456789012"

    detail = await api.get(f"{ORDERS}/{order['id']}", headers=headers)
    assert detail.json()["has_proof"] is True


async def test_utr_must_be_12_digits(api: AsyncClient, session: AsyncSession) -> None:
    headers = auth(await _token(api, session))
    order = (await api.post(ORDERS, json={"purpose": "NEW"}, headers=headers)).json()
    for utr in ("12345", "12345678901a", "1234567890123"):
        response = await api.post(
            f"{ORDERS}/{order['id']}/submit", json={"utr": utr}, headers=headers
        )
        assert response.status_code == 422, utr


async def test_a_fake_image_upload_is_422(api: AsyncClient, session: AsyncSession) -> None:
    headers = auth(await _token(api, session))
    order = (await api.post(ORDERS, json={"purpose": "NEW"}, headers=headers)).json()
    response = await api.put(
        f"{ORDERS}/{order['id']}/proof",
        content=b"<html>not a picture</html>",
        headers={**headers, "Content-Type": "image/png"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "proof_type_invalid"


async def test_subscriptions_current_still_needs_a_hospital(
    api: AsyncClient, session: AsyncSession
) -> None:
    response = await api.get(
        "/api/v1/subscriptions/current", headers=auth(await _token(api, session))
    )
    assert response.status_code == 403


async def test_submitting_emails_the_payer_and_the_owner(
    api: AsyncClient, session: AsyncSession
) -> None:
    import sqlalchemy as sa

    from app.core.config import settings
    from app.modules.identity.models import PlatformEmail, PlatformEmailKind

    headers = auth(await _token(api, session))
    order = (await api.post(ORDERS, json={"purpose": "NEW"}, headers=headers)).json()
    await api.post(f"{ORDERS}/{order['id']}/submit", json={"utr": "123456789012"}, headers=headers)

    sent = {
        row.kind: row for row in (await session.execute(sa.select(PlatformEmail))).scalars().all()
    }
    assert set(sent) == {PlatformEmailKind.PAYMENT_SUBMITTED, PlatformEmailKind.PAYMENT_ALERT}
    assert sent[PlatformEmailKind.PAYMENT_ALERT].to_email == settings.contact_inbox
    assert order["reference_code"] in sent[PlatformEmailKind.PAYMENT_ALERT].body_text
    assert "₹3,000" in sent[PlatformEmailKind.PAYMENT_SUBMITTED].body_text
