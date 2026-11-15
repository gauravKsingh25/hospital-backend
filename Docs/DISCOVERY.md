# Discovery — marketing website, self-service signup, UPI subscriptions

Date: 2026-11-13 · Phase 0 (read-only) · Branch inspected: `feat/platform-console`

## 1. Headline

The brief assumes a single-tenant HMS with no auth, invites, email or platform
console. **None of that is true here.** The repo is already multi-tenant with
database-enforced isolation, and roughly half of the brief's backend is built.
The work is therefore smaller than the brief in some places (tenancy, auth,
invites, email, platform console) and needs re-shaping in others (module names,
route names, the order of "create hospital" vs "accept invite").

## 2. What exists today

| Area | State |
|---|---|
| Backend | FastAPI 0.141, Python 3.14 venv (3.12 min), SQLModel/SQLAlchemy 2 async, asyncpg, Pydantic v2 |
| Database | **PostgreSQL** (local Docker for dev/tests, Neon for shared). Alembic, 39 revisions |
| Structure | Modular monolith, `backend/app/modules/{identity,tenancy,patients,scheduling,clinical,diagnostics,ipd,billing,notifications,reporting}`; router → service; routes under `/api/v1` |
| Multi-tenancy | **Done.** `hospitals` is the tenant table; every tenant table has `hospital_id`; Postgres RLS on all of them; app connects as a non-`BYPASSRLS` role; `require_tenant` dependency sets the context |
| Auth | **Done.** argon2id, JWT access (15 min) + rotating refresh with reuse detection, idle and max-session limits, per-account lockout, TOTP 2FA for chosen roles, forced password change |
| RBAC | Data-driven: 134 permissions, 17 roles incl. `PLATFORM_ADMIN`, `PLATFORM_SUPPORT`, `HOSPITAL_ADMIN`; `require(...)` dependency on every route |
| Audit | Append-only `audit_logs`, written on every sensitive action |
| Email | **Done.** `core/mail.py`: `SmtpMailer` (Gmail, STARTTLS, app password) and `ConsoleMailer` (writes `.eml`), chosen by `MAIL_BACKEND`; every send logged in `platform_emails` |
| Invites | **Done** for hospital admins and platform team: hashed single-use token, 48 h, resend/revoke/expire, public preview + accept that signs the person in |
| Platform console | **Backend committed, frontend uncommitted in the working tree**: `/platform` overview, hospitals (create + invite admin, suspend), invites, emails, activity, team |
| Suspension | Suspending a hospital blocks sign-in, live tokens and refresh |
| Background jobs | ARQ worker on Redis with an hourly cron (auto-close, notification retry, invite expiry) |
| Error envelope | Already `{error: {code, message, details}}` |
| Frontend | Next.js 16 App Router, React 19, shadcn/ui + Tailwind v4, TanStack Query/Table, RHF + Zod, next-intl (en + hi). Browser never calls the API: a BFF route (`/bff/*`) holds httpOnly cookies. Route guard is `src/proxy.ts` |
| UPI today | Each hospital has its own `upi_vpa` for **patient** bills; QR drawn client-side with `qrcode-generator` |
| Tests | ~1,216 pytest tests (most need Postgres); 42 Playwright specs; `npm run verify` chain |
| Not present | Per-IP rate limiting, CI workflows, coverage tooling, any PDF or server-side QR library, Jinja2, `website/`, plans/subscriptions/entitlements, self-service signup, email OTP, contact form |

## 3. Baseline (before any change)

| Check | Result |
|---|---|
| `ruff check` | clean |
| `ruff format --check` | 2 files would be reformatted |
| `mypy app` (strict) | **23 errors in 3 files** (`identity/platform_router.py`, `identity/invites.py`, `workers/tasks.py`) — 21 are the same `sqlalchemy` vs `sqlmodel` `AsyncSession` mismatch |
| `pytest` | **Not a valid baseline yet.** Docker Desktop is not running, so 950 tests skipped ("no local Postgres") and 1 failed (`test_health` cannot reach the database). The rest passed |
| Frontend `eslint`, `tsc --noEmit`, unit tests, `codegen:check` | all clean |
| Playwright | not run (needs the stack up) |

A real backend baseline needs `docker compose up postgres redis` first.

## 4. Where the brief and the repo disagree

