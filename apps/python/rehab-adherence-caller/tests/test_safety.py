"""Safety properties, asserted rather than documented.

The medical boundary, the phone-number masking, and the whitelist on what the
caller may say are all testable claims. These are the tests that make them so.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from rehab_adherence.calle import FixturePort, redact
from rehab_adherence.decide import decide
from rehab_adherence.goal import build_task, reference
from rehab_adherence.model import CourseFile, InputError, mask_phone
from rehab_adherence.report import render, write_jsonl
from rehab_adherence.workflow import build_call_arguments, masked_call_arguments, plan, run

TODAY = date(2026, 8, 11)
HERE = Path(__file__).resolve().parent.parent
EXAMPLE = HERE / "examples" / "course.example.json"
RESPONSES = HERE / "examples" / "responses.example.json"


@pytest.fixture(scope="module")
def raw() -> dict:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def course_file(raw: dict) -> CourseFile:
    return CourseFile.parse(raw)


# --- what the caller is allowed to say -----------------------------------------


def test_unknown_clinical_fields_cannot_reach_the_caller(raw: dict) -> None:
    """The goal is built from a whitelist of validated fields. Adding clinical
    content to the course file must not change a single character of the task."""
    patient_id = "p_rosa"
    baseline = CourseFile.parse(raw)
    decision = decide(
        next(p for p in baseline.patients if p.id == patient_id),
        baseline.course,
        baseline.policy,
        TODAY,
    )
    before = build_task(
        baseline.clinic, baseline.course, next(p for p in baseline.patients if p.id == patient_id), decision
    )

    poisoned = json.loads(json.dumps(raw))
    poisoned["course"]["diagnosis"] = "grade II medial meniscus tear"
    poisoned["course"]["clinical_note"] = "advise 20 degrees more flexion before loading"
    for patient in poisoned["patients"]:
        patient["medication"] = "ibuprofen 400mg three times daily"
        patient["prognosis"] = "expect full range of motion by week eight"

    after_file = CourseFile.parse(poisoned)
    after = build_task(
        after_file.clinic, after_file.course, next(p for p in after_file.patients if p.id == patient_id), decision
    )

    assert before == after
    for leak in ("meniscus", "flexion", "ibuprofen", "400mg", "prognosis", "range of motion"):
        assert leak not in after


def test_every_task_carries_the_clinical_boundary(course_file: CourseFile) -> None:
    tasks = [
        build_task(course_file.clinic, course_file.course, patient, decision)
        for decision, _ in plan(course_file, TODAY)
        if decision.will_call
        for patient in [next(p for p in course_file.patients if p.id == decision.patient_id)]
    ]
    assert tasks, "the example course must exercise at least one call"
    for task in tasks:
        assert "must not give or discuss any medical" in task
        assert "a clinician will call them back" in task
        assert "If the person asks to stop being called, agree immediately" in task


def test_no_task_asks_how_the_patient_feels(course_file: CourseFile) -> None:
    """"What is stopping you attending" is logistics. "How is your pain" is
    assessment, and must not exist anywhere in the goal text."""
    forbidden = ("how are you feeling", "how is your pain", "is the pain", "how is your recovery", "rate your pain")
    for decision, _ in plan(course_file, TODAY):
        if not decision.will_call:
            continue
        patient = next(p for p in course_file.patients if p.id == decision.patient_id)
        task = build_task(course_file.clinic, course_file.course, patient, decision).lower()
        for phrase in forbidden:
            assert phrase not in task


def test_disclosure_offers_an_out_of_band_check(course_file: CourseFile) -> None:
    """A recipient who suspects a scam is behaving correctly. The caller must not
    argue; it must hand over a checkable reference and the clinic's own number."""
    patient = next(p for p in course_file.patients if p.id == "p_ivy")
    decision = decide(patient, course_file.course, course_file.policy, TODAY)
    task = build_task(course_file.clinic, course_file.course, patient, decision)
    assert "automated assistant" in task
    assert "hang up" in task
    assert course_file.clinic.public_callback_number in task
    assert reference(course_file.clinic, course_file.course, patient) in task


def test_reference_is_stable_and_carries_no_identity(course_file: CourseFile) -> None:
    patient = next(p for p in course_file.patients if p.id == "p_ivy")
    code = reference(course_file.clinic, course_file.course, patient)
    assert code == reference(course_file.clinic, course_file.course, patient)
    assert patient.first_name.lower() not in code.lower()
    assert patient.phone_e164 not in code


# --- phone numbers --------------------------------------------------------------


def test_mask_keeps_only_the_ends() -> None:
    assert mask_phone("+15550101001") == "+15*******01"
    assert "5550101" not in mask_phone("+15550101001")


