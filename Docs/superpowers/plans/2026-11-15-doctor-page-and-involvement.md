# Doctor Page and Doctors Involvement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** This plan delivers four things:
- a per-doctor "Doctors involvement" switch that the server enforces;
- a doctor page showing timings, days off and patients seen;
- the NMC number visibly optional;
- "Coming soon" badges on lab and messages.

**Architecture:** `scheduling` owns the flag (`doctors.involvement_enabled`) and its route.
- `clinical` reads the flag through `scheduling`'s service and refuses doctor-side writes on an
  off doctor's OPD visits.
- `mark_seen` is allowed only for off doctors, and then completes the visit through the
  existing consultation-close logic.
- The frontend follows the flag and adds one new page, `/doctors/[doctorId]`.

**Tech Stack:** FastAPI, SQLModel, Alembic, pytest (backend); Next.js 16, TanStack Query, next-intl,
Playwright (frontend).

**Spec:** `Docs/superpowers/specs/2026-11-15-doctor-page-and-involvement-design.md`

## Global Constraints

- **No git commits.** The owner commits manually. Changes stay uncommitted on `version4`.
- **Status changes:**
  - Every status change goes through `clinical/state_machine.py` `transition`. No direct status
    writes.
  - Every sensitive action writes an audit row through `identity_service.record_audit`.
- **Module boundaries:** cross-module reads go through the other module's `service.py`, never
  its models.
- **Every list endpoint is paginated** (`PageParams` / `Page[T]` in `app/core/pagination.py`).
- **Every schema change has an Alembic migration.** Its revision follows
  `20261114_1400-a5c9e3b7d1f2_plan_change_effective_at.py`.
- **Error codes and copy (exact):**
  - `doctor_entry_off`: "Doctor entry is off for this doctor. Reception marks patients seen."
  - `doctor_involved`: "This doctor records their own visits."
- **Who may change things:**
  - Only HOSPITAL_ADMIN (and PLATFORM_ADMIN) may flip the switch, through the new permission
    `doctor:involvement:manage`.
  - Timings are editable in the UI only by the admin.
- **Who may see the patients-seen list:** only holders of `doctor:involvement:manage`, and the
  doctor themselves.
- **Visits that count as "seen":** IN_CONSULTATION, AWAITING_RESULTS, PENDING_CLEARANCE, COMPLETED,
  ADMITTED, REFERRED_OUT, LAMA, DECEASED. Not NO_SHOW or CANCELLED.
- **Not covered by the switch:**
  - IPD;
  - vitals;
  - lab, pharmacy and billing clearance;
  - queue call-next, start and skip;
  - reassigning a visit to another doctor;
  - templates.
- **No follow-up date** is set for an off doctor's patients.
- **Look:** the restyle's shared pieces (`PageHeader`/`PageTitle`, `Panel`, status chips,
  `.tabular`). No new styles, no new animation.
- **Python patch scripts** write with `newline="\n"`.

## Review Focus

1. **A stale board** shows Seen for a doctor who was just switched back on. The server answers
   409 `doctor_involved`, and the board shows the message and refetches. Task 3 has the API test;
   Task 7 handles the error in the UI.
2. **A visit with no doctor** (`doctor_id` is None, as with an emergency registration) is never
   blocked by the guard. Covered by a Task 2 test.
3. **An off doctor's visit with a pending lab order or an unpaid charge** is marked Seen. It goes to
   AWAITING_RESULTS or PENDING_CLEARANCE, not COMPLETED. Covered by a Task 2 test.
4. **Another doctor, or reception, asks for a doctor's patients-seen list** and gets 403. Covered by
   a Task 4 test.
5. **An off doctor's visit from an earlier day, still PENDING_CLEARANCE, has its bill paid.** The
   payment still clears it to COMPLETED, because the guard never blocks clearance. Covered by a
   Task 2 test.

---

## File Structure

**Backend**
- Modify: `backend/app/modules/scheduling/models.py` (`Doctor.involvement_enabled`), `schemas.py`,
  `rbac.py`, `service.py`, `router.py`
- Create: `backend/alembic/versions/20261115_1000-b6d0f4c8e2a3_doctor_involvement.py`
- Modify: `backend/app/modules/clinical/service.py`, `router.py`, `schemas.py`
- Create tests:
  - `backend/tests/test_doctor_involvement.py` (switch, guard, Seen);
  - `backend/tests/test_doctor_page_api.py` (patients seen, timings routes).
