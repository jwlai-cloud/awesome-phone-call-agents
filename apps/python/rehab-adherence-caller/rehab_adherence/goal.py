"""Build the call goal and the result schema.

Two safety properties are enforced here rather than documented and hoped for:

1. **The goal is assembled from a whitelist.** It is built from typed, validated,
   length-capped fields only. Arbitrary keys in the course file cannot reach the
   caller's mouth, because nothing in this module reads the raw document.
2. **The caller cannot ask a clinical question**, because the question set per
   action is fixed here and none of them assess anything. "What is stopping you
   attending" is logistics. "Is your pain worse" is assessment, and does not exist
   in this file.

The conversation itself is CALL-E's, and the model behind it is not selectable.
The only levers are this goal string and the result schema, so both are narrow on
purpose.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .decide import Decision
from .model import Clinic, Course, Patient

# What the caller may say. Anything not derivable from these is not sayable.
_DISCLOSURE = (
    "Open by saying you are an automated assistant calling on behalf of {clinic}. "
    "Say the reference {reference}. "
    "If the person doubts the call is genuine, do not try to persuade them: tell them to hang up "
    "and ring {callback} themselves, which is the clinic's own published number, and quote that reference. "
    "Never read out a phone number the person did not already give you."
)

_ASK_BY_ACTION = {
    "call_light_rebook": (
        "Say {name} missed one session and the clinic kept their place. "
        "Offer these times and book one: {slots}. "
        "Do not ask why they missed it."
    ),
    "call_blocker_and_rebook": (
        "Say {name} has missed some sessions recently. "
        "Ask one open question about what is making it hard to attend, and listen. "
        "Then offer these times and book one: {slots}. "
        "Offer that a clinician can call them back if they would rather talk to a person."
    ),
    "call_final_reengagement": (
        "Say {name} has not attended for a while and the clinic is checking once whether they want to continue. "
        "Ask whether they want to carry on, stop, or be left alone. "
        "If they want to carry on, offer these times and book one: {slots}. "
        "Make clear this is the last automated call either way."
    ),
    "call_confirm_discharge": (
        "A previous call recorded that {name} stopped attending because they felt better. "
        "Do not try to book them back in. "
        "Ask whether they consider the course finished, and whether they would like a clinician to confirm that with them. "
        "Do not ask about symptoms, pain, or progress."
    ),
}

_BOUNDARY = (
    "You are arranging attendance only. You must not give or discuss any medical, "
    "diagnostic, treatment, medication, or exercise advice, and you must not ask how the person "
    "is feeling or how their recovery is going. "
    "If the person raises a symptom or any health concern, do not assess it, do not reassure them, "
    "and do not book anything: say a clinician will call them back, and end the call politely. "
    "If the person asks to stop being called, agree immediately and end the call. "
    "Bring to the session: {bring}."
)


def reference(clinic: Clinic, course: Course, patient: Patient) -> str:
    """Stable, non-identifying reference the patient can quote back to the clinic."""
    digest = hashlib.sha256(f"{course.id}:{patient.id}".encode()).hexdigest()[:6].upper()
    return f"{clinic.reference_prefix}-{digest}"


def idempotency_key(course: Course, patient: Patient, decision: Decision) -> str:
    """One key per (course, patient, action, attempt). Replaying the same decision
    must not create a second call."""
    raw = f"{course.id}:{patient.id}:{decision.action}:{decision.signals.calls_made}"
    return f"rehab-{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


def build_task(clinic: Clinic, course: Course, patient: Patient, decision: Decision) -> str:
    ask = _ASK_BY_ACTION.get(decision.action)
    if ask is None:
        raise ValueError(f"{decision.action} is not a calling action; no task exists for it")
    slots = "; ".join(f"{slot.id} = {slot.label}" for slot in course.offered_slots)
    parts = [
        _DISCLOSURE.format(
            clinic=clinic.name,
            reference=reference(clinic, course, patient),
            callback=clinic.public_callback_number,
        ),
        ask.format(name=patient.first_name, slots=slots),
        _BOUNDARY.format(bring=course.bring),
    ]
    return " ".join(parts)


def build_result_schema(course: Course) -> dict[str, Any]:
    """Constrained output. Free text is allowed in exactly one field, and that
    field is told not to carry phone numbers."""
    return {
        "type": "object",
        "required": [
            "reached_patient",
            "continued_after_ai_disclosure",
            "attendance_intent",
            "chosen_slot_id",
            "barrier",
            "symptom_volunteered",
            "evidence_summary",
        ],
        "properties": {
            "reached_patient": {
                "type": "string",
                "enum": ["yes", "no", "unknown"],
                "description": "Whether the intended patient was reached, as opposed to someone else or an answering machine.",
            },
            "continued_after_ai_disclosure": {
                "type": "string",
                "enum": ["yes", "no", "unknown"],
                "description": "Whether the person agreed to continue after being told this is an automated assistant.",
            },
            "attendance_intent": {
                "type": "string",
                "enum": ["will_attend", "cannot_attend", "wants_to_stop", "considers_course_finished", "undecided", "unknown"],
                "description": "What the person said about continuing the course. Do not infer this from tone.",
            },
            "chosen_slot_id": {
                "type": "string",
                "enum": [*[slot.id for slot in course.offered_slots], "none", "unknown"],
                "description": "The slot the person accepted, none if they accepted no slot, unknown if unclear.",
            },
            "barrier": {
                "type": "string",
                "enum": ["transport", "cost", "work_or_childcare", "feels_better", "health_concern", "other", "none", "unknown"],
                "description": "The single main reason attendance is difficult, in the person's own framing. Use health_concern only when they raised it themselves.",
            },
            "symptom_volunteered": {
                "type": "string",
                "enum": ["yes", "no"],
                "description": "Yes if the person raised any symptom, pain, or health concern at any point, even in passing.",
            },
            "evidence_summary": {
                "type": "string",
                "description": "One short paraphrase of what the person actually said, supporting the fields above. Never include a phone number, address, or any clinical detail.",
            },
        },
        "additionalProperties": False,
    }
