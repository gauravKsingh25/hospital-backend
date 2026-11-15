# Signup and UPI Payments — Implementation Plan (slice 2 of 5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A stranger can sign up with an email OTP and a password, pay for a plan by UPI QR, submit the UTR, and become the admin of a new hospital once a platform admin approves the payment in the console. A receipt is issued and emailed. Forgot-password works by OTP.

**Architecture:** `identity` gains OTP, signup, password reset and a contact inbox behind unauthenticated `/public/*` routes protected by a Redis rate limiter that fails open. `subscriptions` gains payment orders, proofs and receipts, a signed-in order API for pending users, and a platform payment queue. Approval runs in one transaction with the order row locked; emails go out after commit and are logged in `platform_emails`. The app gets `/get-started` for signed-in users with no hospital, `/forgot-password`, a `/auth/handoff` route that turns a one-time signup code into a session, and `/platform/payments` in the console.

**Tech Stack:** FastAPI, SQLModel, Alembic, Postgres, Redis, pytest; Next.js 16, next-intl, TanStack Query, Playwright. New Python dependencies: `segno` (QR as SVG) and `reportlab` (receipt PDF). No new npm dependencies.

**Spec:** `Docs/superpowers/specs/2026-11-14-nirogya-signup-subscriptions-design.md` (§5 flow, §6 data model, §8 backend, §9 app, §11 tests, §12 slice 2). Slice 1's plan, `2026-11-14-subscriptions-core.md`, shows the conventions.

## Global Constraints

- Follow `CLAUDE.md`: thin routers, logic in services, typed domain exceptions, Pydantic v2 on all I/O, paginated lists, audit on sensitive actions, every schema change an Alembic migration with a working downgrade.
- Run backend commands from `backend/` with `../.venv/Scripts/` tools (`PYTHONUTF8=1`). Postgres and Redis come from `docker compose up -d postgres redis`.
- Patch scripts that write files pass `newline="\n"`.
- **The hospital is created at approval, not at signup** (spec §4 A, §5, §8 "Approval"). A signed-up user has `hospital_id = NULL`, no roles, `must_change_password = False`.
- Plans and prices come only from the `plans` table: `standard` ₹3000, `professional` ₹8999. Money is integer rupees. Nothing priced from the request.
- Slice 2 creates `NEW` orders only. `RENEWAL` and `UPGRADE` exist in the enum and are refused by the service with `ValidationError(code="order_purpose_unavailable")` until slice 3.
- OTP: 6 digits from `secrets`, HMAC-SHA256 with `OTP_HMAC_SECRET`, `OTP_TTL_MINUTES=10`, 5 attempts, single use, 60 s resend cooldown, 5 per email per hour, `hmac.compare_digest`, one generic error `otp_invalid` for wrong/expired/used/too-many.
- Reference code: `NIR` + 6 characters from `ABCDEFGHJKMNPQRSTUVWXYZ23456789`.
- UTR: exactly 12 digits. A UTR may be on only one order (partial unique index where not null).
- Proof screenshot: PNG/JPEG/PDF by magic bytes only, ≤ 5 MB enforced while reading.
- Unpaid orders (`AWAITING_PAYMENT`, no proof) expire after `PAYMENT_ORDER_TTL_HOURS=24`.
- Receipt numbers `NIR-<year>-<6-digit sequence>` from a Postgres sequence. Receipts say "Amount includes GST" and carry no GST breakdown.
- New error codes, exactly: `otp_invalid` (400), `rate_limited` (429), `signup_token_invalid` (400), `handoff_invalid` (400), `order_under_review` (409), `order_not_payable` (409), `utr_taken` (409), `proof_type_invalid` (422), `proof_too_large` (413), `order_purpose_unavailable` (422), `already_has_hospital` (403).
- Rate limiter fails open with a logged warning if Redis is unavailable; the OTP limits live in Postgres and still hold.
- Settings added (all in `.env.example` with comments): `OTP_HMAC_SECRET` (SecretStr), `OTP_TTL_MINUTES=10`, `UPI_VPA`, `UPI_PAYEE_NAME="Nirogya"`, `PAYMENT_ORDER_TTL_HOURS=24`, `PAYMENT_REVIEW_HOURS=12`, `CONTACT_INBOX` (default `PLATFORM_SUPPORT_CONTACT`), `WEBSITE_URL="http://localhost:3100"`. `CORS_ORIGINS` gains the website origin in `.env.example`. Production refuses to start without `UPI_VPA` and `OTP_HMAC_SECRET`.
- Never log or store in plain text the OTP code, the signup token or the handoff code; the stored OTP email body has the code replaced by `••••••`.
- All user-facing strings in `en.json` and `hi.json`; existing components and tokens only.
- Gates before a task is done: `ruff check .`, `ruff format --check .`, `mypy app`, `pytest -q`; frontend `npm run verify`.
- **Do not commit.** The owner has not said to; leave the work in the tree and report.