- Modify: existing tests that call `POST /queue/{id}/seen`.

**Frontend**
- Create: `frontend/src/components/coming-soon-badge.tsx`
- Create: `frontend/src/app/(app)/doctors/[doctorId]/page.tsx`
- Create: `frontend/src/features/doctors/` with `use-doctor.ts`, `doctor-page.tsx`,
  `involvement-switch.tsx`, `timings-panel.tsx`, `days-off-panel.tsx` and `patients-seen.tsx`.
- Modify:
  - `components/app-shell/nav-items.ts` (+ `sidebar.tsx`, `mobile-nav.tsx`);
  - `features/admin/doctors-screen.tsx`;
  - `features/queue/doctor-queues.tsx` and `use-queue-board.ts`;
  - `features/patients/record/add-diagnosis.tsx`;
  - the lab and messages page headers;
  - `i18n/messages/en.json` and `hi.json`;
  - `openapi.json` and `types/openapi.d.ts` (regenerated by `npm run codegen`).
- Create e2e: `frontend/e2e/doctor-involvement.spec.ts` and `frontend/e2e/doctor-page.spec.ts`
- Modify e2e: `frontend/e2e/reception-seen.spec.ts`, plus any other spec that clicks "Seen".

---

### Task 1: The involvement flag on the doctor (scheduling)

**Files:**
- Modify: `backend/app/modules/scheduling/models.py` (class `Doctor`, near `is_accepting_appointments`)
- Create: `backend/alembic/versions/20261115_1000-b6d0f4c8e2a3_doctor_involvement.py`
- Modify: `backend/app/modules/scheduling/schemas.py` (`DoctorRead`, `DoctorQueue`), `rbac.py`, `service.py`, `router.py`
- Test: `backend/tests/test_doctor_involvement.py`

**Interfaces:**
- Produces:
  - `Doctor.involvement_enabled: bool`, NOT NULL, `server_default` true;
  - `DoctorRead.involvement_enabled: bool`;
  - `DoctorQueue.involvement_enabled: bool`;
  - `SchedulingPermissions.DOCTOR_INVOLVEMENT_MANAGE = "doctor:involvement:manage"`, in
    `PERMISSION_CATALOGUE` with the description "Turn a doctor's own data entry on or off."
    HOSPITAL_ADMIN and PLATFORM_ADMIN already receive every catalogue code, so no role edits are
    needed;
  - `async def set_doctor_involvement(session: AsyncSession, doctor: Doctor, *, enabled: bool) -> Doctor`;
  - `async def is_doctor_involved(session: AsyncSession, doctor_id: uuid.UUID | None, *, hospital_id: uuid.UUID) -> bool`.
    It returns True when `doctor_id` is None or the doctor is not found, so it never blocks a
    visit that has no doctor;
  - the route `PUT /api/v1/doctors/{doctor_id}/involvement` with body
    `DoctorInvolvementUpdate { enabled: bool }` (`extra="forbid"`). It returns `DoctorRead` and
    writes the audit action `doctor.involvement_changed` with `before={"enabled": old}` and
    `after={"enabled": new}`.

- [ ] **Step 1: Write the failing tests** in `test_doctor_involvement.py`. Build a doctor and a
  visit with the same setup helpers `tests/test_api_queue_board.py` uses.
  - `test_new_doctor_is_involved_by_default`: create a doctor; `DoctorRead.involvement_enabled is True`.
  - `test_admin_turns_involvement_off_and_it_is_audited`: the admin calls `PUT .../involvement`
    with `{"enabled": false}` and gets 200 with `involvement_enabled is False`. An audit row
    exists with action `doctor.involvement_changed`, `before == {"enabled": True}`,
    `after == {"enabled": False}`.
  - `test_receptionist_cannot_flip_involvement`: the receptionist gets 403.
  - `test_involvement_rejects_unknown_fields`: body `{"enabled": false, "x": 1}` gets 422.
  - `test_board_column_carries_involvement`: `GET /queue/board` shows the doctor's column with
    `involvement_enabled` matching the flag.
- [ ] **Step 2: Run the tests to verify they fail.**
  Run: `cd backend && ../.venv/Scripts/python.exe -m pytest tests/test_doctor_involvement.py -q`
  Expected: FAIL (404 on the route, or the attribute missing).
