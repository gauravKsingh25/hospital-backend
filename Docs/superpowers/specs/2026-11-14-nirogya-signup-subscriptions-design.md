# Nirogya — public website, self-service signup and UPI subscriptions — design

Date: 2026-11-14 · Status: draft for approval · Background: `Docs/DISCOVERY.md`

## 1. Intent

Today a hospital gets onto the system only when the platform owner creates it
in the console and invites its admin. We want hospitals to arrive on their own:
find the product on a public website, pick a plan, verify their email, pay by
UPI, and be running the same day once the owner has confirmed the payment.

There is no payment gateway. Money goes to one fixed UPI ID, so the system
cannot know a payment happened; the owner checks each one against their bank
app and approves it. The software's job is to make that check fast and safe,
and to make access follow payment exactly: nothing before approval, the right
features after it, and a clean wind-down when a month goes unpaid.

Success: a stranger can go from the landing page to a working hospital account
with only one human step in the middle (the owner's approve click), and the
owner never has to touch the database to sell, renew, upgrade or cut off a
hospital.

## 2. Goals, non-goals, anti-goals

**Goals**
- Public marketing site for Nirogya with pricing, signup, UPI payment and
  status pages, contact form and legal pages.
- Signup: details → email OTP → password → account exists (no hospital yet).
- UPI order with server-priced amount, QR, deep link, UTR + optional
  screenshot; manual approve/reject in the platform console.
- Approval creates the hospital and makes that user its `HOSPITAL_ADMIN`.
- Two monthly plans; Standard is OPD only, Professional is everything.
- Renewal by a new UPI order, reminders at 7/3/1 days, 7-day grace, then
  expired (subscription page and data export only).
- Upgrade for the pro-rated difference; downgrade at next renewal.
- Forgot-password by email OTP.
- Existing hospitals, tests and demo data keep working unchanged.

**Non-goals**
- Any payment gateway, auto-debit or automatic payment confirmation.
- Free tier or trial.
- Email OTP at login (authenticator-app 2FA stays as it is).
- Inviting hospital staff by email (admins keep creating staff as today).
- One account owning several hospitals; seat limits (both plans unlimited).
- GST invoices or a GST breakdown (prices are GST-inclusive, simple receipts).
- A second tenancy mechanism: `hospitals` + `hospital_id` + RLS stay as is.

**Anti-goals**
- Any access to hospital features on the strength of a submitted UTR alone.
- Two hospitals, two subscriptions or two receipts from one approved order.
- A price, plan or amount taken from the browser.
- A payment screenshot reachable by anyone but the platform team.
- Locking an existing hospital, or deleting any data on downgrade or expiry.
- The UPI ID, SMTP password or OTP secret in code, git or logs.
- A website that reads as a generic template.

## 3. Constraints

- CLAUDE.md: modular monolith, fixed stack, Alembic for every schema change,
  thin routers, typed domain exceptions, paginated lists, audit on sensitive
  actions, Pydantic v2 at every boundary, env-driven config.
- Only `identity` and `tenancy` cross tenants today. The new `subscriptions`
  module is platform-level and joins them explicitly (CLAUDE.md §2/§3 updated).
- Existing names stay: routes under `/api/v1`, console under `/platform`,
  settings `SECRET_KEY`, `SMTP_USERNAME`, `MAIL_FROM`, `APP_PUBLIC_URL`.
- The app's browser never calls the API directly (BFF). The website is static
  and does, so its endpoints are unauthenticated-or-token, CORS-restricted and
  rate-limited.
- Dev and tests never send real email or move real money.

## 4. Approaches considered

**Where the account comes from** (owner's instruction: create the user first,
make them hospital admin only after payment)

- **A — account with password at signup (chosen).** After OTP the visitor sets
  a password; the user exists with no hospital and no role. Signing in shows
  only payment and status. Approval creates the hospital and attaches the same
  user as admin in one transaction. Fewer emails, the payer can return to pay
  or fix a rejected UTR by signing in, and status needs no anonymous token.
- B — passwordless user row, invite link after approval. Reuses the invite
  code but adds a step and an email, and status/resubmit must work logged out.
- C — no user until approval (the original brief). Contradicts the instruction.

**Where the subscription code lives**

- **New `subscriptions` module (chosen).** Platform-level tables, its own
  service and routers. Patient billing and SaaS billing never share a table.
- Inside `billing`: rejected, that module is tenant-scoped patient money.
- Inside `tenancy`: workable but buries ~15 endpoints in a module about
  facility records.

**How features are locked**

- **Feature dependency on routers plus one global subscription check
  (chosen).** `require_feature(key)` added at `include_router` time for the
  locked routers; subscription state checked inside the existing auth
  dependency. One registry file maps feature keys to routers and nav items.
- Rewriting role permissions per plan: rejected, it would fork the seeded RBAC
  per hospital and make an upgrade a data migration.

## 5. The flow

```
website                         API                              owner / app
-------                         ---                              -----------
/pricing → pick plan
/signup  name, email, hospital → POST public/signup/start        OTP email
/signup/verify  6 digits       → POST public/signup/verify       → signup_token
/signup/password               → POST public/signup/complete     user created (no hospital)
                                                                 → signed-in handoff to app
app /get-started  (signed in, no hospital)
  plan + QR + UTR form         → POST subscriptions/orders
                               → POST subscriptions/orders/{id}/proof
                                                                 emails: payer + owner
  "Under review" (polls)                                         console /platform/payments
                                                                 Approve → hospital + admin role
                                                                           + subscription + receipt
  → "Approved" → /admin/hospital (finish profile) → normal app
```

*My call:* after the password step the visitor is handed to the **app** for
payment, not kept on the static site. They are already signed in there, so the
payment, status, resubmit, renew and upgrade screens are one set of screens
behind one session, and the website needs no token storage beyond the few
minutes of signup. The website still has a `/pay` explainer and its pricing
page states that payments are verified manually within 12 hours.

Rules:
- `signup/start` for an email that already has an account answers exactly as
  for a new one, and the email it sends says "you already have an account —
  sign in or reset your password". No enumeration.
- An order's amount comes from the `plans` table on the server.
- Rejected order: payer is emailed the reason and resubmits a UTR on the same
  order. One live submission per order.
- An order with no proof expires after 24 h; the payer can start another.

## 6. Data model

All platform-level (no `hospital_id` RLS policy), UUID keys, timestamps, in
`subscriptions/models.py` unless noted. Money is integer rupees (`amount_inr`).

| Table | Columns that matter |
|---|---|
| `plans` | `code` PK (`standard`, `professional`), `name`, `price_inr`, `interval_days` (30), `rank`, `is_active`. Seeded: 3000, 8999 |
| `plan_features` | `plan_code`, `feature_key`, unique together. Presence = enabled |
| `subscriptions` | `hospital_id` (unique), `plan_code`, `status` (`ACTIVE`/`GRACE`/`EXPIRED`/`CANCELLED`), `current_period_start`, `current_period_end` (null = complimentary, never lapses), `pending_plan_code` (downgrade at renewal), `cancel_at_period_end`, `complimentary` |
| `payment_orders` | `reference_code` (unique, `NIR` + 6 of an unambiguous alphabet), `purpose` (`NEW`/`RENEWAL`/`UPGRADE`), `user_id`, `hospital_id` (null for `NEW` until approval), `plan_code`, `amount_inr`, `upi_vpa` + `payee_name` (snapshot), `upi_uri`, `status` (`AWAITING_PAYMENT`/`SUBMITTED`/`APPROVED`/`REJECTED`/`EXPIRED`), `utr` (partial unique where not null), `payer_upi_id`, `submitted_at`, `reviewed_by_id`, `reviewed_at`, `rejection_reason`, `expires_at`, `hospital_name` (for `NEW`) |
| `payment_proofs` | `payment_order_id`, `content_type`, `data` (bytes ≤ 5 MB), `sha256`. Separate table so listing orders never loads blobs |
| `payment_receipts` | `payment_order_id` (unique), `receipt_number` (from a Postgres sequence, `NIR-2026-000001`), `issued_at`, `pdf` (bytes) |
| `subscription_reminders` | `subscription_id`, `period_end`, `days_before`, unique together — makes reminders idempotent |
| `identity.email_otps` | `email`, `purpose` (`SIGNUP`/`RESET`), `code_hash` (HMAC-SHA256 with `OTP_HMAC_SECRET`), `expires_at`, `attempts`, `consumed_at`, `created_ip` |
| `identity.signup_intents` | `email`, `full_name`, `hospital_name`, `plan_code`, `phone`, `verified_at`, `user_id`, `status` (`STARTED`/`VERIFIED`/`COMPLETED`/`EXPIRED`) |
| `identity.contact_messages` | `name`, `email`, `phone`, `hospital_name`, `message`, `created_ip` |

Changes to existing tables: none to columns. `users.hospital_id` stays
nullable; its comment changes — null now also means "signed up, not yet
approved". Such a user has no roles, therefore no permissions, and no tenant
context, therefore RLS shows them nothing.

Migration also inserts a complimentary `ACTIVE` Professional subscription for
every existing hospital. Hospitals created from the console get the same
(complimentary) unless the owner picks otherwise — the console keeps working
for hand-sold deals. Every migration has a working downgrade.

## 7. Plans and features

One registry, `subscriptions/features.py`:

| Feature key | Standard | Professional | Backend routers gated | Nav / screens |
|---|---|---|---|---|
| `opd` (patients, scheduling, queue, clinical, OPD billing, admin setup) | ✓ | ✓ | — (never locked) | — |
| `ipd` | — | ✓ | all `/ipd/*` except medicines | Admissions, Wards, Rounds, admin Wards |
| `diagnostics` | — | ✓ | `/diagnostics/*` | Lab, admin Catalogue |
| `medicines` | — | ✓ | `/ipd/medicines` | Medicines |
| `accounts` | — | ✓ | `/billing/accounts`, `/billing/claims`, IPD-scope invoice routes | Accounts dashboard, IPD bill editor |
| `messaging` | — | ✓ | `/notifications/*`; dispatcher records `SUPPRESSED / NOT_IN_PLAN` | Messages |
| `reports_full` | — | ✓ | `/reports/*` except the Today view | Reports (period views) |

Inside an OPD-only hospital, cross-module actions that need a locked feature
(admit from consultation, order a lab test) are refused by the service with
`feature_not_in_plan`, and the UI hides those buttons. Billing event handlers
keep running: they only react to events that can no longer be raised.

**Enforcement**
- `get_auth_context` loads the hospital's subscription (cached in Redis next to
  the RBAC cache, invalidated on every subscription write) and puts
  `plan_code`, `status`, `features` on `AuthContext`.