## Review Focus

1. Two platform admins approve the same order at the same moment — expect one hospital, one subscription, one receipt; the second call returns the first result. Test in Task 7 (`test_concurrent_approve_creates_one_hospital`).
2. A pending user calls a tenant route (`/patients`, `/subscriptions/current`) or tries to create an order after already being approved — expect 403, never data and never a second hospital. Tests in Task 4 and Task 6.
3. A payer submits the UTR of someone else's approved order, or resubmits after rejection with the same UTR — expect `utr_taken` for the first and acceptance for the second (the rejected order's own UTR is freed on rejection). Test in Task 6.
4. A file named `.png` that is really HTML, or an upload that streams past 5 MB with a lying `Content-Length` — expect `proof_type_invalid` / `proof_too_large` and nothing stored. Test in Task 6.
5. The approval email fails to send (SMTP down) — expect the approval to stand, the email logged `FAILED`, and a working "resend receipt" action. Test in Task 7.

---

## File map

Backend, new:
- `app/core/rate_limit.py` — Redis fixed-window limiter
- `app/modules/identity/otp.py` — issue/verify OTPs
- `app/modules/identity/signup.py` — signup intents, handoff, password reset, contact messages
- `app/modules/identity/public_router.py` — `/public/*` routes
- `app/modules/identity/public_schemas.py`
- `app/modules/subscriptions/orders.py` — reference codes, order lifecycle, proof intake
- `app/modules/subscriptions/upi.py` — UPI URI + SVG QR
- `app/modules/subscriptions/approval.py` — approve/reject, hospital creation
- `app/modules/subscriptions/receipts.py` — receipt PDF
- `app/modules/subscriptions/emails.py` — every subscription email + logging
- `app/modules/subscriptions/platform_router.py` — `/platform/payments`, `/platform/contact-messages`
- `alembic/versions/20261114_1200-<rev>_signup_and_payments.py`
- Tests: `tests/test_rate_limit.py`, `tests/test_otp.py`, `tests/test_api_signup.py`, `tests/test_api_password_reset.py`, `tests/test_api_contact.py`, `tests/test_upi.py`, `tests/test_orders_service.py`, `tests/test_api_orders.py`, `tests/test_approval.py`, `tests/test_api_platform_payments.py`, `tests/test_signup_full_flow.py`, `tests/payments_support.py`

Backend, modified: `pyproject.toml`, `app/core/config.py`, `app/core/exceptions.py`, `app/core/security.py` (token types), `app/modules/identity/models.py` (+3 tables, email kinds), `app/modules/identity/rbac.py` or `subscriptions/rbac.py` (platform payment permissions), `app/modules/identity/platform.py` (overview count), `app/modules/subscriptions/models.py`, `schemas.py`, `router.py`, `app/workers/main.py` + `tasks.py`, `app/main.py`, `app/registry.py`, `.env.example`, `README.md`, `Docs/runbook.md`.

Frontend, new: `src/app/get-started/page.tsx`, `src/features/subscriptions/{get-started-screen,payment-panel,proof-form,order-status}.tsx`, `src/features/subscriptions/use-orders.ts`, `src/app/auth/handoff/route.ts`, `src/app/forgot-password/page.tsx`, `src/features/auth/forgot-password-form.tsx`, `src/app/(app)/platform/payments/page.tsx`, `src/features/platform/{payments-screen,payment-review-dialog}.tsx`, `e2e/signup-payment.spec.ts`. Modified: `src/proxy.ts`, `src/app/(app)/layout.tsx`, `src/lib/permissions.ts`, `src/features/auth/actions.ts`, `src/app/login/page.tsx`, platform nav, `src/features/platform/overview-screen.tsx`, `src/features/platform/emails-screen.tsx` (contact messages tab), `src/i18n/messages/{en,hi}.json`, `openapi.json` + generated types, `e2e/support.ts`. Website: `website/src/components/sections/ContactSection.tsx` (`data-endpoint`).

---

### Task 0: Green e2e baseline

Slice 1's Playwright run never finished. Before any slice 2 code:

- [ ] **Step 1:** With Postgres, Redis, API and web up, run `cd frontend && npx playwright test --reporter=line`. Expected: all pass.
- [ ] **Step 2:** For each failure, decide real bug vs environment (timeouts from a loaded machine). Fix real bugs with superpowers:systematic-debugging; re-run that spec. Record the final tally in the task report.

### Task 1: Settings, errors, rate limiter, dependencies

**Files:** Modify `pyproject.toml`, `app/core/config.py`, `app/core/exceptions.py`, `.env.example`. Create `app/core/rate_limit.py`, `tests/test_rate_limit.py`.