- [ ] **Step 3: Implement** the model field, the migration (add the column with
  `server_default=sa.true()`, nullable=False; downgrade drops it), the schemas, the permission,
  both service functions and the route. Route guard: a `require_permission`-style dependency for
  `doctor:involvement:manage`, built the way `CanManageAvailability` is. The doctor is loaded with
  `get_doctor(..., hospital_id=tenant_id)`.
- [ ] **Step 4: Run the tests to verify they pass.**
  Run: `cd backend && ../.venv/Scripts/python.exe -m alembic upgrade head && ../.venv/Scripts/python.exe -m pytest tests/test_doctor_involvement.py -q`
  Expected: PASS.

---

### Task 2: The clinical guard, and Seen completing the visit

**Files:**
- Modify: `backend/app/modules/clinical/service.py`
- Test: `backend/tests/test_doctor_involvement.py`

**Interfaces:**
- Consumes `scheduling_service.is_doctor_involved` (Task 1). Import it as
  `from app.modules.scheduling import service as scheduling_service`; `scheduling.service` does
  not import `clinical`, so there is no cycle.
- Produces:
  - `async def _require_doctor_entry(session: AsyncSession, encounter: Encounter) -> None`. It
    raises `ConflictError("Doctor entry is off for this doctor. Reception marks patients seen.", code="doctor_entry_off")`
    when `not await scheduling_service.is_doctor_involved(session, encounter.doctor_id, hospital_id=encounter.hospital_id)`.
    It returns without checking when `encounter.status is EncounterStatus.ADMITTED`, because IPD
    is outside the switch.
  - `async def _close_consultation(session, encounter, *, actor, follow_up_date=None, follow_up_instructions=None, reason=None) -> Encounter`.
    This is the current body of `complete_consultation`, moved unchanged.
  - `complete_consultation` becomes the guard followed by `_close_consultation`. Its signature
    is unchanged.
  - The guard is called first in `start_consultation`, `update_encounter`, `place_order`,
    `cancel_order` (via the order's encounter), `write_note`, `sign_note` (via the note's
    encounter) and `record_diagnosis`.
    `record_vitals`, `complete_order`, `start_order`, billing clearance and the terminal-outcome
    functions are NOT guarded.
  - `record_seen_by_staff(session, encounter, *, actor)`:
    - it moves REGISTERED to IN_CONSULTATION (as now);
    - then, when the visit's doctor is not involved and the visit is not closed, it calls
      `_close_consultation(session, encounter, actor=actor, reason="Marked as seen by reception.")`;
    - it returns the encounter.

- [ ] **Step 1: Write the failing tests** (service level, in `test_doctor_involvement.py`):
  - `test_off_doctor_refuses_each_doctor_write`: parametrised over write_note, record_diagnosis,
    place_order, start_consultation, complete_consultation and update_encounter. Each raises
    `ConflictError` with `code == "doctor_entry_off"`.
  - `test_off_doctor_refuses_sign_note_and_cancel_order`: write the note and place the order
    while the doctor is on, then switch the doctor off; both calls raise `doctor_entry_off`.
  - `test_admin_is_refused_too`: the actor is a hospital admin; still `doctor_entry_off`.
  - `test_vitals_still_recorded_for_off_doctor`: `record_vitals` succeeds.
  - `test_visit_without_doctor_is_never_blocked`: an encounter with `doctor_id=None`;
    `write_note` succeeds.
  - `test_seen_completes_off_doctors_visit`: nothing is pending, so after `record_seen_by_staff`
    `status is EncounterStatus.COMPLETED` and one `EncounterEvent` row has reason
    "Marked as seen by reception.".
  - `test_seen_with_pending_lab_order_awaits_results`: a lab order is placed while the doctor is
    on, the doctor is switched off, then Seen gives `status is EncounterStatus.AWAITING_RESULTS`.
  - `test_seen_with_unpaid_charge_goes_to_clearance`: a billable charge is pending, the doctor is
    off, then Seen gives `PENDING_CLEARANCE`.
  - `test_earlier_days_visit_still_clears_when_paid`: a PENDING_CLEARANCE visit of an off doctor;
    settling the charge through billing's service moves it to COMPLETED.
  - `test_switch_back_on_restores_entry`: off, then on; `write_note` succeeds.
  - `test_admitted_visit_is_outside_the_switch`: an ADMITTED encounter of an off doctor;
    `place_order` and `write_note` succeed.
