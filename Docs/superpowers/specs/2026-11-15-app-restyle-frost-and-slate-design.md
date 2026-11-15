# App restyle: Frost and slate

Date: 2026-11-15. Scope: `frontend/` only (the hospital app). The website is untouched.

## Intent

The owner wants the app to look calm and pleasant to use all day: soft borders, quieter
text, one consistent look across every screen. They picked direction **A, Frost and slate**,
from the mockups, for its borders and its type.

This is a **cosmetic restyle only**:
- no new features and no changed workflows;
- no new animation;
- no heavier loading.

## Goals

- Every screen uses the A palette, type, corners and borders, with no screen left in the old look.
- Numbers people compare (tokens, UHIDs, money, vitals, times) render in a mono face with
  tabular digits.
- The status chips use the softer A colours. Each chip still shows its label, so colour is
  never the only signal.
- The existing clinical rules hold:
  - body text at 16px or more, and nothing smaller than 13px;
  - tap targets of 44px;
  - a visible focus ring;
  - high contrast, at least WCAG AA for all text and AAA for body text.

## Non-goals and anti-goals

- **No new features.** The "visit card" from the mockup is a new feature, so it is left out.
- **No new motion.** The mockup's spinning step dot and progress bar are not built. Existing
  dialog and menu fades stay as they are, and `prefers-reduced-motion` is still honoured.
- **No new runtime dependency** and no heavier JS bundle.
  - The fonts come through `next/font`, served from the app itself.
  - The CSP stays `font-src 'self'`.
- **No dark mode work.** The app has no theme switch today. The `.dark` block is retuned to the
  same hues so it isn't left inconsistent, but it isn't exposed.
- **Anti-goal:** it becomes "designy" at the cost of speed. Lower contrast, smaller text, or
  more clicks would make the restyle a failure.

## Approaches considered

1. **Restyle the foundation, then sweep the screens (chosen).**
   - The app is already token-driven: there are 9 hard-coded colour classes in 201 files, and
     the theme lives in `globals.css`.
   - So changing the tokens, fonts and the ~15 shared components moves most of the app at once.
   - A sweep then fixes the screens that hand-roll their own boxes. There are about 153
     `rounded-lg border` blocks, which become one shared `Panel` look.
2. **Tokens only.**
   - Cheapest, but the hand-rolled boxes, headers and tables would stay inconsistent.
   - That is the opposite of "consistent throughout".
3. **Rebuild the screens one by one.**
   - Most consistent in theory, but it means rewriting workflows.
   - That is risky against the e2e suite and outside "cosmetic only".

## Design

### Tokens (`frontend/src/app/globals.css`, `:root`)

The values below are taken from mockup A. They map onto the existing shadcn variables, so no
component needs new variable names.

| Token | A value | Use |
|---|---|---|
| `--background` | `#f7f9fa` | page ground |
| `--card`, `--popover` | `#ffffff` | panels, dialogs |
| `--sidebar` | `#f0f4f5` | sidebar ground |
| `--secondary`, `--muted` | `#ecf0f2` | surfaces, the selected row, the token box |
| new `--band` | `#f4f7f8` | panel header strip, log blocks |
| `--foreground` | `#1f3038` | ink |
| `--muted-foreground` | `#5b6b72` | secondary text |
| `--border` | `#dbe2e5` | lines |
| new `--border-soft` | `#e9eef0` | row dividers inside a panel |
| `--input` | `#cfd8dc` | field borders, one step darker than `--border` so fields stay visible |
| `--primary`, `--ring`, `--sidebar-primary` | `#0d7480` | accent |
| `--accent` | `#dcefed` | hover and active backgrounds |
| `--accent-foreground` | `#0d5a63` | text on accent backgrounds |
| `--success` | `#1f8a5b` | done |
| `--radius` | `0.75rem` (12px) | panels; buttons and fields take `--radius-md` and `--radius-sm` through the existing calc scale |
| new `--shadow-panel` | `0 1px 2px rgb(31 48 56 / .05)` | the only shadow in the app |

The clinical semantic colours (`--critical`, `--caution`, `--info`) keep their meaning. Only their
lightness and chroma are tuned to sit with the slate palette, and their contrast is checked.

**Status chip tones** follow A:
- waiting: `#7a5a00` on `#fbf1d6`;
- with doctor: `#0d6670` on `#dcefed`;
- lab or results: `#3b5bab` on `#e3e9f8`;
- billing or clearance: `#8a4b16` on `#f8e7d8`;
- closed: muted on surface.

The edge-case terminals stay distinct: LAMA in critical, and DECEASED in solid ink.

### Type (`frontend/src/app/layout.tsx`)

- **Instrument Sans** (400, 500, 600) replaces Inter as `--font-latin`.
- **Noto Sans Devanagari** stays as the second family for Hindi.
- **DM Mono** (400, 500) becomes `--font-mono`, used by `.tabular` numbers, tokens, UHIDs, times
  and money.
- Headings use weight 500 with -0.01em letter-spacing, not bold.
- Sizes keep the 16px body floor and the 13px minimum.

### Shared pieces

These are restyled in place: same props, same markup contracts, so tests keep their selectors.
- `components/ui/*`:
  - button: 8px radius, flat accent fill, a hover step in colour only;
  - input, select, textarea: white field, `--input` border, accent ring on focus;
  - table: band header, soft row dividers, the selected row on surface;
  - dialog: 16px radius, panel shadow;
  - dropdown, tooltip, alert, skeleton (static, no pulse change).
- `status-chip.tsx`: a pill with a 6px dot, plus the A tones above.
- `app-shell/sidebar.tsx`:
  - frost ground, 14px muted items;
  - the active item gets a white surface, ink text and a 2px accent inset on its left edge (as in
    the mockup).
- `app-shell` top bar and universal search: a white field with a mono `F3` key hint.
- **New `components/panel.tsx`:** `Panel`, `PanelHeader` (band strip, title, optional mono count)
  and `PanelBody`. This is a presentational wrapper only, and it replaces hand-rolled
  `rounded-lg border` boxes.
- A `PageHeader` pattern: title at 20px/500, a muted subline, actions on the right.

### Screen sweep

Every route under `src/app/(app)`, `(print)`, `login`, `forgot-password`, `get-started`,
`invite` and `change-password` is moved onto `Panel`, `PageHeader` and the mono number style.
Groups:
1. shell and auth;
2. reception, patients, queue;
3. doctor and nurse screens (chart, vitals, rounds, wards, admissions);
4. lab, medicines, billing, messages;
5. admin, platform, reports, subscriptions.

**Print views:**
- They keep a white ground and black ink.
- Only the type changes, so printed bills and slips stay printer-friendly.
- The two files with hard-coded colours (`subscription-banner.tsx`, `printed-bill.tsx`) move onto
  tokens.

### Checks (definition of done)

- `npm run verify` (lint, types, unit tests, build) passes, and the build's first-load JS does not
  grow by more than the size of the font files.
- The full Playwright e2e suite passes, with no selectors loosened to make it pass.
- Contrast is checked for every text and chip token pair: AA minimum, and body text AAA.
- Before and after screenshots of each screen group at 1440px and 390px wide go to the owner for
  review.

## Left to the builder

- Exact oklch conversions of the hex values.
- Hover shades.
- Which local boxes count as a panel and which are inline groups.
- Ordering inside each sweep group.
