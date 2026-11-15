# Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove that no hospital can reach another's data through any route, find and fix security holes in the signup / payments / subscription work, and leave the owner the documents to run it: UPI verification guide, runbook, decisions, plus the website's sitemap and robots.

**Architecture:** One parametrised integration test walks every OpenAPI route that takes a path parameter, as a user of hospital A holding every hospital role, against hospital B's real ids harvested from the database. A security review of slices 1–4 by a fresh reviewer feeds one TDD fix pass. The rest is documents and two Next.js metadata routes.

**Tech Stack:** pytest + httpx `AsyncClient` against the app; Postgres RLS; Next.js 16 `app/sitemap.ts` / `app/robots.ts` under `output: "export"`.

**Spec:** `Docs/superpowers/specs/2026-11-14-nirogya-signup-subscriptions-design.md` (§2 anti-goals, §10 website bar, §11 isolation test, §12.5)

## Global Constraints

- Spec §11: "Isolation: one parametrised test walking every tenant route in the OpenAPI schema as hospital A against hospital B's ids."
- Spec §11: "≥ 85 % coverage on new modules; mypy strict clean." (`mypy app` is clean today: 137 files.)
- Spec §2 anti-goals are the security review's checklist: no access on a UTR alone; one hospital / subscription / receipt per approved order; no price, plan or amount from the browser; payment screenshots platform-only; no data deleted on downgrade or expiry; UPI ID, SMTP password, OTP secret never in code, git or logs.
- Spec §10: website ships "meta + OpenGraph + sitemap + robots".
- Never run two pytest processes against `hospital_test` at once. Never git commit (owner's rule).
- Every security fix is RED → GREEN with a test that failed first; the full backend suite is green at the end.

## Review Focus

1. A route whose handler loads by id **before** checking the tenant, then fails late (after a side effect such as an audit row or an email). Covered: Task 1 asserts no 2xx and, for B's patient, that no audit row or notification was written for B by A's requests.
2. A list or search route without a path parameter that takes an id in the **query string** (`?patient_id=`, `?encounter_id=`). Covered: Task 1 also walks GET routes whose query parameters end in `_id`, with B's ids.
3. Platform routes (`/platform/*`) reached by a hospital user. Covered: Task 1 includes them; A's user must get 403.
4. A secret printed in a log line (OTP code, UPI VPA, SMTP password, signup / handoff token) during a normal signup and payment. Covered: Task 2 adds a caplog test over the full signup → approval flow.
5. A route the walk cannot exercise with a real id (no B data of that kind): it still runs with a random UUID, and the test prints the list, so a gap is visible rather than silent. Covered: Task 1's coverage floor.

---

### Task 1: The cross-tenant walk

**Files:**
- Create: `backend/tests/test_isolation_walk.py`, `backend/tests/isolation_support.py`

**Interfaces:**
- Consumes: `tests.conftest._make_user`, `login`, `auth`; `tests.accounts_support.price_list`, `register`, `open_visit`, `admit`, `admitted_stay`; `app.main.app.openapi()`.
- Produces: `async def build_hospital_b(session, api) -> dict[str, list[str]]` (param name → ids of B's rows); `ROUTES: list[tuple[str, str]]` (method, path) collected from `app.openapi()` at import time.

- [ ] **Step 1: `isolation_support.py`**
  - `all_roles_user(session, hospital_id) -> User`: one user holding every non-platform seeded role (multiple `UserRole` rows), so a 403 from RBAC never hides a missing tenant check.
  - `build_hospital_b`: give B a price list, a patient with an open OPD visit, a lab order with a result entered, an admission (with ward and bed), an invoice with a charge and a payment, a template, a notification row — by calling the existing support helpers and B's own API as B's all-roles user. Then harvest ids: for each param name in `PARAM_TABLES` (`patient_id→patients`, `encounter_id→encounters`, `appointment_id→appointments`, `admission_id→admissions`, `bed_id→beds`, `ward_id→wards`, `invoice_id→invoices`, `charge_id→charges`, `payment_id→payments`, `order_id→clinical orders`, `report_id`, `specimen_id`, `department_id`, `doctor_id`, `user_id→users`, `template_id`, `item_id`, `note_id`, `notification_id`, … — the executor fills the table names from the models), select ids `WHERE hospital_id = B` under `system_context`. `uhid` maps to B's patient UHID.
- [ ] **Step 2: Write `test_isolation_walk.py`**
  - `ROUTES` = every (method, path) in `app.openapi()["paths"]` whose path has a `{param}`, plus every GET whose query parameters include a name ending in `_id`. Exclude only `/public/*`, `/auth/*` and `/health*` (no tenant data), listed by name in the file.
  - `test_every_route_refuses_another_hospitals_ids` parametrised over `ROUTES` (id = `"METHOD path"`): as A's all-roles user, fill each path param with B's id (random UUID if B has none), body `{}` for writes; assert `response.status_code not in range(200, 300)`; and for a 2xx-free GET, assert B's id does not appear in `response.text`.
  - `test_the_walk_uses_real_ids_for_the_core_kinds`: the param names `patient_id, encounter_id, appointment_id, admission_id, invoice_id, user_id, ward_id, bed_id, order_id` all resolve to B data (guards against a harvest that silently returns nothing).
  - `test_a_hospital_user_cannot_reach_the_platform_console`: every `/platform/*` route → 403 for A's all-roles user.
  - `test_the_walk_left_no_trace_in_hospital_b`: after the walk, B's audit log has no row whose actor is A's user, and no notification for B's patient was queued by it.
- [ ] **Step 3: Run**
  Run: `cd backend && python -m pytest tests/test_isolation_walk.py -q -p no:randomly`
  Expected: all pass. **Any failure is a finding**: fix it under TDD (the failing parametrised case is the RED), ledger it as `Task 1: Ruling/fix`, rerun.
- [ ] **Step 4: Ruling to ledger:** writes are sent with `{}`, so a 422 from body validation counts as refused without proving the tenant check behind it; RLS is the backstop for those. Cost if wrong: a write route that validates a full body and then mis-scopes would pass the walk.

### Task 2: Security review of slices 1–4, one fix pass

**Files:**
- Test: `backend/tests/test_secrets_not_logged.py`, plus one test file per fix (or a test added to the owning module's file)
- Modify: whatever the findings name

- [ ] **Step 1: `test_secrets_not_logged.py`** — `test_signup_and_approval_log_no_secret`: with `caplog` at DEBUG over `signup start → verify → complete → handoff → order → submit → approve`, assert none of: the OTP code, the signup token, the handoff code, the access/refresh tokens, `settings.UPI_VPA`, `settings.SMTP_PASSWORD` (if set), `settings.OTP_HMAC_SECRET` appears in any record's formatted message. Run; a failure is a finding fixed under TDD.
- [ ] **Step 2: Dispatch one security reviewer** (fresh context, most capable model) over the working-tree changes of slices 1–4 (backend `subscriptions/`, `identity/{signup,otp,public_router,public_schemas}.py`, `core/{rate_limit,security,config,mail,deps}.py`, `features.py`, frontend `auth/handoff`, `(app)/layout.tsx`, `features/subscriptions/*`, `features/platform/payment*`, website `src/lib/signup.ts`, `SignupFlow.tsx`), with the §2 anti-goals as its checklist plus: authz on every new route, IDOR on order/proof/receipt ids, enumeration, rate-limit bypass via forwarded headers, upload handling (type sniffing, size, content-type served back), CSRF on the BFF, open redirects in handoff/login `next`, token lifetimes and revocation. Ask for Critical / High / Medium / Low with a concrete exploit each.
- [ ] **Step 3: Fix pass** — Critical and High (and any Medium a real attacker reaches without an account): each gets a failing test first, then the fix, then the module's tests. Lows go to the ledger as deferred.
- [ ] **Step 4: Run** `cd backend && python -m pytest -q -p no:randomly -x` (alone) and `python -m mypy app`
  Expected: suite green, mypy clean.

### Task 3: Coverage gate

- [ ] **Step 1: Run** `cd backend && python -m pytest -q -p no:randomly --cov=app/modules/subscriptions --cov=app/modules/identity/signup --cov=app/modules/identity/otp --cov=app/core/rate_limit --cov-report=term-missing` (alone)
  Expected: each ≥ 85 %. Below that: add tests for the uncovered branches that a person can reach (not for defensive `assert`s), rerun.

### Task 4: Website sitemap and robots

**Files:**
- Create: `website/src/app/sitemap.ts`, `website/src/app/robots.ts`

- [ ] **Step 1:** Read `website/node_modules/next/dist/docs/` for `sitemap` / `robots` metadata files under static export. `sitemap.ts` lists `/`, `/signup/`, `/privacy/`, `/terms/`, `/refunds/` as absolute URLs on `SITE.siteUrl` with trailing slashes; `robots.ts` allows all and points at `${SITE.siteUrl}/sitemap.xml`. Both `export const dynamic = "force-static"` if the docs require it for export.
- [ ] **Step 2: Verify** `cd website && npm run build && test -f out/sitemap.xml && test -f out/robots.txt && grep -c "<loc>" out/sitemap.xml`
  Expected: build clean, 5 locations.

### Task 5: Owner documents

**Files:**
- Modify: `Docs/runbook.md` (new "Checking a UPI payment" guide, replacing or extending "Approving a signup payment"), `Docs/DECISIONS.md` (sections for slices 2, 3, 4, 5), `Docs/HANDOFF.md`, `README.md` (pointer to the guide)

- [ ] **Step 1: UPI verification guide** in the runbook, written for the owner, not an engineer: where the queue is; in the bank app, find the credit by **amount and time**, then match the **UTR** (12 digits) and, when the payer typed it, the **reference** (`NIR…`) in the remark; the screenshot is a hint, never proof; what to do when: amount differs, UTR not found yet (wait, banks lag up to a few hours), same UTR used twice (the system refuses a duplicate UTR), payer paid the wrong VPA, payment from a business account with a different name; how to reject with a reason the payer can act on; the 12-hour promise; extending by hand when a bank delay is to blame.
- [ ] **Step 2: DECISIONS.md** — one table per slice (2–5) of the calls made, copied from each ledger's `Ruling:` lines, each with "cost if wrong"; plus a short "Open for the owner" list (data export for expired hospitals; separate `/pricing` and `/contact` pages; website `/pay` explainer).
- [ ] **Step 3: HANDOFF.md** — all five slices done, verification state, what remains (deferred minors by ledger, open questions).
- [ ] **Step 4: Verify** links: every path the docs name exists (`grep -o` the backticked paths and `test -e` each).

### Task 6: Final verification

- [ ] Run alone, in order: `cd backend && python -m pytest -q -p no:randomly`, `python -m mypy app`, `cd ../frontend && npm run verify`, `npx playwright test e2e/signup-payment.spec.ts e2e/subscription-lifecycle.spec.ts e2e/website-signup.spec.ts` (clear the `rl:signup-*` Redis keys first), `cd ../website && npm test && npm run build`.
  Expected: all green.