- [ ] **Step 2: Run the tests to verify they fail.**
  Run: `cd backend && ../.venv/Scripts/python.exe -m pytest tests/test_doctor_involvement.py -q`
  Expected: the new tests FAIL (writes succeed; Seen leaves the visit IN_CONSULTATION).
- [ ] **Step 3: Implement** the guard, the `_close_consultation` split and the
  `record_seen_by_staff` change, as described in Interfaces.
- [ ] **Step 4: Run the tests to verify they pass,** then run the clinical suite:
  `cd backend && ../.venv/Scripts/python.exe -m pytest tests/test_doctor_involvement.py tests/test_clinical_service.py -q`
  Expected: PASS.

---

### Task 3: Queue rules: Seen only for off doctors, and no doctor "complete" while off

**Files:**
- Modify: `backend/app/modules/scheduling/router.py` (the `mark_seen` route at `"/{entry_id}/seen"`
  and the `"/{entry_id}/complete"` route)
- Modify: every existing test that posts to `/queue/{id}/seen`. Find them with
  `grep -rln "/seen" backend/tests`. Switch the doctor's involvement off in each test's setup
  first.
- Test: `backend/tests/test_doctor_involvement.py`

**Interfaces:**
- Consumes `scheduling_service.is_doctor_involved` and `QueueEntry.doctor_id`.
- `mark_seen` route: when the entry's doctor is involved, raise
  `ConflictError("This doctor records their own visits.", code="doctor_involved")` before any
  change.
- `complete` route: when the entry's doctor is NOT involved, raise `doctor_entry_off` (copy as in
  Global Constraints).

- [ ] **Step 1: Write the failing tests:**
  - `test_seen_refused_while_doctor_involved`: the doctor is on; `POST /queue/{id}/seen` gets 409
    and `error.code == "doctor_involved"`. The token is unchanged.
  - `test_reception_and_nurse_mark_seen_for_off_doctor`: the doctor is off. The receptionist gets
    200, and the nurse gets 200 on a second token. Both encounters end COMPLETED.
  - `test_queue_complete_refused_for_off_doctor`: the doctor is off; `POST /queue/{id}/complete`
    gets 409 `doctor_entry_off`.
  - `test_reception_still_calls_and_starts_for_off_doctor`: `call-next` and `start` both get 200.
- [ ] **Step 2: Run the tests to verify they fail.** Run the Task 1 command. Expected: the new
  tests FAIL.
- [ ] **Step 3: Implement** the two route guards. Update the existing `/seen` tests' setup.
- [ ] **Step 4: Run the whole backend suite.**
  Run: `cd backend && ../.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider > ../.superpowers/sdd/2026-11-15-doctor-page-and-involvement/pytest-t3.log 2>&1; tail -3 ../.superpowers/sdd/2026-11-15-doctor-page-and-involvement/pytest-t3.log`
  Expected: all pass, including `test_isolation_walk.py`, which walks the new id routes on its
  own. Then run `mypy app` cleanly.

---

### Task 4: Doctor page APIs: patients seen, removing hours, days off

**Files:**
- Modify: `backend/app/modules/clinical/service.py`, `schemas.py`, `router.py`
- Modify: `backend/app/modules/scheduling/service.py`, `router.py`
- Test: `backend/tests/test_doctor_page_api.py`

**Interfaces:**
- **Clinical:**
  - `SEEN_STATUSES: Final[frozenset[EncounterStatus]]` holds the eight statuses in Global
    Constraints.
  - `async def list_doctor_visits(session, params: PageParams, *, hospital_id: uuid.UUID, doctor_id: uuid.UUID, date_from: date | None = None, date_to: date | None = None) -> tuple[list[Encounter], int]`.
    It returns OPD encounters with status in `SEEN_STATUSES`, newest `created_at` first. The date
    filter is on the visit date in Asia/Kolkata, as `list_encounters(on_date=...)` already
    computes it.
  - The schema `DoctorVisitRead { encounter_id: UUID, patient_id: UUID, patient_name: str, uhid: str, status: EncounterStatus, visit_at: datetime }`.
    Patient name and UHID come from `patients_service`, with one lookup for the page, not per row.
  - The route `GET /api/v1/encounters/by-doctor/{doctor_id}` with query `date_from` and
    `date_to`, returning `Page[DoctorVisitRead]`.
  - Access: the context has `doctor:involvement:manage`, OR
    `doctor.user_id == context.user.id`. The doctor is loaded through
    `scheduling_service.get_doctor`. Anyone else gets
    `PermissionDeniedError(code="doctor_visits_forbidden")`, which is 403.
