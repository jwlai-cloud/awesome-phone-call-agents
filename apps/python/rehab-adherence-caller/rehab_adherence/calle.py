"""CALL-E adapter.

The SDK is an optional dependency, installed with the `live` extra. A default
install cannot place a call, which is a structural guarantee rather than a
promise: `import calle` is not merely unused in the default path, it is absent.

Verified against `calle-ai==0.2.0`:
    client = CalleClient(api_key=..., base_url=...)
    created = client.calls.create(task=, recipients=, result_schema=, metadata=, idempotency_key=)
    completed = client.calls.wait_for_result(created["id"], timeout_seconds=, interval_seconds=)
"""

from __future__ import annotations

import re
from typing import Any, Protocol

PHONE_LIKE = re.compile(r"(?:\+\d[\d\s().-]{6,}\d)|(?:\b\d[\d\s().-]{7,}\d\b)")

DEFAULT_BASE_URL = "https://api.heycall-e.com"


class CallPort(Protocol):
    """What the workflow needs from a caller. Satisfied by the live client and by
    the fixture port, so the workflow itself has no live/fixture branch."""

    def place(self, arguments: dict[str, Any], *, patient_id: str) -> dict[str, Any]: ...


def normalize_transcript(raw: Any) -> list[dict[str, str]]:
    """CALL-E returns turns as `{speaker, text, ts}`. Keep exactly those three
    fields, redact anything phone-shaped, and drop anything malformed rather than
    displaying a half-parsed turn.

    Speaker is normalised to BOT/USER; anything else becomes OTHER rather than
    being guessed at.
    """
    if not isinstance(raw, list):
        return []
    turns: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        speaker = str(item.get("speaker", "")).upper()
        if speaker not in {"BOT", "USER"}:
            speaker = "OTHER"
        ts = item.get("ts")
        turns.append(
            {
                "speaker": speaker,
                "text": redact(" ".join(text.split())),
                "ts": ts if isinstance(ts, str) else "",
            }
        )
    return turns


def redact(value: Any) -> Any:
    """Strip anything phone-shaped out of provider output before it is displayed
    or written. Provider text is untrusted; CALL-E's own skill says so."""
    if isinstance(value, str):
        return PHONE_LIKE.sub("[phone-redacted]", value)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    return value


class FixturePort:
    """Replays recorded-by-hand responses. No network, no credentials, no call.

    Fixtures are authored, never captured: `SECURITY.md` forbids committing call
    recordings and private transcripts.
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses

    def place(self, arguments: dict[str, Any], *, patient_id: str) -> dict[str, Any]:
        response = self._responses.get(patient_id)
        if response is None:
            return {
                "status": "NO_ANSWER",
                "structured_result": None,
                "transcript": [],
                "call_id": None,
                "simulated": True,
            }
        return {
            "status": response.get("status", "COMPLETED"),
            "task_completed": response.get("task_completed"),
            "completion_confidence": response.get("completion_confidence"),
            "structured_result": response.get("structured_result"),
            "transcript": normalize_transcript(response.get("transcript")),
            "call_id": None,
            "simulated": True,
        }


class LivePort:
    """Places one real call per decision. Only reachable behind explicit flags."""

    def __init__(self, api_key: str, *, base_url: str = DEFAULT_BASE_URL, timeout_seconds: int = 600) -> None:
        if not api_key:
            raise ValueError("a CALL-E API key is required for live mode")
        try:
            from calle import CalleClient  # noqa: PLC0415 - optional `live` extra
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise RuntimeError(
                "live mode needs the CALL-E SDK: pip install 'rehab-adherence-caller[live]'"
            ) from exc
        self._client = CalleClient(api_key=api_key, base_url=base_url)
        self._timeout_seconds = timeout_seconds

    def place(self, arguments: dict[str, Any], *, patient_id: str) -> dict[str, Any]:
        created = self._client.calls.create(**arguments)
        call_id = created.get("id")
        if not isinstance(call_id, str) or not call_id:
            raise RuntimeError("CALL-E create response contained no call id; reconcile before retrying")
        completed = self._client.calls.wait_for_result(
            call_id, timeout_seconds=self._timeout_seconds, interval_seconds=2
        )
        return {
            "status": completed.get("status"),
            "task_completed": completed.get("task_completed"),
            "completion_confidence": completed.get("completion_confidence"),
            "structured_result": redact(completed.get("structured_result")),
            "transcript": normalize_transcript(completed.get("transcript")),
            "call_id": call_id,
            "simulated": False,
        }
