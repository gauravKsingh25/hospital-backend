# Subscriptions Core and Feature Locking — Implementation Plan (slice 1 of 5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every hospital has a subscription with a plan; the API refuses hospitals whose subscription has lapsed (402) and features outside their plan (403); the app hides what a plan does not include. Existing hospitals keep everything.

**Architecture:** A new platform-level module `app/modules/subscriptions/` owns plans, plan features and subscriptions. The existing auth dependency resolves a hospital's entitlements once per request (Redis-cached) and hangs them on `AuthContext`; a `require_feature` dependency is attached to locked routers where they are mounted. Status is computed from the clock at read time, so correctness never depends on a background job. The frontend gets entitlements on `/auth/me` and filters navigation by them.

**Tech Stack:** FastAPI, SQLModel, Alembic, Postgres, Redis, pytest; Next.js 16, next-intl, Playwright. No new dependencies in this slice.

**Spec:** `Docs/superpowers/specs/2026-11-14-nirogya-signup-subscriptions-design.md` (§6, §7, §11, §12 slice 1). Background: `Docs/DISCOVERY.md`.

## Global Constraints

- Follow `CLAUDE.md`: thin routers, logic in `service.py`, typed domain exceptions, Pydantic v2 on all I/O, every schema change an Alembic migration with a working downgrade.
- Run backend commands from `backend/` with `.venv/Scripts/` tools; Postgres and Redis come from `docker compose up -d postgres redis`.
- Patch scripts that write files must pass `newline="\n"`.
- Plans: `standard` ₹3000, `professional` ₹8999, `interval_days` 30. Money is integer rupees.
- Grace period: `GRACE_PERIOD_DAYS=7`.
- Feature keys, exactly: `opd`, `ipd`, `diagnostics`, `medicines`, `accounts`, `messaging`, `reports_full`. Standard has `opd` only; Professional has all seven.
- Error codes, exactly: `subscription_inactive` (HTTP 402), `feature_not_in_plan` (HTTP 403).
- Platform users (`PLATFORM_ADMIN`, `PLATFORM_SUPPORT`) are never gated.
- Existing and console-created hospitals get a complimentary Professional subscription with `current_period_end = NULL` (never lapses).
- No data is deleted or hidden at the database level by a plan; gating is access only.
- All user-facing strings in `en.json` and `hi.json`.
- Gates before a task is done: `ruff check .`, `ruff format --check .`, `mypy app`, `pytest -q` all clean; frontend `npm run verify`.
- Commit steps run only if the owner has said to commit per task; otherwise leave the work staged and report.

## Review Focus

1. A hospital row with **no** subscription row (created by a script or a future code path that forgot) — expect 402, not silent full access. Test in Task 3.
2. Redis down or returning garbage for the entitlement cache — expect a fall back to Postgres and a correct answer, not a 500 and not a stale "active". Test in Task 2.
3. A subscription extended or changed while a user is signed in — expect the very next request to see it (cache invalidated on write), not 60 seconds later. Test in Task 2.
4. A Standard hospital reaching a locked feature **through an unlocked route** (ordering a lab test or requesting admission from the consultation screen) — expect `feature_not_in_plan`, not an orphan order nobody can fulfil. Tests in Task 5.
5. A user deep-linking to a locked or lapsed screen in the app — expect the upgrade panel or the lapsed screen, not a page of failed requests. Test in Task 7.

---

## File map

Backend, new:
- `app/modules/subscriptions/__init__.py`
- `app/modules/subscriptions/models.py` — `Plan`, `PlanFeature`, `Subscription`, `SubscriptionStatus`
- `app/modules/subscriptions/features.py` — `FeatureKey`, `PLAN_FEATURES`, `require_feature`
- `app/modules/subscriptions/entitlements.py` — `Entitlements`, `effective_status` (pure, no I/O)
- `app/modules/subscriptions/cache.py` — Redis cache of the subscription row
- `app/modules/subscriptions/service.py` — `get_entitlements`, `grant_complimentary`, `hospital_has_feature`, `ensure_feature`
- `app/modules/subscriptions/schemas.py`, `router.py`, `rbac.py`
- `alembic/versions/20261114_1000-<rev>_subscriptions_schema.py`
- `tests/test_subscriptions_entitlements.py`, `tests/test_subscriptions_service.py`, `tests/test_api_subscription_gate.py`, `tests/test_api_feature_gate.py`, `tests/subscriptions_support.py`

Backend, modified: `app/registry.py`, `app/main.py`, `app/core/config.py`, `app/core/deps.py`, `app/core/exceptions.py`, `app/modules/identity/service.py` (`AuthContext`), `app/modules/identity/schemas.py` + `router.py` (`/auth/me`), `app/modules/identity/platform_router.py` and `app/modules/tenancy/router.py` (grant on hospital creation), `app/modules/clinical/service.py`, `app/modules/ipd/service.py`, `app/modules/notifications/{models,service}.py`, `app/modules/reporting/router.py`, `app/modules/billing/router.py`, `scripts/seed_demo.py`, `tests/conftest.py`, `.env.example`.

