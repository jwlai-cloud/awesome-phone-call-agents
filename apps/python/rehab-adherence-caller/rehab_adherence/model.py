"""Input model for a rehab course and its patients.

Everything clinical is authored by the clinician and carried as opaque text that
this application never interprets. The only fields the caller is allowed to
speak are whitelisted in `goal.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

E164 = re.compile(r"^\+[1-9]\d{7,14}$")

SESSION_STATUSES = {"attended", "missed", "cancelled_with_notice", "rescheduled"}

# Outcomes a previous call may have recorded. These feed the next decision, which
# is the whole point of the state machine: the same missed session means
# different things depending on what the last call established.
CONTACT_OUTCOMES = {
    "no_answer",
    "voicemail",
    "promised_return",
    "cannot_attend",
    "wants_to_stop",
    "stopped_feels_better",
    "refused_contact",
    "symptom_reported",
    "identity_unconfirmed",
    "undecided",
}


class InputError(ValueError):
    """Raised when the course file cannot be trusted. Never guess a value."""


def _text(raw: Any, field_name: str, *, maximum: int = 200) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise InputError(f"{field_name} must be a non-empty string")
    value = " ".join(raw.split())
    if len(value) > maximum:
        raise InputError(f"{field_name} must be {maximum} characters or fewer")
    return value


def _int(raw: Any, field_name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise InputError(f"{field_name} must be an integer")
    if not minimum <= raw <= maximum:
        raise InputError(f"{field_name} must be between {minimum} and {maximum}")
    return raw


def _date(raw: Any, field_name: str) -> date:
    if not isinstance(raw, str):
        raise InputError(f"{field_name} must be an ISO date string")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise InputError(f"{field_name} must be an ISO date (YYYY-MM-DD)") from exc


@dataclass(frozen=True)
class Clinic:
    name: str
    public_callback_number: str
    reference_prefix: str

    @staticmethod
    def parse(raw: Any) -> "Clinic":
        if not isinstance(raw, dict):
            raise InputError("clinic must be an object")
        number = _text(raw.get("public_callback_number"), "clinic.public_callback_number", maximum=20)
        if not E164.fullmatch(number):
            raise InputError("clinic.public_callback_number must be E.164, e.g. +15550100000")
        prefix = _text(raw.get("reference_prefix"), "clinic.reference_prefix", maximum=8)
        if not prefix.isalnum():
            raise InputError("clinic.reference_prefix must be alphanumeric")
        return Clinic(
            name=_text(raw.get("name"), "clinic.name", maximum=80),
            public_callback_number=number,
            reference_prefix=prefix.upper(),
        )


@dataclass(frozen=True)
class Slot:
    id: str
    label: str
    date: date

    @staticmethod
    def parse(raw: Any, index: int) -> "Slot":
        if not isinstance(raw, dict):
            raise InputError(f"course.offered_slots[{index}] must be an object")
        return Slot(
            id=_text(raw.get("id"), f"course.offered_slots[{index}].id", maximum=32),
            label=_text(raw.get("label"), f"course.offered_slots[{index}].label", maximum=80),
            date=_date(raw.get("date"), f"course.offered_slots[{index}].date"),
        )


@dataclass(frozen=True)
class Course:
    id: str
    programme: str
    planned_sessions: int
    lapse_after_days: int
    offered_slots: tuple[Slot, ...]
    bring: str

    @staticmethod
    def parse(raw: Any) -> "Course":
        if not isinstance(raw, dict):
            raise InputError("course must be an object")
        slots_raw = raw.get("offered_slots")
        if not isinstance(slots_raw, list):
            raise InputError("course.offered_slots must be a list")
        slots = tuple(Slot.parse(item, index) for index, item in enumerate(slots_raw))
        if len({slot.id for slot in slots}) != len(slots):
            raise InputError("course.offered_slots ids must be unique")
        return Course(
            id=_text(raw.get("id"), "course.id", maximum=40),
            programme=_text(raw.get("programme"), "course.programme", maximum=80),
            planned_sessions=_int(raw.get("planned_sessions"), "course.planned_sessions", minimum=2, maximum=60),
            lapse_after_days=_int(
                raw.get("lapse_after_days", 21), "course.lapse_after_days", minimum=7, maximum=120
            ),
            offered_slots=slots,
            bring=_text(raw.get("bring"), "course.bring", maximum=120),
        )


@dataclass(frozen=True)
class ContactPolicy:
    min_days_between_calls: int
    max_calls_per_course: int
    grace_days_after_promise: int

    @staticmethod
    def parse(raw: Any) -> "ContactPolicy":
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise InputError("contact_policy must be an object")
        return ContactPolicy(
            min_days_between_calls=_int(
                raw.get("min_days_between_calls", 7), "contact_policy.min_days_between_calls",
                minimum=1, maximum=90,
            ),
            max_calls_per_course=_int(
                raw.get("max_calls_per_course", 3), "contact_policy.max_calls_per_course",
                minimum=1, maximum=10,
            ),
            grace_days_after_promise=_int(
                raw.get("grace_days_after_promise", 2), "contact_policy.grace_days_after_promise",
                minimum=0, maximum=30,
            ),
        )


@dataclass(frozen=True)
class Session:
    date: date
    status: str

    @staticmethod
    def parse(raw: Any, patient_id: str, index: int) -> "Session":
        if not isinstance(raw, dict):
            raise InputError(f"{patient_id}.sessions[{index}] must be an object")
        status = _text(raw.get("status"), f"{patient_id}.sessions[{index}].status", maximum=32)
        if status not in SESSION_STATUSES:
            raise InputError(
                f"{patient_id}.sessions[{index}].status must be one of {sorted(SESSION_STATUSES)}"
            )
        return Session(date=_date(raw.get("date"), f"{patient_id}.sessions[{index}].date"), status=status)


@dataclass(frozen=True)
class Contact:
    date: date
    outcome: str
    promised_date: date | None

    @staticmethod
    def parse(raw: Any, patient_id: str, index: int) -> "Contact":
        if not isinstance(raw, dict):
            raise InputError(f"{patient_id}.contact_log[{index}] must be an object")
        outcome = _text(raw.get("outcome"), f"{patient_id}.contact_log[{index}].outcome", maximum=32)
        if outcome not in CONTACT_OUTCOMES:
            raise InputError(
                f"{patient_id}.contact_log[{index}].outcome must be one of {sorted(CONTACT_OUTCOMES)}"
            )
        promised_raw = raw.get("promised_date")
        promised = (
            _date(promised_raw, f"{patient_id}.contact_log[{index}].promised_date")
            if promised_raw is not None
            else None
        )
        if outcome == "promised_return" and promised is None:
            raise InputError(f"{patient_id}.contact_log[{index}] promised_return needs promised_date")
        return Contact(
            date=_date(raw.get("date"), f"{patient_id}.contact_log[{index}].date"),
            outcome=outcome,
            promised_date=promised,
        )


@dataclass(frozen=True)
class Patient:
    id: str
    first_name: str
    phone_e164: str
    consent_to_automated_contact: bool
    sessions: tuple[Session, ...]
    contact_log: tuple[Contact, ...]

    @staticmethod
    def parse(raw: Any, index: int) -> "Patient":
        if not isinstance(raw, dict):
            raise InputError(f"patients[{index}] must be an object")
        patient_id = _text(raw.get("id"), f"patients[{index}].id", maximum=40)
        phone = _text(raw.get("phone_e164"), f"{patient_id}.phone_e164", maximum=20)
        consent = raw.get("consent_to_automated_contact")
        if not isinstance(consent, bool):
            raise InputError(f"{patient_id}.consent_to_automated_contact must be true or false")
        sessions_raw = raw.get("sessions")
        if not isinstance(sessions_raw, list):
            raise InputError(f"{patient_id}.sessions must be a list")
        log_raw = raw.get("contact_log", [])
        if not isinstance(log_raw, list):
            raise InputError(f"{patient_id}.contact_log must be a list")
        sessions = tuple(
            sorted(
                (Session.parse(item, patient_id, i) for i, item in enumerate(sessions_raw)),
                key=lambda session: session.date,
            )
        )
        contacts = tuple(
            sorted(
                (Contact.parse(item, patient_id, i) for i, item in enumerate(log_raw)),
                key=lambda contact: contact.date,
            )
        )
        return Patient(
            id=patient_id,
            first_name=_text(raw.get("first_name"), f"{patient_id}.first_name", maximum=40),
            phone_e164=phone,
            consent_to_automated_contact=consent,
            sessions=sessions,
            contact_log=contacts,
        )

    @property
    def phone_is_e164(self) -> bool:
        return bool(E164.fullmatch(self.phone_e164))


@dataclass(frozen=True)
class CourseFile:
    clinic: Clinic
    course: Course
    policy: ContactPolicy
    patients: tuple[Patient, ...] = field(default=())

    @staticmethod
    def parse(raw: Any) -> "CourseFile":
        if not isinstance(raw, dict):
            raise InputError("course file must be a JSON object")
        patients_raw = raw.get("patients")
        if not isinstance(patients_raw, list) or not patients_raw:
            raise InputError("patients must be a non-empty list")
        patients = tuple(Patient.parse(item, index) for index, item in enumerate(patients_raw))
        if len({patient.id for patient in patients}) != len(patients):
            raise InputError("patient ids must be unique")
        return CourseFile(
            clinic=Clinic.parse(raw.get("clinic")),
            course=Course.parse(raw.get("course")),
            policy=ContactPolicy.parse(raw.get("contact_policy")),
            patients=patients,
        )


def mask_phone(phone: str) -> str:
    """Mask a phone number for display. Never print a full number."""
    if len(phone) <= 5:
        return "*" * len(phone)
    return f"{phone[:3]}{'*' * (len(phone) - 5)}{phone[-2:]}"
