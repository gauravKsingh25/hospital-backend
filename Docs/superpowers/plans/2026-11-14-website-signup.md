# Website signup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A visitor goes from the website's pricing cards through `/signup` (details → emailed code → password) and lands signed in on the app's `/get-started`, proven by one Playwright run across both apps.

**Architecture:** The website stays a static export. One pure module (`src/lib/signup.ts`) owns validation, the JSON calls to the public API and the mapping from API errors to sentences; one client component (`SignupFlow`) owns the three steps and keeps the signup token in memory only. The last step sends the browser to the app's existing `/auth/handoff?code=…`, which already turns the code into a session (slice 2).

**Tech Stack:** Next.js 16 static export, React 19, TypeScript; `node --test` (Node 24 strips types) for the pure module; Playwright in `frontend/e2e` for the cross-app flow.

**Spec:** `Docs/superpowers/specs/2026-11-14-nirogya-signup-subscriptions-design.md` (§5 flow, §10 website, §11 full-flow test, §12.4)

## Global Constraints

- `website/` is its own Next.js app; it shares no code with `frontend/` and has no client data library: plain `fetch`.
- No new runtime dependency in `website/` (no Zod, no form library). Validation is hand-written and mirrors the server: name and hospital 2–200 characters, email with `@`, mobile ≤ 20, code `^\d{6}$`, password ≥ 12 characters (`PASSWORD_MIN_LENGTH`).
- Endpoints (all under `${SITE.apiUrl}/api/v1/public`): `signup/start` → 202 `{status:"sent"}`; `signup/resend` → 202; `signup/verify` `{email, code}` → `{signup_token, expires_in}`; `signup/complete` `{signup_token, password}` → `{handoff_code, expires_in}`. Errors are `{"error": {"code", "message"}}`; 429 carries `Retry-After` seconds.
- Error codes the page must handle: `otp_invalid`, `signup_token_invalid`, `weak_password`, `plan_unknown`, `validation_error`, `rate_limited` (status 429), network failure.
- The signup token lives in component state only: never in `localStorage`, `sessionStorage`, a cookie or the URL.
- `signup/start` must not reveal whether the email has an account: the page shows the same next step either way.
- Plan codes: `standard`, `professional`. The page preselects from `?plan=`; anything else falls back to `professional`.
- Copy is plain and short, matches the site's voice; the pricing note "checked by hand within 12 hours" stays.
- Never git commit (owner's standing rule); the plan's commit steps are skipped.

## Review Focus

1. A visitor who refreshes or comes back on the code step: the token is gone, so the page must start them on details again with their typed fields kept where possible, never a dead form. Covered: Task 2 resets to details on `signup_token_invalid`.
2. The rate limiter (10 starts per IP window) answering 429 to a real person: the page must say how long to wait, not "something went wrong". Covered: Task 1 `messageFor` test with `retryAfter`.
3. The API down or CORS blocking the browser (the site's origin missing from `CORS_ORIGINS`): `fetch` throws; the page must say it could not reach Nirogya and keep the typed values. Covered: Task 1 network-failure test.
4. Prices on the website drifting from the `plans` table: the full-flow e2e compares each card's price to `GET /public/plans`. Covered: Task 4.
5. Double-clicking "Create account" sending `complete` twice (the second fails with `signup_token_invalid` after the first already succeeded): buttons disable while a request is in flight. Covered: Task 2 (busy state) and the Task 4 run.

---

### Task 1: The signup client module

**Files:**
- Create: `website/src/lib/signup.ts`
- Test: `website/src/lib/signup.test.ts`
- Modify: `website/package.json` (script `"test": "node --test src/lib/"`), `website/tsconfig.json` (exclude `**/*.test.ts`)

**Interfaces:**
- Produces:
  - `type PlanCode = "standard" | "professional"`; `planFrom(search: string): PlanCode`
  - `type Details = { full_name: string; email: string; phone: string; hospital_name: string; plan_code: PlanCode }`
  - `validateDetails(d: Details): Partial<Record<keyof Details, string>>` (empty object = valid)
  - `validateCode(code: string): string | null`; `validatePassword(password: string, confirm: string): string | null`
  - `type ApiResult<T> = { ok: true; data: T } | { ok: false; status: number; code: string; message: string; retryAfter: number | null }`
  - `postJson<T>(apiUrl: string, path: string, body: unknown, fetchImpl?: typeof fetch): Promise<ApiResult<T>>` — status 0 and code `"network"` when `fetch` throws.
  - `messageFor(result: Extract<ApiResult<unknown>, { ok: false }>): string`
  - `handoffUrl(appUrl: string, code: string): string` → `${appUrl}/auth/handoff?code=${encodeURIComponent(code)}`

- [ ] **Step 1: Write the failing tests** (`node:test` + `node:assert/strict`, a fake `fetchImpl`):
  - `planFrom("?plan=standard") === "standard"`, `planFrom("?plan=gold") === "professional"`, `planFrom("") === "professional"`.
  - `validateDetails` with blank name → has `full_name`; `"a@b"`-less email `"nobody"` → has `email`; valid details → `{}`; phone `"9".repeat(21)` → has `phone`.
  - `validateCode(" 123456 ")` → `null`; `"12345"` → a message; `"12a456"` → a message.
  - `validatePassword("short", "short")` → mentions `12`; `"x".repeat(12)` vs different confirm → a "do not match" message; equal 12+ → `null`.
  - `postJson` on 202 JSON → `{ok:true,data}`; on 400 `{"error":{"code":"otp_invalid","message":"…"}}` → `{ok:false,status:400,code:"otp_invalid"}`; on 429 with header `Retry-After: 120` → `retryAfter === 120`; `fetchImpl` that throws → `{ok:false,status:0,code:"network"}`.
  - `messageFor` on 429 `retryAfter 120` contains `"2 minutes"`; on `network` contains `"could not reach"` (case-insensitive); on `weak_password` returns the server's message unchanged.
  - `handoffUrl("http://app", "a/b+c")` → `"http://app/auth/handoff?code=a%2Fb%2Bc"`.

- [ ] **Step 2: Run to see them fail**
  Run: `cd website && npm test`  Expected: FAIL, module not found.

- [ ] **Step 3: Implement `website/src/lib/signup.ts`** — no imports from the rest of the site (keeps it runnable under `node --test`); `messageFor` rounds `retryAfter` up to whole minutes ("1 minute"/"N minutes").

- [ ] **Step 4: Run to see them pass**
  Run: `cd website && npm test && npm run typecheck`  Expected: all pass, tsc clean.

### Task 2: The `/signup` page

**Files:**
- Create: `website/src/app/signup/page.tsx` (server component: metadata, nav, footer, heading, the flow), `website/src/components/SignupFlow.tsx` (`"use client"`)
- Modify: `website/src/styles/page.css` (only what the kit lacks: step indicator, code input width), `website/src/content/site.ts` (`PLANS[i].code: PlanCode`)

**Interfaces:**
- Consumes: everything Task 1 produces; `SITE.apiUrl`, `SITE.appUrl`, `PLANS`.
- Produces: the page at `/signup/` with these accessible names, used by Task 4:
  - step 1: labels `Your name`, `Email`, `Mobile (optional)`, `Hospital or clinic`; a radio group `Plan` with options `Standard ₹3,000 a month` / `Professional ₹8,999 a month`; button `Send the code`.
  - step 2: text "We sent a 6-digit code to <email>"; label `Code`; buttons `Check the code`, `Send a new code` (disabled 60 s after each send, showing the countdown), `Change details`.
  - step 3: labels `Password`, `Password again`; button `Create account`; note "At least 12 characters."
  - errors in one `role="alert"` element per step; field errors linked by `aria-describedby`.

- [ ] **Step 1:** Add `code` to each `PLANS` entry and build `SignupFlow` as a three-state component (`"details" | "code" | "password"`) with a `busy` flag that disables the step's buttons while a request is in flight. Behaviour:
  - Plan from `planFrom(window.location.search)` in an effect (static export: no `useSearchParams` Suspense boundary needed).
  - `signup/start` 202 → step 2 regardless of whether the email exists (the email itself tells an existing user to sign in).
  - `signup/verify` ok → keep `signup_token` in state → step 3. `otp_invalid` → message, stay.
  - `signup/complete` ok → `window.location.assign(handoffUrl(SITE.appUrl, handoff_code))`; keep `busy` true so the button stays disabled during navigation. `signup_token_invalid` → back to step 1 with fields kept and the server's message shown.
  - Any other failure → `messageFor(result)` in the step's alert, values kept.
  - Focus moves to the new step's heading on each change (screen readers hear the step).
- [ ] **Step 2:** `page.tsx`: `export const metadata` with title "Start with Nirogya" and a description; a short side column: what happens next (code by email → password → pay by UPI in the app → we check within `SITE.paymentCheckHours` hours), and "Already have an account? Sign in" → `${SITE.appUrl}/login`.
- [ ] **Step 3: Verify**
  Run: `cd website && npm run typecheck && npm run build`  Expected: clean; `out/signup/index.html` exists.

### Task 3: Links into signup

**Files:**
- Modify: `website/src/components/sections/PricingSection.tsx`, `website/src/components/SiteNav.tsx`, `website/src/components/sections/Hero.tsx` (only if it has a primary CTA), `website/src/content/site.ts` (`action` copy), `website/src/app/sitemap.ts` if one exists (add `/signup/`)

- [ ] **Step 1:** Each plan card's button → `/signup/?plan=<code>`, text `Start with Standard` / `Start with Professional`; keep `Ask which plan fits` → `#contact-form`. Nav actions gain `Get started` → `/signup/` beside `Sign in`; `Request a demo` stays. Nav and footer links on `/signup` and the legal pages that point at `#section` become `/#section` so they work off the home page.
- [ ] **Step 2: Verify**
  Run: `cd website && npm run build` and `grep -c 'signup/?plan=' out/index.html`  Expected: build clean, count ≥ 2.

### Task 4: The full-flow e2e across website and app

**Files:**
- Create: `frontend/e2e/website-signup.spec.ts`
- Modify: `frontend/e2e/signup-support.ts` (export `SITE = process.env.E2E_SITE_URL ?? "http://localhost:3100"`; update the header comment that says the pages arrive in slice 4)

- [ ] **Step 1: Write the specs**
  - `a visitor signs up on the website and lands signed in, ready to pay`: go to `${SITE}/`; for each plan from `GET ${API}/public/plans`, expect the pricing section to contain its price formatted `₹8,999`; click `Start with Professional`; expect the `Professional ₹8,999 a month` radio checked; fill details with `unique("web")`; `Send the code`; fill `Code` from `codeFor(email)`; `Check the code`; fill both password fields with `PASSWORD`; `Create account`; expect URL `/get-started` on the app's origin; expect `amount` to show `8,999`; send a UTR; owner approves through `POST /platform/payments/{id}/approve`; expect the page to show the approved state and reach the app (reuse the assertions in `signup-payment.spec.ts`).
  - `a wrong code is refused and the visitor can try again`: on step 2 enter `000000` (retry with `111111` if the real code happens to be `000000`), expect the alert to say the code is not valid, then the real code works.
  - `an email that already has an account sees the same next step`: start with `ACCOUNTS.admin`'s email; expect step 2's "We sent a 6-digit code" text (no hint the account exists).
- [ ] **Step 2: Run them** (website dev server on 3100, app on 3000, API in Docker)
  Run: `cd frontend && npx playwright test e2e/website-signup.spec.ts`  Expected: 3 passed.
- [ ] **Step 3: Regression**
  Run: `cd frontend && npx playwright test e2e/signup-payment.spec.ts e2e/subscription-lifecycle.spec.ts && npm run verify`  Expected: all pass, exit 0.

### Task 5: Docs

**Files:**
- Modify: `website/README.md` (signup page, `npm test`, the "Not done yet" list), `Docs/HANDOFF.md` (slice 4 done, slice 5 next), `Docs/runbook.md` (the website must list `NEXT_PUBLIC_APP_URL`/`NEXT_PUBLIC_API_URL`, and its origin must be in `CORS_ORIGINS`, or signup fails with "could not reach")

- [ ] **Step 1:** Write the three updates.
- [ ] **Step 2: Verify**
  Run: `cd website && npm test && npm run build`  Expected: pass.
