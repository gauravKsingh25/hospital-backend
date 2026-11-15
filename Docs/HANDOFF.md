# Handoff — where the Nirogya subscriptions work stands

Last updated: 2026-11-15 (after slice 5: all five slices built). Branch: `feat/subscriptions`. **Nothing from slice 1 or
the website is committed yet**; commit only when the owner says so (no
Co-Authored-By trailer).

## Read first

1. `Docs/superpowers/specs/2026-11-14-nirogya-signup-subscriptions-design.md`:
   the approved design. §5 flow, §6 data model, §8 backend, §12 delivery slices.
2. `Docs/superpowers/plans/2026-11-14-subscriptions-core.md`: slice 1's plan,
   the model for the next slice's plan.
3. `website/README.md`: the marketing site.

## Owner decisions (fixed)

- Monthly plans. **Standard ₹3,000**: OPD only. **Professional ₹8,999**: everything.
- Unlimited staff seats. One hospital per subscription.
- No trial. 7-day grace period. No refunds; a cancelled plan runs to the end of
  its paid cycle.
- Upgrades are pro-rated.
- Payment by UPI QR to `UPI_VPA` (env), checked by hand within 12 hours.
  Receipts are GST-inclusive.
- OTP by email, only at signup and for forgot-password.
- The user account is created first; the user becomes the hospital's admin only
  after the payment is approved.
- Contact address: the owner's email (in `website/src/content/site.ts`).
- The website is its own Next.js app in `website/`, never plain HTML, separate
  from `frontend/`.

## Done

- **Slice 1, subscriptions core (uncommitted).** Plans, entitlements, 402/403
  gating, nav locking. Plan: `Docs/superpowers/plans/2026-11-14-subscriptions-core.md`.
- **Website marketing pages (uncommitted).** `website/`, static export, port 3100.
  The contact form now posts JSON to `POST /api/v1/public/contact`.
- **Slice 2, signup and payments (uncommitted).** Plan:
  `Docs/superpowers/plans/2026-11-14-signup-and-payments.md`; decisions taken
  while building are the `Ruling:` lines in
  `.superpowers/sdd/2026-11-14-signup-and-payments/progress.md`.
  - Email OTP, signup → handoff, forgot password, contact inbox (`identity`).
  - UPI orders + QR + proof upload, approval/rejection, receipt PDFs, payment
    emails, console payment queue (`subscriptions`).
  - App: `/get-started`, `/forgot-password`, `/auth/handoff`,
    `/platform/payments`, contact messages under Emails.
  - The hospital is created **at approval** (spec §4), not at signup.

- **Slice 3, lifecycle (uncommitted).** Plan:
  `Docs/superpowers/plans/2026-11-14-subscription-lifecycle.md`; rulings in
  `.superpowers/sdd/2026-11-14-subscription-lifecycle/progress.md`.
  Renew / upgrade (pro-rated) / downgrade at renewal / cancel / resume,
  reminders + grace/expired emails (worker sweep), `/admin/subscription`
  (also shown in place of every screen to a lapsed hospital's admin), console
  `/platform/subscriptions` with Extend. Seed adds Demo Lapsed Clinic.

## Verification state

- Slice 1 e2e: 223/230 passed on a loaded machine; every failure re-run alone
  passes (one real tap-target bug in `needs-identification.tsx` fixed).
- Slice 2: backend tests for every task, `e2e/signup-payment.spec.ts` (4
  passing), `npm run verify` clean, website build clean.
- Local stack: the Docker `api`/`worker` images were rebuilt for `segno` and
  `reportlab`; `.env` has `UPI_VPA=nirogya.demo@okaxis` and the website origin
  in `CORS_ORIGINS`. Dev DB migrated to `e3a7c1f5d9b4`.

- After slices 2 and 3 (2026-11-14): full backend suite green (~1,625 tests);
  `npm run verify` clean; e2e `signup-payment.spec.ts` + `subscription-lifecycle.spec.ts`
  7/7. Both slices had a fresh whole-slice review; every Critical/Important
  finding fixed with a failing test first. Deferred minors are listed in each
  slice's ledger under `.superpowers/sdd/`. Dev DB at `a5c9e3b7d1f2`; re-run
  `scripts/seed_demo.py` for the lapsed demo clinic.

- After slice 4 (2026-11-14): the website's `/signup/` (details → code →
  password) calls the public API from the browser and ends at the app's
  `/auth/handoff`; pricing cards link to `/signup/?plan=…`, the nav has
  "Get started". `website/`: `npm test` (10, `node --test`) and build clean.
  e2e `website-signup.spec.ts` 3/3 (needs the website on :3100), slice 2–3
  specs 7/7, `npm run verify` clean. Plan:
  `Docs/superpowers/plans/2026-11-14-website-signup.md`.

- After slice 5, hardening (2026-11-15). Plan: `Docs/superpowers/plans/2026-11-15-hardening.md`.
  - `tests/test_isolation_walk.py` calls every API route that takes an id in
    its path or a GET query (150+), as hospital A, with hospital B's real ids:
    no write succeeds, no read answers with B's record, nothing crashes, no
    audit row or message is left in B. Kinds B has no records of (lab,
    pharmacy, claims) are walked with random ids only; the test lists them.
  - **Open, needs a decision:** ids sent *inside a request body* were not
    checked against the hospital. Postgres foreign keys ignore row-level
    security, so A could store B's patient on A's appointment. Fixed and tested
    for 8 paths (appointments, wards, note templates, messages, admissions);
    about a dozen other create/update paths likely have the same gap. See the
    final message / ledger for the options.
  - `tests/test_secrets_not_logged.py`: no code, token, password, UPI ID or
    secret is logged from signup to approval.
  - A fresh security review found one Critical (a hospital admin could make a
    custom role with platform powers and approve their own payments) and two
    Mediums (reset-code guessing; forgot-password timing). All three fixed,
    each with a failing test first. Six Lows are in the slice's ledger.
    Ten wrong codes in a day now lock an address for a day; anyone can
    trigger that (runbook has the one-line SQL to clear it).
  - Coverage on the new modules 96 %; mypy clean.
  - Owner documents: "Checking a UPI payment" in `Docs/runbook.md`; slices 2–5
    and the open questions in `Docs/DECISIONS.md`.
  - Website: `sitemap.xml` and `robots.txt` generated at build.

## What is left

- Deferred smaller findings, by slice, in `.superpowers/sdd/*/progress.md`
  (lines starting `Final: minor (deferred)`). The security Lows (L1–L6) are
  the ones to do first.
- Nothing is committed. Commit when the owner says so.

## Open questions for the owner

- The spec lets an expired hospital use "the data export route", but no data
  export exists yet. Build one (which slice)?


## Open items on the website

- The logo is a stand-in.
- No domain or canonical link yet.
- The legal pages need a lawyer's review.
- The kit in `website/public/assets` (~1,600 files) could be pruned.
