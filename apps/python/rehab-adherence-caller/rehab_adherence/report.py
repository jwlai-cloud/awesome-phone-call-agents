"""Text rendering. Phone numbers are masked everywhere, without exception."""

from __future__ import annotations

import json
from pathlib import Path

from .decide import CALLING_ACTIONS
from .workflow import Row

FIXTURE_BANNER = (
    "FIXTURE RUN -- SIMULATED, NO CALL PLACED\n"
    "No network, no telephone, no credentials. Responses come from the fixture file."
)

PREVIEW_BANNER = "PREVIEW -- NO CALL PLACED\nThis is the plan only. Nothing was dialled."

LIVE_BANNER = "LIVE RUN -- REAL PHONE CALLS WERE PLACED"


def render(rows: list[Row], *, banner: str, course_id: str, today: str) -> str:
    lines = [banner, "", f"Course: {course_id}    as of: {today}", ""]

    for index, row in enumerate(rows, start=1):
        head = f"{index}. {row.patient_id}  {row.masked_phone}  ->  {row.trajectory} / {row.action}"
        lines.append(head)
        lines.append(f"     why: {row.reason}")

        signals = row.signals or {}
        lines.append(
            "     signals: attended={attended} missed={missed} consecutive={consecutive_missed} "
            "last4_missed={missed_in_last_four} days_since_attended={days_since_last_attended} "
            "calls={calls_made}".format(**signals)
        )

        if row.blockers:
            lines.append(f"     BLOCKED: {', '.join(row.blockers)} -- no call placed")
        if row.called:
            lines.append(f"     result: status={row.status} outcome={row.outcome}")
            if row.promised_date:
                lines.append(f"     booked: {row.promised_date}")
            if row.note:
                lines.append(f"     note: {row.note}")
            if row.escalate_to_clinician:
                lines.append("     >>> ESCALATED TO CLINICIAN -- not rebooked by this application")
        elif row.outcome == "call_failed":
            lines.append(f"     call failed: {row.note}")
        lines.append("")

    called = sum(1 for row in rows if row.called)
    eligible = sum(1 for row in rows if row.action in CALLING_ACTIONS and not row.blockers)
    blocked = sum(1 for row in rows if row.blockers)
    # Escalation happens two ways: the trajectory decided it before any call, or a
    # call result forced it. Both are escalations and both must be counted.
    escalated = sum(
        1 for row in rows if row.escalate_to_clinician or row.action == "escalate_to_clinician"
    )
    no_action = sum(1 for row in rows if row.action in {"no_action", "skip"})
    stopped = sum(1 for row in rows if row.action == "stop_contact")

    verb = f"{called} called" if called else f"{eligible} would be called"
    lines.append(
        f"{len(rows)} patients: {verb}, {blocked} blocked by the gate, "
        f"{escalated} escalated to a clinician, {stopped} contact stopped, {no_action} needed nothing."
    )
    return "\n".join(lines)


def write_jsonl(path: Path, rows: list[Row]) -> None:
    """Durable output, one JSON object per patient. Never contains a full phone
    number, because `Row` only ever carries the masked form."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row.to_dict(), sort_keys=True) + "\n")
