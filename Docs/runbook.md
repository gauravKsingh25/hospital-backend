# Runbook — when something is wrong

Short, symptom-first fixes for the problems this system actually has. Each
entry: what you see → why → what to do. Background and rationale live in the
[README](../README.md); this page is for when you need the answer now.

---

## First, find the request

Every API response carries an `X-Request-ID` header, every backend log line
prints it in brackets, and a 500 response includes it in the body:

```json
{ "error": { "code": "internal_error", "message": "…", "request_id": "3f9c…" } }
```

Find it in the browser's Network tab (the `/bff/...` request → Response
headers), then search the API's log output for that value. You get every line
that request wrote, including the ones from event handlers it triggered.

```
11:40:02 ERROR    [3f9c1e…] app.core.exceptions : unhandled exception: …
```

A load balancer or another service can pass its own `X-Request-ID`; if it is
safe (letters, digits, `.`, `_`, `-`, up to 64 characters) it is kept, so one ID
can follow a request across systems.

---

## "The app opens but sign-in fails" / "everything is empty"

**Why:** Postgres is down. `/health` still says `ok` because it deliberately
touches nothing — it only answers "is the process up?".

**Do:**

```bash
docker ps                                 # are postgres and redis listed and healthy?
docker compose up -d postgres redis       # start them if not
curl http://localhost:8000/health/ready   # checks database AND redis
```

Docker Desktop restarts stop these containers; this is the most common cause.

## "Every request takes ~4 seconds" / "the tests seem to hang"

**Why:** Redis is down. The RBAC permission cache reads it on every
authenticated request and waits out a connect retry before falling back to the
database. Correct, but slow.

**Do:** `docker compose up -d redis`, then confirm with `/health/redis`.

## "The first request after a while is very slow"

**Why:** Neon's free tier suspends idle compute; the next connection pays a cold
start. Expected, not an error.

