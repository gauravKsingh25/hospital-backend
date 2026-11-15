"""No hospital reaches another's data, through any route (spec §11).

The walk: every route in the OpenAPI schema that takes an id — in its path, or
as a `…_id` query parameter on a GET — is called by a user of hospital A who
holds every hospital role, with the ids of hospital B's real records. No write
may succeed, no read may return any of B's records (an empty 200 for a
filtered list is the right answer), nothing may crash, and no answer may carry
B's patients' names.

New routes join the walk on their own, because the list is read from the
schema each run. A route whose kind of record B does not have is still called,
with a random id; `test_the_walk_uses_real_ids_for_the_core_kinds` keeps the
harvest honest for the kinds that matter most.

Writes are sent with an empty body, so a 422 from body validation counts as
refused without proving the tenant check behind it; row-level security is the
backstop for those (ledgered ruling).
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database as db
from app.main import create_app
from tests.conftest import auth, login
from tests.isolation_support import (
    TABLES,
    all_roles_user,
    fill_hospital,
    harvest,
    make_hospital,
    table_for,
)

pytestmark = pytest.mark.integration

PREFIX = "/api/v1"
# No tenant data behind these: anyone may call them, or they act on the
# caller's own session only.
EXCLUDED = (f"{PREFIX}/public/", f"{PREFIX}/auth/", "/health")
B_NAMES = ("Queue Patient", "Ward Patient")
CORE_KINDS = (
    "patients",
    "encounters",
    "appointments",
    "admissions",
    "invoices",
    "users",
    "wards",
    "beds",
    "queue_entries",
    "charges",
    "departments",
    "doctors",
    "note_templates",
    "clinical_notes",
    "notifications",
    "payment_orders",
)
# Kinds hospital B is not given (building them needs the lab catalogue, a
# dispensing flow or a claim): their routes are walked with random ids, which
# proves only that they do not crash or succeed on an unknown id. Listed so the
# gap is visible and a new kind cannot slip in unnoticed.
KNOWN_RANDOM = {
    "orders",
    "diagnostic_reports",
    "specimens",
    "test_analytes",
    "test_catalogue_items",
    "medication_orders",
    "medication_administrations",
    "medicine_items",
    "medicine_issues",
    "insurance_claims",
    "discharge_summaries",
    "notification_suppressions",
    "notification_templates",
    "invites",
    "payments",
    "rate_cards",
    "service_items",
    "admission_requests",
}


def _routes() -> list[tuple[str, str, list[str]]]:
    """(method, path, query id params) for every route the walk covers."""
    schema: dict[str, Any] = create_app().openapi()
    routes: list[tuple[str, str, list[str]]] = []
    for path, operations in schema["paths"].items():
        if path.startswith(EXCLUDED):
            continue
        for method, operation in operations.items():
            query_ids = [
                p["name"]
                for p in operation.get("parameters", [])
                if p.get("in") == "query" and p["name"].endswith("_id")
            ]
            if "{" in path or (method == "get" and query_ids):
                routes.append((method.upper(), path, query_ids if method == "get" else []))
    return routes


ROUTES = _routes()


def _id_for(param: str, path: str, ids: dict[str, str], hospital_b: str) -> str:
    if param == "hospital_id":
        return hospital_b
    if param == "uhid":
        return ids.get("uhid", "NOPE-0000")
    table = table_for(param, path)
    return ids.get(table or "", str(uuid.uuid4()))


async def _count(session: AsyncSession, table: str, hospital_id: uuid.UUID) -> int:
    async with db.system_context(session):
        total = (
            await session.execute(
                sa.text(f"SELECT count(*) FROM {table} WHERE hospital_id = :h"),  # noqa: S608
                {"h": hospital_id},
            )
        ).scalar_one()
    await session.commit()
    return int(total)


@pytest.fixture
async def walk(session: AsyncSession, api: AsyncClient) -> dict[str, Any]:
    hospital_a = await make_hospital(session, "WALKA")
    hospital_b = await make_hospital(session, "WALKB")
    a_user = await all_roles_user(session, hospital_a.id)
    b_user = await all_roles_user(session, hospital_b.id)
    await fill_hospital(session, hospital_b, b_user)
    ids = await harvest(session, hospital_b.id)
    token = await login(api, a_user)
    return {
        "a_user": a_user.id,
        "b": hospital_b.id,
        "ids": ids,
        "headers": auth(token),
    }


async def test_every_route_refuses_another_hospitals_ids(
    walk: dict[str, Any], api: AsyncClient, session: AsyncSession
) -> None:
    ids: dict[str, str] = walk["ids"]
    hospital_b = str(walk["b"])
    findings: list[str] = []
    statuses: list[int] = []
    real_ids = set(ids.values())
    # Positive control: the walker can read its own hospital. Without this, a
    # broken login would make every route answer 401 and the walk pass.
    own = await api.get(f"{PREFIX}/patients", headers=walk["headers"])
    assert own.status_code == 200, own.text
    b_notifications = await _count(session, "notifications", walk["b"])
    for method, path, query_ids in ROUTES:
        url = re.sub(r"\{(\w+)\}", lambda m, p=path: _id_for(m.group(1), p, ids, hospital_b), path)
        params = {name: _id_for(name, path, ids, hospital_b) for name in query_ids}
        kwargs: dict[str, Any] = {"headers": walk["headers"], "params": params}
        if method in {"POST", "PUT", "PATCH"}:
            kwargs["json"] = {}
        response = await api.request(method, url, **kwargs)
        statuses.append(response.status_code)
        label = f"{method} {path}"
        # A read may answer 200 with nothing in it (a list filtered by an id
        # it cannot see is empty); it may not answer with B's data. A write
        # that succeeds is always a finding.
        sent = set(re.findall(r"[0-9a-f-]{36}", url)) | set(params.values())
        b_data = [v for v in ids.values() if v in response.text and v not in sent]
        if hospital_b in response.text:
            b_data.append(hospital_b)
        # A detail read of B's real record answering with that record.
        path_ids = set(re.findall(r"[0-9a-f-]{36}", url)) & real_ids
        if method == "GET" and path_ids and 200 <= response.status_code < 300:
            body = response.json()
            if isinstance(body, dict) and str(body.get("id")) in path_ids:
                b_data.append(str(body["id"]))
        if 200 <= response.status_code < 300 and (method != "GET" or b_data):
            findings.append(f"{label}: {response.status_code} {response.text[:200]}")
        elif response.status_code >= 500:
            findings.append(f"{label}: crashed {response.status_code}")
        elif any(name in response.text for name in B_NAMES):
            findings.append(f"{label}: {response.status_code} carries B's patient name")

    # The walk left no trace in B: nothing B can see was done in A's name.
    async with db.system_context(session):
        traces = (
            await session.execute(
                sa.text(
                    "SELECT action FROM audit_logs WHERE hospital_id = :b AND actor_user_id = :a"
                ),
                {"b": walk["b"], "a": walk["a_user"]},
            )
        ).all()
    await session.commit()
    findings.extend(f"audit row in B by A: {row[0]}" for row in traces)
    if await _count(session, "notifications", walk["b"]) != b_notifications:
        findings.append("a message was queued in B during the walk")

    # The walk reached handlers, not just the door: most answers are
    # "not found", not "not signed in" / "payment required".
    assert statuses.count(404) > 50, sorted(set(statuses))
    assert not {401, 402} & set(statuses), sorted(set(statuses))
    assert len(ROUTES) > 150, "the schema walk found too few routes"
    assert findings == [], "\n".join(findings)


async def test_the_walk_uses_real_ids_for_the_core_kinds(walk: dict[str, Any]) -> None:
    missing = [kind for kind in CORE_KINDS if kind not in walk["ids"]]
    assert missing == [], f"hospital B has no {missing}: the walk would aim at random ids"
    assert "uhid" in walk["ids"]
    random_only = {t for t in TABLES if t not in walk["ids"]}
    assert random_only <= KNOWN_RANDOM, f"walked with random ids only: {random_only - KNOWN_RANDOM}"


async def test_a_hospital_user_cannot_reach_the_platform_console(
    walk: dict[str, Any], api: AsyncClient
) -> None:
    wrong: list[str] = []
    schema: dict[str, Any] = create_app().openapi()
    for path, operations in schema["paths"].items():
        if not path.startswith(f"{PREFIX}/platform"):
            continue
        for method in operations:
            url = re.sub(r"\{\w+\}", str(uuid.uuid4()), path)
            response = await api.request(
                method.upper(),
                url,
                headers=walk["headers"],
                json={} if method in {"post", "put", "patch"} else None,
            )
            if response.status_code != 403:
                wrong.append(f"{method.upper()} {path}: {response.status_code}")
    assert wrong == [], "\n".join(wrong)
