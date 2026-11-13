"""Invites over HTTP: the platform team's side and the invited person's side."""

from __future__ import annotations

import re

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.mail import MailMessage, MailResult
from app.modules.identity import invites
from app.modules.identity.models import User
from app.modules.identity.rbac import Roles
from app.modules.tenancy.models import Hospital
from tests.conftest import auth, login

pytestmark = pytest.mark.integration

PASSWORD = "a-perfectly-reasonable-passphrase"
PLATFORM = "/api/v1/platform"


class CapturingMailer:
    def __init__(self) -> None:
        self.sent: list[MailMessage] = []
        self.fail = False

    async def send(self, message: MailMessage) -> MailResult:
        self.sent.append(message)
        if self.fail:
            return MailResult(ok=False, error="SMTPAuthenticationError: (535, b'rejected')")
        return MailResult(ok=True, message_id=f"<m{len(self.sent)}@example>")

    def last_token(self) -> str:
        match = re.search(r"/invite/(\S+)", self.sent[-1].text)
        assert match is not None
        return match.group(1)


@pytest.fixture
def mailer(monkeypatch: pytest.MonkeyPatch) -> CapturingMailer:
    fake = CapturingMailer()
    monkeypatch.setattr(invites, "get_mailer", lambda: fake)
    return fake


async def _support(session: AsyncSession) -> User:
    from tests.conftest import _make_user

    return await _make_user(session, role_code=Roles.PLATFORM_SUPPORT, hospital_id=None)


async def _invite(
    api: AsyncClient, token: str, hospital: Hospital, email: str
) -> dict[str, object]:
    response = await api.post(
        f"{PLATFORM}/hospitals/{hospital.id}/invites",
        json={"full_name": "New Admin", "email": email},
        headers=auth(token),
    )
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


async def test_owner_invites_admin_then_admin_signs_in(
    api: AsyncClient, hospital: Hospital, platform_admin: User, mailer: CapturingMailer
) -> None:
    owner = await login(api, platform_admin)

    invite = await _invite(api, owner, hospital, "admin.one@example.com")
    assert invite["status"] == "PENDING"
    assert invite["last_email_status"] == "SENT"
    assert invite["hospital_name"] == hospital.name

    link_token = mailer.last_token()
    preview = await api.post("/api/v1/invites/preview", json={"token": link_token})
    assert preview.status_code == 200, preview.text
    assert preview.json()["hospital_name"] == hospital.name
    assert preview.json()["email"] == "admin.one@example.com"

    accepted = await api.post(
        "/api/v1/invites/accept", json={"token": link_token, "password": PASSWORD}
    )
    assert accepted.status_code == 200, accepted.text
    access = accepted.json()["access_token"]

    me = (await api.get("/api/v1/auth/me", headers=auth(access))).json()
    assert me["roles"] == [Roles.HOSPITAL_ADMIN]
    assert me["hospital_id"] == str(hospital.id)


async def test_used_link_is_refused_with_its_reason(
    api: AsyncClient, hospital: Hospital, platform_admin: User, mailer: CapturingMailer
) -> None:
    owner = await login(api, platform_admin)
    await _invite(api, owner, hospital, "admin.two@example.com")
    link_token = mailer.last_token()
    await api.post("/api/v1/invites/accept", json={"token": link_token, "password": PASSWORD})

    again = await api.post(
        "/api/v1/invites/accept", json={"token": link_token, "password": PASSWORD}
    )

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "invite_used"


async def test_preview_unknown_token_404(api: AsyncClient) -> None:
    response = await api.post("/api/v1/invites/preview", json={"token": "not-a-real-token"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "invite_invalid"


async def test_support_can_resend_but_not_invite_or_revoke(
    api: AsyncClient,
    session: AsyncSession,
    hospital: Hospital,
    platform_admin: User,
    mailer: CapturingMailer,
) -> None:
    owner = await login(api, platform_admin)
    invite = await _invite(api, owner, hospital, "admin.three@example.com")
    support = await login(api, await _support(session))

    refused = await api.post(
        f"{PLATFORM}/hospitals/{hospital.id}/invites",
        json={"full_name": "Other", "email": "other@example.com"},
        headers=auth(support),
    )
    assert refused.status_code == 403

    resent = await api.post(f"{PLATFORM}/invites/{invite['id']}/resend", headers=auth(support))
    assert resent.status_code == 200, resent.text
    assert resent.json()["id"] != invite["id"]
    assert resent.json()["send_count"] == 1

    revoked = await api.post(
        f"{PLATFORM}/invites/{resent.json()['id']}/revoke", headers=auth(support)
    )
    assert revoked.status_code == 403


async def test_owner_can_revoke(
    api: AsyncClient, hospital: Hospital, platform_admin: User, mailer: CapturingMailer
) -> None:
    owner = await login(api, platform_admin)
    invite = await _invite(api, owner, hospital, "admin.four@example.com")

    revoked = await api.post(f"{PLATFORM}/invites/{invite['id']}/revoke", headers=auth(owner))

    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "REVOKED"


async def test_hospital_admin_cannot_reach_platform_routes(
    api: AsyncClient, hospital_admin: User
) -> None:
    token = await login(api, hospital_admin)

    assert (await api.get(f"{PLATFORM}/invites", headers=auth(token))).status_code == 403
    assert (await api.get(f"{PLATFORM}/emails", headers=auth(token))).status_code == 403


async def test_emails_list_filters_failed(
    api: AsyncClient, hospital: Hospital, platform_admin: User, mailer: CapturingMailer
) -> None:
    owner = await login(api, platform_admin)
    mailer.fail = True
    invite = await _invite(api, owner, hospital, "admin.five@example.com")
    assert invite["last_email_status"] == "FAILED"
    assert "535" in str(invite["last_email_error"])

    response = await api.get(f"{PLATFORM}/emails", params={"status": "FAILED"}, headers=auth(owner))

    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    assert [row["to_email"] for row in rows] == ["admin.five@example.com"]
    assert rows[0]["error"].startswith("SMTPAuthenticationError")
    assert rows[0]["hospital_name"] == hospital.name
    assert "/invite/••••" in rows[0]["body_text"]


async def test_invites_list_by_status(
    api: AsyncClient, hospital: Hospital, platform_admin: User, mailer: CapturingMailer
) -> None:
    owner = await login(api, platform_admin)
    await _invite(api, owner, hospital, "admin.six@example.com")

    response = await api.get(
        f"{PLATFORM}/invites", params={"status": "PENDING"}, headers=auth(owner)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["invited_by_name"] == platform_admin.full_name


async def test_token_travels_in_the_body_not_the_path(
    api: AsyncClient, hospital: Hospital, platform_admin: User, mailer: CapturingMailer
) -> None:
    """Review I2: a token in a URL path lands in every access log."""
    owner = await login(api, platform_admin)
    await _invite(api, owner, hospital, "body.token@example.com")
    link_token = mailer.last_token()

    preview = await api.post("/api/v1/invites/preview", json={"token": link_token})
    assert preview.status_code == 200, preview.text
    assert preview.json()["email"] == "body.token@example.com"

    accepted = await api.post(
        "/api/v1/invites/accept", json={"token": link_token, "password": PASSWORD}
    )
    assert accepted.status_code == 200, accepted.text

    # The old path-token route is gone.
    old_route = await api.get(f"/api/v1/invites/{link_token}")
    assert old_route.status_code in (404, 405)