Frontend, new: `src/lib/entitlements.ts`, `src/components/feature-locked.tsx`, `src/components/app-shell/subscription-banner.tsx`, `src/components/subscription-ended.tsx`, `e2e/plan-gating.spec.ts`. Modified: `src/components/app-shell/nav-items.ts`, `sidebar.tsx`, `mobile-nav.tsx`, `src/app/(app)/layout.tsx`, locked route pages, `src/i18n/messages/{en,hi}.json`, generated types.

---

### Task 0: Branch and a real green baseline

**Files:** `app/modules/identity/platform_router.py`, `app/modules/identity/invites.py`, `app/workers/tasks.py`, the two files `ruff format --check .` names.

- [ ] **Step 1:** `git switch -c feat/subscriptions` from `feat/platform-console`.
- [ ] **Step 2:** Start Docker Desktop, then `docker compose up -d postgres redis`. Run `pytest -q` in `backend/`. Expected: 0 failed, 0 skipped for "no local Postgres". Record the pass count in the task report. If anything fails, stop and report — do not build on a red suite.
- [ ] **Step 3:** Fix the 23 existing `mypy app` errors. 21 are `sqlalchemy.ext.asyncio.AsyncSession` passed where `sqlmodel.ext.asyncio.session.AsyncSession` is declared: widen the service signatures to `AsyncSession | SAAsyncSession` the way `tenancy/service.py` already does. 2 are `no-any-return` in `invites.py:113,125`: bind the result to a typed local. No behaviour change.
- [ ] **Step 4:** `ruff format .` (2 files).
- [ ] **Step 5:** `ruff check . && ruff format --check . && mypy app && pytest -q` → all clean.
- [ ] **Step 6:** Commit `chore: clean type and format baseline before subscriptions`.

### Task 1: Tables, plan seed and complimentary backfill

**Files:** Create `subscriptions/__init__.py`, `models.py`, `features.py`, `rbac.py`, the migration. Modify `app/registry.py`. Test: `tests/test_subscriptions_service.py`, existing `tests/test_rbac_seed.py`, `tests/test_models.py`.

**Interfaces — Produces:**
- `features.py`: `class FeatureKey(enum.StrEnum)` with members `OPD="opd"`, `IPD="ipd"`, `DIAGNOSTICS="diagnostics"`, `MEDICINES="medicines"`, `ACCOUNTS="accounts"`, `MESSAGING="messaging"`, `REPORTS_FULL="reports_full"`; `PLAN_FEATURES: dict[str, frozenset[FeatureKey]]` = `{"standard": {OPD}, "professional": all seven}`; `PLAN_STANDARD = "standard"`, `PLAN_PROFESSIONAL = "professional"`.
- `models.py`:
  - `Plan(table "plans")`: `code: str` PK (max 32), `name`, `price_inr: int`, `interval_days: int`, `rank: int`, `is_active: bool`, timestamps.
  - `PlanFeature(table "plan_features")`: composite PK (`plan_code` FK plans, `feature_key` max 32).
  - `SubscriptionStatus(StrEnum)`: `ACTIVE`, `GRACE`, `EXPIRED`, `CANCELLED`.
  - `Subscription(IdentifiedModel, table "subscriptions")`: `hospital_id` (FK hospitals, **unique**, indexed), `plan_code` (FK plans), `status: SubscriptionStatus` (stored hint; the truth is computed), `current_period_start: datetime`, `current_period_end: datetime | None`, `pending_plan_code: str | None`, `cancel_at_period_end: bool = False`, `complimentary: bool = False`.
  - These are platform tables: **no** RLS tenant policy (like `hospitals`, `invites`).
- `rbac.py`: `SubscriptionPermissions.SUBSCRIPTION_READ = "subscription:read"`, `SUBSCRIPTION_MANAGE = "subscription:manage"`, `PLATFORM_SUBSCRIPTION_MANAGE = "platform:subscription_manage"`; `PERMISSION_CATALOGUE`; `MODULE_ROLE_PERMISSIONS = {HOSPITAL_ADMIN: (read, manage), PLATFORM_ADMIN: (read, manage, platform manage), PLATFORM_SUPPORT: (read,)}`. Follow `billing/rbac.py` for shape.

- [ ] **Step 1: Failing tests** in `tests/test_subscriptions_service.py`:

```python
async def test_plans_are_seeded(session):
    rows = {p.code: p for p in (await session.execute(select(Plan))).scalars()}
    assert rows["standard"].price_inr == 3000
    assert rows["professional"].price_inr == 8999
    assert rows["standard"].interval_days == rows["professional"].interval_days == 30
    assert rows["standard"].rank < rows["professional"].rank

async def test_seeded_plan_features_match_the_registry(session):
    seeded: dict[str, set[str]] = {}
    for pf in (await session.execute(select(PlanFeature))).scalars():
        seeded.setdefault(pf.plan_code, set()).add(pf.feature_key)
    assert seeded == {code: {f.value for f in feats} for code, feats in PLAN_FEATURES.items()}
```

