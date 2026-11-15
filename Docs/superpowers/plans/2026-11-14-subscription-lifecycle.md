# Subscription Lifecycle — Implementation Plan (slice 3 of 5)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A paying hospital can renew, upgrade (pro-rated), schedule a downgrade, cancel and resume from `/admin/subscription`; it is reminded before its month ends, keeps working through a 7-day grace, and is locked to the subscription screen once expired; the platform owner sees every subscription and can extend one.

**Architecture:** Pure period maths in `subscriptions/periods.py` (fake clock in tests). Slice 2's order pipeline gains the `RENEWAL` and `UPGRADE` purposes — same QR, UTR, queue and receipt — and approval applies the maths. A worker sweep sends idempotent reminders (7/3/1 days, grace, expired) and refreshes the stored status hint; access itself stays computed from the clock (slice 1). The app gets one subscription screen that also renders inside the "subscription ended" shell, so an expired admin can always pay.

**Tech Stack:** as slice 2. No new dependencies.

**Spec:** `Docs/superpowers/specs/2026-11-14-nirogya-signup-subscriptions-design.md` (§2 goals, §6 `subscription_reminders`, §7 period maths, §8 routes, §9 `/admin/subscription`, §11 periods tests, §12 slice 3). Previous: `2026-11-14-signup-and-payments.md`.

## Global Constraints

- Everything in slice 2's Global Constraints still holds (no commits; rulings ledgered; gates).
- Period = 30 days (`plans.interval_days`), not a calendar month. Money whole rupees, server-priced.
- **Renewal:** start = later of current `current_period_end` and approval time; end = start + interval. Applies `pending_plan_code` (the renewal order is priced on it). Clears `cancel_at_period_end`.
- **Upgrade:** amount = `ceil((new − old price) × remaining_days / interval_days)`, minimum ₹1, remaining days as a fraction (seconds / 86400) at order time. Period end unchanged; features switch on approval. Refused (`upgrade_unavailable`) in GRACE/EXPIRED/CANCELLED, for complimentary subscriptions, or to a plan of equal/lower rank.
- **Downgrade:** sets `pending_plan_code`; nothing else changes until renewal. Refused unless the target ranks lower. Undo = choose the current plan again.
- **Cancel:** `cancel_at_period_end = true`; no reminders; at period end → CANCELLED (no grace). **Resume** clears it while the period is still running.
- Complimentary subscriptions (`current_period_end IS NULL`) never renew, upgrade or cancel from the app (`subscription_complimentary`).
- Reminders at 7, 3 and 1 days before period end, one email each to every active `HOSPITAL_ADMIN`, idempotent via `subscription_reminders (subscription_id, period_end, days_before)` unique. `days_before = 0` marks the "you are in grace" email, `-1` the "expired" email. None for cancelled or complimentary.
- Order routes: `NEW` stays pending-user only; `RENEWAL`/`UPGRADE` need `subscription:manage` and a hospital. A hospital has at most one live (AWAITING/SUBMITTED/REJECTED) order of each purpose.
- New error codes: `upgrade_unavailable` (409), `downgrade_unavailable` (409), `subscription_complimentary` (409), `renewal_unavailable` (409).
- Email kinds added: `SUBSCRIPTION_REMINDER`, `SUBSCRIPTION_GRACE`, `SUBSCRIPTION_EXPIRED`.
- All strings en + hi.
- Not in this slice: the spec lets an expired hospital use "the data export route", but no data export exists in the codebase yet. Raised with the owner; not invented here.

## Review Focus

1. Approving a renewal for a hospital already in grace or expired — expect the new period to start at approval, not at the old end (no paid days lost to the lapse window), and access restored on the next request (cache invalidated). Test in Task 3.
2. An upgrade order raised on day 1 and approved on day 2 — expect the approved amount to stand (the owner checked that amount) and the plan to switch; the period end unchanged. Test in Task 3.
3. The sweep running twice in the same hour, or two workers at once — expect each reminder sent once (unique row inserted before the email). Test in Task 4.
4. An expired hospital's admin opening any app screen — expect the renewal panel, not a dead "contact your administrator" page; a non-admin still sees the contact message. Test in Task 6 (e2e).
5. A downgrade scheduled, then a renewal — expect the renewal priced at the lower plan and the plan switched only at approval. Test in Task 3.

---

## File map

