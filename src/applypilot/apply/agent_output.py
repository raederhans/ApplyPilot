"""Parse and classify the bounded output contract of a browser agent.

This module is deliberately independent from browser, database, and dashboard
state.  Keeping the wire-format contract pure makes it possible to evolve the
runtime orchestration without changing how one agent turn is interpreted.
"""

from __future__ import annotations

import json
import re


def validate_preview_audit(output: str) -> str | None:
    """Return a failure reason when a PREVIEWED result lacks safety evidence."""
    marker = re.search(r"PREVIEW_AUDIT\s*:?\s*", output)
    if marker:
        payload = output[marker.end():]
    else:
        result_marker = re.search(r"RESULT:PREVIEWED\b", output)
        if not result_marker:
            return "preview_audit_missing"
        payload = output[result_marker.end():]
    object_start = payload.find("{")
    if object_start < 0:
        return "preview_audit_missing" if not marker else "preview_audit_invalid_json"
    try:
        audit, _ = json.JSONDecoder().raw_decode(payload[object_start:])
    except (json.JSONDecodeError, TypeError):
        return "preview_audit_invalid_json"
    if not isinstance(audit, dict):
        return "preview_audit_not_object"
    if audit.get("submission_attempted") is not False:
        return "preview_submission_state_unsafe"
    if audit.get("resume_uploaded") is not True:
        return "preview_resume_not_verified"
    if not isinstance(audit.get("filled_fields"), (list, dict)):
        return "preview_filled_fields_missing"
    if not isinstance(audit.get("manual_review_fields"), list):
        return "preview_manual_review_fields_missing"
    if not str(audit.get("final_control_label", "")).strip():
        return "preview_final_control_missing"
    return None


def validate_submission_evidence(output: str) -> dict | None:
    """Return structured visible-confirmation evidence, or fail closed."""
    marker = re.search(r"SUBMISSION_EVIDENCE\s*:?\s*", output)
    if not marker:
        return None
    payload = output[marker.end():].lstrip()
    try:
        evidence, _ = json.JSONDecoder().raw_decode(payload)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(evidence, dict):
        return None
    receipt_visible = evidence.get("receipt_visible") is True
    applied_badge_visible = evidence.get("applied_badge_visible") is True
    confirmation_text = str(evidence.get("confirmation_text") or "").strip()
    confirmation_url = evidence.get("confirmation_url")
    if not (receipt_visible or applied_badge_visible) or not confirmation_text:
        return None
    if confirmation_url is not None and not isinstance(confirmation_url, str):
        return None
    return {
        "receipt_visible": receipt_visible,
        "applied_badge_visible": applied_badge_visible,
        "confirmation_text": confirmation_text,
        "confirmation_url": str(confirmation_url or "").strip(),
    }


_RESULT_LINE = re.compile(
    r"^RESULT:(READY_TO_SUBMIT|PREVIEWED|APPLIED|SUBMISSION_UNCERTAIN|"
    r"COVER_NOT_REQUIRED|COVER_LETTER_REQUIRED|EXPIRED|CAPTCHA|LOGIN_ISSUE|FAILED)(?::([^\r\n]+))?$"
)


def parse_result_line(output: str) -> tuple[str, str | None] | None:
    """Parse exactly one standalone RESULT line, rejecting prose and duplicates."""
    if len(re.findall(r"RESULT:", output)) != 1:
        return None
    result_lines = [line.strip() for line in output.splitlines() if "RESULT:" in line]
    if len(result_lines) != 1:
        return None
    match = _RESULT_LINE.fullmatch(result_lines[0])
    if match is None:
        return None
    return match.group(1), (match.group(2) or "").strip() or None


def result_status(marker: str, reason: str | None) -> str:
    if marker == "FAILED":
        return f"failed:{reason or 'unknown'}"
    return marker.lower()


def interpret_agent_output(
    output: str,
    *,
    dry_run: bool,
    submission_phase: str,
) -> tuple[str, dict | None]:
    """Fail closed on phase-inappropriate, duplicated, or malformed results."""
    parsed = parse_result_line(output)
    if parsed is None:
        status = (
            "submission_uncertain"
            if submission_phase == "submit" and not dry_run
            else "failed:invalid_result_marker"
        )
        return status, None

    marker, reason = parsed
    blockers = {"EXPIRED", "CAPTCHA", "LOGIN_ISSUE", "FAILED"}
    if dry_run:
        if marker == "PREVIEWED":
            audit_error = validate_preview_audit(output)
            return (
                (f"failed:{audit_error}", None)
                if audit_error
                else ("previewed", None)
            )
        if marker in blockers:
            return result_status(marker, reason), None
        if marker in {"APPLIED", "SUBMISSION_UNCERTAIN"}:
            return "submission_uncertain", None
        return "failed:invalid_preview_result", None

    if submission_phase == "prepare":
        if marker == "READY_TO_SUBMIT":
            return "ready_to_submit", None
        if marker == "COVER_NOT_REQUIRED":
            return "cover_not_required", None
        if marker == "COVER_LETTER_REQUIRED":
            return "cover_letter_required", None
        if marker in blockers:
            return result_status(marker, reason), None
        if marker in {"APPLIED", "SUBMISSION_UNCERTAIN"}:
            return "submission_uncertain", None
        return "failed:invalid_prepare_result", None

    if submission_phase == "submit":
        if marker == "APPLIED":
            evidence = validate_submission_evidence(output)
            return ("applied", evidence) if evidence else ("submission_uncertain", None)
        if marker == "SUBMISSION_UNCERTAIN":
            return "submission_uncertain", None
        return "submission_uncertain", None

    return "failed:invalid_submission_phase", None


def parse_unanswered_questions(output: str) -> list[dict] | None:
    """Parse the compact unresolved-question record emitted by the browser agent."""
    marker = re.search(r"UNANSWERED_QUESTIONS\s*:\s*", output)
    if not marker:
        return None
    payload = output[marker.end():].lstrip()
    try:
        value, _ = json.JSONDecoder().raw_decode(payload)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, list):
        return None
    return [item for item in value if isinstance(item, dict)]


def parse_failure_context(output: str) -> dict[str, object] | None:
    """Parse a compact, non-sensitive browser failure diagnostic."""
    marker = re.search(r"FAILURE_CONTEXT\s*:\s*", output)
    if not marker:
        return None
    payload = output[marker.end():].lstrip()
    try:
        value, _ = json.JSONDecoder().raw_decode(payload)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, dict):
        return None

    result: dict[str, object] = {}
    for key in ("category", "field_label", "visible_state", "blocking_material"):
        text = " ".join(str(value.get(key) or "").split())[:180]
        if text:
            result[key] = text
    attempts = value.get("attempts")
    if isinstance(attempts, int) and not isinstance(attempts, bool):
        result["attempts"] = max(0, min(attempts, 10))
    return result or None


def format_failure_error(reason: str, context: dict[str, object] | None) -> str:
    """Add actionable upload/material context without leaking file paths."""
    if not context:
        return reason
    details: list[str] = []
    field_label = str(context.get("field_label") or "").strip()
    blocking_material = str(context.get("blocking_material") or "").strip()
    visible_state = str(context.get("visible_state") or "").strip()
    if field_label:
        details.append(f"field={field_label}")
    if blocking_material and blocking_material.casefold() != field_label.casefold():
        details.append(f"required_material={blocking_material}")
    if visible_state:
        details.append(f"state={visible_state}")
    if "attempts" in context:
        details.append(f"attempts={context['attempts']}")
    return f"{reason}; {'; '.join(details)}" if details else reason
