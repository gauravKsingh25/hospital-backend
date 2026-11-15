"""A Standard hospital has OPD and nothing else; Professional has everything."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.models import utc_now
from app.modules.identity.models import User
from app.modules.subscriptions.features import GATED_PREFIXES, OPEN_PREFIXES, FeatureKey
from app.modules.tenancy.models import Hospital
from tests.conftest import auth, login
from tests.subscriptions_support import set_plan

pytestmark = pytest.mark.integration

V1 = "/api/v1"

LOCKED = [
    ("ipd", "/ipd/wards"),
    ("ipd", "/ipd/beds"),
    ("ipd", "/ipd/admissions"),
    ("ipd", "/ipd/admission-requests"),
    ("ipd", "/ipd/doses"),
    ("ipd", "/ipd/summaries"),
    ("medicines", "/ipd/medicines"),
    ("diagnostics", "/diagnostics/catalogue"),
    ("diagnostics", "/diagnostics/worklist"),
    ("diagnostics", "/diagnostics/reports"),
    ("diagnostics", "/diagnostics/specimens"),
    ("messaging", "/notifications"),
    ("messaging", "/notifications/templates"),
    ("messaging", "/notifications/suppressions"),
    ("accounts", "/billing/accounts/summary"),
    ("accounts", "/billing/accounts/bills"),
    ("accounts", "/billing/accounts/payments"),
    ("accounts", "/billing/accounts/outstanding"),
    ("accounts", "/billing/accounts/patients"),
    ("accounts", "/billing/claims"),
    ("reports_full", "/reports/occupancy"),
    ("reports_full", "/reports/inpatient"),
    ("reports_full", "/reports/follow-up"),
]

OPEN = [
    "/patients",
    "/appointments",
    "/queue",
    "/doctors",
    "/departments",
    "/encounters",
    "/orders",
    "/note-templates",
    "/users",
    "/billing/accounts",
    "/billing/invoices",
    "/billing/services",
    "/billing/charges",
    "/billing/payments",
    "/reports/dashboard",
    "/reports/queue",
]


async def _standard(session: AsyncSession, hospital: Hospital) -> None:
    await set_plan(session, hospital, "standard", period_end=utc_now() + timedelta(days=10))
    await session.commit()


@pytest.mark.parametrize(("feature", "path"), LOCKED)
async def test_standard_is_refused_locked_features(
    api: AsyncClient,
    session: AsyncSession,
    hospital: Hospital,
    hospital_admin: User,
    feature: str,
    path: str,
) -> None:
    await _standard(session, hospital)

    response = await api.get(V1 + path, headers=auth(await login(api, hospital_admin)))

    assert response.status_code == 403, response.text
    error = response.json()["error"]
    assert error["code"] == "feature_not_in_plan"
    assert error["details"] == {"feature": feature, "plan": "standard"}


@pytest.mark.parametrize(("feature", "path"), LOCKED)
async def test_professional_reaches_them(
    api: AsyncClient, hospital: Hospital, hospital_admin: User, feature: str, path: str
) -> None:
    response = await api.get(V1 + path, headers=auth(await login(api, hospital_admin)))

    assert response.status_code not in (402, 403), response.text


@pytest.mark.parametrize("path", OPEN)
async def test_standard_keeps_opd(
    api: AsyncClient, session: AsyncSession, hospital: Hospital, hospital_admin: User, path: str
) -> None:
    await _standard(session, hospital)

    response = await api.get(V1 + path, headers=auth(await login(api, hospital_admin)))

    assert response.status_code not in (402, 403), response.text
    assert response.status_code < 500, response.text


async def test_standard_reports_today_only(
    api: AsyncClient, session: AsyncSession, hospital: Hospital, hospital_admin: User
) -> None:
    await _standard(session, hospital)
    headers = auth(await login(api, hospital_admin))
    past = {"date_from": "2026-01-01", "date_to": "2026-01-31"}

    for report in ("footfall", "revenue"):
        today = await api.get(f"{V1}/reports/{report}", headers=headers)
        assert today.status_code == 200, today.text
        refused = await api.get(f"{V1}/reports/{report}", params=past, headers=headers)
        assert refused.status_code == 403, refused.text
        assert refused.json()["error"]["details"]["feature"] == "reports_full"


async def test_standard_dashboard_is_today_only(
    api: AsyncClient, session: AsyncSession, hospital: Hospital, hospital_admin: User
) -> None:
    await _standard(session, hospital)
    headers = auth(await login(api, hospital_admin))

    default = await api.get(f"{V1}/reports/dashboard", headers=headers)
    assert default.status_code == 200, default.text
    body = default.json()
    window = body["footfall"]["window"]
    assert window["date_from"] == window["date_to"] == body["generated_for"]
    today = await api.get(f"{V1}/reports/dashboard", params={"trailing_days": 1}, headers=headers)
    assert today.status_code == 200, today.text
    month = await api.get(f"{V1}/reports/dashboard", params={"trailing_days": 30}, headers=headers)
    assert month.status_code == 403, month.text
    assert month.json()["error"]["details"]["feature"] == "reports_full"


async def test_standard_dashboard_has_no_inpatient_sections(
    api: AsyncClient, session: AsyncSession, hospital: Hospital, hospital_admin: User
) -> None:
    """Bed occupancy and inpatient outcomes for a hospital that has no wards
    in its plan would be two panels of zeroes about a feature it cannot open."""
    await _standard(session, hospital)

    body = (
        await api.get(f"{V1}/reports/dashboard", headers=auth(await login(api, hospital_admin)))
    ).json()

    assert body["occupancy"] is None
    assert body["inpatient"] is None
    assert body["follow_up"] is None
    assert body["footfall"] is not None
    assert body["revenue"] is not None
    assert body["queue"] is not None


async def test_professional_dashboard_keeps_inpatient_sections(
    api: AsyncClient, hospital: Hospital, hospital_admin: User
) -> None:
    body = (
        await api.get(f"{V1}/reports/dashboard", headers=auth(await login(api, hospital_admin)))
    ).json()

    assert body["occupancy"] is not None
    assert body["inpatient"] is not None
    assert body["follow_up"] is not None


async def test_professional_dashboard_defaults_to_the_month(
    api: AsyncClient, hospital: Hospital, hospital_admin: User
) -> None:
    response = await api.get(
        f"{V1}/reports/dashboard", headers=auth(await login(api, hospital_admin))
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["footfall"]["window"]["date_from"] < body["generated_for"]


async def test_professional_reports_any_period(
    api: AsyncClient, hospital: Hospital, hospital_admin: User
) -> None:
    headers = auth(await login(api, hospital_admin))
    past = {"date_from": "2026-01-01", "date_to": "2026-01-31"}

    for report in ("footfall", "revenue"):
        response = await api.get(f"{V1}/reports/{report}", params=past, headers=headers)
        assert response.status_code == 200, response.text


async def test_platform_admin_is_not_feature_gated(api: AsyncClient, platform_admin: User) -> None:
    response = await api.get(f"{V1}/ipd/wards", headers=auth(await login(api, platform_admin)))

    assert response.status_code != 403 or (
        response.json()["error"]["code"] != "feature_not_in_plan"
    )


# ---------------------------------------------------------------------------
# No route ships unclassified
# ---------------------------------------------------------------------------
def _features_on(dependant: Any) -> set[FeatureKey]:
    found: set[FeatureKey] = set()
    feature = getattr(dependant.call, "feature", None)
    if feature is not None:
        found.add(feature)
    for child in dependant.dependencies:
        found |= _features_on(child)
    return found


def _longest(path: str, prefixes: Any) -> str | None:
    matches = [p for p in prefixes if path == p or path.startswith(p + "/")]
    return max(matches, key=len) if matches else None


def _api_routes() -> list[tuple[str, Any]]:
    """Every mounted API route as (path under the prefix, its dependant).

    Routers are included lazily, so the app's own route list holds wrappers;
    each wrapper lists the routes it contributes with the include-time
    dependencies already folded in.
    """
    from app.main import create_app

    found: list[tuple[str, Any]] = []
    for route in create_app().routes:
        contexts = (
            route.effective_route_contexts()
            if hasattr(route, "effective_route_contexts")
            else [route]
        )
        for context in contexts:
            full = getattr(context, "path", None) or ""
            if full.startswith(settings.API_V1_PREFIX + "/"):
                found.append((full[len(settings.API_V1_PREFIX) :], context.dependant))
    return found


def test_every_route_is_gated_or_declared_open() -> None:
    routes = _api_routes()
    # A walk that finds nothing would pass everything below.
    assert len(routes) > 150, len(routes)

    unclassified: list[str] = []
    wrong: list[str] = []
    for path, dependant in routes:
        gated = _longest(path, GATED_PREFIXES)
        opened = _longest(path, OPEN_PREFIXES)
        if gated is not None and (opened is None or len(gated) > len(opened)):
            if GATED_PREFIXES[gated] not in _features_on(dependant):
                wrong.append(f"{path} should require {GATED_PREFIXES[gated].value}")
        elif opened is None:
            unclassified.append(path)

    assert not unclassified, f"add to GATED_PREFIXES or OPEN_PREFIXES: {sorted(set(unclassified))}"
    assert not wrong, sorted(set(wrong))