Backend new: `app/modules/subscriptions/periods.py`, `app/modules/subscriptions/lifecycle.py` (change-plan, cancel, resume, extend, reminders sweep), migration `…_subscription_lifecycle.py` (`subscription_reminders`, email kinds), tests `test_periods.py`, `test_lifecycle.py`, `test_api_lifecycle.py`, `test_reminders.py`.
Backend modified: `subscriptions/{models,orders,approval,emails,router,platform_router,schemas,queue}.py`, `identity/models.py` (email kinds), `workers/tasks.py`.
Frontend new: `src/app/(app)/admin/subscription/page.tsx`, `src/features/subscriptions/{subscription-manager,order-panel}.tsx`, `src/app/(app)/platform/subscriptions/page.tsx`, `src/features/platform/subscriptions-screen.tsx`, `e2e/subscription-lifecycle.spec.ts`. Modified: `get-started-screen.tsx` (extract `OrderPanel`), `(app)/layout.tsx` + `subscription-ended.tsx` (admin sees the manager), admin sections, platform nav, banner link, i18n.

---

### Task 1: Period maths (pure)

**Files:** Create `subscriptions/periods.py`, `tests/test_periods.py`.

**Produces:** `renewal_period(*, current_end: datetime | None, approved_at: datetime, interval_days: int) -> tuple[datetime, datetime]`; `upgrade_amount(*, old_price: int, new_price: int, now: datetime, period_end: datetime, interval_days: int) -> int`; `reminder_due(*, period_end: datetime, now: datetime, grace_days: int, cancelled: bool) -> int | None` (returns the `days_before` bucket now due — 7, 3, 1, 0, -1 — or None).

- [ ] **Step 1: Failing tests:** `test_early_renewal_extends_from_the_old_end`, `test_late_renewal_starts_at_approval`, `test_upgrade_pro_rates_and_rounds_up` (8999−3000, 15 days left of 30 → 3000; 10.5 days → `ceil(5999×10.5/30)` = 2100), `test_upgrade_minimum_is_one_rupee`, `test_reminder_buckets` (8 days → None, 6.9 → 7, 2.5 → 3, 0.5 → 1, −0.1 → 0, −7.1 → −1, cancelled → None).
- [ ] **Step 2–4:** FAIL → implement → PASS; gates.

### Task 2: Schema and order purposes

**Files:** migration (`subscription_reminders` with unique `(subscription_id, period_end, days_before)`; `ALTER TYPE platformemailkind ADD VALUE` ×3), `subscriptions/models.py` (`SubscriptionReminder`), `identity/models.py`, `orders.py`, `router.py`, `schemas.py`; tests in `test_lifecycle.py`, `test_api_lifecycle.py`.

