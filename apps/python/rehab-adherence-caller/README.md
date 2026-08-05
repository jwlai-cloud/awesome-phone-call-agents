# Rehab course-adherence caller

Rehabilitation is a course, not an appointment. Eight, twelve, twenty sessions,
and the clinical failure is not one missed visit — it is the quiet slide out of
the course that nobody notices until the file is closed.

A missed session on its own says almost nothing. **The same missed session means
different things depending on what came before it**, and that is the part this
app decides. Calling everyone who missed something is a reminder bot. Calling
the right person, about the right thing, once, is a different job.

```text
1. p_ivy   +15*******01  ->  first_slip / call_light_rebook
     why: one missed session after 6 attended; rebook without interrogating
     signals: attended=6 missed=1 consecutive=1 last4_missed=1 days_since_attended=12 calls=0

2. p_omar  +15*******02  ->  early_slip / call_blocker_and_rebook
     why: missed a session with only 1 attended; early drop-off risk is highest here
     signals: attended=1 missed=1 consecutive=1 last4_missed=1 days_since_attended=7 calls=0
```

Ivy and Omar missed exactly one session, the same one, on the same day. Ivy gets
a short call that books her back in and **does not ask why she missed it** —
she has attended six times and does not need interrogating. Omar has been once.
Early drop-off is where a course is lost, so his call asks what is making it
hard and offers a clinician callback. Same event, different call. A regression
asserts this, because if it ever stops being true the app is a reminder bot with
extra steps.

## What it does not do

It arranges attendance. That is the whole remit.

It never gives or discusses medical, diagnostic, treatment, medication, or
exercise advice, and it never asks how the person is feeling or how recovery is
going — "what is making it hard to attend" is logistics, "is your pain worse" is
assessment and does not exist in this codebase. **If the person raises a symptom,
the call does not assess it, does not reassure, and does not book anything**: a
clinician is told, and the patient is dropped out of the automated path
entirely. That holds even when the same call also accepted an appointment slot —
there is a test for it.

Everything clinical is authored by the clinician. The goal text is assembled
from a whitelist of validated fields, so adding `diagnosis`, `medication`, or a
clinical note to the course file does not change one character of what the caller
says. There is a test for that too.

## Try it without an account

Python 3.11 or newer. No dependencies.

```bash
cd apps/python/rehab-adherence-caller
python3 -m pytest -q                       # 44 tests, no credentials, no network
python3 -m rehab_adherence preview --course examples/course.example.json --today 2026-08-11
```

`preview` is the default and places no call. To see the exact words the caller
would use, with the number masked:

```bash
python3 -m rehab_adherence preview --course examples/course.example.json \
  --today 2026-08-11 --show-task p_rosa
```

To play the whole course against authored responses — still no network, no
telephone, no credentials:

```bash
python3 -m rehab_adherence run --course examples/course.example.json \
  --fixture examples/responses.example.json --today 2026-08-11
```

The shipped fixture covers a booking, a patient with no transport, a voicemail,
a patient who considers the course finished, and a patient who raises a health
concern and is escalated instead of rebooked.

Fixtures are **written by hand, never captured**: `SECURITY.md` forbids
committing call recordings and private call transcripts, so no real call is ever
the source of a file in this directory.

## Trajectories

Checked in this order. Order is the design, not an accident — a refusal outranks
everything, and a broken promise outranks the missed session that revealed it.

| Trajectory | When | Action |
| --- | --- | --- |
| `refused` | a previous call recorded a refusal | `stop_contact` — permanent for the course |
| `symptom_flagged` | a previous call recorded a volunteered symptom | `escalate_to_clinician`, no call |
| `no_history` | no scheduled sessions recorded | `skip` — nothing to reason about |
| `course_complete` | planned sessions attended | `no_action` |
| `broken_promise` | agreed to return, then did not attend | `escalate_to_clinician`, no call |
| `stopped_improved` | a previous call recorded "feels better" | `call_confirm_discharge` — confirm a finish, do not chase |
| `lapsed` | no attendance for longer than the threshold | `call_final_reengagement` — one last offer, then stop |
| `disengaging` | two or more consecutive misses | `call_blocker_and_rebook` + clinician callback offer |
| `wobbling` | two of the last four missed, not consecutive | `call_blocker_and_rebook` |
| `first_slip` | one miss after three or more attended | `call_light_rebook` |
| `early_slip` | one miss with few attended | `call_blocker_and_rebook` |
| `on_track` | attending as planned | `no_action` |

**A cancellation with notice is engagement, not absence.** It interrupts a run of
misses rather than extending it, so patients who ring ahead are not treated like
patients who vanish. (This started as a bug — the first implementation filtered
cancellations out before counting the run, which silently closed the run over
them. The test caught it.)