- **Scheduling:**
  - `async def remove_availability(session, *, doctor_id, availability_id, hospital_id) -> None`
    sets `is_active=False`.
  - `async def list_availability_exceptions(session, *, doctor_id, hospital_id, from_date: date) -> list[AvailabilityException]`
    returns rows with `exception_date >= from_date` that are not deleted, ordered by date. The list
    is bounded, because it is future dates for one doctor.
  - `async def remove_availability_exception(session, *, doctor_id, exception_id, hospital_id) -> None`
    soft-deletes by setting `deleted_at`.
  - Routes:
    - `DELETE /api/v1/doctors/{doctor_id}/availability/{availability_id}` → 204, guard
      `CanManageAvailability`;
    - `GET /api/v1/doctors/{doctor_id}/exceptions?from_date=YYYY-MM-DD` → list of the existing
      exception read schema, guard `CanReadDoctor`;
    - `DELETE /api/v1/doctors/{doctor_id}/exceptions/{exception_id}` → 204, guard
      `CanManageAvailability`.
  - Both deletes are audited: `doctor.availability_removed` and `doctor.leave_removed`.
  - Ruling: the existing `availability:manage` holders (including a doctor for their own hours,
    via `_may_manage_doctor`) keep their API rights. The UI shows editing only to the admin, as
    the spec says.

- [ ] **Step 1: Write the failing tests** in `test_doctor_page_api.py`:
  - `test_admin_lists_doctors_seen_visits_newest_first`: five visits, of which one is NO_SHOW and
    one is CANCELLED. Expect `total == 3`, newest first, each with `patient_name` and `uhid`.
  - `test_doctor_sees_own_list`: the doctor's own login gets 200.
  - `test_other_doctor_and_reception_are_refused`: both get 403
    `error.code == "doctor_visits_forbidden"`.
  - `test_visits_are_paginated_and_date_filtered`: with `limit=2`, two items and the correct
    `total`; `date_from` excludes an older visit.
  - `test_remove_weekly_session`: add a session, delete it (204); it is gone from
    `GET .../availability`.
  - `test_list_and_remove_day_off`: add leave for tomorrow; `GET .../exceptions?from_date=today`
    lists it; delete (204); the list is empty.
- [ ] **Step 2: Run the tests to verify they fail.**
  Run: `cd backend && ../.venv/Scripts/python.exe -m pytest tests/test_doctor_page_api.py -q`
  Expected: FAIL (404s).
- [ ] **Step 3: Implement** to the Interfaces.
- [ ] **Step 4: Run** `tests/test_doctor_page_api.py`, `tests/test_isolation_walk.py` and
  `mypy app`. Expected: PASS and clean.

---

### Task 5: NMC wording and "Coming soon" badges (frontend)

**Files:**
- Create: `frontend/src/components/coming-soon-badge.tsx`
- Modify: `frontend/src/components/app-shell/nav-items.ts`, `sidebar.tsx`, `mobile-nav.tsx`
- Modify: the lab and messages screen headers: `src/app/(app)/lab/page.tsx`, `src/app/(app)/messages/page.tsx`
- Modify: `frontend/src/features/admin/doctors-screen.tsx`, `src/i18n/messages/en.json`, `hi.json`
- Test: `frontend/src/lib/style-guard.test.mjs` (existing guard), a new unit test
  `frontend/src/lib/nav-items.test.mjs`, and `frontend/e2e/doctor-page.spec.ts` (the badge part)

**Interfaces:**
- Produces `ComingSoonBadge(): JSX.Element`: a muted pill
  `bg-muted text-muted-foreground rounded-full px-2 py-0.5 text-xs font-medium`, text
  `t("common.comingSoon")`.
- `NavItem` gains `comingSoon?: true`, set on the `diagnostics` and `messages` items.
- Copy:
  - `common.comingSoon`: en "Coming soon", hi "जल्द आ रहा है".
  - `registrationNumberHint`: en "Optional. Needed only on lab reports this doctor signs.", hi
    "वैकल्पिक। केवल उन लैब रिपोर्ट पर ज़रूरी जिन पर यह डॉक्टर हस्ताक्षर करते हैं।"
