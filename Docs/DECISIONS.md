# Decisions — Nirogya signup, subscriptions and website

What the owner decided, and the defaults taken where no answer was given.
Design: `Docs/superpowers/specs/2026-11-14-nirogya-signup-subscriptions-design.md`.

## Answered by the owner (2026-11-13 / 14)

| # | Question | Answer |
|---|---|---|
| 1 | Billing interval | Monthly for both plans |
| 2 | Modules per plan | Standard is OPD only; Professional is everything (table below) |
| 3 | Staff seats | Unlimited on both plans |
| 4 | Email OTP | Once at signup, and for forgot-password. Never at login |
| 5 | Hospitals per subscription | One |
| 6 | GST | Prices include GST; simple receipts, no GST breakdown, no GSTIN |
| 7 | Free trial | None |
| 8 | Grace period | 7 days |
| 9 | Refunds | None. Cancelling ends access at the end of the paid month |
| 10 | Brand | Product name "Nirogya". Logo to follow |
| 11 | Contact shown | The owner's Gmail, for now |
| 12 | Sending account | The owner's personal Gmail |
| 15 | UPI payee name | Default accepted: "Nirogya" |
| 16 | Verification turnaround | Within 12 hours |
| 17 | Upgrade pricing | Pro-rated difference |
| — | Order of account and payment | Create the user first; make them hospital admin only after payment is approved |
| — | Execution | Build in this session, one review at the end. No commits until asked |

### What Standard includes and locks (approved)

| Included in Standard | Professional only |
|---|---|
| Patient registration and records | IPD: admissions, wards, beds, rounds, discharge summary |
| Appointments, queue, tokens | Lab and radiology |
| Doctor consultation, notes, prescriptions | Medicines list and inpatient issue |
| OPD billing and payments at reception | Accounts dashboard and IPD bills |
| Admin setup (staff, doctors, departments, services) | Patient messaging (WhatsApp/SMS) |
| Today view on reports | Full reports |

## Defaults taken (no answer given)

| # | Question | Default used |
|---|---|---|
| 13 | Deployment target | Local for now; website and app origins set by env |
| 14 | Redis | Already a required part of the stack; used for caching and, later, rate limits |
| 18 | Staff invites by email | Not built. Admins keep creating staff with a temporary password |
| 19 | Existing hospitals | Each gets a complimentary Professional subscription with no end date |
| 10 | Domains, colours | Not given. The app's existing theme is used; domains come from env |

## Calls made while building slice 1 (subscriptions core)

- "Today view on reports" on Standard means: the dashboard for today only
  (asking for a longer window is refused), the live queue, and footfall and
  revenue for today. Occupancy, inpatient and follow-up reports are
  Professional.
- A hospital with no subscription row is refused (402), not let through.
- `GET /billing/accounts` (the cash counter's board) and
  `GET /billing/accounts/{visit}` (the bill at the desk) stay open on
  Standard: they are OPD billing. Only the accounts dashboard, IPD bill
  editor and claims are locked.
- Prescriptions on an OPD visit stay allowed on Standard. Only lab and
  radiology orders need the diagnostics feature.
- The messaging lock covers messages to patients. Messages to the hospital's
  own staff still go.
- The platform support role has no subscription permission yet; it gets read
  access with the console's subscriptions page (slice 3).
- A "month" is 30 days. Money is stored in whole rupees.
- Entitlements are returned on `/auth/me` rather than a separate
  `/me/entitlements`; `GET /subscriptions/current` exists for the hospital
  admin.

## Calls made while building the website (marketing pages, before signup)

- **Order changed with the owner's agreement:** the marketing pages were built before signup and
  payments; the signup, payment and status pages are added to the website in slice 2.
- **The website is its own Next.js app (`website/`), separate from `frontend/`.** The owner asked for a
  framework, not hand-written HTML, and for the website to be a module of its own. It is Next.js 16 +
  React 19 + TypeScript with its own `package.json`, built as a static export (`output: "export"`) to
  `website/out/`, which deploys to any static host. It shares no code or dependencies with the hospital
  app; the two meet only at the "Sign in" link (`NEXT_PUBLIC_APP_URL`). The design kit (taste-skill)
  stays as plain files in `website/public/assets`; its scripts load from a client component's effect
  after hydration, so React never discards what the kit draws. The kit's checker and finish gate run on
  the built `out/index.html`. Prices, plans and the FAQ live once in `website/src/content/site.ts`.
- **Product pictures are drawn in live HTML, not screenshots.** Screenshots of the running app showed
  test data ("Testpatient 79710441", test wards) and a broken logo tile, so they cannot be shown
  publicly. The queue, consultation note, checklist, billing feed, bed chart and role switches are
  the kit's UI fragments rewritten with realistic Indian names and the app's real screens and rules.
- **Main action "Request a demo"** until signup exists; plan buttons ("Request Standard",
  "Request Professional") also go to the demo form. They become "Choose a plan" in slice 2.
- **Demo form sends by email for now:** it opens the visitor's mail app addressed to the owner's
  Gmail. It switches to `POST /api/v1/public/contact` (saved and emailed, with a honeypot and rate
  limit) in slice 2. The address is linked ("Email us"), not printed as text.
- **No photographs:** none of the client's exist yet, and stock photographs of hospital staff would
  pass strangers off as customers. Add real photographs of a customer hospital when one agrees.
- **No backup, uptime or certification claims** on the security section: only what the code does
  today (database-level separation per hospital, audit log, authenticator-app sign-in for admins and
  doctors, 10-minute screen lock).
- **"Sign in" links to `http://localhost:3000/login`** until the app has its real address.
- **Legal pages** (privacy, terms, refunds) are drafts marked "TO BE REVIEWED by a lawyer before
  launch", written from the owner's answers (no refunds, cancel at period end, 7-day grace, manual UPI
  checks within 12 hours).
