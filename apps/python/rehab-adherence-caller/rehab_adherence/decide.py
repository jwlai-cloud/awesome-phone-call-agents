"""The trajectory state machine and the contact gate.

This is the part of the application that is not a phone call. A missed session
is not an event to react to; it is one point on a course. The same missed
session produces a different action depending on what came before it, and on
what the previous call established.

Nothing here is clinical. The states describe *attendance behaviour* and the
actions describe *logistics*. Any clinical judgement stays with the clinician,
and a volunteered symptom always leaves this machine and goes to a human.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .model import ContactPolicy, Course, Patient

# Statuses that mean "a session was on the calendar and the patient decided".
# A cancellation with notice is engagement, not absence, so it breaks a run of
# misses instead of extending it.
COUNTS_AS_SCHEDULED = {"attended", "missed"}

CALLING_ACTIONS = {
    "call_light_rebook",
    "call_blocker_and_rebook",
    "call_final_reengagement",
    "call_confirm_discharge",
}


@dataclass(frozen=True)
class Signals:
    """Why the machine decided what it decided. Rendered in every report."""

    attended: int
    missed: int
    consecutive_missed: int
    missed_in_last_four: int
    days_since_last_attended: int | None
    calls_made: int
    last_outcome: str | None


@dataclass(frozen=True)
class Decision:
    patient_id: str
    trajectory: str
    action: str
    reason: str
    signals: Signals
    blockers: tuple[str, ...] = ()

    @property
    def will_call(self) -> bool:
        return self.action in CALLING_ACTIONS and not self.blockers


def _signals(patient: Patient, today: date) -> Signals:
    scheduled = [s for s in patient.sessions if s.status in COUNTS_AS_SCHEDULED]
    attended = sum(1 for s in scheduled if s.status == "attended")
    missed = sum(1 for s in scheduled if s.status == "missed")

    # Count the trailing run of misses over *every* session, not just the
    # scheduled ones. Filtering first would delete the cancellation and let the
    # run close over it, which is the opposite of the intent: cancelling with
    # notice is engagement and must interrupt the run.
    consecutive = 0
    for session in reversed(patient.sessions):
        if session.status != "missed":
            break
        consecutive += 1

    last_four = scheduled[-4:]
    attended_dates = [s.date for s in patient.sessions if s.status == "attended"]
    days_since = (today - max(attended_dates)).days if attended_dates else None

    return Signals(
        attended=attended,
        missed=missed,
        consecutive_missed=consecutive,
        missed_in_last_four=sum(1 for s in last_four if s.status == "missed"),
        days_since_last_attended=days_since,
        calls_made=len(patient.contact_log),
        last_outcome=patient.contact_log[-1].outcome if patient.contact_log else None,
    )


def _broken_promise(patient: Patient, policy: ContactPolicy, today: date) -> bool:
    """Said yes, then did not come. The most common real outcome, and the one
    that must not produce another identical call."""
    promises = [c for c in patient.contact_log if c.outcome == "promised_return"]
    if not promises:
        return False
    promise = promises[-1]
    assert promise.promised_date is not None  # guaranteed by model validation
    if today <= promise.promised_date + timedelta(days=policy.grace_days_after_promise):
        return False  # still inside the window; not yet broken
    return not any(
        s.status == "attended" and s.date >= promise.date for s in patient.sessions
    )


def classify(patient: Patient, course: Course, policy: ContactPolicy, today: date) -> tuple[str, str, str]:
    """Return (trajectory, action, reason). Order of these checks is the design."""
    signals = _signals(patient, today)
    outcomes = {c.outcome for c in patient.contact_log}

    # Hard stops first. A refusal is permanent for the course.
    if "refused_contact" in outcomes:
        return ("refused", "stop_contact", "patient refused further automated contact")

    # A symptom leaves the machine. It is never triaged, rebooked, or re-asked.
    if "symptom_reported" in outcomes:
        return (
            "symptom_flagged",
            "escalate_to_clinician",
            "a previous call recorded a volunteered symptom; a clinician must review before any further contact",
        )

    if not signals.attended and not signals.missed:
        return ("no_history", "skip", "no scheduled sessions recorded; nothing to reason about")

    if signals.attended >= course.planned_sessions:
        return ("course_complete", "no_action", "planned sessions completed")

    if _broken_promise(patient, policy, today):
        return (
            "broken_promise",
            "escalate_to_clinician",
            "patient agreed to return and then did not attend; repeating the same call is not the answer",
        )

    if signals.last_outcome == "stopped_feels_better":
        return (
            "stopped_improved",
            "call_confirm_discharge",
            "patient stopped because symptoms improved; confirm an intentional finish rather than chasing attendance",
        )

    if signals.days_since_last_attended is not None and signals.days_since_last_attended > course.lapse_after_days:
        return (
            "lapsed",
            "call_final_reengagement",
            f"no attendance for {signals.days_since_last_attended} days "
            f"(threshold {course.lapse_after_days}); one final offer, then stop",
        )

    if signals.consecutive_missed >= 2:
        return (
            "disengaging",
            "call_blocker_and_rebook",
            f"{signals.consecutive_missed} consecutive missed sessions; find the barrier and offer a clinician callback",
        )

    if signals.missed_in_last_four >= 2:
        return (
            "wobbling",
            "call_blocker_and_rebook",
            f"{signals.missed_in_last_four} of the last {min(4, signals.attended + signals.missed)} "
            "sessions missed without a consecutive run; attendance is slipping",
        )

    if signals.consecutive_missed == 1 and signals.attended >= 3:
        return (
            "first_slip",
            "call_light_rebook",
            f"one missed session after {signals.attended} attended; rebook without interrogating",
        )

    if signals.consecutive_missed == 1:
        return (
            "early_slip",
            "call_blocker_and_rebook",
            f"missed a session with only {signals.attended} attended; early drop-off risk is highest here",
        )

    return ("on_track", "no_action", "attending as planned")


def gate(patient: Patient, course: Course, policy: ContactPolicy, today: date, action: str) -> tuple[str, ...]:
    """Named blockers that stop a call. Empty tuple means the call may proceed.

    Every refusal names itself. Nothing here guesses a missing value.
    """
    if action not in CALLING_ACTIONS:
        return ()

    blockers: list[str] = []

    if not patient.consent_to_automated_contact:
        blockers.append("consent_missing")
    if not patient.phone_is_e164:
        blockers.append("phone_not_e164")
    if not course.offered_slots:
        blockers.append("no_offered_slots")
    if len(patient.contact_log) >= policy.max_calls_per_course:
        blockers.append("attempts_exhausted")

    if patient.contact_log:
        days_since_call = (today - patient.contact_log[-1].date).days
        if days_since_call < policy.min_days_between_calls:
            blockers.append("cooldown_active")

    promises = [c for c in patient.contact_log if c.outcome == "promised_return"]
    if promises:
        promise = promises[-1]
        assert promise.promised_date is not None
        if today <= promise.promised_date + timedelta(days=policy.grace_days_after_promise):
            blockers.append("promise_window_open")

    return tuple(blockers)


def decide(patient: Patient, course: Course, policy: ContactPolicy, today: date) -> Decision:
    trajectory, action, reason = classify(patient, course, policy, today)
    return Decision(
        patient_id=patient.id,
        trajectory=trajectory,
        action=action,
        reason=reason,
        signals=_signals(patient, today),
        blockers=gate(patient, course, policy, today, action),
    )


# --- after the call: what the result means for the next decision ----------------
#
# This closes the loop. A call result is not a report, it is the input to the next
# classification, which is why "said yes then did not come" is detectable at all.

_TERMINAL_NOT_REACHED = {"NO_ANSWER", "BUSY", "DECLINED", "FAILED", "CANCELED", "CANCELLED", "EXPIRED"}


@dataclass(frozen=True)
class Interpretation:
    outcome: str
    promised_date: date | None
    escalate_to_clinician: bool
    note: str


def interpret(status: str, structured: dict | None, course: Course) -> Interpretation:
    """Map one CALL-E result onto the next contact-log entry.

    Conservative by construction: anything unclear becomes `undecided`, never a
    booking. Silence is never read as agreement.
    """
    upper = (status or "").upper()

    if upper == "VOICEMAIL":
        return Interpretation("voicemail", None, False, "voicemail reached; no answer treated as no reply")
    if upper in _TERMINAL_NOT_REACHED:
        return Interpretation("no_answer", None, False, f"not reached ({upper})")
    if not isinstance(structured, dict):
        return Interpretation("undecided", None, False, "call completed without a usable structured result")

    # A health concern always leaves the machine, whatever else was said, and
    # even if the patient also accepted a slot. A clinician decides next.
    if structured.get("symptom_volunteered") == "yes" or structured.get("barrier") == "health_concern":
        return Interpretation(
            "symptom_reported",
            None,
            True,
            "patient raised a symptom or health concern; not triaged, not rebooked, escalated to a clinician",
        )

    reached = structured.get("reached_patient")
    if reached == "no":
        return Interpretation("no_answer", None, False, "the intended patient was not reached")

    if structured.get("continued_after_ai_disclosure") == "no":
        return Interpretation(
            "refused_contact", None, False, "patient declined to continue after AI disclosure; no further automated contact"
        )

    intent = structured.get("attendance_intent")
    if intent == "wants_to_stop":
        return Interpretation("refused_contact", None, False, "patient asked to stop; contact ends for this course")
    if intent == "considers_course_finished":
        return Interpretation(
            "stopped_feels_better", None, True, "patient considers the course finished; clinician should confirm discharge"
        )
    if intent == "will_attend":
        slot_id = structured.get("chosen_slot_id")
        slot = next((s for s in course.offered_slots if s.id == slot_id), None)
        if slot is None:
            return Interpretation(
                "undecided", None, False, "patient said they would attend but accepted no identifiable slot"
            )
        if reached != "yes":
            # Someone took the appointment, but the caller never confirmed it was
            # the patient. Recording it as a booking would put an unverified
            # person's word into the record; discarding it would lose a real
            # slot. Neither is safe on its own, so a human checks.
            return Interpretation(
                "identity_unconfirmed",
                None,
                True,
                f"a slot was accepted ({slot.label}) but the caller never confirmed "
                "it was speaking to the patient; a clinician should verify before this is treated as booked",
            )
        return Interpretation("promised_return", slot.date, False, f"booked {slot.label}")
    if intent == "cannot_attend":
        return Interpretation("cannot_attend", None, False, "patient cannot attend the offered times")

    return Interpretation("undecided", None, False, "no clear intent was established")