**Do:** nothing. If *every* request is slow, read
[Database latency](../README.md#database-latency-read-this-before-blaming-the-ui)
— a distant Neon region costs ~870ms per SQL round trip from India.

## "The API logs `rls_enforced=False`" / "refuses to boot in production"

**Why:** `DATABASE_URL` connects as a role that can bypass row-level security
(usually the database owner). Tenant isolation is then not enforced at all.

**Do:**

```bash
cd backend
python scripts/bootstrap_db_role.py   # creates the restricted hms_app role
```

Then point `DATABASE_URL` at `hms_app` and restart. Keep `DIRECT_URL` on the
owner — migrations need it.

## "My backend change has no effect" (Windows)

**Why:** `uvicorn --reload` started from a background shell on Windows can log
"Reloading…" and keep serving the old code.

**Do:** stop the server and start it again without `--reload`:

```bash
cd backend
PYTHONUTF8=1 ../.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`PYTHONUTF8=1` matters too: without it, logging Hindi text or `₹` raises
`UnicodeEncodeError`. Confirm a new route exists via `/openapi.json`.

## "The Dockerised api cannot reach the database"

**Why:** `.env` holds host-machine values. `localhost:5432` inside the `api`
container is the container itself.

**Do:** either run the API on the host (above), or point both URLs at the
compose service name, as shown at the top of `docker-compose.yml`:
`postgres:5432`.

## "A migration produced an empty file" / "autogenerate wants to drop tables"

**Why:** the module's `models.py` is not imported by `backend/app/registry.py`,
so Alembic cannot see its tables.

**Do:** add the import there, delete the bad revision, and autogenerate again.
Never edit the database by hand.

```bash
cd backend
alembic current            # where the database is
alembic upgrade head       # bring it up to date
alembic downgrade -1       # undo the last revision
```

## "E2E specs fail with timeouts on the billing / lab boards"

**Why:** accumulated test visits. Boards are ordered oldest-first, so after
many runs a fresh visit lands on the last page.

**Do:**

```bash
cd backend
python scripts/reset_demo_data.py --yes   # local databases only; keeps staff, prices, wards
python scripts/seed_demo.py
```

## "The frontend build fails with `Generated API types are out of date`"

**Why:** a backend schema or permission changed and the committed TypeScript
types were not regenerated.

**Do:**

```bash
cd frontend
npm run codegen         # regenerate openapi.json, openapi.d.ts, rbac.generated.ts
npm run codegen:check   # what CI runs
```

---

## "The invite email never arrived" / sending real email with Gmail

Open **Platform console → Emails sent**. Every invite email is there with
`Sent` or `Failed` and, when it failed, the mail server's own reason.

- **Nothing was sent, it says Sent, but no email:** `MAIL_BACKEND` is still
  `console`. The message is in `backend/.mail-outbox/` (and the API log).
- **To send for real through Gmail:**
  1. Turn on 2-Step Verification for the Google account.
  2. Create an App Password at <https://myaccount.google.com/apppasswords>
     (name it "Hospital Management").
  3. In `.env`: `MAIL_BACKEND=smtp`, `SMTP_PASSWORD=<the 16 characters>`
     (`SMTP_USERNAME` and `MAIL_FROM` are the Gmail address).
  4. Recreate the API container so it rereads `.env` —
     `docker compose --profile app up -d api worker` (a plain restart keeps
     the old environment).
- **`SMTPAuthenticationError (535 …)`:** the App Password is wrong, or the
  normal Google password was used instead of an App Password.
- **`TimeoutError` / `ConnectionRefused`:** outbound port 587 is blocked on
  this network.
- **Gmail limits:** a personal account sends about 500 emails a day — far more
  than invites need. For a branded sender, point `SMTP_*` at a domain mailbox
  (Google Workspace, Zoho, SES); nothing else changes.

Fix the cause, then press **Resend** on the invite: it sends a fresh link and
the old one stops working.

## "Everything returns 402" / "a hospital is locked out"

`subscription_inactive` means the hospital's paid period ended more than
`GRACE_PERIOD_DAYS` ago, it cancelled and the period ran out, or it has no
subscription row. The response's `details.status` says which (`EXPIRED`,
`CANCELLED`, `NONE`).

Look at it:

```sql
SELECT h.code, s.plan_code, s.current_period_end, s.cancel_at_period_end, s.complimentary
  FROM hospitals h LEFT JOIN subscriptions s ON s.hospital_id = h.id
 WHERE h.code = 'KMC';
```

Until the console's subscriptions page exists (slice 3 of the design), put a
hospital on a plan by hand. Extend a paid period by 30 days:

```sql
UPDATE subscriptions
   SET current_period_end = GREATEST(current_period_end, now()) + interval '30 days',
       cancel_at_period_end = false, updated_at = now()
 WHERE hospital_id = (SELECT id FROM hospitals WHERE code = 'KMC')
   AND current_period_end IS NOT NULL;
```

The last line matters: a complimentary hospital has no end date, `GREATEST`
skips the NULL, and without that guard this would quietly give it one — and
lock it out a month later.

Change the plan with `SET plan_code = 'standard'` or `'professional'`; make it
complimentary with `SET current_period_end = NULL, complimentary = true`. A
hospital with no row: run `grant_complimentary` from a Python shell, or insert
one. The row is cached in Redis for 60 seconds (`sub:row:<hospital id>`);
after a hand edit either wait a minute or `DEL` that key.

In an incident, `SUBSCRIPTIONS_ENFORCED=false` and a restart lets every
hospital through and unlocks every feature. It is an escape hatch: turn it
back on.

## "A screen says Available on Professional" / "403 feature_not_in_plan"

The hospital is on Standard, which is OPD only. Nothing is broken. The lab,
wards, medicine list, accounts dashboard, patient messaging and past-period
reports are Professional. Change the plan as above.

## Checking a UPI payment (signup, renewal or upgrade)

Money goes straight to the UPI ID in `UPI_VPA`, so the system cannot know a
payment arrived: **you** check it against your bank app, then approve. Every
hospital, renewal and upgrade starts with this check, so do it carefully.
Payers are promised a decision within `PAYMENT_REVIEW_HOURS` (12 by default).

**Where:** Console → **Payments**. The overview shows how many are waiting.
Open one; it shows the **amount**, the **reference** (`NIR` and six letters
and digits), the **UTR** the payer typed, when they sent it, and the
screenshot if they added one.

**In your bank or UPI app** (the account `UPI_VPA` pays into):

1. Find a **credit of exactly that amount**, at about the time shown. Banks
   and apps call the UTR "UTR", "UPI Ref No", "RRN" or "Transaction ID": it
   is 12 digits.
2. **Match the UTR** digit by digit. This is the check that counts.
3. The reference (`NIR…`) is in the payment's note only if the payer typed it
   or their app kept it from the QR. Nice to see, not required.
4. The **screenshot is a hint, never proof**: it is easy to fake. Never
   approve on a screenshot, and never on a UTR you cannot find in your bank.

**Then:**

- **Found, same amount, same UTR** → **Approve** → confirm. For a signup this
  creates the hospital, makes the payer its administrator, starts the month
  and emails a receipt; for a renewal or upgrade it extends or upgrades the
  plan and emails a receipt. A new administrator is asked to sign in again,
  this time with two-step sign-in.
- **Not in the bank yet** → wait. UPI credits are usually instant but can lag
  a few hours. Check again before the 12 hours run out; if it is still
  missing, reject with "We have not received this payment yet — check the
  UTR in your UPI app, or wait for your bank and send it again."
- **Different amount** (they paid less, or the old price) → reject: "We
  received ₹X, the plan is ₹Y. …" and sort out the difference by email. Do
  not approve a short payment: the amount is fixed by the plan.
- **UTR already used** → the system refuses a UTR that another payment
  already carries, so the payer cannot send it twice. If they say it was a
  mistake, check whether the earlier payment it went with was theirs.
- **Paid to a different UPI ID** (an old QR, a typo) → it did not reach you;
  reject and say so.
- **Payer's name differs** from the account holder (a business or family
  account paid) → fine, if the amount and UTR match.
- **Your mistake or the bank's delay cost them days** → approve, then
  Console → **Subscriptions** → **Extend** with the days and the reason.

**Rejecting:** write the reason as you would to the person ("No payment with
this UTR reached our account — check the number in your UPI app"). It is
emailed to them; they correct the UTR on the same payment and it comes back
to your queue.

**"The receipt email didn't arrive."** Open the payment (Approved tab) →
**Email the receipt again**. The approval stands whatever the mail server
did; every attempt is in the Emails list with its error.

**"I approved the wrong payment."** There is no undo in the console. Suspend
the hospital from Hospitals while you sort it out; the audit log has
`payment.approve` with who, when, the UTR and the receipt number.

## "The signup code never arrived" / "Too many attempts"

Codes go by the platform mailer (check the Emails list for `OTP`; the code
itself is blanked there). One code a minute, five an hour per address; a code
lasts 10 minutes and survives five wrong tries. The public routes also cap
requests per client address per hour (signup 10, forgot-password 5,
contact 5): a whole hospital behind one NAT address shares that budget. If
every address looks the same, the web app is missing from `TRUSTED_PROXIES`.

**Ten wrong codes in a day close the address.** After 10 wrong codes for one
address (signup or reset, counted separately) within 24 hours, no new code is
sent or accepted for it until the oldest wrong tries are a day old. The person
is still told "code sent". This stops anyone guessing a reset code, at a
price: **anyone who knows an address can lock it this way**, by asking for
codes and typing wrong ones. If a real person is locked out, check the Emails
list for a run of codes they did not ask for (someone may be trying their
account), then clear the count on the database:

```sql
-- the address in lower case
UPDATE email_otps SET attempts = 0 WHERE email = 'person@example.com';
```

A longer-term fix is a reset link with a long random token instead of six
digits; then the cap can go.

## "Signup on the website says it could not reach Nirogya"

The website calls the API straight from the browser, so three things must line up:

- the website was **built** with `NEXT_PUBLIC_API_URL` pointing at the API and
  `NEXT_PUBLIC_APP_URL` at the app (they are baked in at build time; rebuild after changing them);
- the website's origin (e.g. `http://localhost:3100`) is in the API's `CORS_ORIGINS`. The Docker
  `api` container reads `.env` when it is created: after editing it, run
  `docker compose up -d api` so the container is recreated;
- the API is up (`curl $API/health/ready`).

Check CORS with:
`curl -i -X OPTIONS $API/api/v1/public/signup/start -H "Origin: <site origin>" -H "Access-Control-Request-Method: POST"`
— the answer must carry `access-control-allow-origin: <site origin>`.

Running the website e2e spec more than twice an hour from one machine uses up the
signup rate limit for that address (10 starts an hour) and the page answers "Too many tries".
In development, clear it with
`docker exec hospital-management-redis-1 redis-cli --scan --pattern 'rl:signup-*' | xargs docker exec -i hospital-management-redis-1 redis-cli del`.

If the last step lands on the app's sign-in page with "link expired", the one-time
handoff code (2 minutes) ran out: the account exists, so the person signs in with
the password they just chose.

## "We renewed but we're still locked out"

1. Console → **Payments**: is their renewal still *Awaiting review*? Then it
   has not been approved yet — check the bank app and approve it.
2. Approved, but still locked? Console → **Subscriptions**: the row's *Paid
   until* should be ~30 days ahead. If it is, ask them to reload; access is
   read fresh on every request (and the cached row is dropped on every change).
3. Still wrong: Activity log, filter `payment.approve` / `subscription.extend`
   for the hospital.

## Extending a subscription by hand

Console → **Subscriptions** → **Extend** → days (1–365) and a reason (it goes
into the activity log). Days count from the period end, or from today if it
has already ended. Use it for a delayed bank transfer you have verified, a
goodwill gesture, or an approval that needs untangling. Plans change only by
payment; there is no "change plan" button.

## "A doctor says they cannot save notes" / "reception has no Seen button"

Both come from the doctor's **Doctors involvement** switch, on their page
(`/doctors/{id}`, reached from Admin › Doctors or the doctor's name on the
reception board). Only the hospital admin can change it.

- Off: the doctor can only view (writes answer 409 `doctor_entry_off`), and
  reception and nurses mark that doctor's patients **Seen**.
- On: the doctor records their own visits, and there is no Seen button for
  them (`doctor_involved`).

Every change is in the audit log as `doctor.involvement_changed`.

## Health endpoints

| Endpoint | Answers | Touches |
|---|---|---|
| `/health` | is the process up? | nothing |
| `/health/db` | can it reach Postgres, how fast, is RLS enforced? | database |
| `/health/redis` | can it reach Redis? | redis |
| `/health/ready` | can this instance serve traffic? | both — 503 if either fails |

## Before every commit

```bash
cd backend  && ruff check . && ruff format --check . && mypy app && pytest
cd frontend && npm run verify     # codegen check + lint + typecheck + build
```