**Produces:** `orders.create_order` accepts `RENEWAL` (plan = `pending_plan_code` or current; amount = that plan's price; hospital admin only; refused for complimentary) and `UPGRADE` (`plan_code` required, amount from `periods.upgrade_amount`). `POST /subscriptions/orders` routes by purpose: NEW → pending user; others → `subscription:manage` + tenant. `GET /subscriptions/history` → `Page[PaymentOrderRead]` for the hospital (`subscription:read`). `GET /subscriptions/upgrade-quote?plan_code=` → `{amount_inr, period_end}`.

- [ ] **Step 1: Failing tests:** `test_admin_raises_a_renewal_at_the_plan_price`, `test_renewal_after_downgrade_is_priced_lower`, `test_upgrade_order_is_pro_rated`, `test_upgrade_in_grace_is_refused`, `test_receptionist_cannot_raise_a_renewal` (403), `test_pending_user_cannot_raise_a_renewal` (403), `test_complimentary_hospital_cannot_renew`, `test_history_lists_the_hospitals_orders_only`, `test_expired_hospital_reaches_the_order_routes` (402 gate lets `/subscriptions/*` through).
- [ ] **Step 2–4:** FAIL → implement → PASS; migration up/down/up; gates.

### Task 3: Approval of renewals and upgrades; plan changes; cancel/resume; extend

**Files:** `approval.py`, `lifecycle.py`, `emails.py`, `router.py`, `platform_router.py`, `queue.py`; tests `test_lifecycle.py`, `test_api_lifecycle.py`.

**Produces:** `approve_once` handles RENEWAL (renewal_period, plan = order plan, clear pending + cancel) and UPGRADE (plan switch, period kept); receipt period = the period paid for. `lifecycle.schedule_change(session, *, hospital_id, plan_code, actor)`, `cancel(...)`, `resume(...)`, `extend(session, *, subscription_id, days: 1–365, reason, actor)`. Routes: `POST /subscriptions/change-plan {plan_code}`, `POST /subscriptions/cancel`, `POST /subscriptions/resume` (`subscription:manage`); `GET /platform/subscriptions` (paginated, `?status=`; hospital name, plan, status, period end, complimentary, pending plan) and `POST /platform/subscriptions/{id}/extend {days, reason}` (`platform:subscription_manage`). Approval email wording per purpose. Every write audited.

- [ ] **Step 1: Failing tests:** `test_renewal_in_grace_starts_at_approval_and_restores_access` (Review Focus 1), `test_early_renewal_appends_thirty_days`, `test_upgrade_switches_features_keeps_period_end` (Standard admin gets 200 on `/ipd/wards` after), `test_late_upgrade_approval_keeps_the_ordered_amount` (Review Focus 2), `test_downgrade_applies_only_at_renewal` (Review Focus 5), `test_cancel_then_period_end_is_cancelled_without_grace`, `test_resume_clears_cancel`, `test_extend_moves_period_end_and_audits`, `test_support_cannot_extend`.
- [ ] **Step 2–4:** FAIL → implement → PASS; gates.

### Task 4: Reminder and status sweep

**Files:** `lifecycle.py` (`send_due_reminders(session, now) -> dict[str, int]`), `emails.py` (reminder/grace/expired), `workers/tasks.py` (call in `run_sweeps`); `tests/test_reminders.py`.

**Produces:** for every non-complimentary subscription: compute `reminder_due`; insert the `SubscriptionReminder` row first (ON CONFLICT DO NOTHING) and only email when the insert happened; refresh the stored `status` via `write_subscription` when the computed one differs.

- [ ] **Step 1: Failing tests:** `test_seven_day_reminder_goes_to_every_admin_once`, `test_running_twice_sends_once` (Review Focus 3, includes two concurrent sessions), `test_cancelled_hospital_gets_no_reminder`, `test_grace_and_expired_emails`, `test_stored_status_follows_the_clock`.
- [ ] **Step 2–4:** FAIL → implement → PASS; gates; full backend suite.

### Task 5: App — `/admin/subscription` and the ended shell

**Files:** see file map.

**Produces:** `OrderPanel` extracted from `GetStartedScreen` (Payment / UnderReview / Approved / Expired states, purpose-aware copy). `SubscriptionManager`: plan, status chip, period end + days left, pending downgrade note, buttons Renew / Upgrade (shows quote) / Switch to Standard at renewal / Cancel / Resume (each shown only when allowed), the live order's `OrderPanel`, payment history with receipt links. `/admin/subscription` renders it (`subscription:read`; buttons need `subscription:manage`); admin sections card; the grace banner links here; `SubscriptionEnded` renders `SubscriptionManager` for `subscription:manage` holders.

- [ ] **Step 0:** `scripts/seed_demo.py` gains a third tenant, **Demo Lapsed Clinic** (Standard, paid), whose `current_period_end` the seed sets to 30 days ago on every run (so it is always EXPIRED), with `lapsed-admin@demo.hospital` and `lapsed-reception@demo.hospital`. Add both to `ACCOUNTS` in `e2e/support.ts`.
- [ ] **Step 1:** Playwright `e2e/subscription-lifecycle.spec.ts` (fails first): (a) a hospital signed up and approved inside the spec (slice 2's API helpers, moved to `e2e/support.ts`) opens `/admin/subscription`, sees Standard and its period end; the upgrade quote shows; raising the upgrade shows a `NIR` reference; (b) `lapsed-admin` opening `/reception` sees the renewal panel with ₹3,000, and `lapsed-reception` sees "contact your administrator".
- [ ] **Step 2–4:** FAIL → implement → PASS; `npm run verify`.

### Task 6: Console — subscriptions page

**Files:** `src/app/(app)/platform/subscriptions/page.tsx`, `src/features/platform/subscriptions-screen.tsx`, nav item, i18n.

**Produces:** table (hospital, plan, status chip, period end / "never", pending plan, complimentary badge), status filter tabs, an Extend dialog (days + reason) for `platform:subscription_manage`.

- [ ] **Step 1:** extend the lifecycle e2e: the platform owner opens Subscriptions, finds the hospital the spec created, extends it by 7 days with a reason; the row's period end moves by 7 days. (The lapsed demo clinic is left lapsed so the spec can be re-run.)
- [ ] **Step 2–4:** FAIL → implement → PASS; `npm run verify`.

### Task 7: Docs and full run

- [ ] README (renew/upgrade/downgrade/cancel), runbook ("A hospital says it renewed but is still locked"; "Extending a subscription by hand"), `.env.example` unchanged check, HANDOFF for slice 4.
- [ ] Backend full suite, `npm run verify`, the two new e2e specs plus `plan-gating.spec.ts`. Whole-slice review; fix; report. No commit.