**"Said yes, then did not come" is a first-class state.** It is the most common
real outcome of a rebooking call, and the response is not to place the same call
again — it goes to a human.

## The gate

A trajectory that wants a call still has to get past the gate. Every refusal
names itself, and nothing is guessed:

| Blocker | Meaning |
| --- | --- |
| `consent_missing` | the patient has not agreed to automated contact |
| `phone_not_e164` | the number is not E.164; it is not repaired or inferred |
| `no_offered_slots` | there is nothing to book, so there is nothing to call about |
| `attempts_exhausted` | the per-course call cap is reached |
| `cooldown_active` | the minimum gap since the last call has not passed |
| `promise_window_open` | the patient's promised date has not been and gone yet |

A bad phone number is a named blocker, not an exception — one unusable record
must not stop the rest of the course.

## How it uses CALL-E

Per patient the app builds one `task`, one constrained `result_schema`, and a
stable `idempotency_key` derived from course, patient, action and attempt, then
calls `client.calls.create(...)` and `client.calls.wait_for_result(...)` against
`calle-ai==0.2.0`.

The conversation belongs to CALL-E and its model is not selectable, so the two
levers this app has are used narrowly on purpose: the goal string, and the
result schema. The schema is closed (`additionalProperties: false`) with enum
fields for whether the patient was reached, whether they continued after AI
disclosure, their attendance intent, the slot accepted, the single main barrier,
and whether any symptom was volunteered. Exactly one free-text field exists, and
it is instructed to carry no phone number, address, or clinical detail.

Results are read conservatively:

- **Silence is never agreement.** A completed call with no usable structured
  result becomes `undecided`, never a booking.
- "Will attend" without an identifiable slot is `undecided`, not a booking.
- Voicemail is a voicemail, not a refusal.
- Declining after the AI disclosure ends automated contact for the course.

Provider text is untrusted — CALL-E's own skill says so — so anything
phone-shaped is redacted out of provider output before it is displayed or
written.

## Disclosure, and the patient who suspects a scam

The caller opens by saying it is an automated assistant, names the clinic, and
gives a stable reference derived from course and patient that contains no name
or number.

A patient who suspects a scam call is behaving correctly, and an automated caller
cannot win that argument. So it does not try: it tells them to hang up, ring the
clinic's own published number themselves, and quote the reference. Failing safe
into an out-of-band check is the only honest answer to "how do I know this is
really you."

## Running it live

Live mode places real phone calls. Use it only with numbers you own or are
explicitly authorized to call, and never with the reserved `+1555…` sample
numbers in `examples/`.

```bash
pip install -e '.[live]'
export CALLE_API_KEY="<CALL_E_API_KEY>"

python3 -m rehab_adherence run \
  --course private/course.json \
  --live --confirm-authorized \
  --output private/ledger.jsonl
```

- The CALL-E SDK is an **optional `live` extra**. A default install cannot place
  a call because `calle` is not installed at all — a structural guarantee rather
  than a promise.
- `--live` alone is not enough; `--confirm-authorized` is also required.
- The API key is read from the environment only. Never put it in a file or paste
  it into a chat.
- Calls are placed serially, one per eligible patient. A failure on one patient
  is recorded and the course continues.
- The idempotency key is stable per (course, patient, action, attempt), so
  re-running the same plan does not create a second call.

**Side effects and cancellation.** The only side effect is outbound phone calls,
plus the JSONL ledger written to `--output`. There is no recurring job, no
scheduler and no retry: recurrence belongs to the host, so this app does exactly
one pass per invocation. Cancelling means not running it — there is nothing to
disable, and nothing to roll back beyond deleting the ledger file. Booking a slot
is recorded here, not written into a clinic system; a human still confirms it.

## Limits, and what has not been tested

- **No live call has been placed with this code.** The CALL-E path is written
  against the `calle-ai==0.2.0` surface used by the other Python apps in this
  repository and exercised through an injected port in tests, but the live path
  is unverified against a real call. Treat it as unproven until you run it.
- Thresholds — the lapse window, the call cap, the cooldown, "three attended"
  for a light rebook — are **defaults chosen for a demonstration, not clinical
  guidance.** They belong to whoever runs the clinic.
- The app cannot tell why someone stopped unless a call established it. The
  first call into a lapsed course is genuinely a guess about which conversation
  to have.
- Attendance history has to come from somewhere. This app reads a JSON file; a
  real deployment needs whatever the practice-management system will give it,
  and that is usually an export rather than an API.
- The trajectory reads attendance, not recovery. A patient can attend every
  session and be doing badly, and nothing here would know or should claim to.