- Hospital users with status `EXPIRED`/`CANCELLED`-past-period get **402**
  `subscription_inactive` on everything except: `/auth/*`, `/me/*`,
  `/subscriptions/*`, and the data export route. `GRACE` has full access and a
  banner. Platform users are exempt.
- `require_feature(key)` → **403** `feature_not_in_plan` with the key in
  `details`.
- `GET /me/entitlements` → plan, status, period end, days left, features. The
  frontend filters nav by it and shows an "Available on Professional" panel
  with an Upgrade button on locked routes.

**Period maths** (pure functions, fake clock in tests)
- New: period = approval time → +30 days.
- Renewal: new start = later of old `current_period_end` and approval time;
  end = start + 30 days. Applies `pending_plan_code` if set.
- Upgrade: amount = ceil((new − old price) × remaining days / 30), minimum ₹1;
  period end unchanged; features switch on approval. Not offered in grace or
  expired (renew on the higher plan instead).
- Downgrade: sets `pending_plan_code`; nothing changes until renewal.
- Cancel: `cancel_at_period_end`; no reminders; at period end → `CANCELLED`
  (same access as expired). No refunds.
- Lapse: period end passed unpaid → `GRACE`; +7 days → `EXPIRED`.

## 8. Backend

**Module `app/modules/subscriptions/`**: `models.py`, `schemas.py`,
`service.py` (orders, approval, lifecycle), `periods.py` (pure maths),
`features.py` (registry + `require_feature`), `providers.py`
(`PaymentProvider` protocol: `create_order`, `get_payment_instructions`,
`submit_proof`, `confirm`; `ManualUpiProvider`), `upi.py` (URI + SVG QR with
`segno`), `receipts.py` (PDF with `reportlab`), `emails.py`, `rbac.py`,
`router.py`.