- [ ] **Step 2:** `pytest tests/test_subscriptions_service.py -v` → FAIL (import error).
- [ ] **Step 3:** Implement the models, `features.py` (constants only for now), `rbac.py`; import `subscriptions.models` in `app/registry.py`.
- [ ] **Step 4:** Write the migration (`down_revision = "b9d4e1f3a6c8"`): create the three tables with indexes on every FK; insert the two plans and their feature rows; insert the three permissions and role grants (copy the `INSERT … ON CONFLICT DO NOTHING` pattern from `20261101_1200-b9d4e1f3a6c8_billing_read_scope.py`); grant the app DB role the same table privileges other platform tables get (see how `…a7c3e9f1b2d4_platform_invites_and_emails.py` does it); then backfill:

```sql
INSERT INTO subscriptions (id, hospital_id, plan_code, status, current_period_start,
                           current_period_end, complimentary, cancel_at_period_end,
                           created_at, updated_at)
SELECT gen_random_uuid(), h.id, 'professional', 'ACTIVE', now(), NULL, true, false, now(), now()
FROM hospitals h
WHERE NOT EXISTS (SELECT 1 FROM subscriptions s WHERE s.hospital_id = h.id);
```

Downgrade removes role grants, permissions and the three tables.
- [ ] **Step 5:** `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` on the local DB → no errors. `pytest tests/test_subscriptions_service.py tests/test_rbac_seed.py tests/test_models.py -v` → PASS.
- [ ] **Step 6:** `python scripts/export_rbac.py`. Commit `feat(subscriptions): plans, plan features and subscription tables`.

### Task 2: Entitlements — pure status, cached lookup, complimentary grant

**Files:** Create `subscriptions/entitlements.py`, `cache.py`, `service.py`. Modify `app/core/config.py`, `.env.example`. Test: `tests/test_subscriptions_entitlements.py`, `tests/test_subscriptions_service.py`, `tests/subscriptions_support.py`.

**Interfaces — Produces:**
- `config.py`: `GRACE_PERIOD_DAYS: int = 7`, `SUBSCRIPTIONS_ENFORCED: bool = True`.
- `entitlements.py`:

```python
def effective_status(
    *, period_end: datetime | None, cancel_at_period_end: bool,
    now: datetime, grace_days: int,
) -> SubscriptionStatus: ...

@dataclass(frozen=True, slots=True)
class Entitlements:
    plan_code: str
    status: SubscriptionStatus
    period_end: datetime | None
    complimentary: bool
    features: frozenset[str]
    @property
    def usable(self) -> bool: ...          # ACTIVE or GRACE
    def has(self, feature: str) -> bool: ...  # usable and feature in features
    def days_left(self, now: datetime) -> int | None: ...  # None when period_end is None; never negative
```

  Rules for `effective_status`: `period_end is None` → `ACTIVE`; `now <= period_end` → `ACTIVE`; past the end with `cancel_at_period_end` → `CANCELLED`; `now <= period_end + grace_days` → `GRACE`; otherwise `EXPIRED`.
