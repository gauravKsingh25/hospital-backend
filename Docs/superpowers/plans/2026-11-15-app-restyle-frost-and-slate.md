# App Restyle (Frost and Slate) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restyle every screen of the hospital app (`frontend/`) to direction A, "Frost and slate":
- palette, type, corners, borders and status chips change;
- no new features, no new motion, no new dependencies.

**Architecture:** The app is already driven by the theme tokens in `globals.css`. The work goes in
this order:
1. Retune the tokens and fonts.
2. Restyle the shared `components/ui` pieces and the status chip in place.
3. Add two presentational components, `Panel` and `PageHeader`.
4. Sweep the screens, group by group, onto those components.

A Node style-guard test locks each swept area so that it cannot drift back.

**Tech Stack:** Next.js 16, React 19, Tailwind v4, shadcn/ui, next-intl, `node --test`, Playwright.

**Spec:** `Docs/superpowers/specs/2026-11-15-app-restyle-frost-and-slate-design.md`

## Global Constraints

- **Cosmetic only.**
  - No workflow, copy, route, prop-contract or data change.
  - Visible text, roles and labels stay identical, because e2e selectors depend on them.
- **No new animation.**
  - Do not add `animate-*`, `transition-*` beyond colour transitions, or keyframes.
  - The mockup's spinning step dot and progress bar are NOT built.
  - Keep the `prefers-reduced-motion` block.
- **No new runtime dependency.** Fonts come only through `next/font/google`, served from the app
  (the CSP stays `font-src 'self'`).
- **Fonts:**
  - Instrument Sans 400/500/600 as `--font-latin`;
  - Noto Sans Devanagari stays as `--font-devanagari`;
  - DM Mono 400/500 as `--font-mono`.
- **Floors:** body text 16px; nothing smaller than 13px (`--text-xs` stays `0.8125rem`);
  `--tap-target` stays `2.75rem`; the focus ring stays visible.
- **Contrast:** every text and chip token pair is at least 4.5:1, and body ink on the background is
  at least 7:1.
- **Light only.** Retune `.dark` to the same hues, but do not expose a switch.
- **Print views:** white ground and black ink; only the type changes.
- **No git commits.** The owner commits manually. The plan has no commit steps: leave changes
  uncommitted on the current branch (`version4`).
- **Line endings:** any patch script writes with `newline="\n"`.

## Review Focus

1. **Hindi screens** (`hi` locale): Devanagari still renders in Noto in the same line as Latin
   drug names. Instrument Sans has no Devanagari, so the stack order must stay Latin first, then
   Devanagari. Covered by the font test in Task 1 and the `hi` screenshots in Task 8.
2. **Phone width (390px):** panels and page headers wrap, with no sideways scroll and actions
   still reachable. Covered by the `noSidewaysScroll` assertion on every screen in Task 8.
3. **Status chips on the DECEASED, LAMA and REFERRED_OUT terminals** stay visibly distinct from
   "closed" and "cancelled". Covered by the chip-tone test in Task 2.
4. **Printed bill, token slip and discharge summary:** no tinted grounds or grey ink leak into
   print. Covered by the print-path guard in Task 7 and the existing print e2e specs.
5. **Disabled and invalid form fields** stay distinguishable (`aria-invalid` red border, disabled
   at reduced opacity) under the lighter borders. Covered by the contrast pair `critical on card`
   in Task 1, and by the visual review in Task 8.

---

## File Structure

**Create:**
- `frontend/src/components/panel.tsx`: `Panel`, `PanelHeader`, `PanelBody`. Presentational only.
- `frontend/src/components/page-header.tsx`: `PageHeader`.
- `frontend/src/lib/theme.test.mjs`: parses `globals.css`, checks the token values and contrast.
- `frontend/src/lib/style-guard.test.mjs`: scans the swept folders for hand-rolled boxes, raw
  palette classes, ad-hoc `<h1>` and new animations.
- `frontend/e2e/restyle-screenshots.spec.ts`: photographs every screen group at 1440 and 390
  wide, in `en` and `hi`.