- The doctors list shows "—" (muted) for an empty NMC number instead of any warning.

- [ ] **Step 1: Regenerate the API types** for Tasks 1–4.
  Run: `cd frontend && npm run codegen`
  Expected: `openapi.json` and `types/openapi.d.ts` updated; `npm run typecheck` passes.
- [ ] **Step 2: Write the failing unit test** `nav-items.test.mjs`:
  `test("lab and messages are marked coming soon")`. Import `nav-items.ts` the way
  `entitlements.test.mjs` imports `.ts`. Assert that the items with labelKey `diagnostics` and
  `messages` have `comingSoon === true`, and no other item has it.
- [ ] **Step 3: Run it to verify it fails.**
  Run: `cd frontend && node --test src/lib/nav-items.test.mjs`. Expected: FAIL.
- [ ] **Step 4: Implement** the badge, the nav flag, the badge rendering in the sidebar, mobile
  nav and the two page headers, the copy, and the "—".
- [ ] **Step 5: Run** `npm run verify`. Expected: exit 0.

---

### Task 6: The doctor page (frontend)

**Files:**
- Create: `frontend/src/app/(app)/doctors/[doctorId]/page.tsx`. A server component: the gate is
  `doctor:read` (render `NoPermission` otherwise), then it renders `DoctorPage`.
- Create: `frontend/src/features/doctors/use-doctor.ts`, `doctor-page.tsx`,
  `involvement-switch.tsx`, `timings-panel.tsx`, `days-off-panel.tsx`, `patients-seen.tsx`
- Modify:
  - `frontend/src/features/admin/doctors-screen.tsx`: each name links to `/doctors/{id}`.
  - `frontend/src/features/queue/doctor-queues.tsx`: the column's doctor name links to
    `/doctors/{id}`.
  - en and hi message files: a new `doctors` namespace.
- Test: `frontend/e2e/doctor-page.spec.ts`

**Interfaces:**
- `use-doctor.ts` hooks (TanStack Query, `api` client):
  - `useDoctor(id)` → `GET /doctors/{id}`;
  - `useSetInvolvement(id)` → `PUT /doctors/{id}/involvement`, invalidating `["doctor", id]` and
    `["queue","board"]`;
  - `useAvailability(id)`, `useAddAvailability(id)`, `useRemoveAvailability(id)`;
  - `useDaysOff(id)` (`from_date` = today in Asia/Kolkata), `useAddDayOff(id)`,
    `useRemoveDayOff(id)`;
  - `useDoctorVisits(id, { page, dateFrom, dateTo })` → `GET /encounters/by-doctor/{id}`, 25 a page.
- Visibility, from `useCurrentUser()` and `can()`:
  - the admin (`can(user, "doctor:involvement:manage")`) sees the toggle, can edit timings and
    days off, and sees the patients list;
  - the doctor whose `doctor.user_id === user.id` sees a read-only page plus the patients list;
  - everyone else sees a read-only header, timings, days off and the switch as a status chip.
- Layout:
  - `PageHeader`: title is the doctor's display name; subtitle is department · specialty ·
    qualification; NMC appears as "NMC {number}" or "NMC —".
  - `Panel` "Doctors involvement": a switch for the admin, with the help line "On: the doctor
    records and closes their own visits. Off: reception and nurses mark this doctor's patients
    seen; the doctor can only view."
  - `Panel` "Timings": a table of day, hours, room, slot and max tokens; for the admin, an add
    form (weekday, start, end, slot minutes, room) and a remove button on each row.
  - `Panel` "Days off": upcoming dates; for the admin, an add form (date, optional reason) and a
    remove button.
  - `Panel` "Patients seen": a table of date, patient (link to `/patients/{id}`), UHID (`.tabular`)
    and an `EncounterStatusChip`, plus the date-range inputs and the existing `Pager`.

- [ ] **Step 1: Write the failing e2e test** `doctor-page.spec.ts`:
  - `admin sees the doctor page and edits timings`:
    - sign in as admin; open the admin doctors list; click "Dr Vikram Rao" and land on
      `/doctors/…`;
    - the heading level 1 has the text "Dr Vikram Rao";
    - add a Monday 09:00–10:00 session; the row appears; remove it; it is gone.
  - `admin adds and removes a day off`.
  - `the patients-seen list shows a visit Rao has seen`: register and send a patient to Rao,
    switch Rao off through the API, and mark the patient Seen as reception. As admin, the doctor
    page "Patients seen" shows the patient's name. Restore Rao to on in `finally`.
  - `reception sees timings but not the patients list`.
  - `lab and messages show Coming soon`: the sidebar items "Diagnostics" and "Messages" each
    contain "Coming soon".
