"""Parse and classify the bounded output contract of a browser agent.

This module is deliberately independent from browser, database, and dashboard
state.  Keeping the wire-format contract pure makes it possible to evolve the
runtime orchestration without changing how one agent turn is interpreted.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from applypilot.apply.contracts import (
    MAX_AGENT_REPORT_BYTES,
    AgentTurnResult,
    agent_turn_result_from_mapping,
)
from applypilot.apply.failure_taxonomy import classify_failure


def _preview_audit_error(audit: object) -> str | None:
    if not isinstance(audit, dict):
        return "preview_audit_not_object"
    if audit.get("submission_attempted") is not False:
        return "preview_submission_state_unsafe"
    direct_email = str(audit.get("channel") or "").casefold() == "direct_email"
    if direct_email:
        if audit.get("attachments_verified") is not True:
            return "preview_email_attachments_not_verified"
        if not str(audit.get("recipient") or "").strip():
            return "preview_email_recipient_missing"
        if not str(audit.get("subject") or "").strip():
            return "preview_email_subject_missing"
    elif audit.get("resume_uploaded") is not True:
        return "preview_resume_not_verified"
    if not isinstance(audit.get("filled_fields"), (list, dict)):
        return "preview_filled_fields_missing"
    if not isinstance(audit.get("manual_review_fields"), list):
        return "preview_manual_review_fields_missing"
    if not str(audit.get("final_control_label", "")).strip():
        return "preview_final_control_missing"
    return None


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
    return _preview_audit_error(audit)


def _normalized_submission_evidence(evidence: object) -> dict | None:
    if not isinstance(evidence, dict):
        return None
    if str(evidence.get("channel") or "").casefold() == "direct_email":
        recipient = str(evidence.get("recipient") or "").strip()
        subject = str(evidence.get("subject") or "").strip()
        confirmation_text = str(evidence.get("confirmation_text") or "").strip()
        attachment_names = evidence.get("attachment_names")
        if (
            evidence.get("send_accepted") is not True
            or evidence.get("sent_copy_verified") is not True
            or not recipient
            or not subject
            or not confirmation_text
            or not isinstance(attachment_names, list)
            or not attachment_names
            or not all(isinstance(name, str) and name.strip() for name in attachment_names)
        ):
            return None
        return {
            "channel": "direct_email",
            "send_accepted": True,
            "sent_copy_verified": True,
            "recipient": recipient[:254],
            "subject": subject[:240],
            "attachment_names": [name.strip()[:180] for name in attachment_names[:8]],
            "confirmation_text": confirmation_text[:240],
            "provider_message_id": str(evidence.get("provider_message_id") or "")[:180],
        }
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
    return _normalized_submission_evidence(evidence)


_RESULT_LINE = re.compile(
    r"^RESULT:(READY_TO_SUBMIT|PREVIEWED|APPLIED|SUBMISSION_UNCERTAIN|"
    r"COVER_NOT_REQUIRED|COVER_LETTER_REQUIRED|LINKEDIN_LOGIN_COMPLETED|"
    r"EXPIRED|CAPTCHA|LOGIN_ISSUE|FAILED)(?::([^\r\n]+))?$"
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
        if (
            marker == "LINKEDIN_LOGIN_COMPLETED"
            and submission_phase == "prepare"
        ):
            return "linkedin_login_completed", None
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
        if marker == "LINKEDIN_LOGIN_COMPLETED":
            return "linkedin_login_completed", None
        if marker in {"READY_TO_SUBMIT", "PREVIEWED"}:
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


def load_agent_turn_report(path: Path, *, expected_run_id: str) -> AgentTurnResult:
    """Load a control-tool report without binding to one runtime or schema release."""
    if path.stat().st_size > MAX_AGENT_REPORT_BYTES:
        raise ValueError("structured Agent report is too large")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("structured Agent report must be an object")
    return agent_turn_result_from_mapping(raw, expected_run_id=expected_run_id)


def interpret_agent_turn_result(
    result: AgentTurnResult,
    *,
    dry_run: bool,
    submission_phase: str,
) -> tuple[str, dict | None]:
    """Adapt an open Agent result to the existing fail-closed application state."""
    status = result.status.strip().casefold()
    # Some browser agents use ``previewed`` to mean "the form is complete and
    # no submit action occurred" even during a real run's prepare phase.  That
    # is the same non-submitting state as ``ready_to_submit`` here; the launcher
    # still performs its independent pre-submit observation before reservation.
    if status == "previewed" and not dry_run and submission_phase == "prepare":
        status = "ready_to_submit"
    marker_by_status = {
        "linkedin_login_completed": "LINKEDIN_LOGIN_COMPLETED",
        "ready_to_submit": "READY_TO_SUBMIT",
        "previewed": "PREVIEWED",
        "applied": "APPLIED",
        "submission_uncertain": "SUBMISSION_UNCERTAIN",
        "cover_not_required": "COVER_NOT_REQUIRED",
        "cover_letter_required": "COVER_LETTER_REQUIRED",
        "expired": "EXPIRED",
        "captcha": "CAPTCHA",
        "login_issue": "LOGIN_ISSUE",
    }
    if status.startswith("failed:"):
        synthetic = f"RESULT:FAILED:{status.split(':', 1)[1]}"
    elif status == "failed":
        synthetic = "RESULT:FAILED:unknown"
    elif status in marker_by_status:
        synthetic = f"RESULT:{marker_by_status[status]}"
    else:
        synthetic = "RESULT:FAILED:unsupported_structured_status"

    if status == "previewed" and "preview_audit" in result.observations:
        synthetic += "\nPREVIEW_AUDIT: " + json.dumps(
            result.observations["preview_audit"], ensure_ascii=False
        )
    if status == "applied" and "submission_evidence" in result.observations:
        synthetic += "\nSUBMISSION_EVIDENCE: " + json.dumps(
            result.observations["submission_evidence"], ensure_ascii=False
        )
    return interpret_agent_output(
        synthetic,
        dry_run=dry_run,
        submission_phase=submission_phase,
    )


def reconcile_agent_turn_outputs(
    output: str,
    structured_result: AgentTurnResult | None,
    *,
    dry_run: bool,
    submission_phase: str,
) -> tuple[str, dict | None, str]:
    """Prefer a typed report, while retaining and cross-checking legacy output."""
    if structured_result is None:
        status, evidence = interpret_agent_output(
            output,
            dry_run=dry_run,
            submission_phase=submission_phase,
        )
        return status, evidence, "legacy"

    status, evidence = interpret_agent_turn_result(
        structured_result,
        dry_run=dry_run,
        submission_phase=submission_phase,
    )
    if parse_result_line(output) is None:
        return status, evidence, "structured"

    legacy_status, legacy_evidence = interpret_agent_output(
        output,
        dry_run=dry_run,
        submission_phase=submission_phase,
    )
    if (
        submission_phase == "submit"
        and not dry_run
        and structured_result.status.strip().casefold() == "applied"
        and legacy_status == "applied"
        and legacy_evidence is not None
    ):
        # The control report and the final text are two views of the same
        # bounded turn.  Some runtimes put the state in the typed report and
        # the receipt payload in the legacy-compatible marker.  Admit that
        # split representation only when both say APPLIED and the receipt
        # payload has already passed strict validation.  The worker still
        # requires an independent browser observation before persistence.
        return "applied", legacy_evidence, "structured+legacy"
    if (
        submission_phase == "prepare"
        and not dry_run
        and structured_result.status.strip().casefold() == "previewed"
        and legacy_status in {"cover_not_required", "cover_letter_required"}
    ):
        # During cover discovery, ``previewed`` is a generic no-side-effect
        # control-plane status.  The legacy result carries the more specific
        # branch the worker needs in order to continue the same application.
        return legacy_status, legacy_evidence, "structured+legacy"
    if legacy_status != status or legacy_evidence != evidence:
        conflict = (
            "submission_uncertain"
            if submission_phase == "submit" and not dry_run
            else "failed:conflicting_agent_results"
        )
        return conflict, None, "conflict"
    return status, evidence, "structured+legacy"


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
    for key in (
        "category",
        "recoverability",
        "missing_capability",
        "missing_material",
        "next_action",
        "field_label",
        "visible_state",
        "blocking_material",
    ):
        text = " ".join(str(value.get(key) or "").split())[:180]
        if text:
            result[key] = text
    attempts = value.get("attempts")
    if isinstance(attempts, int) and not isinstance(attempts, bool):
        result["attempts"] = max(0, min(attempts, 10))
    return result or None


def format_failure_error(reason: str, context: dict[str, object] | None) -> str:
    """Add actionable upload/material context without leaking file paths."""
    descriptor = classify_failure(reason).as_dict()
    context = {**descriptor, **(context or {})}
    details: list[str] = []
    for key, label in (
        ("category", "category"),
        ("recoverability", "recoverability"),
        ("missing_capability", "missing_capability"),
        ("missing_material", "missing_material"),
        ("next_action", "next_action"),
    ):
        value = str(context.get(key) or "").strip()
        if value:
            details.append(f"{label}={value}")
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