- **English only** on the website for now; the app itself is English and Hindi.

## Calls made while building slice 2 (signup and payments)

Each with what it costs if it was the wrong call. The full engineering list is in the slice's
ledger (`.superpowers/sdd/2026-11-14-signup-and-payments/progress.md`).

| Call | Cost if wrong |
|---|---|
| The hospital is created when you approve the payment, not at signup | If you wanted to see unpaid hospitals by name, they exist only as signups |
| Signup and payment emails are sent during the request, not queued | A slow mail server makes "Send the code" take a few seconds |
| An email that already has an account gets "you already have an account", never a code; the website shows the same next step either way | None: nobody can learn who has an account |
| The screenshot is uploaded separately from the UTR | Two requests from the app instead of one |
| The payment screen shows the UPI ID and payee name too, for people typing it by hand | None |
| A new administrator signs in again after approval (with two-step sign-in); the sign-in they already have keeps working up to 15 minutes | A 15-minute window without two-step sign-in, once, for the payer |
| Contact-form messages appear under Console → Emails | None |

## Calls made while building slice 3 (renewals, upgrades, reminders)

| Call | Cost if wrong |
|---|---|
| Renewing early adds a month after the current one; a plan change paid with it starts with that month | None |
| A hospital that has renewed ahead cannot upgrade until its new month starts | That hospital waits up to a month to upgrade |
| A paid plan switch lands at the next worker sweep after the month turns | The hospital keeps the higher plan a few minutes longer |
| Extending by hand counts from today when the hospital has already lapsed | None |
| Support staff can see the Subscriptions list; only you can extend | None |
| Reminders are kept only if at least one email went out; otherwise they are tried again next sweep | An administrator could get the same reminder twice if the mail server half-fails |
| Demo Lapsed Clinic is seeded with a month that ended 30 days ago | One more demo hospital |

## Calls made while building slice 4 (website signup)

| Call | Cost if wrong |
|---|---|
| Pricing and contact stay sections of the home page (`/#pricing`, `/#contact-form`), not their own pages | Two small pages to add if you want them |
| No separate "how to pay" page; the signup page lists the steps (UPI in the app, checked within 12 hours) | One static page to add |
| Prices on the website are written into the site; an automated test compares them with the app's prices | A price change in the app must be copied to `website/src/content/site.ts` |
| The signup page keeps its one-time token only in memory: a refresh starts again | The visitor retypes their details after a refresh |
| A terms and privacy line sits above "Send the code" | None |

## Calls made while hardening (slice 5)

| Call | Cost if wrong |
|---|---|
| One test calls every API route with an id, as one hospital, with another hospital's real ids; no write may succeed and no read may return the other's records | A route that checks a full request body before the hospital could pass it; database row-level security is the backstop |
| The development mail backend prints whole emails (codes included) to the log; production refuses that backend | A staging server left on the development backend would log signup codes |
| Sitemap and robots are generated at build time from `NEXT_PUBLIC_SITE_URL` | Building with the wrong site address publishes wrong sitemap links |
| A hospital role can never carry a platform permission (approve payments, extend subscriptions, read every hospital); one that somehow does opens nothing | A hospital cannot be given a support-style role; that needs a platform account |
| Ten wrong email codes in a day close that address for new codes until the oldest ages out | Anyone who knows an address can lock its signup or password reset for a day, again and again; you clear it with one SQL line (runbook). A reset link with a long token would remove both the guessing and the lockout |
| The forgot-password email is sent just after the answer, not during it | A mail failure is no longer seen by the person asking; it is in Console → Emails |

## Open for the owner

- **Data export for an expired hospital:** the design allows "the data export route", but no export
  exists yet. Build one, and when?
- **Separate `/pricing` and `/contact` pages, and a "how to pay" page** on the website: not built
  (see slice 4). Wanted?
- **Legal pages:** still drafts for a lawyer to review before launch.