**Modify:**
- `frontend/src/app/globals.css`
- `frontend/src/app/layout.tsx`
- `frontend/src/components/ui/*.tsx`
- `frontend/src/components/status-chip.tsx`
- `frontend/src/components/app-shell/*.tsx`
- the screens under `frontend/src/app/**` and `frontend/src/features/**`, task by task.

---

### Task 1: Tokens, fonts and the theme test

**Files:**
- Create: `frontend/src/lib/theme.test.mjs`
- Modify: `frontend/src/app/globals.css` (the `:root`, `.dark`, `@theme inline` and `@layer base`
  blocks)
- Modify: `frontend/src/app/layout.tsx` (the font imports and `themeColor`)

**Interfaces:**
- Produces, as CSS variables:
  - `--band`, `--border-soft` and `--shadow-panel`;
  - the Tailwind colours `bg-band` and `border-border-soft`, plus the utility `shadow-panel`
    (through `@theme inline`: `--color-band`, `--color-border-soft`, `--shadow-panel`);
  - the status tones `--s-wait`, `--s-wait-bg`, `--s-doc`, `--s-doc-bg`, `--s-lab`, `--s-lab-bg`,
    `--s-bill`, `--s-bill-bg`, each exposed as `--color-s-*`.
- Produces `.tabular` = DM Mono plus tabular figures. Every existing `.tabular` number turns mono
  without touching its file.
- Ruling: the `:root` tokens are written as hex (`#rrggbb`), not oklch, so that the test can
  compute contrast without a colour library. Tailwind v4's `/opacity` modifiers work on hex
  variables through `color-mix`.

- [ ] **Step 0: Record the bundle baseline**

Run: `cd frontend && npx next build > ../.superpowers/sdd/restyle-baseline-build.log 2>&1`
Then write the first-load JS of `/reception` and the shared-chunk size into the ledger, so Task 8
can compare against them.

- [ ] **Step 1: Write the failing test**

`frontend/src/lib/theme.test.mjs` reads `../app/globals.css` and `../app/layout.tsx` as text.
- A helper `rootToken(name)` returns the hex value inside the first `:root {…}` block.
- A helper `contrast(a, b)` computes the WCAG 2.x relative-luminance ratio.

Tests:

```js
test("frost and slate tokens", () => {
  assert.equal(rootToken("background"), "#f7f9fa");
  assert.equal(rootToken("card"), "#ffffff");
  assert.equal(rootToken("sidebar"), "#f0f4f5");
  assert.equal(rootToken("muted"), "#ecf0f2");
  assert.equal(rootToken("band"), "#f4f7f8");
  assert.equal(rootToken("foreground"), "#1f3038");
  assert.equal(rootToken("muted-foreground"), "#5b6b72");
  assert.equal(rootToken("border"), "#dbe2e5");
  assert.equal(rootToken("border-soft"), "#e9eef0");
  assert.equal(rootToken("primary"), "#0d7480");
  assert.equal(rootToken("ring"), "#0d7480");
  assert.equal(rootToken("accent"), "#dcefed");
  assert.equal(rootToken("accent-foreground"), "#0d5a63");
  assert.equal(rootToken("success"), "#1f8a5b");
  assert.equal(rootToken("radius"), "0.75rem");
  assert.equal(rootToken("tap-target"), "2.75rem");
});
test("body ink is AAA on every ground", () => {
  for (const g of ["background", "card", "sidebar", "band"])
    assert.ok(contrast(rootToken("foreground"), rootToken(g)) >= 7, g);
});
test("every text pair is at least AA", () => {
  const pairs = [["muted-foreground","background"],["muted-foreground","card"],["muted-foreground","muted"],
    ["primary-foreground","primary"],["accent-foreground","accent"],["critical","card"],
    ["critical-foreground","critical"],["caution-foreground","caution"],["success-foreground","success"],
    ["info-foreground","info"],["s-wait","s-wait-bg"],["s-doc","s-doc-bg"],["s-lab","s-lab-bg"],["s-bill","s-bill-bg"]];
  for (const [fg, bg] of pairs) assert.ok(contrast(rootToken(fg), rootToken(bg)) >= 4.5, `${fg} on ${bg}`);
});
test("fonts: Instrument Sans + Devanagari stack, DM Mono for numbers", () => {
  assert.match(layout, /Instrument_Sans/);
  assert.match(layout, /DM_Mono/);
  assert.match(layout, /Noto_Sans_Devanagari/);
  assert.doesNotMatch(layout, /\bInter\b/);
  assert.match(css, /--font-sans:\s*var\(--font-latin\),\s*var\(--font-devanagari\)/);
  assert.match(css, /--font-mono:\s*var\(--font-dm-mono\)/);
  assert.match(css, /\.tabular\s*\{[^}]*font-family:\s*var\(--font-mono\)/);
});
test("no keyframes added to the theme", () => {
  assert.doesNotMatch(css, /@keyframes/);
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd frontend && node --test src/lib/theme.test.mjs`
Expected: FAIL on the first token assertion, because `background` is still oklch.