- `service.py`:
  - `async def get_entitlements(session, hospital_id: uuid.UUID, *, now: datetime | None = None) -> Entitlements | None` — `None` when the hospital has no subscription row. Reads cache first; on miss reads Postgres under `system_context` and fills the cache. Features come from `PLAN_FEATURES[plan_code]` (the code registry is the authority at runtime; the table is for listing and is checked against it by Task 1's test).
  - `async def grant_complimentary(session, hospital_id: uuid.UUID, *, plan_code: str = PLAN_PROFESSIONAL) -> Subscription` — idempotent: returns the existing row if one exists. Invalidates the cache.
  - `async def hospital_has_feature(session, hospital_id, feature: FeatureKey) -> bool`
  - `async def ensure_feature(session, hospital_id, feature: FeatureKey) -> None` — raises `FeatureNotInPlanError` (defined in Task 3; until then a local placeholder import is not acceptable — implement Task 3's two exception classes here first if working in order, they live in `core/exceptions.py`).
  - `async def write_subscription(session, sub: Subscription) -> Subscription` — the single place a subscription row is saved; flushes and invalidates the cache. Later slices use it.
- `cache.py`: `get(hospital_id) -> dict | None`, `put(hospital_id, row: dict) -> None`, `invalidate(hospital_id) -> None`; key `sub:row:{hospital_id}`, TTL 60 s, stores `plan_code`, `period_end` (ISO or null), `cancel_at_period_end`, `complimentary`. Same failure contract as `identity/cache.py`: Redis errors and undecodable payloads are a miss, never an exception.
- `tests/subscriptions_support.py`: `async def set_plan(session, hospital, plan_code, *, period_end=None, cancel=False) -> Subscription` (upsert via `write_subscription`).

- [ ] **Step 1: Failing tests** in `tests/test_subscriptions_entitlements.py` (no database):

```python
NOW = datetime(2026, 11, 14, 12, tzinfo=UTC)
D = timedelta(days=1)

@pytest.mark.parametrize("end,cancel,expected", [
    (None, False, "ACTIVE"),
    (NOW + D, False, "ACTIVE"),
    (NOW, False, "ACTIVE"),
    (NOW - D, False, "GRACE"),
    (NOW - 7 * D, False, "GRACE"),
    (NOW - 7 * D - timedelta(seconds=1), False, "EXPIRED"),
    (NOW + D, True, "ACTIVE"),
    (NOW - timedelta(seconds=1), True, "CANCELLED"),
])
def test_effective_status(end, cancel, expected):
    assert effective_status(period_end=end, cancel_at_period_end=cancel, now=NOW, grace_days=7) == expected

def test_expired_entitlements_have_no_features():
    e = Entitlements("professional", SubscriptionStatus.EXPIRED, NOW - 30 * D, False, frozenset({"ipd"}))
    assert not e.usable and not e.has("ipd")

def test_days_left():
    e = Entitlements("standard", SubscriptionStatus.ACTIVE, NOW + timedelta(days=2, hours=1), False, frozenset({"opd"}))
    assert e.days_left(NOW) == 2
    assert Entitlements("standard", SubscriptionStatus.EXPIRED, NOW - 9 * D, False, frozenset()).days_left(NOW) == 0
    assert Entitlements("professional", SubscriptionStatus.ACTIVE, None, True, frozenset()).days_left(NOW) is None
```

  and in `tests/test_subscriptions_service.py`:

```python
async def test_grant_complimentary_is_idempotent(session, hospital):
    a = await service.grant_complimentary(session, hospital.id)
    b = await service.grant_complimentary(session, hospital.id)
    assert a.id == b.id and a.complimentary and a.current_period_end is None
    e = await service.get_entitlements(session, hospital.id)
    assert e.plan_code == "professional" and e.has("ipd")

async def test_standard_has_only_opd(session, hospital):
    await set_plan(session, hospital, "standard", period_end=utc_now() + timedelta(days=10))
    e = await service.get_entitlements(session, hospital.id)
    assert e.features == frozenset({"opd"})

async def test_a_plan_change_is_seen_on_the_next_read(session, hospital):
    await set_plan(session, hospital, "standard", period_end=utc_now() + timedelta(days=10))
    assert not await service.hospital_has_feature(session, hospital.id, FeatureKey.IPD)
    await set_plan(session, hospital, "professional", period_end=utc_now() + timedelta(days=10))
    assert await service.hospital_has_feature(session, hospital.id, FeatureKey.IPD)

async def test_cache_garbage_and_redis_errors_fall_back_to_the_database(session, hospital, monkeypatch):
    await service.grant_complimentary(session, hospital.id)
    await get_redis().set(f"sub:row:{hospital.id}", "not json")
    assert (await service.get_entitlements(session, hospital.id)).usable
    monkeypatch.setattr(cache, "get_redis", lambda: (_ for _ in ()).throw(OSError("down")))
    assert (await service.get_entitlements(session, hospital.id)).usable
```

  Note: the `hospital` fixture gains a complimentary subscription in Task 3; until then the first assertion of each test that needs one calls `grant_complimentary`/`set_plan` itself, as written.
- [ ] **Step 2:** run both files → FAIL.
- [ ] **Step 3:** Implement. Add the two settings and document them in `.env.example` under a new "Subscriptions" heading.
- [ ] **Step 4:** run both files → PASS; `mypy app` clean.
- [ ] **Step 5:** Commit `feat(subscriptions): entitlements with cached lookup and complimentary grants`.

### Task 3: The subscription gate on every request, and `/auth/me`

**Files:** Modify `app/core/exceptions.py`, `app/core/deps.py`, `app/modules/identity/service.py`, `identity/schemas.py`, `identity/router.py`, `tests/conftest.py`. Create `subscriptions/schemas.py`, `subscriptions/router.py`; mount in `app/main.py`. Test: `tests/test_api_subscription_gate.py`.

**Interfaces — Consumes:** `get_entitlements`, `Entitlements` (Task 2).
**Produces:**
- `exceptions.py`: `SubscriptionInactiveError(DomainError)` — status 402, code `subscription_inactive`, default message "This hospital's subscription has ended."; `FeatureNotInPlanError(DomainError)` — status 403, code `feature_not_in_plan`, default message "This feature is not included in your plan."
- `AuthContext` gains `entitlements: Entitlements | None = None`. `build_auth_context` fills it for users with a `hospital_id`; platform users and hospital-less users get `None`.
- `deps.get_auth_context`: after building the context, for a non-platform user **with** a hospital, when `settings.SUBSCRIPTIONS_ENFORCED` and (`entitlements is None` or not `entitlements.usable`), raise `SubscriptionInactiveError(details={"status": <status or "NONE">})` unless `request.url.path` starts with `f"{API_V1_PREFIX}/auth/"` or `f"{API_V1_PREFIX}/subscriptions"`.
- `schemas.py`: `EntitlementsRead` — `plan_code: str`, `plan_name: str`, `status: SubscriptionStatus`, `period_end: datetime | None`, `days_left: int | None`, `complimentary: bool`, `features: list[str]` (sorted).
- `CurrentUserRead` gains `entitlements: EntitlementsRead | None = None`.
- `GET /api/v1/subscriptions/current` → `EntitlementsRead`; requires `subscription:read`; 404 `subscription_not_found` if none.
- `conftest.py`: a session-scoped autouse fixture registers a SQLAlchemy `after_insert` listener on `Hospital` that inserts a complimentary Professional subscription row on the same connection, so the ~17 places tests construct `Hospital(...)` directly keep working. A marker `@pytest.mark.no_auto_subscription` disables it for one test.

- [ ] **Step 1: Failing tests** in `tests/test_api_subscription_gate.py`:

```python
PATIENTS = "/api/v1/patients"
ME = "/api/v1/auth/me"
CURRENT = "/api/v1/subscriptions/current"

async def test_active_hospital_is_let_through_and_me_reports_the_plan(api, session, hospital, hospital_admin):
    token = await login(api, hospital_admin)
    assert (await api.get(PATIENTS, headers=auth(token))).status_code == 200
    me = (await api.get(ME, headers=auth(token))).json()
    assert me["entitlements"]["plan_code"] == "professional"
    assert me["entitlements"]["status"] == "ACTIVE"
    assert me["entitlements"]["days_left"] is None

async def test_grace_is_let_through(api, session, hospital, hospital_admin):
    await set_plan(session, hospital, "standard", period_end=utc_now() - timedelta(days=3))
    await session.commit()
    token = await login(api, hospital_admin)
    assert (await api.get(PATIENTS, headers=auth(token))).status_code == 200
    assert (await api.get(ME, headers=auth(token))).json()["entitlements"]["status"] == "GRACE"

@pytest.mark.parametrize("days_past,cancel,status", [(8, False, "EXPIRED"), (1, True, "CANCELLED")])
async def test_lapsed_hospital_gets_402_but_can_still_see_itself(api, session, hospital, hospital_admin, receptionist, days_past, cancel, status):
    await set_plan(session, hospital, "standard", period_end=utc_now() - timedelta(days=days_past), cancel=cancel)
    await session.commit()
    for user in (hospital_admin, receptionist):
        token = await login(api, user)
        r = await api.get(PATIENTS, headers=auth(token))
        assert r.status_code == 402
        assert r.json()["error"]["code"] == "subscription_inactive"
        assert r.json()["error"]["details"]["status"] == status
        assert (await api.get(ME, headers=auth(token))).status_code == 200
    assert (await api.get(CURRENT, headers=auth(await login(api, hospital_admin)))).json()["status"] == status

@pytest.mark.no_auto_subscription
async def test_hospital_without_any_subscription_row_gets_402(api, session, hospital, hospital_admin):
    r = await api.get(PATIENTS, headers=auth(await login(api, hospital_admin)))
    assert r.status_code == 402 and r.json()["error"]["details"]["status"] == "NONE"

async def test_platform_admin_is_never_gated(api, platform_admin):
    token = await login(api, platform_admin)
    assert (await api.get("/api/v1/platform/overview", headers=auth(token))).status_code == 200
    assert (await api.get(ME, headers=auth(token))).json()["entitlements"] is None

async def test_gate_can_be_switched_off(api, session, hospital, hospital_admin, monkeypatch):
    await set_plan(session, hospital, "standard", period_end=utc_now() - timedelta(days=30))
    await session.commit()
    monkeypatch.setattr(settings, "SUBSCRIPTIONS_ENFORCED", False)
    assert (await api.get(PATIENTS, headers=auth(await login(api, hospital_admin)))).status_code == 200
```

- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** Implement. Login itself must keep working for a lapsed hospital (it is under `/auth/`); confirm `identity.service.login` does not go through the gate.
- [ ] **Step 4:** `pytest tests/test_api_subscription_gate.py -v` → PASS; then the **whole** suite `pytest -q` → PASS with the same count as Task 0 plus the new tests. This is the "existing hospitals unaffected" gate.
- [ ] **Step 5:** Commit `feat(subscriptions): refuse lapsed hospitals and report entitlements on /auth/me`.

### Task 4: Lock routers by feature

**Files:** Modify `subscriptions/features.py`, `app/main.py`, `app/modules/billing/router.py`, `app/modules/reporting/router.py`. Test: `tests/test_api_feature_gate.py`.

**Interfaces — Produces:** `features.require_feature(feature: FeatureKey) -> Callable[[AuthContext], Awaitable[AuthContext]]` — a dependency on `CurrentUser`; platform users pass; otherwise raises `FeatureNotInPlanError(details={"feature": feature.value, "plan": entitlements.plan_code})` unless `entitlements.has(feature)`.

Mounting (in `_register_routers`, `dependencies=[Depends(require_feature(...))]`):

| Feature | Routers |
|---|---|
| `ipd` | `ipd_router.wards_router`, `beds_router`, `admissions_router`, `chart_router`, `summaries_router`, `admission_requests_router` |
| `medicines` | `ipd_router.medicines_router` |
| `diagnostics` | `diagnostics_router.catalogue_router`, `specimens_router`, `reports_router`, `worklist_router` |
| `messaging` | `notifications_router.templates_router`, `suppressions_router`, `notifications_router` |
| `accounts` | `billing_router.accounts_router`, `claims_router`; plus, route by route in `billing/router.py`, every route whose permission dependency is `IPD_BILL_EDIT`, `IPD_BILL_FINALISE` or `ACCOUNTS_READ` |
| `reports_full` | in `reporting/router.py`: `/reports/occupancy`, `/reports/inpatient`, `/reports/follow-up` always; `/reports/footfall` and `/reports/revenue` when the requested range is anything other than the hospital's today alone (check in the route after `local_today` is known, via `subscriptions.service.ensure_feature`). `/reports/dashboard` and `/reports/queue` stay open |

- [ ] **Step 1: Failing tests** in `tests/test_api_feature_gate.py`:

```python
LOCKED = [
    ("ipd", "GET", "/api/v1/ipd/wards"),
    ("ipd", "GET", "/api/v1/ipd/admissions"),
    ("ipd", "GET", "/api/v1/ipd/admission-requests"),
    ("medicines", "GET", "/api/v1/ipd/medicines"),
    ("diagnostics", "GET", "/api/v1/diagnostics/catalogue"),
    ("diagnostics", "GET", "/api/v1/diagnostics/worklist"),
    ("messaging", "GET", "/api/v1/notifications"),
    ("messaging", "GET", "/api/v1/notifications/templates"),
    ("accounts", "GET", "/api/v1/billing/accounts/summary"),   # use the first real GET under /billing/accounts
    ("accounts", "GET", "/api/v1/billing/claims"),
    ("reports_full", "GET", "/api/v1/reports/occupancy"),
    ("reports_full", "GET", "/api/v1/reports/inpatient"),
    ("reports_full", "GET", "/api/v1/reports/follow-up"),
]
OPEN = ["/api/v1/patients", "/api/v1/appointments", "/api/v1/queue", "/api/v1/doctors",
        "/api/v1/billing/invoices", "/api/v1/billing/services", "/api/v1/departments",
        "/api/v1/reports/dashboard", "/api/v1/reports/queue", "/api/v1/users"]

@pytest.mark.parametrize("feature,method,path", LOCKED)
async def test_standard_is_refused_locked_features(api, session, hospital, hospital_admin, feature, method, path):
    await set_plan(session, hospital, "standard", period_end=utc_now() + timedelta(days=10)); await session.commit()
    r = await api.request(method, path, headers=auth(await login(api, hospital_admin)))
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "feature_not_in_plan"
    assert r.json()["error"]["details"] == {"feature": feature, "plan": "standard"}

@pytest.mark.parametrize("feature,method,path", LOCKED)
async def test_professional_reaches_them(api, hospital_admin, feature, method, path):
    r = await api.request(method, path, headers=auth(await login(api, hospital_admin)))
    assert r.status_code not in (402, 403), r.text

@pytest.mark.parametrize("path", OPEN)
async def test_standard_keeps_opd(api, session, hospital, hospital_admin, path):
    await set_plan(session, hospital, "standard", period_end=utc_now() + timedelta(days=10)); await session.commit()
    r = await api.get(path, headers=auth(await login(api, hospital_admin)))
    assert r.status_code == 200, r.text

async def test_standard_reports_today_only(api, session, hospital, hospital_admin):
    await set_plan(session, hospital, "standard", period_end=utc_now() + timedelta(days=10)); await session.commit()
    h = auth(await login(api, hospital_admin))
    assert (await api.get("/api/v1/reports/footfall", headers=h)).status_code == 200
    past = {"date_from": "2026-01-01", "date_to": "2026-01-31"}
    assert (await api.get("/api/v1/reports/footfall", params=past, headers=h)).status_code == 403
    assert (await api.get("/api/v1/reports/revenue", params=past, headers=h)).status_code == 403

def test_every_router_is_either_gated_or_declared_open():
    """A new router must be classified. Walks app.routes; each /api/v1 path prefix
    must appear in features.GATED_PREFIXES or features.OPEN_PREFIXES."""
```

  Adjust a `LOCKED`/`OPEN` path only if it does not exist as written (check `frontend/openapi.json`); keep at least one path per router in the table above. Add `GATED_PREFIXES` / `OPEN_PREFIXES` to `features.py` for the last test.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `pytest tests/test_api_feature_gate.py -v` → PASS; `pytest -q` → PASS.
- [ ] **Step 5:** Commit `feat(subscriptions): lock IPD, lab, medicines, accounts, messaging and full reports by plan`.

### Task 5: Close the side doors

A Standard hospital must not create work for a locked module through an open route.

**Files:** Modify `app/modules/clinical/service.py`, `app/modules/ipd/service.py`, `app/modules/notifications/models.py`, `notifications/service.py`, the notifications migration enum if `SuppressionReason` is a Postgres enum (new Alembic revision `…_not_in_plan_suppression.py`). Test: additions to `tests/test_api_feature_gate.py`, `tests/test_notifications_suppression.py`.

**Interfaces — Consumes:** `subscriptions.service.ensure_feature`, `hospital_has_feature`.
**Produces:** `SuppressionReason.NOT_IN_PLAN = "NOT_IN_PLAN"`.

Behaviour:
- `clinical.service` order placement: `OrderType` values fulfilled by diagnostics (lab, radiology) call `ensure_feature(…, DIAGNOSTICS)`; values fulfilled by the inpatient pharmacy/medicines flow call `ensure_feature(…, MEDICINES)`. Prescriptions written on an OPD consultation stay allowed.
- Admission requests and the encounter transition to `ADMITTED`: `ensure_feature(…, IPD)` at the service entry point the consultation screen calls.
- `notifications.service.enqueue`: when the hospital lacks `messaging`, record the notification as `SUPPRESSED` with reason `NOT_IN_PLAN` (same shape as the existing `DISABLED` path) and send nothing. Re-check in the dispatcher at send time, as the deceased rule does. The deceased rule keeps precedence.

- [ ] **Step 1: Failing tests** (names and assertions):
  - `test_standard_cannot_order_a_lab_test` — POST the existing create-order route with a lab `order_type` → 403 `feature_not_in_plan`, `details.feature == "diagnostics"`, and no `Order` row exists.
  - `test_standard_can_still_prescribe_on_an_opd_visit` — existing prescription path → 2xx.
  - `test_standard_cannot_request_admission` — the route the consultation screen uses → 403, `details.feature == "ipd"`; encounter status unchanged.
  - `test_standard_messages_are_recorded_as_not_in_plan` — trigger an event that enqueues a message; the `Notification` row is `SUPPRESSED` / `NOT_IN_PLAN`; the fake gateway saw nothing.
  - `test_message_queued_on_professional_is_not_sent_after_downgrade` — enqueue on Professional, `set_plan(standard)`, run the dispatcher → suppressed, not sent.
  - `test_deceased_still_wins_over_not_in_plan` — reason stays `DECEASED`.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** run those → PASS; `pytest -q` → PASS (the deceased-suppression and state-machine suites are the ones to watch).
- [ ] **Step 5:** Commit `feat(subscriptions): refuse locked work started from open screens`.

### Task 6: New hospitals get a subscription

**Files:** Modify `app/modules/identity/platform_router.py` (create hospital + invite), `app/modules/tenancy/router.py` (`POST /hospitals`), `scripts/seed_demo.py`, `scripts/reset_demo_data.py` if it creates hospitals. Test: `tests/test_api_platform.py`, `tests/test_seed_guard.py`.

- [ ] **Step 1: Failing tests:**
  - `test_console_created_hospital_has_a_complimentary_professional_subscription` — `POST /platform/hospitals`, then the row exists with `complimentary is True`, `plan_code == "professional"`, `current_period_end is None`. Mark `no_auto_subscription` so the test listener does not mask a missing grant.
  - Same for `POST /hospitals`.
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** Call `subscriptions.service.grant_complimentary` in the same transaction as `create_hospital`, from the two routers (routers compose two services; `tenancy` does not import `subscriptions`). In `seed_demo.py`: grant the main demo hospital Professional, and add a second demo hospital `Demo OPD Clinic` (code `DOPD`) on Standard with `period_end = now + 20 days` and one admin `opd-admin@demo.hospital` and one receptionist `opd-reception@demo.hospital`, both with `DEMO_PASSWORD`.
- [ ] **Step 4:** run → PASS; `python scripts/seed_demo.py` against the local DB runs clean twice in a row.
- [ ] **Step 5:** Commit `feat(subscriptions): grant a subscription when a hospital is created`.

### Task 7: The app shows what the plan includes

**Files:** Create `frontend/src/lib/entitlements.ts`, `src/components/feature-locked.tsx`, `src/components/subscription-ended.tsx`, `src/components/app-shell/subscription-banner.tsx`, `e2e/plan-gating.spec.ts`. Modify `nav-items.ts`, `sidebar.tsx`, `mobile-nav.tsx`, `src/app/(app)/layout.tsx`, the page files of locked routes, `en.json`, `hi.json`, `e2e/support.ts`; regenerate types.

**Interfaces — Produces:**
- `entitlements.ts`: `type Feature = "opd" | "ipd" | "diagnostics" | "medicines" | "accounts" | "messaging" | "reports_full"`; `hasFeature(user: CurrentUser | null, feature: Feature): boolean` (true when `user.entitlements` is null — platform users); `subscriptionState(user): "ok" | "grace" | "ended"`.
- `NavItem` gains `feature?: Feature`; `visibleNavItems` drops items whose feature the user lacks.
- `<FeatureLocked feature>`: server component, one heading ("Available on Professional"), one sentence naming the feature, and for holders of `subscription:manage` a button to `/admin/subscription` (that page arrives in slice 3; until then the button is omitted and the text says to contact Nirogya at the support contact).
- `<SubscriptionEnded>`: full-page state rendered by `(app)/layout.tsx` instead of children when `subscriptionState(user) === "ended"`; admins and other staff get different sentences.
- `<SubscriptionBanner>`: shown in the shell in grace: "Your subscription ended on {date}. Access stops in {n} days."

Nav mapping: Admissions, Wards, Rounds, admin Wards → `ipd`; Lab, admin Catalogue → `diagnostics`; Medicines → `medicines`; Messages → `messaging`; Reports stays visible (Today view) and its period controls are hidden without `reports_full`; the accounts dashboard and IPD bill editor entry points → `accounts`.

- [ ] **Step 1:** With the backend running: `npm run codegen`; `npm run codegen:check` → clean. `CurrentUser` now carries `entitlements`.
- [ ] **Step 2: Failing e2e** `e2e/plan-gating.spec.ts` (sign-in helpers in `e2e/support.ts`; users from Task 6's seed):
  - `standard admin sees only OPD navigation` — as `opd-admin@demo.hospital`: sidebar has Reception, Patients, Queue, Billing, Admin; has no Admissions, Wards, Lab, Medicines, Messages.
  - `deep link to a locked screen shows the upgrade panel` — goto `/wards` → heading "Available on Professional"; no failed network request with status 403 for `/bff/` during the visit.
  - `professional admin is unchanged` — as `admin@demo.hospital`: Admissions, Wards, Lab, Medicines, Messages all present.
  - `reports on standard offers today only` — `/reports` renders today's tiles; no period picker.
- [ ] **Step 3:** run → FAIL.
- [ ] **Step 4:** Implement. Each locked `page.tsx` checks `hasFeature` on the server and returns `<FeatureLocked>` before any data fetch. Add en + hi strings under a `subscription` namespace. Hide the "order lab test" and "request admission" controls on the consultation and reception screens without the feature.
- [ ] **Step 5:** `npm run verify` → PASS; `npx playwright test e2e/plan-gating.spec.ts` → PASS; then the full Playwright suite → PASS. Check at 390 px and 1440 px, light and dark, that the locked panel, banner and ended screen read well (screenshots into `test-results/plan-gating/`, opened and reviewed).
- [ ] **Step 6:** Commit `feat(web): navigation and screens follow the hospital's plan`.

### Task 8: Docs

**Files:** `CLAUDE.md` (§2 module table: add `subscriptions` — "Plans, hospital subscriptions, entitlements; platform-level, may cross tenants"; §3: add it to the modules allowed to operate across tenants), `README.md`, `Docs/runbook.md` (how to put a hospital on a plan by hand until the console screen lands in slice 3; the `SUBSCRIPTIONS_ENFORCED` escape hatch), `.env.example` (already done in Task 2 — verify), create `Docs/DECISIONS.md` with the answered questions and the defaults taken (copy from the spec's §13 and the owner's answers).

- [ ] **Step 1:** Write them.
- [ ] **Step 2:** Final gates, output pasted into the report: `ruff check . && ruff format --check . && mypy app && pytest -q` in `backend/`; `npm run verify && npx playwright test` in `frontend/`.
- [ ] **Step 3:** Commit `docs: subscriptions module, plan gating and decisions log`.

---

## Self-review notes

- Spec §6 tables for orders, proofs, receipts, reminders, OTPs, signup intents and contact messages belong to slices 2–3 and are deliberately absent here.
- Spec §7 names `GET /me/entitlements`; this plan puts entitlements on the existing `/auth/me` (the app already loads it once per request) and adds `GET /subscriptions/current`. One fewer round trip; same data.
- The 402 allowlist has no data-export route yet: that route does not exist and is built in slice 3.
- "Reports: Today view" is pinned as: dashboard, queue, and footfall/revenue for today only. That reading is mine; the owner approved "Today view on reports" without more detail.