**Identity additions**: `identity/otp.py`, `identity/signup.py`, public routes
in `identity/public_router.py`.

**Routes** (all `/api/v1`)

Public, rate-limited, no auth:
- `GET /public/plans`
- `POST /public/signup/start`, `/public/signup/resend`, `/public/signup/verify`
  (→ short-lived signed `signup_token`), `/public/signup/complete` (password →
  user + one-time handoff code the app exchanges for a session)
- `POST /public/password/forgot`, `/public/password/reset` (OTP + new password;
  revokes all refresh tokens)
- `POST /public/contact`

Signed-in (pending user or hospital admin; `subscription:manage` permission
for hospital users, ownership check for pending users):
- `GET /subscriptions/current`, `GET /me/entitlements`
- `POST /subscriptions/orders` (`plan_code`, `purpose`), `GET
  /subscriptions/orders`, `GET /subscriptions/orders/{id}` (with QR),
  `POST /subscriptions/orders/{id}/proof` (multipart)
- `POST /subscriptions/change-plan` (downgrade), `/subscriptions/cancel`,
  `/subscriptions/resume`
- `GET /subscriptions/receipts/{id}/pdf`

Platform (`platform:payment_read`, `platform:payment_review`,
`platform:subscription_manage`; support gets read only):
- `GET /platform/payments` (filter by status), `GET /platform/payments/{id}`,
  `GET /platform/payments/{id}/proof`, `POST …/approve`, `POST …/reject`
