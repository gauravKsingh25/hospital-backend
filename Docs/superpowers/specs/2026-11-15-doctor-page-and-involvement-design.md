# Doctor page, per-doctor "Doctors involvement", NMC optional, "Coming soon" badges

Date: 2026-11-15. Scope: backend `scheduling`, `clinical`, and the `frontend/` app.

## Intent

Some doctors in a hospital use the software and some never log in. The owner wants:

- **A switch per doctor**, called "Doctors involvement".
  - **On:** today's flow. The doctor records and closes their own visits.
  - **Off:** reception, nurses and anyone with access to that doctor's patient list mark each
    patient Seen. The doctor can still look at their patients but cannot change anything.
  - The admin can flip it at any time, including mid-day, and it takes effect straight away.
- **A proper page for each doctor**, showing their timings and the patients they have seen.
- **The NMC registration number optional** when adding a doctor.
- **Lab testing and messages labelled "Coming soon".** Messages are not sent through a real
  WhatsApp or SMS provider yet.

## Decided with the owner

- The switch is **per doctor**, not hospital-wide.
- **Only the hospital admin can change the switch**, and only the admin edits a doctor's timings.
- **"Read-only" is enforced by the server** (option 1 below), not only by hiding buttons.
- **Lab and messages:** a "Coming soon" **badge only**. Both keep working underneath; doctors can
  still order tests.
- **No "Attendant" role is added.** Anyone with queue access gets the Seen action.

## Approaches considered (the owner picked 1)

1. **Server-enforced setting on the doctor record (chosen).**
   - A per-doctor flag, `involvement_enabled` (default on), is stored on the doctor's record.
   - While it is off, the server refuses that doctor's patients' doctor-side writes and allows
     the Seen action for queue staff.
   - The screens follow the flag.
   - Read-only then holds against open tabs and refreshes, and every visit still closes through
     the state machine.
2. **Screens only.** Rejected: an old or open tab could still save notes, so it isn't truly
   read-only.
3. **Per-person permission overrides in RBAC.** Rejected:
   - roles are per role, not per person, so this needs a new override mechanism;
   - the setting is about a doctor's patients, not a login.

## Design

### 1. The per-doctor switch (`scheduling` owns it)

**Data:**
- `doctors.involvement_enabled: bool`, NOT NULL, default `true`.
- An Alembic migration sets every existing doctor to `true`, so today's behaviour is unchanged
  on deploy.

**Changing it:**
- New route: `PUT /api/v1/doctors/{doctor_id}/involvement` with body `{ "enabled": bool }`.
- New permission `doctor:involvement:manage`, granted to HOSPITAL_ADMIN (and PLATFORM_ADMIN, as
  for other hospital settings).
- The route is tenant-scoped like every doctor route.
- Every change writes an audit row: who, when, before and after.
- `DoctorRead` gains `involvement_enabled`.