| Brief says | Repo reality | Proposed resolution |
|---|---|---|
| Add `tenant_id`, default tenant, backfill (Phase 5) | `hospital_id` + RLS everywhere already | **Drop Phase 5.** Keep only the "every route" isolation test |
| New `tenants`, `tenant_memberships` tables | `hospitals`; a user belongs to exactly one hospital (`users.hospital_id`), email unique deployment-wide | Reuse. No tenant switching, no `tenant_id` claim change |
| New `auth/` module | `identity` | Extend `identity` |
| New subscription code in `billing/` | `billing` is the 7,000-line **patient** billing module | New platform-level module, e.g. `subscriptions`, so SaaS money and patient money never mix |
| `notifications/` owns email | `notifications` is patient messaging (WhatsApp/SMS); platform mail is `core/mail.py` + `identity` | Reuse the existing mailer and `platform_emails` log |
| `/api/public/*`, `/api/platform-admin/*` | everything is `/api/v1/...`; console is `/api/v1/platform/*` | `/api/v1/public/*` and `/api/v1/platform/*` |
| `/platform-admin/payments`, `/accept-invite`, `/settings/team`, `middleware` | `/platform/*`, `/invite/[token]`, `/admin/staff`, `proxy.ts` | Keep existing paths; add `/platform/payments`, `/platform/subscriptions`, `/admin/subscription` |
| `TENANT_ADMIN` | `HOSPITAL_ADMIN` | Reuse |
| Invite accepted → wizard creates the tenant | A hospital admin cannot exist without a hospital; console creates hospital + invite together | On approval, create the hospital from the signup's hospital name and invite its admin with the existing machinery. The "wizard" becomes a first-login step that completes the hospital profile |
| Email OTP as login second factor | TOTP authenticator 2FA exists | See question 4 |
| Staff invited by email | Staff are created by the admin with a temporary password + forced change | See question 18 |
| Password min 10 | 12 | Keep 12 |
| APScheduler / cron CLI | ARQ cron exists | Use ARQ |
| Proofs on disk at `PAYMENT_PROOF_DIR` | Logos are stored in Postgres so local and Neon behave the same | Store proofs in Postgres too (≤ 5 MB), served only through the platform route |
| Jinja2 templates | Templates are plain Python strings | Keep plain strings; no new dependency |
| Env names `JWT_SECRET`, `SMTP_USER`, `EMAIL_FROM`, `APP_URL` | `SECRET_KEY`, `SMTP_USERNAME`, `MAIL_FROM`, `APP_PUBLIC_URL` | Keep existing names; add only the new ones |
| "DB-only, no Redis" default | Redis is already required | Rate-limit with Redis |
| `docs/` with `PLAN.md`, `RUNBOOK.md` | `Docs/` with `runbook.md` and `superpowers/{specs,plans}` | Follow the existing layout |

## 5. Genuinely new work

1. **`website/`** — static Next.js marketing site, signup pages, UPI payment page, status page, legal pages, contact form.
2. **Public API** — signup start, email OTP, signup token, order creation, proof upload, status polling, contact, plan listing. First endpoints the browser calls directly, so CORS and per-IP rate limits start to matter.
3. **Subscriptions** — plans, plan features, subscriptions, payment orders, receipts, `PaymentProvider` + `ManualUpiProvider`, server-side QR, approve/reject, renewals, grace/expiry, pro-rated upgrades.
4. **Entitlements** — `require_active_subscription` (402) and `require_feature` (403) on tenant routes; `/me/entitlements`; nav locking and upgrade prompts.
5. **Console additions** — payment review queue, subscriptions list, extend/suspend.
6. **Hospital-admin additions** — subscription page (renew/upgrade QR, history, receipts), seat usage.
7. **Docs** — security review, runbook additions, "how to verify a UPI payment".

New dependencies needed: a QR library (`segno`), a PDF library (`reportlab`), `pytest-cov`.

## 6. Risks

1. **Uncommitted work.** 67 files are modified/untracked on `feat/platform-console` (the console frontend and a billing read-scope change). The new work builds directly on it. It should be finished and committed before this starts, or the two will be tangled.
2. **Locking out existing hospitals and ~1,200 tests.** A global "no active subscription → 402" gate breaks every existing hospital, the demo seed and most tests unless existing hospitals are given a subscription by migration and test fixtures create one by default.
3. **Cross-tenant rule.** CLAUDE.md lets only `identity` and `tenancy` work across tenants. Subscriptions are platform-level by nature; the new module needs the same explicit status.
4. **Manual UPI is fraud-prone by design.** A UTR proves nothing until checked against the bank statement; the approve step is the only control. Unique UTR, exact-amount orders and audit rows help; they do not replace the check.
5. **Personal UPI ID for business receipts.** Receiving subscription revenue on a personal VPA can hit bank limits, and above the GST threshold receipts must be tax invoices. A business decision, flagged, not a code problem.
6. **Gmail limits and deliverability.** ~500 recipients/day and OTP mail from a personal Gmail may land in spam. OTP volume is the first thing that would force a move to SES or similar.
7. **Feature gating granularity.** Modules depend on each other (OPD charges flow into billing; IPD needs clinical). Only leaf modules can be cleanly locked; which ones is question 2.
8. **mypy is already red** on the branch; "mypy --strict clean" as a gate needs those 23 errors fixed first.
9. **Size.** This is four projects (website, signup + payments, entitlements, console/admin UI). Each should get its own design, plan and review.