- [ ] **Step 2: Run it to verify it fails.**
  Run: `cd frontend && npx playwright test e2e/doctor-page.spec.ts`. Expected: FAIL (no link, no
  page). The badge test passes after Task 5.
- [ ] **Step 3: Implement** to the Interfaces.
- [ ] **Step 4: Run** the spec plus `node --test src/lib/style-guard.test.mjs`. Expected: PASS.
  The style guard holds: no raw `<h1>`, boxes are `Panel`s.

---

### Task 7: Screens follow the switch (frontend)

**Files:**
- Modify:
  - `frontend/src/features/queue/doctor-queues.tsx`: show the Seen button only when
    `column.involvement_enabled === false`;
  - `use-queue-board.ts`: on a 409 `doctor_involved` or `doctor_entry_off`, show the server's
    message in a toast and invalidate `["queue","board"]`;
  - `frontend/src/features/patients/record/add-diagnosis.tsx` and every other control that calls
    a refused route (find them with
    `grep -rn "encounters/\${[^}]*}/\(notes\|diagnoses\|orders\|start\|complete\)\|/queue/\${[^}]*}/complete" frontend/src`):
    hide the control when the visit's doctor is off, and show the notice.
- Create: `frontend/src/features/doctors/doctor-entry-notice.tsx`:
  `DoctorEntryNotice({ doctorId }: { doctorId: string | null })`. It renders nothing while the
  doctor is involved or `doctorId` is null; otherwise a muted line: "Doctor entry is off for this
  doctor. Reception marks patients seen." Add the hook
  `useDoctorInvolved(doctorId: string | null): boolean` (true while loading or when null) in
  `use-doctor.ts`.
- Modify e2e: `frontend/e2e/reception-seen.spec.ts`, and any other spec that clicks "Seen"
  (`grep -rln "markSeen\|name: \"Seen\"\|/Seen/" frontend/e2e`). Before clicking, switch Rao off
  through the API as admin, and restore him in `afterEach`.
- Create: `frontend/e2e/doctor-involvement.spec.ts`

- [ ] **Step 1: Write the failing e2e test** `doctor-involvement.spec.ts`:
  - `switch off: reception marks seen and the doctor can only view`:
    - the admin turns Rao off on `/doctors/{id}`;
    - reception registers a patient to Rao; that row now has a "Seen" button; clicking it strikes
      the patient through;
    - signed in as Rao (`ACCOUNTS.doctor`), the patient's record shows the notice text, and there
      is no "Add diagnosis" button.
  - `switch on: no Seen button, the doctor works as before`:
    - the admin turns Rao on;
    - reception's row for a new patient has no "Seen" button;
    - as Rao, "Add diagnosis" is visible.
  - Both tests restore Rao to on in `finally`.
- [ ] **Step 2: Run it to verify it fails.**
  Run: `cd frontend && npx playwright test e2e/doctor-involvement.spec.ts`. Expected: FAIL.
- [ ] **Step 3: Implement** to the Files list above.
- [ ] **Step 4: Run** `doctor-involvement`, `reception-seen`, the other reception specs and
  `patient-record`. Expected: all pass.

---

### Task 8: Whole-system check

- [ ] **Step 1: Backend.** Run the full suite (the Task 3 command) and `mypy app`. Expected: all
  pass, mypy clean.
- [ ] **Step 2: Frontend.** Run `npm run verify` (expected exit 0), then the full
  `npx playwright test` to a log.
  - Expected: everything passes except the failures already recorded as pre-existing:
    - `idle-lock.spec.ts` ×2–3;
    - `clearance.spec.ts` ×3, while the local database holds more than 500 open test accounts.
  - Name any other failure.
- [ ] **Step 3: Docs.** Add a short "Doctors involvement" section to `README.md` (what the switch
  does, and who may flip it). Add a runbook line to `Docs/runbook.md`: "A doctor says they cannot
  save notes → check Doctors involvement on their doctor page."