def test_preview_never_prints_a_full_number(course_file: CourseFile) -> None:
    for decision, _ in plan(course_file, TODAY):
        if not decision.will_call:
            continue
        rendered = json.dumps(masked_call_arguments(course_file, decision.patient_id, decision))
        patient = next(p for p in course_file.patients if p.id == decision.patient_id)
        assert patient.phone_e164 not in rendered


def test_live_arguments_do_carry_the_real_number(course_file: CourseFile) -> None:
    """The masked form is for display. The provider must receive E.164, or no call
    can be placed — this test exists so masking is never applied to the wire."""
    decision = decide(
        next(p for p in course_file.patients if p.id == "p_ivy"), course_file.course, course_file.policy, TODAY
    )
    arguments = build_call_arguments(course_file, "p_ivy", decision)
    assert arguments["recipients"][0]["phones"] == ["+15550101001"]


def test_redact_strips_phone_shaped_provider_text() -> None:
    assert "5550101001" not in redact("call me back on +1 555 010 1001 tomorrow")
    assert redact({"a": ["+15550101001"]}) == {"a": ["[phone-redacted]"]}


def test_ledger_contains_no_full_numbers(course_file: CourseFile, tmp_path: Path) -> None:
    responses = json.loads(RESPONSES.read_text(encoding="utf-8"))
    rows = run(course_file, TODAY, FixturePort(responses))
    output = tmp_path / "ledger.jsonl"
    write_jsonl(output, rows)
    written = output.read_text(encoding="utf-8")
    for patient in course_file.patients:
        assert patient.phone_e164 not in written


# --- input validation refuses to guess ------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: d["patients"][0].pop("consent_to_automated_contact"), id="consent-missing"),
        pytest.param(lambda d: d["clinic"].__setitem__("public_callback_number", "555-0100"), id="callback-not-e164"),
        pytest.param(lambda d: d["patients"][0]["sessions"][0].__setitem__("status", "probably-came"), id="bad-status"),
        pytest.param(lambda d: d["course"].__setitem__("offered_slots", "thursday"), id="slots-not-a-list"),
        pytest.param(lambda d: d["patients"].clear(), id="no-patients"),
        pytest.param(
            lambda d: d["patients"][3]["contact_log"][0].pop("promised_date"), id="promise-without-a-date"
        ),
    ],
)
def test_bad_input_is_rejected_not_guessed(raw: dict, mutate) -> None:
    broken = json.loads(json.dumps(raw))
    mutate(broken)
    with pytest.raises(InputError):
        CourseFile.parse(broken)


def test_non_e164_patient_phone_blocks_rather_than_crashes(raw: dict) -> None:
    """A bad number must be a named blocker, not an exception, so the rest of the
    course still runs."""
    edited = json.loads(json.dumps(raw))
    edited["patients"][0]["phone_e164"] = "+1555"
    course_file = CourseFile.parse(edited)
    decision = decide(course_file.patients[0], course_file.course, course_file.policy, TODAY)
    assert "phone_not_e164" in decision.blockers


# --- the fixture path places no calls -------------------------------------------


def test_fixture_run_places_no_call(course_file: CourseFile) -> None:
    responses = json.loads(RESPONSES.read_text(encoding="utf-8"))
    rows = run(course_file, TODAY, FixturePort(responses))
    assert rows, "the run must produce a row per patient"
    assert all(row.simulated is not False for row in rows)
    assert all(row.call_id is None for row in rows)


def test_render_shows_the_no_call_banner(course_file: CourseFile) -> None:
    responses = json.loads(RESPONSES.read_text(encoding="utf-8"))
    rows = run(course_file, TODAY, FixturePort(responses))
    text = render(rows, banner="FIXTURE RUN -- SIMULATED, NO CALL PLACED", course_id="x", today="2026-08-11")
    assert "NO CALL PLACED" in text
    assert "ESCALATED TO CLINICIAN" in text, "Rosa's symptom must be visible in the report"


def test_a_failing_call_does_not_end_the_course(course_file: CourseFile) -> None:
    class Exploding:
        def __init__(self) -> None:
            self.calls = 0

        def place(self, arguments, *, patient_id):
            self.calls += 1
            if patient_id == "p_ivy":
                raise RuntimeError("provider unreachable")
            return {"status": "VOICEMAIL", "structured_result": None, "call_id": None, "simulated": True}

    port = Exploding()
    rows = run(course_file, TODAY, port)
    failed = [row for row in rows if row.outcome == "call_failed"]
    assert len(failed) == 1
    assert "provider unreachable" in (failed[0].note or "")
    assert port.calls > 1, "the run must continue past the failure"


# --- transcripts ----------------------------------------------------------------


