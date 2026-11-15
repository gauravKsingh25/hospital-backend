"""The website's contact form posts here (spec §8, `POST /public/contact`)."""

from __future__ import annotations

import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.identity.models import ContactMessage, PlatformEmail, PlatformEmailKind

URL = "/api/v1/public/contact"


def _message(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "Ravi Kumar",
        "email": "Ravi@Example.com",
        "phone": "9812345678",
        "hospital_name": "Sunrise Hospital",
        "message": "We run a 40-bed hospital and would like a demo, please.",
    }
    body.update(overrides)
    return body


async def test_contact_is_stored_and_emailed(api: AsyncClient, session: AsyncSession) -> None:
    response = await api.post(URL, json=_message())
    assert response.status_code == 202
    stored = (await session.execute(sa.select(ContactMessage))).scalars().one()
    assert stored.email == "ravi@example.com"
    assert stored.hospital_name == "Sunrise Hospital"
    email = (await session.execute(sa.select(PlatformEmail))).scalars().one()
    assert email.kind == PlatformEmailKind.CONTACT_ALERT
    assert email.to_email == settings.contact_inbox
    assert "40-bed hospital" in email.body_text


async def test_honeypot_is_accepted_and_dropped(api: AsyncClient, session: AsyncSession) -> None:
    response = await api.post(URL, json=_message(website="http://spam.example"))
    assert response.status_code == 202
    count = (
        await session.execute(sa.select(sa.func.count()).select_from(ContactMessage))
    ).scalar_one()
    assert count == 0


async def test_a_too_short_message_is_422(api: AsyncClient) -> None:
    assert (await api.post(URL, json=_message(message="hi"))).status_code == 422


async def test_contact_is_rate_limited(api: AsyncClient) -> None:
    statuses = [(await api.post(URL, json=_message())).status_code for _ in range(6)]
    assert statuses == [202] * 5 + [429]


async def test_cors_allows_the_website_origin(api: AsyncClient) -> None:
    origin = "http://localhost:3100"
    assert origin in settings.CORS_ORIGINS
    response = await api.options(
        URL,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