- `GET /platform/subscriptions`, `POST /platform/subscriptions/{id}/extend`
  (days + reason), existing hospital suspend/reactivate reused
- `GET /platform/contact-messages`

**Approval** (one transaction, order row locked `FOR UPDATE`):
status must be `SUBMITTED`, otherwise return the existing result unchanged
(idempotent). `NEW`: create hospital (name from the order; code generated from
the name and made unique), set `users.hospital_id`, grant `HOSPITAL_ADMIN`,
seed whatever a console-created hospital gets, create subscription. `RENEWAL`
/ `UPGRADE`: apply period maths. Then receipt row + number, audit rows. Emails
(approval + receipt PDF) go out after commit and are logged in
`platform_emails`; a failed email never undoes an approval and can be resent.

**OTP**: 6 digits from `secrets`, HMAC-hashed, 10-minute life, 5 attempts,
single use, 60 s resend cooldown, 5 per email per hour, constant-time compare,
one generic error for wrong/expired/used.

**Rate limiting**: small Redis fixed-window limiter as a dependency
(`core/rate_limit.py`), keyed by client IP (the existing trusted-proxy logic)
and, where relevant, email. Fails open with a logged warning if Redis is down
— *my call*: a Redis blip should not take signup offline; OTP attempt limits
are in Postgres and still hold.

**Uploads**: PNG/JPEG/PDF only, type decided by magic bytes not the filename
or header, ≤ 5 MB enforced while reading, stored in `payment_proofs`, served
only by the platform proof route with `Content-Disposition: attachment` for
PDFs and `X-Content-Type-Options: nosniff`.

**Worker** (existing ARQ cron, hourly): send due renewal reminders, move
`ACTIVE → GRACE → EXPIRED`, expire unpaid orders and stale signup intents.

**Email**: existing mailer and `platform_emails` log. New kinds: OTP (code
redacted in the stored body), signup-exists, payment-submitted, owner alert,
approved (+ receipt), rejected, reminder, grace, expired. Sent after commit;
OTP send is awaited in a background task so the request does not wait on SMTP.

**Settings added**: `OTP_HMAC_SECRET`, `OTP_TTL_MINUTES`, `UPI_VPA`,
`UPI_PAYEE_NAME` (Nirogya), `PAYMENT_ORDER_TTL_HOURS`, `GRACE_PERIOD_DAYS`,
`PAYMENT_REVIEW_HOURS` (12), `CONTACT_INBOX`, `WEBSITE_URL`,
`SUBSCRIPTIONS_ENFORCED` (default true; an escape hatch, not a feature).
`CORS_ORIGINS` gains the website origin. Production refuses to start without
`UPI_VPA` and `OTP_HMAC_SECRET`.

## 9. App (`frontend/`)

- `/get-started` — for a signed-in user with no hospital: plan summary, QR,
  amount, reference with copy, "Open UPI app", expiry countdown, numbered
  steps, UTR (12 digits) + optional screenshot + optional payer UPI ID; then
  the under-review state (polls every 20 s), rejected state with the reason
  and a resubmit form, and approved state leading to the hospital profile.