**Effect while off.** This applies to OPD visits whose encounter `doctor_id` is that doctor.
- **Refused (409, code `doctor_entry_off`, message "Doctor entry is off for this doctor.
  Reception marks patients seen."), for any actor:**
  - start consultation;
  - complete consultation;
  - write note;
  - sign note;
  - record diagnosis;
  - place order.

  These checks live in `clinical/service.py`. Clinical reads the flag through
  `scheduling`'s service and never reads its table.
- **Still allowed:**
  - every read;
  - recording vitals (a nurse's act);
  - lab and billing work on orders that already exist;
  - the edge-case terminals (DECEASED, REFERRED_OUT, LAMA) by RECORDS_OFFICER, which are not
    doctor-entry acts.
- **Seen action:** `POST /queue/{entry_id}/seen` (already exists, gated by `queue:manage`, which
  reception and nurses hold) is **allowed only when the token's doctor has involvement off**.
  When it is on, it answers 409 `doctor_involved` ("This doctor records their own visits.").
  - This is a change from today, where reception can always mark Seen.
  - The visit still closes through the existing `mark_seen` path and the clinical state machine,
    with no direct status writes.

**What Seen does for an off doctor (my call, from the builder check):**
- Today `mark_seen` closes the token, but the visit itself only moves to IN_CONSULTATION and waits
  for the doctor's "complete visit".
- For an off doctor nobody can complete it, so **Seen also completes the visit through the
  clinical state machine**:
  - COMPLETED when nothing is pending;
  - PENDING_CLEARANCE when charges, lab or medicines are still pending, exactly as the doctor's
    own "complete visit" would.
- One transition call, one audit row, no direct status writes.

**The rest of the guard (my calls):**
- **Also refused for an off doctor's visits, for any actor including the hospital admin:**
  - editing the visit's follow-up and instructions (`update_encounter`);
  - cancelling an order;
  - the doctor's own queue "complete".
  The admin's route around the guard is to flip the switch.
- **Still allowed:**
  - reception and nurses calling and starting tokens on that doctor's queue (they run it now);
  - reassigning a visit to another doctor (the patient then goes to a doctor who records
    their own visit; that is not a bypass);
  - doctors creating templates (they are not patient records).
- **Which visits:** every OPD visit whose `doctor_id` is the off doctor, including still-open
  visits from earlier days. The guard never blocks lab, pharmacy or billing clearing those
  visits.

**Follow-ups for an off doctor's patients (my call; the owner did not choose):**
- None are set.
- Seen takes no follow-up date, and nobody can edit the visit's follow-up while the switch is off.
- Reception books the next visit by hand if the patient asks.
- Adding an optional follow-up date to the Seen step is a later, separate change.

**Status:** the owner approved this design, including the calls marked above, on 2026-11-15.

**Switching mid-day:**
- Patients already marked Seen stay closed.
- A visit already IN_CONSULTATION when the switch goes off becomes read-only for the doctor;
  queue staff mark it Seen.
- A switch turned on mid-day gives the doctor full entry on every visit that is not yet closed.

**IPD is unchanged.** Ward rounds, admission orders and the discharge summary are not affected
by the switch. The owner's request is about OPD screening. *(My call, marked so the owner can
correct it.)*

**Screens:**
- **Reception board and the nurse's queue:** the Seen button shows only on rows whose doctor has
  involvement off.
- **The doctor's consultation screen,** for an off doctor:
  - every edit control is hidden;
  - a one-line notice says "Doctor entry is off for you. Reception marks your patients seen.";
  - the patient's details, vitals and history stay visible.

### 2. The doctor page

**Route:** `/doctors/{doctorId}` in the app, linked from the admin doctors list and from the
doctor's name on the queue board.

**Who sees what:**
- **Admin:** everything, and edits the timings, the days off and the involvement switch.
- **Reception and nurses:** read-only header, timings and switch state. Not the patients list.
- **The doctor:** their own page, read-only, including their patients list.

**Contents:**
- **Header:** name, department, specialty, qualification, NMC number (or "—"), and the
  involvement switch (a toggle for the admin, a status chip for everyone else).
- **Timings:** the weekly hours from `DoctorAvailability` (day, start to end, room/location,
  slot length, maximum tokens) and upcoming days off from `availability_exceptions`. The admin
  adds, edits and removes them through the routes that already exist (`/doctors/{id}/availability`,
  `/doctors/{id}/exceptions`).
- **Patients seen:**
  - The doctor's past OPD visits, newest first, paginated 25 a page.
  - Columns: date, patient name (linked to the record), UHID, how the visit ended (the
    status chip).
  - **Which visits (my call):** those that reached a consultation:
    - IN_CONSULTATION, AWAITING_RESULTS, PENDING_CLEARANCE, COMPLETED, ADMITTED;
    - the three edge-case terminals.
    Today's are included. NO_SHOW and CANCELLED are left out, because the doctor never saw those
    patients.
  - A date-range filter.
  - Served by a new paginated `clinical` list endpoint filtered by `doctor_id`, guarded so that
    only the admin or that doctor gets it.

**Look:** the Frost and slate shared pieces (`PageHeader`/`PageTitle`, `Panel`, status chips,
mono numbers), with no new styles.

### 3. NMC optional (small change)

- It is already optional in the API and the "Add doctor" form.
- Change the hint `registrationNumberHint` (en + hi) to: "Optional. Needed only on lab reports
  this doctor signs."
- The doctors list shows "—" for an empty number instead of a warning.

### 4. "Coming soon" badges (small change)

- A small muted "Coming soon" pill, from one shared component:
  - next to the **Diagnostics** and **Messages** sidebar and mobile-nav items;
  - in the page header of the lab and messages screens.
- Nothing is disabled.
- Text in en and hi.

## Non-goals and anti-goals

- No Attendant role.
- No hospital-wide switch.
- No change to IPD.
- No lab or messaging behaviour change.
- No new visual styles.
- **Anti-goal:** an off switch that a doctor can get around from an open tab, or a visit closed
  outside the state machine.
- **Anti-goal:** a doctor's full patient history shown to staff who do not need it.

## Tests (definition of done)

- **Service tests:**
  - each refused write for an off doctor;
  - vitals still allowed;
  - Seen allowed only when off;
  - switch flips mid-consultation;
  - audit row written;
  - only admin can flip;
  - the patients-seen list is restricted to admin and that doctor, and paginated;
  - tenant isolation on the new routes, added to the isolation walk.
- **e2e:**
  - admin flips a doctor off, reception marks a patient Seen, the doctor's screen is read-only;
    flip back on and the doctor completes a visit;
  - doctor page timings edit by admin;
  - badges visible.
- **Update** the existing `reception-seen.spec.ts`: it must first switch the doctor's involvement
  off, because Seen is no longer always available.

## Left to the builder

- Exact component split of the doctor page.
- The form layout for timings.
- The badge component's name.
- Where the link to the doctor page sits on the queue board.