**Interfaces — Produces:**
- `exceptions.RateLimitedError(DomainError)`: status 429, code `rate_limited`, `details={"retry_after": int}`; the handler also sets a `Retry-After` header.
- `exceptions.PayloadTooLargeError(DomainError)`: status 413 (code set by the raiser).
- `rate_limit.hit(bucket: str, key: str, *, limit: int, window_seconds: int) -> bool` — `True` when allowed. Redis `INCR` + `EXPIRE` on first hit, key `rl:{bucket}:{key}:{window_index}`. Any Redis error → log warning, return `True`.
- `rate_limit.limit_by_ip(bucket: str, *, limit: int, window_seconds: int) -> Callable[..., Awaitable[None]]` — a FastAPI dependency using `deps.get_client_ip`; raises `RateLimitedError` when `hit` is `False`.
- Settings listed in Global Constraints. `UPI_VPA` validated like `HospitalBase._validate_upi_vpa`.

- [ ] **Step 1: Failing tests** in `tests/test_rate_limit.py`: `test_allows_up_to_limit_then_refuses` (limit 3 → True, True, True, False), `test_separate_keys_do_not_share_a_budget`, `test_fails_open_when_redis_errors` (monkeypatch `get_redis` to return an object whose `incr` raises `redis.ConnectionError` → `True`), `test_dependency_returns_429_with_retry_after` (tiny app with the dependency; 2nd request → 429, body `error.code == "rate_limited"`, header `Retry-After` present), and in `tests/test_config.py` (or the existing settings test) `test_production_requires_upi_vpa_and_otp_secret`.
- [ ] **Step 2:** Run them → FAIL.
- [ ] **Step 3:** Implement. Add `segno` and `reportlab` (exact current versions, `pip index versions`) to `pyproject.toml`; `pip install -e .[dev]` into `.venv`; also `docker compose exec api pip install segno reportlab` if the Docker api is serving (it is on this machine).
- [ ] **Step 4:** Tests pass; full gates clean.

### Task 2: Tables, enums, permissions — one migration

**Files:** Modify `identity/models.py`, `subscriptions/models.py`, `subscriptions/rbac.py`, `app/registry.py`. Create the migration (`down_revision = "d2f6b0e4c8a3"`). Test: `tests/test_models.py`, `tests/test_rbac_seed.py`.

**Interfaces — Produces** (all platform tables: no RLS tenant policy; grant the app role privileges the way `…a7c3e9f1b2d4_platform_invites_and_emails.py` does):
- `identity.models.OtpPurpose(StrEnum)`: `SIGNUP`, `RESET`. `EmailOtp(IdentifiedModel, table "email_otps")`: `email` (indexed, lowercased), `purpose`, `code_hash` (64), `expires_at`, `attempts: int = 0`, `consumed_at`, `created_ip` (45). Index `(email, purpose, created_at)`.
- `SignupIntentStatus(StrEnum)`: `STARTED`, `VERIFIED`, `COMPLETED`, `EXPIRED`. `SignupIntent(IdentifiedModel, table "signup_intents")`: `email` (indexed), `full_name` (200), `hospital_name` (200), `plan_code` (FK plans), `phone` (20, nullable), `status`, `verified_at`, `user_id` (FK users, nullable), `handoff_code_hash` (64, nullable, unique), `handoff_expires_at`.
- `ContactMessage(IdentifiedModel, table "contact_messages")`: `name` (200), `email` (255), `phone` (20, nullable), `hospital_name` (200, nullable), `message` (4000), `created_ip` (45, nullable).
- `PlatformEmailKind` gains `OTP`, `SIGNUP_EXISTS`, `PASSWORD_RESET`, `PAYMENT_SUBMITTED`, `PAYMENT_ALERT`, `PAYMENT_APPROVED`, `PAYMENT_REJECTED`, `CONTACT_ALERT` (migration: `ALTER TYPE platformemailkind ADD VALUE IF NOT EXISTS …`, downgrade leaves labels, as `d2f6b0e4c8a3` does).
- `subscriptions.models.OrderPurpose(StrEnum)`: `NEW`, `RENEWAL`, `UPGRADE`. `OrderStatus(StrEnum)`: `AWAITING_PAYMENT`, `SUBMITTED`, `APPROVED`, `REJECTED`, `EXPIRED`.
- `PaymentOrder(IdentifiedModel, table "payment_orders")`: every column in spec §6 (`reference_code` unique 9 chars; `utr` 12 with partial unique index `WHERE utr IS NOT NULL`; `payer_upi_id` 100; `upi_vpa`, `payee_name`, `upi_uri` 500; `hospital_name` 200; `rejection_reason` 500; FKs indexed). Index `(status, submitted_at)`.
- `PaymentProof(IdentifiedModel, table "payment_proofs")`: `payment_order_id` (FK, **unique** — one live screenshot per order, replaced on re-upload), `content_type` (32), `data: bytes` (`LargeBinary`), `size_bytes: int`, `sha256` (64).
- `PaymentReceipt(IdentifiedModel, table "payment_receipts")`: `payment_order_id` (FK, unique), `receipt_number` (24, unique), `issued_at`, `pdf: bytes`. Migration creates `CREATE SEQUENCE payment_receipt_seq` (dropped on downgrade).
- `SubscriptionPermissions` gains `PLATFORM_PAYMENT_READ = "platform:payment_read"`, `PLATFORM_PAYMENT_REVIEW = "platform:payment_review"`; `PLATFORM_ADMIN` gets both, `PLATFORM_SUPPORT` gets read. Migration inserts permissions and grants with the `ON CONFLICT DO NOTHING` pattern.