- [ ] **Step 3: Implement the tokens and fonts**

In `globals.css`:
- `:root` takes the spec's token table as hex.
- `--critical`, `--caution`, `--info` and their foregrounds become hex values, tuned to pass the
  pairs above. The builder picks the exact shades; keep the hues red, amber and blue.
- Add `--band`, `--border-soft`, `--shadow-panel: 0 1px 2px rgb(31 48 56 / .05)` and the eight
  `--s-*` tones from the spec.
- Set `--input: #cfd8dc` and `--secondary`/`--muted: #ecf0f2`. The `--sidebar-*` tokens mirror
  `:root`: `--sidebar-accent` is `#ffffff`, the white surface of the active item.
- The `.dark` block gets the same hues on a dark slate ground.
- Base layer:
  - `h1, h2, h3 { font-weight: 500; letter-spacing: -0.01em }`;
  - `.tabular` gains `font-family: var(--font-mono)`.

In `layout.tsx`:
- import `Instrument_Sans` (weights `400`, `500`, `600`; variable `--font-latin`) and `DM_Mono`
  (weights `400`, `500`; variable `--font-dm-mono`);
- drop `Inter`;
- add the new variables to `<html className>`;
- set `themeColor` light to `#f7f9fa`;
- update the font comment to name the new families.

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd frontend && node --test src/lib/theme.test.mjs`
Expected: PASS, 5 tests.

- [ ] **Step 5: Verify the build**

Run: `cd frontend && npm run verify > ../.superpowers/sdd/restyle-verify.log 2>&1; echo $?`
Expected: `0`.

---

### Task 2: Shared UI pieces, the status chip, Panel and PageHeader

**Files:**
- Create: `frontend/src/components/panel.tsx`
- Create: `frontend/src/components/page-header.tsx`
- Create: `frontend/src/lib/style-guard.test.mjs`
- Modify: `frontend/src/components/ui/button.tsx`, `input.tsx`, `input-group.tsx`, `select.tsx`,
  `textarea.tsx`, `table.tsx`, `dialog.tsx`, `dropdown-menu.tsx`, `tooltip.tsx`, `alert.tsx`,
  `command.tsx`, `skeleton.tsx`, `link-button.tsx`
- Modify: `frontend/src/components/status-chip.tsx`, `pager.tsx`, `load-error.tsx`,
  `no-permission.tsx`, `feature-locked.tsx`, `subscription-ended.tsx`

**Interfaces:**
- Consumes the tokens from Task 1.
- Produces:
  - `Panel(props: React.ComponentProps<"section">)`: `rounded-xl border bg-card shadow-panel
    overflow-hidden`;
  - `PanelHeader({ title, count, actions, className }: { title: React.ReactNode; count?:
    React.ReactNode; actions?: React.ReactNode; className?: string })`:
    - a `bg-band border-b` strip;
    - the title in a `<h2>` at `text-sm font-medium`;
    - `count` in `.tabular text-muted-foreground`, pushed right;
    - `actions` after it;
  - `PanelBody(props: React.ComponentProps<"div">)`: `p-4`;
  - `PageHeader({ title, subtitle, actions }: { title: React.ReactNode; subtitle?: React.ReactNode;
    actions?: React.ReactNode })`:
    - an `<h1 className="text-xl font-medium tracking-tight">`;
    - the subtitle in `text-sm text-muted-foreground`;
    - actions right-aligned, wrapping under the title below `sm`;
  - `style-guard.test.mjs` exports nothing. It holds a `SWEPT` array of folder paths that later
    tasks append to.

- [ ] **Step 1: Write the failing test**

`frontend/src/lib/style-guard.test.mjs`:
- It walks every `.tsx` under each path in `SWEPT`, skipping `components/ui/`, `panel.tsx` and
  `page-header.tsx`.
- It asserts four rules, failing with the file and line:
  1. no `/\brounded-(lg|xl|2xl)\b[^"]*\bborder\b/` box classes. A line ending in
     `// restyle: inline` is allowed for genuine non-panel boxes (token box, field group). Each
     such marker needs a reason.
  2. no raw palette classes: `/\b(bg|text|border)-(gray|slate|zinc|neutral|red|green|amber|blue|yellow|emerald|teal|sky|orange|rose)-\d{2,3}\b/`;
  3. no `<h1` (page titles go through `PageHeader`);
  4. no `animate-` other than `animate-in`, `animate-out`, `animate-none` or `animate-spin` in
     `components/ui`.

The second test reads `status-chip.tsx`:
```js
test("chip tones follow frost and slate and keep the terminals distinct", () => {
  assert.match(chip, /AWAITING_RESULTS:\s*"[^"]*s-lab/);
  assert.match(chip, /PENDING_CLEARANCE:\s*"[^"]*s-bill/);
  assert.match(chip, /IN_CONSULTATION:\s*"[^"]*s-doc/);
  assert.match(chip, /REGISTERED:\s*"[^"]*s-wait/);
  assert.match(chip, /DECEASED:\s*"[^"]*bg-foreground/);
  assert.match(chip, /LAMA:\s*"[^"]*critical/);
  assert.match(chip, /REFERRED_OUT:\s*"[^"]*info/);
  assert.match(chip, /COMPLETED:\s*"[^"]*success/);
});
```
Start with `const SWEPT = ["src/components"]`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd frontend && node --test src/lib/style-guard.test.mjs`
Expected: FAIL. It lists hand-rolled boxes in `src/components`, and the chip test fails on
`AWAITING_RESULTS`.

- [ ] **Step 3: Implement**

- **Panel and PageHeader:** create them to the Interfaces above.
- **`ui/*`:**
  - button: `rounded-md`, a flat `bg-primary` with a `hover:bg-primary/90` colour step only, no
    shadow; outline and ghost variants use `border-input` and `hover:bg-accent`;
  - fields: `bg-card border-input rounded-md`, with `focus-visible:ring-ring/25` plus the border
    turning `primary`;
  - table: `TableHeader` gets `bg-band text-muted-foreground text-sm font-medium`, rows get
    `border-border-soft`, and the selected row gets `bg-muted`;
  - dialog: `rounded-2xl shadow-panel`;
  - leave the existing `animate-in` and `animate-out` classes as they are.
- **`status-chip.tsx`:**
  - pill with `rounded-full`, `px-2.5 py-0.5`, `text-xs font-medium`, no border;
  - a leading 6px `bg-current rounded-full` dot (`aria-hidden`);
  - tones: waiting family `bg-s-wait-bg text-s-wait`, doctor `bg-s-doc-bg text-s-doc`,
    lab/results `bg-s-lab-bg text-s-lab`, billing/clearance `bg-s-bill-bg text-s-bill`, closed
    `bg-muted text-muted-foreground`;
  - the terminals stay as in the existing comment (DECEASED solid ink, LAMA critical, REFERRED_OUT
    info);
  - the queue and worklist maps follow the same families.
- **The rest of `src/components`:** move its boxes onto `Panel` or `Alert`, or add a reasoned
  `// restyle: inline` marker.

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd frontend && node --test src/lib/style-guard.test.mjs src/lib/theme.test.mjs`
Expected: PASS.

- [ ] **Step 5: Verify the build**

Run: `cd frontend && npm run verify > ../.superpowers/sdd/restyle-verify.log 2>&1; echo $?`
Expected: `0`.

---

### Task 3: Shell and auth screens

**Files:**
- Modify: `frontend/src/components/app-shell/sidebar.tsx`, `mobile-nav.tsx`,
  `universal-search.tsx`, `user-menu.tsx`, `subscription-banner.tsx`, `locale-switcher.tsx`
- Modify: `frontend/src/app/(app)/layout.tsx`, `frontend/src/app/login/**`,
  `frontend/src/app/forgot-password/**`, `frontend/src/app/change-password/**`,
  `frontend/src/app/invite/**`, `frontend/src/app/get-started/**`, `frontend/src/app/error.tsx`,
  `frontend/src/app/not-found.tsx`, `frontend/src/features/auth/**`
- Test: `frontend/src/lib/style-guard.test.mjs` (extend `SWEPT`)

**Interfaces:**
- Consumes `Panel`, `PageHeader` and the tokens (Tasks 1–2).

- [ ] **Step 1: Extend the guard**

Append to `SWEPT`: `"src/components/app-shell"`, `"src/app/login"`, `"src/app/forgot-password"`,
`"src/app/change-password"`, `"src/app/invite"`, `"src/app/get-started"`, `"src/features/auth"`.

- [ ] **Step 2: Run the guard to verify it fails**

Run: `cd frontend && node --test src/lib/style-guard.test.mjs`
Expected: FAIL. It names `subscription-banner.tsx` (raw palette classes) and the auth pages'
`<h1>` and boxes.

- [ ] **Step 3: Implement**

- **Sidebar:**
  - `bg-sidebar border-r`;
  - items `text-sm text-muted-foreground rounded-md px-2.5 h-tap`;
  - the active item gets `bg-card text-foreground font-medium shadow-[inset_2px_0_0_var(--primary)]`;
  - the brand mark is a 22px `rounded-md bg-primary` square.
- **Top bar search:** a `bg-card border-input` field with the `F3` hint in a `.tabular`
  `kbd`-styled box.
- **Banner:** semantic tokens instead of raw colours (caution for expiring, critical for ended).
- **Auth pages:** a centred `Panel` (max-w-md) on `bg-background`. The title becomes a
  `PageHeader`, or an `<h1>` inside `Panel` replaced by `PanelHeader` where the page has no
  shell.
- Visible text is unchanged.

- [ ] **Step 4: Run the guard and the auth e2e**

Run: `cd frontend && node --test src/lib/style-guard.test.mjs`
Expected: PASS.

Run: `cd frontend && npx playwright test e2e/auth.spec.ts e2e/two-factor.spec.ts e2e/idle-lock.spec.ts e2e/search.spec.ts e2e/queue-nav.spec.ts`
Expected: all pass. The local stack must be running: Postgres and Redis in Docker, the API and
web on the host.

---

### Task 4: Reception, patients and queue

**Files:**
- Modify: `frontend/src/app/(app)/page.tsx`, `src/app/(app)/reception/**`,
  `src/app/(app)/patients/**`, `src/app/(app)/queue/**`, `src/app/(app)/no-home/**`
- Modify: `src/features/patients/**`, `src/features/queue/**`, `src/features/scan/**`,
  `src/features/emergency/**`, `src/features/outcomes/**`, `src/components/patient/**`
- Test: `frontend/src/lib/style-guard.test.mjs`

- [ ] **Step 1: Extend the guard**

Append the folders above to `SWEPT`.

- [ ] **Step 2: Run the guard to verify it fails**

Run: `cd frontend && node --test src/lib/style-guard.test.mjs`
Expected: FAIL, listing the `features/patients/record` boxes and the page `<h1>`s.

- [ ] **Step 3: Implement**

- Page titles go through `PageHeader`; bordered sections become `Panel`, `PanelHeader` and
  `PanelBody`.
- Queue rows:
  - the token number in a `.tabular rounded-md bg-muted border px-1.5` box (marked
    `// restyle: inline`);
  - the patient name with a muted subline;
  - the status chip on the right.
- The safety banner stays loud. It keeps its critical and caution tokens and is never softened
  into the band colour.
- The token slip print layout keeps a white ground.

- [ ] **Step 4: Run the guard and the area e2e**

Run: `cd frontend && node --test src/lib/style-guard.test.mjs`
Expected: PASS.

Run: `cd frontend && npx playwright test e2e/reception*.spec.ts e2e/patient-record.spec.ts e2e/scan.spec.ts e2e/emergency.spec.ts e2e/outcomes.spec.ts e2e/drug-allergy.spec.ts`
Expected: all pass.

---

### Task 5: Clinical and ward screens

**Files:**
- Modify: `src/app/(app)/admissions/**`, `src/app/(app)/wards/**`, `src/app/(app)/rounds/**`
- Modify: `src/features/chart/**`, `src/features/vitals/**`, `src/features/wards/**`,
  `src/features/admissions/**`, `src/features/medicines/**`
- Modify: `src/app/(app)/medicines/**`
- Test: `frontend/src/lib/style-guard.test.mjs`

- [ ] **Step 1: Extend the guard**

Append the folders above to `SWEPT`.

- [ ] **Step 2: Run the guard to verify it fails**

Run: `cd frontend && node --test src/lib/style-guard.test.mjs`
Expected: FAIL, naming `bed-board.tsx`, `stay-panels.tsx`, `latest-vitals.tsx` and the others.

- [ ] **Step 3: Implement**

- Same patterns as Task 4.
- Vitals values, doses and times are `.tabular`.
- Bed-board tiles count as genuine non-panel boxes: `// restyle: inline`, a `rounded-lg border`
  with the status tone as the left border colour.

- [ ] **Step 4: Run the guard and the area e2e**

Run: `cd frontend && node --test src/lib/style-guard.test.mjs`
Expected: PASS.

Run: `cd frontend && npx playwright test e2e/chart.spec.ts e2e/wards.spec.ts e2e/stay-screen.spec.ts e2e/admission-desk.spec.ts e2e/medicines.spec.ts e2e/discharge-summary-print.spec.ts`
Expected: all pass.

---

### Task 6: Lab, billing and messages

**Files:**
- Modify: `src/app/(app)/lab/**`, `src/app/(app)/billing/**`, `src/app/(app)/account/**`,
  `src/app/(app)/messages/**`
- Modify: `src/features/lab/**`, `src/features/billing/**` (except `billing/print/**`, which is
  Task 7), `src/features/messages/**`
- Test: `frontend/src/lib/style-guard.test.mjs`

- [ ] **Step 1: Extend the guard**

Append the folders above to `SWEPT`.

- [ ] **Step 2: Run the guard to verify it fails**

Run: `cd frontend && node --test src/lib/style-guard.test.mjs`
Expected: FAIL, naming the `features/billing/accounts` boxes.

- [ ] **Step 3: Implement**

- Same patterns as Task 4.
- Money is `.tabular` and right-aligned in tables.
- Invoice totals sit in a `PanelBody` with a top `border-border-soft`.

- [ ] **Step 4: Run the guard and the area e2e**

Run: `cd frontend && node --test src/lib/style-guard.test.mjs`
Expected: PASS.

Run: `cd frontend && npx playwright test e2e/accounts.spec.ts e2e/bill-correction.spec.ts e2e/clearance.spec.ts e2e/ipd-bill-editor.spec.ts e2e/patient-bills.spec.ts e2e/patient-billing-access.spec.ts e2e/messages.spec.ts`
Expected: all pass.

---

### Task 7: Admin, platform, reports, subscriptions and print

**Files:**
- Modify: `src/app/(app)/admin/**`, `src/app/(app)/platform/**`, `src/app/(app)/reports/**`,
  `src/app/(print)/**`
- Modify: `src/features/admin/**`, `src/features/platform/**`, `src/features/reports/**`,
  `src/features/subscriptions/**`, `src/features/billing/print/**`
- Test: `frontend/src/lib/style-guard.test.mjs`

**Interfaces:**
- Chart colours keep using `--chart-1..5`. Retune those five tokens in `globals.css` to sit with
  the slate palette: teal `#0d7480` first. Recharts reads them through `var(...)`.

- [ ] **Step 1: Extend the guard, and add a print rule**

- Append the folders above to `SWEPT`.
- Add a test: files under `src/app/(print)` and `src/features/billing/print` contain none of
  `bg-band`, `bg-muted`, `bg-accent`, `bg-s-` or `shadow-panel`. Print stays white, with black
  ink.

- [ ] **Step 2: Run the guard to verify it fails**

Run: `cd frontend && node --test src/lib/style-guard.test.mjs`
Expected: FAIL, naming `printed-bill.tsx` (raw palette classes), the platform screens and the
report stat boxes.

- [ ] **Step 3: Implement**

- Same patterns as Task 4.
- Report stat tiles become `Panel`s with the number in `.tabular text-2xl font-medium`.
- `printed-bill.tsx` uses `text-foreground`, `border-border` or plain black, with no tints.

- [ ] **Step 4: Run the guard and the area e2e**

Run: `cd frontend && node --test src/lib/style-guard.test.mjs`
Expected: PASS.

Run: `cd frontend && npx playwright test e2e/admin.spec.ts e2e/platform.spec.ts e2e/reports.spec.ts e2e/report-period.spec.ts e2e/printed-bill.spec.ts e2e/plan-gating.spec.ts e2e/subscription-lifecycle.spec.ts e2e/hospital-profile.spec.ts`
Expected: all pass.

---

### Task 8: Whole-app check and screenshots

**Files:**
- Create: `frontend/e2e/restyle-screenshots.spec.ts`
- Test: the full suites.

- [ ] **Step 1: Close the sweep**

- Replace the `SWEPT` list with `["src/app", "src/features", "src/components"]`.
- Run `node --test src/lib/style-guard.test.mjs`.
- Fix any file the earlier groups missed until it passes.

- [ ] **Step 2: Write the screenshot spec**

- Model it on `e2e/platform-screenshots.spec.ts`.
- Viewports are `desktop` 1440×900 and `phone` 390×844, and the locales are `en-IN` and `hi`.
  Switch the locale through the app's locale switcher or its cookie, as `locale-switcher.tsx`
  sets it.
- It signs in as reception, doctor, nurse, admin and platform (`ACCOUNTS` in `e2e/support.ts`).
- It visits one screen per group:
  - `/reception`, `/patients`, a patient record;
  - `/queue`, `/wards`, an admission;
  - `/lab`, `/billing`, `/messages`;
  - `/admin`, `/reports`, `/platform/hospitals`.
- Each screen asserts `noSidewaysScroll`.
- Screenshots are saved to `test-results/restyle/<locale>-<viewport>-<screen>.png`.

- [ ] **Step 3: Run it**

Run: `cd frontend && npx playwright test e2e/restyle-screenshots.spec.ts`
Expected: pass, and the PNGs are written.

- [ ] **Step 4: Run the full suites**

Run: `cd frontend && npm run verify > ../.superpowers/sdd/restyle-verify.log 2>&1; echo $?`
Expected: `0`.

Run: `cd frontend && npx playwright test > ../.superpowers/sdd/restyle-e2e.log 2>&1; tail -5 ../.superpowers/sdd/restyle-e2e.log`
Expected: all pass. The admin hub test is known to be flaky under load; it must pass when re-run
alone.

- [ ] **Step 5: Check the bundle**

- Compare the `next build` first-load JS for `/reception` against the number recorded before
  Task 1.
- Expected: no growth beyond the font files. The JS shared chunk changes by less than 2 kB.
- Record both numbers in the ledger.