def test_transcript_read_from_the_documented_api_path() -> None:
    """The turns live at recipients[].attempts[].transcript_turns[].

    This test exists because the first implementation read a top-level
    "transcript" key that the API never returns: every fixture passed and every
    real call would have come back empty. Shape per
    https://docs.heycall-e.com/api-reference/calls
    """
    from rehab_adherence.calle import normalize_transcript

    payload = {
        "status": "completed",
        "recipients": [
            {
                "attempts": [
                    {
                        "transcript_turns": [
                            {"offset_seconds": 0, "speaker": "bot",
                             "text": "  Ring us on +1 555 010 1000  "},
                            {"offset_seconds": 64, "speaker": "user", "text": "ok"},
                            {"offset_seconds": 70, "speaker": "unknown", "text": "joining"},
                            {"offset_seconds": 75, "speaker": "bot", "text": "   "},
                            "not a dict",
                        ]
                    }
                ]
            }
        ],
    }
    turns = normalize_transcript(payload)
    assert [t["speaker"] for t in turns] == ["BOT", "USER", "OTHER"]
    assert "5550101000" not in turns[0]["text"], "phone-shaped text must be redacted"
    assert turns[1]["ts"] == "00:01:04", "offset_seconds becomes a clock stamp"


def test_a_top_level_transcript_key_is_not_read() -> None:
    """Guards the exact bug: a payload with the old shape must yield nothing,
    so a fixture in the wrong shape fails loudly instead of passing quietly."""
    from rehab_adherence.calle import normalize_transcript

    assert normalize_transcript(
        {"transcript": [{"speaker": "BOT", "text": "hello", "ts": "00:00:00"}]}
    ) == []


def test_confidence_is_an_object_not_a_float() -> None:
    from rehab_adherence.calle import confidence_score

    assert confidence_score({"score": 0.92, "label": "high"}) == 0.92
    assert confidence_score(0.75) == 0.75, "a bare number is still tolerated"
    assert confidence_score(None) is None
    assert confidence_score({"label": "high"}) is None


def test_structured_result_falls_back_to_the_recipient() -> None:
    """Both levels exist. An aggregate at task level must not shadow the
    recipient result that actually carries our schema's fields."""
    from rehab_adherence.calle import RESULT_KEYS, structured_result

    ours = {"reached_patient": "yes", "attendance_intent": "will_attend"}
    payload = {
        "structured_result": {"completed_count": 1},
        "recipients": [{"structured_result": ours}],
    }
    assert structured_result(payload, RESULT_KEYS) == ours
    assert structured_result({"structured_result": ours}, RESULT_KEYS) == ours


def test_transcript_render_shows_the_refusal(course_file: CourseFile) -> None:
    """Rosa raises a symptom. The rendered call must show the refusal and the
    escalation, because that is the shot the demo is built around."""
    from rehab_adherence.report import render_transcript

    responses = json.loads(RESPONSES.read_text(encoding="utf-8"))
    rows = run(course_file, TODAY, FixturePort(responses))
    rosa = next(row for row in rows if row.patient_id == "p_rosa")

    text = render_transcript(rosa)
    assert "agent" in text and "patient" in text
    assert "not able to advise" in text
    assert "ESCALATED TO CLINICIAN" in text
    assert rosa.masked_phone in text
    for patient in course_file.patients:
        assert patient.phone_e164 not in text


def test_transcript_render_handles_a_call_with_no_transcript(course_file: CourseFile) -> None:
    from rehab_adherence.report import render_transcript

    responses = json.loads(RESPONSES.read_text(encoding="utf-8"))
    rows = run(course_file, TODAY, FixturePort(responses))
    wu = next(row for row in rows if row.patient_id == "p_wu")
    assert "no transcript returned" in render_transcript(wu)


def test_call_id_is_checkpointed_before_polling(tmp_path: Path) -> None:
    """The call is placed and billed before the poll starts. If the poll is
    interrupted, the id must already be on disk or the result is unrecoverable.

    Regression for a real incident: a live call connected, the poll did not
    complete, and nothing was written because the ledger was only saved at the
    end of the run. The call could not be recovered.
    """
    from rehab_adherence.calle import LivePort

    checkpoint = tmp_path / "ids.callids"

    class Interrupted(LivePort):
        def __init__(self) -> None:  # skip the SDK import entirely
            self._timeout_seconds = 1
            self._checkpoint = checkpoint

            class _Calls:
                @staticmethod
                def create(**_kwargs):
                    return {"id": "call_abc123"}

                @staticmethod
                def wait_for_result(*_a, **_k):
                    raise KeyboardInterrupt("operator stopped the poll")

            class _Client:
                calls = _Calls()

            self._client = _Client()

    with pytest.raises(KeyboardInterrupt):
        Interrupted().place({"task": "x"}, patient_id="p_ivy")

    assert checkpoint.exists(), "the id must survive an interrupted poll"
    assert "call_abc123" in checkpoint.read_text()
    assert "p_ivy" in checkpoint.read_text()