- [ ] **Step 1: Failing tests:** `test_models.py` asserts the new tables exist with the unique/partial-unique indexes (`payment_orders.utr` index has a `WHERE` clause; `payment_proofs.payment_order_id` unique); `test_rbac_seed.py` asserts the two new permissions and their role grants match `MODULE_ROLE_PERMISSIONS`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Models, rbac, migration. `python scripts/export_rbac.py`.
- [ ] **Step 4:** `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` clean on the local DB; tests pass; gates clean.

### Task 3: Email OTP

**Files:** Create `identity/otp.py`, `tests/test_otp.py`. Modify `identity/email_templates.py` (OTP, signup-exists, reset templates).

**Interfaces — Produces:**
- `otp.issue(session, *, email: str, purpose: OtpPurpose, ip: str | None, now: datetime | None = None) -> str | None` — returns the code, or `None` when the 60 s cooldown or the 5-per-hour cap refuses (caller answers identically either way). Previous unconsumed codes for the same email+purpose are consumed.
- `otp.verify(session, *, email: str, purpose: OtpPurpose, code: str, now: datetime | None = None) -> None` — raises `ValidationError(code="otp_invalid")` (status 400) for every failure; increments `attempts` and **commits** the increment before raising (as `_refuse` in `invites.py` does); marks consumed on success.
- `otp.send_code_email(session, *, email: str, code: str, purpose: OtpPurpose) -> None` and `otp.send_signup_exists_email(session, *, email: str) -> None` — send via `get_mailer()`, log a `PlatformEmail` with the code replaced by `••••••`.