- `proxy.ts`: no hospital → `/get-started`; expired → `/admin/subscription`
  (non-admins see "Your hospital's subscription has ended — contact your
  administrator").
- `/admin/subscription` — plan, status, period end, renew, upgrade, downgrade,
  cancel/resume, payment history, receipt downloads. Reuses the QR/UTR
  component.
- Grace banner in the app shell for every role; renew link for admins.
- Nav filtered by entitlements; locked routes show the upgrade panel.
- `/forgot-password` (email → OTP + new password) linked from `/login`.
- Console: `/platform/payments` (queue with status tabs, row opens a review
  dialog: reference, amount, UTR, payer, screenshot, Approve / Reject with
  reason), `/platform/subscriptions` (status, plan, period end, extend),
  contact messages under Emails. Overview gains "payments awaiting review".
- All strings in en + hi; existing components and tokens only.

## 10. Website (`website/`)

Separate Next.js 16 app, `output: "export"`, Tailwind v4, no shadcn runtime
beyond what the pages need, no client data library (plain `fetch`). Brand
tokens copied from the app's theme so the two feel related. Built with the
ui-ux-pro-max and taste skills.

Pages: `/` (hero, problem → solution, features by module, outcomes, security,
FAQ, CTA), `/pricing` (two cards, comparison table from §7, "paid plans only —
everything in your plan from day one", manual-verification and 12-hour note,
GST-inclusive note), `/contact`, `/signup` (details → OTP → password, then
hand-off to the app), `/privacy`, `/terms`, `/refunds` (placeholder text
marked TO BE REVIEWED; refunds page states: cancel any time, access until the
end of the paid month, no refunds). Screenshots are placeholders the owner can
swap; the logo is a wordmark until the real one arrives.

Bar: responsive from 360 px, WCAG 2.1 AA, Lighthouse ≥ 90 in all four
categories, meta + OpenGraph + sitemap + robots, no layout shift, Zod
validation mirrored by the server, honeypot on the contact form.

## 11. Tests

- OTP: expiry, attempt limit, reuse, cooldown, hourly cap, no enumeration.
- Signup: complete creates a hospital-less, role-less user who gets 403 on
  every tenant route.
- Orders: server-side amount, UPI URI encoding, reference uniqueness, expiry,
  UTR format, duplicate UTR, one submission per order, reject → resubmit,
  upload type/size, proof route platform-only.
- Approval: creates hospital + admin + subscription + receipt once;
  double-approve and concurrent approve are idempotent.
- Periods: early/late renewal, upgrade pro-rating and rounding, downgrade at
  renewal, cancel, grace → expired, reminder schedule and idempotency.
- Gating: Standard gets 403 on each locked router and 200 on OPD; expired gets
  402 except the allowed routes; grace gets through; platform users exempt;
  existing hospitals unaffected.
- Isolation: one parametrised test walking every tenant route in the OpenAPI
  schema as hospital A against hospital B's ids.
- Full flow (API): signup → OTP → password → order → UTR → approve → gated
  endpoint works. Same flow in Playwright across website and app.
- Existing suite green; ≥ 85 % coverage on new modules; mypy strict clean
  (including the 23 existing errors, fixed first).

## 12. Delivery slices

Each gets its own plan, review and stop-and-check.

1. **Subscriptions core** — module, migration (plans, complimentary
   subscriptions), entitlements, gating, `/me/entitlements`, nav locking.
   Existing hospitals unaffected is the gate.
2. **Signup and payments** — OTP, signup, orders, QR, proof, approval,
   receipts, emails, console payment queue, `/get-started`, forgot password.
3. **Lifecycle** — renewals, reminders, grace/expiry, upgrade/downgrade/cancel,
   `/admin/subscription`, console subscriptions page.
4. **Website** — all pages, wired to the public API; full-flow e2e.
5. **Hardening** — security review, isolation walk, runbook, UPI verification
   guide, `Docs/DECISIONS.md`.

*My call:* the website comes fourth, not second as in the brief, because its
signup pages cannot be finished or tested until the API behind them exists.

## 13. Calls I made (please check)

- Payment happens in the app after signup, not on the static site (§5).
- New `subscriptions` module; platform-level alongside identity and tenancy.
- Existing and console-created hospitals get a complimentary Professional
  subscription with no end date.
- Money in whole rupees. Period is 30 days, not a calendar month.
- Upgrades not offered during grace/expired; minimum upgrade charge ₹1.
- Proofs and receipt PDFs stored in Postgres, not on disk.
- Rate limiter fails open if Redis is down.
- Reference codes look like `NIR7K2Q9M`.
- Password minimum stays 12; route and env names follow the repo, not the brief.
- Signed-up users who never pay stay as dormant accounts; no auto-delete.
- Seat limits, staff email invites, GST breakdown, login OTP: not built.
- Website build order moved after the API (§12).

## 14. Left to the builder

Hospital-code generation rule, exact email and page copy, receipt layout,
component breakdown, Redis key names, cache TTLs, the precise list of
IPD-scope invoice routes gated under `accounts`, and how the one-time handoff
code from signup is exchanged for the BFF session (same mechanism as invite
acceptance if it fits).