- [ ] **Step 1: Failing tests** (fake `now` passed in): `test_verify_accepts_the_code_once`, `test_expired_code_is_refused` (now + 11 min), `test_fifth_wrong_attempt_locks_the_code` (5 wrong then the right one → `otp_invalid`), `test_resend_within_60s_is_refused` (`issue` → `None`), `test_sixth_code_in_an_hour_is_refused`, `test_new_code_kills_the_old_one`, `test_code_hash_is_not_the_code` (row's `code_hash != code`, length 64), `test_logged_email_body_has_no_code`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. Hash: `hmac.new(OTP_HMAC_SECRET, f"{purpose}:{email}:{code}", sha256).hexdigest()`. In local/test with no secret set, derive from `SECRET_KEY` (production refuses this; Task 1).
- [ ] **Step 4:** Pass; gates clean.

### Task 4: Signup, handoff, password reset, plans list

**Files:** Create `identity/signup.py`, `identity/public_router.py`, `identity/public_schemas.py`, `tests/test_api_signup.py`, `tests/test_api_password_reset.py`. Modify `app/core/security.py` (`TokenType` gains `"signup"`; `create_signup_token(intent_id) -> str`, 30 min), `app/main.py` (mount the public router), `subscriptions/service.py` (`list_active_plans(session) -> list[Plan]`).

**Interfaces — Produces** (routes under `/api/v1/public`, each with `limit_by_ip`):
- `GET /public/plans` → `list[PlanRead]` (`code`, `name`, `price_inr`, `interval_days`, `features: list[str]`) from the table, ordered by `rank`.
- `POST /public/signup/start` `{full_name, email, phone?, hospital_name, plan_code}` → `202 {"status": "sent"}` always. Unknown plan → 422. Existing account → send signup-exists email instead of a code. Limit 10/h/IP.
- `POST /public/signup/resend` `{email}` → `202` always. Limit 10/h/IP.
- `POST /public/signup/verify` `{email, code}` → `{"signup_token": str}`. Latest `STARTED` intent for the email → `VERIFIED`. Limit 30/h/IP.
- `POST /public/signup/complete` `{signup_token, password}` → `{"handoff_code": str, "expires_in": 120}`. Validates strength (`validate_password_strength`), creates the user via `create_user` with `hospital_id=None`, `role_codes=[]`, `must_change_password=False`; intent → `COMPLETED`; stores the SHA-256 of a `generate_opaque_token()` handoff code, valid 2 min. Token for a non-`VERIFIED` intent, or an email that gained an account meanwhile → `signup_token_invalid`. Audit `signup.complete`.
- `POST /public/signup/handoff` `{code}` → the same `TokenPair` body `/auth/login` returns. Single use (hash cleared). Bad/used/expired → `handoff_invalid`. Audit `auth.login`.
- `POST /public/password/forgot` `{email}` → `202` always; code sent only when an active account exists. Limit 5/h/IP.
- `POST /public/password/reset` `{email, code, password}` → `204`. Verifies the OTP, sets the password, clears lockout, `revoke_all_refresh_tokens`, audit `auth.password_reset`.
- `signup.expire_stale_intents(session, now) -> int` — `STARTED`/`VERIFIED` older than 24 h → `EXPIRED` (used by Task 8).

- [ ] **Step 1: Failing tests** in `test_api_signup.py` (read the OTP by monkeypatching `otp.send_code_email` to capture the code): `test_signup_creates_a_user_with_no_hospital_and_no_roles`, `test_signed_up_user_can_log_in_and_gets_403_on_tenant_routes` (`GET /patients` and `GET /subscriptions/current` → 403), `test_start_for_existing_email_answers_the_same_and_sends_no_code`, `test_unknown_plan_is_422`, `test_verify_with_wrong_code_is_otp_invalid`, `test_complete_rejects_a_reused_signup_token`, `test_weak_password_is_refused`, `test_handoff_code_works_once`, `test_handoff_code_expires_after_two_minutes`, `test_start_is_rate_limited_per_ip` (11th → 429), `test_public_plans_lists_both_with_server_prices`. In `test_api_password_reset.py`: `test_reset_sets_password_and_revokes_sessions` (old refresh token → 401), `test_forgot_for_unknown_email_is_202_and_sends_nothing`, `test_reset_with_wrong_code_keeps_the_old_password`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. `/public/*` must not require auth and must be excluded from the BFF's session-minting refusal only where the app calls it server-side (Task 9).
- [ ] **Step 4:** Pass; gates clean.

### Task 5: Contact endpoint

**Files:** Modify `signup.py` (`create_contact_message`), `public_router.py`, `website/src/components/sections/ContactSection.tsx`. Test: `tests/test_api_contact.py`.

**Interfaces — Produces:** `POST /public/contact` `{name, email, phone?, hospital_name?, message (10–4000), website?}` → `202`. A non-empty `website` field (honeypot) → `202`, nothing stored. Stores a `ContactMessage`, emails `CONTACT_INBOX` (`CONTACT_ALERT`). Limit 5/h/IP. CORS: `WEBSITE_URL` origin allowed. The website form's `data-endpoint` becomes `${NEXT_PUBLIC_API_URL}/api/v1/public/contact` (env with a localhost default, documented in `website/README.md`).

- [ ] **Step 1: Failing tests:** `test_contact_is_stored_and_emailed`, `test_honeypot_is_accepted_and_dropped`, `test_contact_is_rate_limited`, `test_cors_allows_the_website_origin` (preflight from `WEBSITE_URL` → `access-control-allow-origin` echoed).
- [ ] **Step 2–4:** FAIL → implement → PASS; gates clean; `cd website && npm run build` clean.

### Task 6: Orders, UPI QR, proof intake

**Files:** Create `subscriptions/upi.py`, `subscriptions/orders.py`, `tests/test_upi.py`, `tests/test_orders_service.py`, `tests/test_api_orders.py`, `tests/payments_support.py`. Modify `subscriptions/router.py`, `schemas.py`.

**Interfaces — Produces:**
- `upi.build_uri(*, vpa: str, payee: str, amount_inr: int, reference: str) -> str` → `upi://pay?pa=…&pn=…&am=3000.00&cu=INR&tn=…&tr=…` with `urllib.parse.quote` (spaces as `%20`). `upi.qr_svg(uri: str) -> str` via `segno.make(uri, error="m").svg_inline(scale=6)`.
- `orders.new_reference_code() -> str`.
- `orders.create_order(session, *, user: User, plan_code: str | None, purpose: OrderPurpose) -> PaymentOrder` (`None` → the signup intent's plan): pending users only (`hospital_id is None`, not a platform user; else `already_has_hospital`); `NEW` only; amount, VPA, payee snapshotted from the `plans` row and settings; `hospital_name` from the user's latest `COMPLETED` signup intent. If the user has a `SUBMITTED` order → `order_under_review`. An existing `AWAITING_PAYMENT`/`REJECTED` order for the **same plan** is returned as is; for another plan it is marked `EXPIRED` and a new one created. Retries on reference collision.
- `orders.get_own_order(session, *, user, order_id) -> PaymentOrder` (another user's → 404), `orders.list_own_orders(session, *, user, params) -> tuple[list, int]`.
- `orders.attach_proof(session, *, user, order_id, body: AsyncIterator[bytes]) -> PaymentProof` — order must be `AWAITING_PAYMENT` or `REJECTED` (else `order_not_payable`); reads chunks, aborts past 5 MB (`proof_too_large`), sniffs `\x89PNG\r\n\x1a\n` / `\xff\xd8\xff` / `%PDF-` (`proof_type_invalid`); upserts the one proof row.
- `orders.submit_payment(session, *, user, order_id, utr: str, payer_upi_id: str | None) -> PaymentOrder` — `AWAITING_PAYMENT`/`REJECTED` only; expired-by-clock → marks `EXPIRED`, `order_not_payable`; UTR on any other order → `utr_taken`; sets `SUBMITTED`, `submitted_at`, clears `rejection_reason`; audit `payment.submit`; after commit emails payer (`PAYMENT_SUBMITTED`) and `CONTACT_INBOX` (`PAYMENT_ALERT`).
- `orders.expire_unpaid(session, now) -> int` — `AWAITING_PAYMENT` past `expires_at` → `EXPIRED`.
- Routes (pending-user dependency `PendingUser`: authenticated, `hospital_id is None`, not platform; else 403 `already_has_hospital`):
  - `POST /subscriptions/orders` `{plan_code?, purpose}` → `OrderRead`
  - `GET /subscriptions/orders` → `Page[OrderRead]`
  - `GET /subscriptions/orders/{id}` → `OrderDetail` (`OrderRead` + `qr_svg`, `upi_uri`, `review_hours`, `has_proof`)
  - `PUT /subscriptions/orders/{id}/proof` raw body (any content type) → `204`
  - `POST /subscriptions/orders/{id}/submit` `{utr, payer_upi_id?}` → `OrderRead`
  - `OrderRead`: `id, reference_code, purpose, plan_code, plan_name, amount_inr, status, utr, submitted_at, rejection_reason, expires_at, created_at`.
  - Raw-body upload instead of multipart: the BFF already forwards bytes untouched and it avoids a `python-multipart` dependency (deviation from spec §8 "multipart" — note it in the task report).
- `/subscriptions/*` currently assumes a tenant (`TenantId`); the order routes must not, and `/subscriptions/current` keeps its tenant requirement.

- [ ] **Step 1: Failing tests:** `test_upi.py`: `test_uri_encodes_payee_and_amount` (payee `"Nirogya Health"` → `pn=Nirogya%20Health`, `am=3000.00`, `tr=` equals reference), `test_qr_svg_is_svg`. `test_orders_service.py`: `test_amount_comes_from_the_plan_table`, `test_reference_code_shape` (regex `^NIR[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{6}$`), `test_same_plan_returns_the_live_order`, `test_switching_plan_expires_the_old_order`, `test_submitted_order_blocks_a_new_one`, `test_renewal_purpose_is_unavailable`, `test_utr_must_be_12_digits` (422 at schema), `test_utr_on_another_order_is_taken`, `test_rejected_order_can_resubmit_with_its_own_utr`, `test_expired_order_cannot_be_submitted`, `test_html_named_png_is_refused`, `test_oversize_stream_is_refused_and_nothing_stored` (6 MB generator), `test_reupload_replaces_the_proof`, `test_expire_unpaid_marks_old_orders`. `test_api_orders.py`: `test_pending_user_creates_and_reads_an_order` (detail has `qr_svg` starting `<svg`), `test_other_users_order_is_404`, `test_hospital_staff_cannot_create_new_orders` (hospital admin → 403 `already_has_hospital`), `test_upload_then_submit_moves_to_submitted`.
- [ ] **Step 2–4:** FAIL → implement → PASS; gates clean.

### Task 7: Approval, rejection, receipts, platform payment queue

**Files:** Create `subscriptions/approval.py`, `subscriptions/receipts.py`, `subscriptions/emails.py`, `subscriptions/platform_router.py`, `tests/test_approval.py`, `tests/test_api_platform_payments.py`. Modify `identity/platform.py` (overview gains `payments_awaiting_review: int`, read through a `subscriptions.service` function), `app/main.py`.

**Interfaces — Produces:**
- `approval.approve(session, *, order_id, actor: User) -> PaymentOrder` — `SELECT … FOR UPDATE`; status not `SUBMITTED` → return unchanged if `APPROVED`, else `order_not_payable`. For `NEW`: `tenancy_service.create_hospital` with `name=order.hospital_name`, `code=unique_hospital_code(name)`; `user.hospital_id = hospital.id`; `assign_roles(user, ["HOSPITAL_ADMIN"], allow_privileged_roles=True)` (and drop the identity RBAC cache for the user); a paid subscription via `write_subscription` (`plan_code`, `current_period_start=now`, `current_period_end=now + interval_days`, `complimentary=False`); receipt row with `nextval('payment_receipt_seq')`; order → `APPROVED`, `reviewed_by_id`, `reviewed_at`; audit `payment.approve` and `hospital.create`. After commit (router calls `emails.send_approved(…)`): approval email with the receipt PDF attached (`MailMessage` gains optional `attachments: tuple[MailAttachment, ...]`).
- `approval.unique_hospital_code(session, name: str) -> str` — initials of the words, letters only, uppercased, 2–6 chars (single word → first 4 letters); taken → append `2`, `3`, … up to 16 chars.
- `approval.reject(session, *, order_id, actor, reason: str (5–500)) -> PaymentOrder` — `SUBMITTED` only; `REJECTED`, `rejection_reason`, `utr = NULL` (frees it); audit; email `PAYMENT_REJECTED` after commit.
- `receipts.render_pdf(*, receipt_number, issued_at, hospital_name, payer_name, payer_email, plan_name, amount_inr, reference_code, utr, period_start, period_end) -> bytes` — reportlab, A4, "Amount includes GST". Must start with `%PDF-`.
- Routes under `/platform/payments` (read: `platform:payment_read`, write: `platform:payment_review`): `GET ""` (`?status=` filter, paginated, `submitted_at` desc; `PlatformPaymentRow` with payer name/email, hospital name, plan, amount, reference, UTR, status, submitted_at, has_proof), `GET /{id}`, `GET /{id}/proof` (stored content type, `X-Content-Type-Options: nosniff`, `Content-Disposition: attachment` for PDF, `inline` for images), `POST /{id}/approve`, `POST /{id}/reject` `{reason}`, `POST /{id}/resend-receipt`. `GET /platform/contact-messages` (paginated, `platform:payment_read`). Pending user's receipt download: `GET /subscriptions/receipts/{order_id}/pdf` (owner or that hospital's admin).

- [ ] **Step 1: Failing tests** (mark `@pytest.mark.no_auto_subscription` where approval creates a hospital): `test_approve_new_creates_hospital_admin_subscription_and_receipt` (user's `hospital_id` set; roles == {HOSPITAL_ADMIN}; subscription `plan_code` = order's, `complimentary` False, period 30 days; receipt number matches `^NIR-\d{4}-\d{6}$`, pdf starts `%PDF-`), `test_double_approve_is_idempotent` (second call → same order, one hospital, one receipt), `test_concurrent_approve_creates_one_hospital` (two sessions, `asyncio.gather`), `test_approved_user_reaches_tenant_routes` (new token → `GET /patients` 200), `test_hospital_code_is_unique` (two "City Care Hospital" → `CCH`, `CCH2`), `test_reject_frees_the_utr_and_records_reason`, `test_cannot_approve_an_awaiting_order`, `test_failed_approval_email_does_not_undo_approval` (mailer returns `ok=False` → order `APPROVED`, `PlatformEmail` `FAILED`; resend-receipt then `SENT`), `test_support_can_read_but_not_approve` (403), `test_hospital_admin_gets_403_on_platform_payments`, `test_proof_route_is_platform_only_and_nosniff`, `test_overview_counts_payments_awaiting_review`.
- [ ] **Step 2–4:** FAIL → implement → PASS; gates clean.

### Task 8: Worker sweep and full API flow

**Files:** Modify `app/workers/tasks.py`, `app/workers/main.py`. Create `tests/test_signup_full_flow.py`; extend the worker test file.

**Interfaces — Produces:** `tasks.sweep_signups_and_orders(ctx) -> dict[str, int]` (`{"orders_expired": n, "intents_expired": m}`) calling `orders.expire_unpaid` and `signup.expire_stale_intents` in a system context; ARQ `cron(..., minute={7})` hourly.

- [ ] **Step 1: Failing tests:** `test_sweep_expires_orders_and_intents`; `test_signup_to_working_hospital` — start → verify → complete → handoff → create order → upload PNG → submit UTR → platform admin approves → user's next request to `GET /auth/me` shows `HOSPITAL_ADMIN` and `GET /subscriptions/current` shows the plan, `GET /patients` → 200.
- [ ] **Step 2–4:** FAIL → implement → PASS. Full `pytest -q` green; coverage on the new files ≥ 85 % (`pytest --cov=app/modules/subscriptions --cov=app/modules/identity/signup.py --cov=app/modules/identity/otp.py --cov=app/core/rate_limit.py`).

### Task 9: App — session handoff and `/get-started`

**Files:** see the file map. `npm run codegen` first.

**Interfaces — Consumes:** Task 4 handoff, Task 6 order routes, Task 7 receipt route.
**Produces:**
- `src/app/auth/handoff/route.ts` — `GET ?code=` → server-side `POST /public/signup/handoff` → `writeSession` → redirect `/get-started`; a bad code → `/login?handoff=failed` with a translated notice. `/auth/handoff` is added to `PUBLIC_PATHS`.
- `homePathFor`: a user with no `hospital_id` and no `platform:console` → `/get-started`. `(app)/layout.tsx` redirects such users to `/get-started`. `/get-started` lives outside `(app)` (no sidebar), requires a session, and redirects users who have a hospital to `/`.
- `/get-started` screen states, driven by the latest order: no live order → the server page creates one with `POST /subscriptions/orders {purpose: "NEW"}` and no `plan_code` (Task 6's `create_order` defaults `plan_code` to the signup intent's), so the payer lands straight on the QR; a "Change plan" control re-posts with the other plan; `AWAITING_PAYMENT`/`REJECTED` → payment panel: amount, plan, reference with copy, QR (render the server's SVG via `dangerouslySetInnerHTML` is not allowed by CSP-safe rules → render as `<img src={"data:image/svg+xml;utf8," + encodeURIComponent(svg)}>`; `img-src data:` is already allowed), "Open UPI app" link (`upi_uri`, shown on touch devices), expiry countdown, numbered steps, rejection reason banner, proof form (UTR 12 digits, optional payer UPI ID, optional screenshot uploaded with `fetch(PUT, body: file)` through `/bff`); `SUBMITTED` → "Under review — usually within 12 hours", polls every 20 s (`refetchInterval: 20_000`); `APPROVED` → "You're in" with a button to `/admin/hospital` (roles are resolved per request, so the existing session already carries the new role), plus "Download receipt".
- Sign-out link on the page.

- [ ] **Step 1:** Playwright spec `e2e/signup-payment.spec.ts` (written now, fails): signup via API helpers in `e2e/support.ts` that read the OTP from the console mailer outbox (`E2E_MAIL_OUTBOX`, default `../backend/.mail-outbox`; newest `.eml` to that address; regex `\b\d{6}\b`); then sign in through `/login` → lands on `/get-started`; choose Standard → sees `₹3,000` and a `NIR` reference; submits UTR `123456789012` → "Under review"; platform admin approves via API; page flips to approved within 25 s; continue → `/admin/hospital`, sidebar shows OPD items only (Standard). Also: `/auth/handoff?code=bogus` → login page with the notice.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement; strings in en + hi.
- [ ] **Step 4:** Spec passes; `npm run verify` clean.

### Task 10: App — forgot password

**Files:** `src/app/forgot-password/page.tsx`, `src/features/auth/forgot-password-form.tsx`, `src/features/auth/actions.ts` (`requestPasswordReset`, `resetPassword` server actions), `src/app/login/page.tsx` (link), `PUBLIC_PATHS`.

**Produces:** two steps on one page: email → "If an account exists, we sent a code"; then code + new password + confirm → redirect `/login?reset=done` with a success notice. 12-character minimum shown up front.

- [ ] **Step 1:** Playwright case in `e2e/signup-payment.spec.ts` (or `e2e/forgot-password.spec.ts`): reset the pending user's password via the UI using the outbox code, then sign in with the new one; wrong code shows the generic error.
- [ ] **Step 2–4:** FAIL → implement → PASS; `npm run verify`.

### Task 11: Console — payment queue and contact messages

**Files:** `src/app/(app)/platform/payments/page.tsx`, `src/features/platform/payments-screen.tsx`, `payment-review-dialog.tsx`, `use-platform.ts`, platform nav, `overview-screen.tsx`, `emails-screen.tsx`.

**Produces:** status tabs (reuse `filter-tabs.tsx`): Awaiting review (default) / Approved / Rejected / All. Table: submitted time, reference, payer, hospital name, plan, amount, UTR. Row opens the dialog: all fields, the screenshot (images inline via the proof route through `/bff`, PDFs as a download link), "Approve" (confirm step: "Only approve once you have seen ₹X with reference NIR… in the bank app"), "Reject" with a required reason. Overview gains an "Payments awaiting review" tile linking to the queue. Emails screen gains a "Contact messages" tab. Nav item visible with `platform:payment_read`; approve/reject buttons only with `platform:payment_review`.

- [ ] **Step 1:** Extend `e2e/signup-payment.spec.ts`: the approval is done through the console UI instead of the API — open `/platform/payments`, see the reference, open the dialog, approve, row leaves the Awaiting tab. Support user sees the queue without the buttons.
- [ ] **Step 2–4:** FAIL → implement → PASS; `npm run verify`.

### Task 12: Docs and the full run

- [ ] **Step 1:** `README.md` (signup flow, new settings), `Docs/runbook.md` ("Approving a payment": check the bank app for amount + reference/UTR, then approve; rejecting; resending a receipt), `.env.example` final check, `Docs/HANDOFF.md` updated for slice 3.
- [ ] **Step 2:** Backend gates + full `pytest -q`; frontend `npm run verify`; full `npx playwright test`. Record counts.
- [ ] **Step 3:** superpowers:requesting-code-review on the slice; fix what holds up. Stop and report to the owner (no commit).
