"""Parse and classify the bounded output contract of a browser agent.

This module is deliberately independent from browser, database, and dashboard
state.  Keeping the wire-format contract pure makes it possible to evolve the
runtime orchestration without changing how one agent turn is interpreted.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

from applypilot.apply.contracts import (
    MAX_AGENT_REPORT_BYTES,
    AgentTurnResult,
    FailureObservation,
    agent_turn_result_from_mapping,
)
from applypilot.apply.failure_taxonomy import classify_failure

_ANSWER_MAPPING_ENVELOPE_KEYS = frozenset(
    {
        "schema_version",
        "adapter",
        "adapter_version",
        "opaque_binding",
        "snapshot_digest",
        "mappings",
    }
)
_ANSWER_MAPPING_ITEM_KEYS = frozenset(
    {
        "field_key_hash",
        "semantic",
        "risk",
        "selected_option_digest",
        "fact_ref",
        "safe_default_rule_id",
    }
)
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_EMAIL_ADDRESS = re.compile(r"[^@\s]+@([^@\s]+)")
_MAX_ANSWER_MAPPINGS = 128
_DIRECT_EMAIL_PLAN_KEYS = frozenset(
    {
        "route",
        "recipient",
        "recipient_domain",
        "recipient_source",
        "listing_evidence",
        "subject",
        "body_sha256",
        "attachment_names",
        "attachments_verified",
        "duplicate_check",
    }
)
_DIRECT_EMAIL_DUPLICATE_KEYS = frozenset(
    {"folder", "completed", "duplicate_found", "provider_query_id"}
)


def _is_strict_direct_email_prepare_plan(observations: object) -> bool:
    if not isinstance(observations, Mapping):
        return False
    plan = observations.get("email_application")
    if (
        not isinstance(plan, Mapping)
        or set(plan) != _DIRECT_EMAIL_PLAN_KEYS
        or plan.get("route") != "direct_email"
    ):
        return False
    recipient = str(plan.get("recipient") or "").strip().casefold()
    match = _EMAIL_ADDRESS.fullmatch(recipient)
    domain = str(plan.get("recipient_domain") or "").strip().casefold().strip(".")
    duplicate = plan.get("duplicate_check")
    attachment_names = plan.get("attachment_names")
    valid_attachment_names = bool(
        isinstance(attachment_names, list)
        and attachment_names
        and all(isinstance(name, str) and name.strip() for name in attachment_names)
        and len(attachment_names) == len(set(attachment_names))
    )
    return bool(
        match is not None
        and domain == match.group(1).casefold().strip(".")
        and plan.get("recipient_source") == "official_listing"
        and recipient in str(plan.get("listing_evidence") or "").casefold()
        and str(plan.get("subject") or "").strip()
        and isinstance(plan.get("body_sha256"), str)
        and _SHA256_HEX.fullmatch(str(plan["body_sha256"]).casefold()) is not None
        and valid_attachment_names
        and plan.get("attachments_verified") is True
        and isinstance(duplicate, Mapping)
        and set(duplicate) == _DIRECT_EMAIL_DUPLICATE_KEYS
        and str(duplicate.get("folder") or "").strip().casefold() == "sent"
        and duplicate.get("completed") is True
        and duplicate.get("duplicate_found") is False
        and str(duplicate.get("provider_query_id") or "").strip()
    )


def validate_ready_answer_mappings(
    status: object,
    observations: object,
) -> str | None:
    """Validate the v2 envelope required by browser-ready reports."""
    if str(status or "").strip().casefold() != "ready_to_submit":
        return None
    if _is_strict_direct_email_prepare_plan(observations):
        return None
    envelope = (
        observations.get("answer_mappings")
        if isinstance(observations, Mapping)
        else None
    )
    if not isinstance(envelope, Mapping):
        return "answer_mappings_v2_required"
    if set(envelope) != _ANSWER_MAPPING_ENVELOPE_KEYS:
        return "answer_mappings_v2_envelope_invalid"
    if envelope.get("schema_version") != "2":
        return "answer_mappings_v2_envelope_invalid"
    for key in ("adapter", "adapter_version"):
        if not isinstance(envelope.get(key), str) or not str(envelope[key]).strip():
            return "answer_mappings_v2_envelope_invalid"
    for key in ("opaque_binding", "snapshot_digest"):
        value = envelope.get(key)
        if not isinstance(value, str) or _SHA256_HEX.fullmatch(value) is None:
            return "answer_mappings_v2_envelope_invalid"
    mappings = envelope.get("mappings")
    if not isinstance(mappings, list) or len(mappings) > _MAX_ANSWER_MAPPINGS:
        return "answer_mappings_v2_envelope_invalid"
    for mapping in mappings:
        if not isinstance(mapping, Mapping) or not set(mapping) <= _ANSWER_MAPPING_ITEM_KEYS:
            return "answer_mappings_v2_item_invalid"
        for key in ("field_key_hash", "selected_option_digest"):
            value = mapping.get(key)
            if not isinstance(value, str) or _SHA256_HEX.fullmatch(value) is None:
                return "answer_mappings_v2_item_invalid"
        for key in ("semantic", "risk"):
            if not isinstance(mapping.get(key), str) or not str(mapping[key]).strip():
                return "answer_mappings_v2_item_invalid"
        fact_ref = mapping.get("fact_ref")
        rule_id = mapping.get("safe_default_rule_id")
        has_fact = isinstance(fact_ref, str) and bool(fact_ref.strip())
        has_rule = isinstance(rule_id, str) and bool(rule_id.strip())
        if has_fact == has_rule:
            return "answer_mappings_v2_item_invalid"
    return None


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
    r"^RESULT:(PREPARED_FOR_AUDIT|READY_TO_SUBMIT|PREVIEWED|APPLIED|SUBMISSION_UNCERTAIN|"
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
        if marker == "PREPARED_FOR_AUDIT":
            return "prepared_for_audit", None
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

    if submission_phase == "receipt":
        if marker == "APPLIED":
            return "applied", None
        if marker == "SUBMISSION_UNCERTAIN":
            return "submission_uncertain", None
        if marker == "FAILED":
            return result_status(marker, reason), None
        return "failed:invalid_receipt_result", None

    return "failed:invalid_submission_phase", None


def load_agent_turn_report(path: Path, *, expected_run_id: str) -> AgentTurnResult:
    """Load a control-tool report without binding to one runtime or schema release."""
    if path.stat().st_size > MAX_AGENT_REPORT_BYTES:
        raise ValueError("structured Agent report is too large")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("structured Agent report must be an object")
    result = agent_turn_result_from_mapping(raw, expected_run_id=expected_run_id)
    if (
        result.status.strip().casefold() == "prepared_for_audit"
        and "answer_mappings" in result.observations
    ):
        return AgentTurnResult(
            run_id=result.run_id,
            status="failed:prepared_for_audit_contract_invalid",
            summary="Prepared-for-audit report carried premature answer mappings",
        )
    contract_error = validate_ready_answer_mappings(result.status, result.observations)
    if contract_error is None:
        return result
    return AgentTurnResult(
        run_id=result.run_id,
        status="failed:answer_provenance_report_invalid",
        summary="Provenance-aware ready report failed its structured contract",
        observations={"report_contract_error": contract_error},
    )


def interpret_agent_turn_result(
    result: AgentTurnResult,
    *,
    dry_run: bool,
    submission_phase: str,
) -> tuple[str, dict | None]:
    """Adapt an open Agent result to the existing fail-closed application state."""
    status = result.status.strip().casefold()
    failure = result.failure
    if failure is not None:
        return _interpret_failure_observation(
            failure,
            dry_run=dry_run,
            submission_phase=submission_phase,
        )
    if submission_phase == "receipt":
        if status == "applied" and isinstance(
            result.observations.get("confirmation_receipt"), dict
        ):
            return "applied", None
        if status == "submission_uncertain":
            return "submission_uncertain", None
        if status.startswith("failed:"):
            return status, None
        return "failed:invalid_receipt_result", None
    # Some browser agents use ``previewed`` to mean "the form is complete and
    # no submit action occurred" even during a real run's prepare phase.  That
    # is the same non-submitting state as ``ready_to_submit`` here; the launcher
    # still performs its independent pre-submit observation before reservation.
    if status == "previewed" and not dry_run and submission_phase == "prepare":
        status = "ready_to_submit"
    if status == "prepared_for_audit":
        if dry_run or submission_phase != "prepare" or "answer_mappings" in result.observations:
            return "failed:prepared_for_audit_contract_invalid", None
        return "prepared_for_audit", None
    contract_error = validate_ready_answer_mappings(status, result.observations)
    if contract_error is not None:
        return "failed:answer_provenance_report_invalid", None
    marker_by_status = {
        "prepared_for_audit": "PREPARED_FOR_AUDIT",
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


def _interpret_failure_observation(
    failure: FailureObservation,
    *,
    dry_run: bool,
    submission_phase: str,
) -> tuple[str, dict | None]:
    """Consume source-emitted failure facts without parsing agent prose."""
    phase = submission_phase.strip().casefold()
    if failure.submit_started:
        return "submission_uncertain", None
    if failure.phase != phase:
        status = (
            "submission_uncertain"
            if phase == "submit" and not dry_run
            else "failed:failure_phase_mismatch"
        )
        return status, None
    if phase == "submit" and not dry_run:
        return "submission_uncertain", None
    if failure.code == "captcha_required":
        return "captcha", None
    if failure.code == "expired":
        return "expired", None
    if failure.code == "submission_uncertain":
        return "submission_uncertain", None
    return f"failed:{failure.code}", None


def reconcile_agent_turn_outputs_with_diagnostics(
    output: str,
    structured_result: AgentTurnResult | None,
    *,
    dry_run: bool,
    submission_phase: str,
) -> tuple[str, dict | None, str, str | None]:
    """Cross-check both contracts and return a bounded conflict classification."""
    if structured_result is None:
        status, evidence = interpret_agent_output(
            output,
            dry_run=dry_run,
            submission_phase=submission_phase,
        )
        if (
            status == "ready_to_submit"
            and submission_phase == "prepare"
            and not dry_run
        ):
            return (
                "failed:answer_provenance_report_missing",
                None,
                "legacy",
                "structured_ready_report_missing",
            )
        if status == "prepared_for_audit":
            return (
                "failed:prepared_for_audit_report_missing",
                None,
                "legacy",
                "structured_prepared_report_missing",
            )
        return status, evidence, "legacy", None

    status, evidence = interpret_agent_turn_result(
        structured_result,
        dry_run=dry_run,
        submission_phase=submission_phase,
    )
    parsed_legacy = parse_result_line(output)
    if parsed_legacy is None and "RESULT:" not in output:
        return status, evidence, "structured", None
    if parsed_legacy is None:
        conflict = (
            "submission_uncertain"
            if submission_phase == "submit" and not dry_run
            else "failed:conflicting_agent_results"
        )
        return conflict, None, "conflict", "legacy_result_invalid"

    legacy_status, legacy_evidence = interpret_agent_output(
        output,
        dry_run=dry_run,
        submission_phase=submission_phase,
    )
    if (
        submission_phase == "prepare"
        and status == "failed:prepared_for_audit_contract_invalid"
        and legacy_status == "prepared_for_audit"
    ):
        return status, None, "structured", None
    if (
        submission_phase == "prepare"
        and status == "failed:answer_provenance_report_invalid"
        and legacy_status == "ready_to_submit"
        and structured_result.status.strip().casefold()
        == "failed:answer_provenance_report_invalid"
    ):
        # A persisted strict-v2 denial is authoritative during the non-submit
        # phase.  A legacy READY marker cannot erase the specific contract
        # failure or turn it into generic conflict telemetry.
        return status, None, "structured", None
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
        return "applied", legacy_evidence, "structured+legacy", None
    if (
        submission_phase == "prepare"
        and not dry_run
        and structured_result.status.strip().casefold() == "previewed"
        and legacy_status in {"cover_not_required", "cover_letter_required"}
    ):
        # During cover discovery, ``previewed`` is a generic no-side-effect
        # control-plane status.  The legacy result carries the more specific
        # branch the worker needs in order to continue the same application.
        return legacy_status, legacy_evidence, "structured+legacy", None
    if legacy_status != status:
        conflict = (
            "submission_uncertain"
            if submission_phase == "submit" and not dry_run
            else "failed:conflicting_agent_results"
        )
        return conflict, None, "conflict", "status_mismatch"
    if legacy_evidence != evidence:
        conflict = (
            "submission_uncertain"
            if submission_phase == "submit" and not dry_run
            else "failed:conflicting_agent_results"
        )
        return conflict, None, "conflict", "evidence_mismatch"
    return status, evidence, "structured+legacy", None


def conflict_status_families(
    output: str,
    structured_result: AgentTurnResult | None,
    *,
    dry_run: bool,
    submission_phase: str,
) -> dict[str, str] | None:
    """Describe a result mismatch without retaining raw statuses or page text."""
    if structured_result is None:
        return None
    structured_status, _ = interpret_agent_turn_result(
        structured_result,
        dry_run=dry_run,
        submission_phase=submission_phase,
    )
    parsed_legacy = parse_result_line(output)
    if parsed_legacy is None:
        legacy_status = "invalid"
    else:
        marker, reason = parsed_legacy
        legacy_status = result_status(marker, reason)

    def family(status: str) -> str:
        normalized = str(status or "").strip().casefold()
        if normalized.startswith("failed") or normalized == "invalid":
            return "failure" if normalized != "invalid" else "invalid"
        if normalized in {"ready_to_submit", "prepared_for_audit"}:
            return "ready"
        if normalized in {"applied"}:
            return "applied"
        if normalized in {"submission_uncertain"}:
            return "uncertain"
        if normalized in {"captcha", "login_issue"}:
            return "manual_gate"
        if normalized in {"previewed", "cover_not_required", "cover_letter_required"}:
            return "non_submit"
        return "other"

    return {
        "structured": family(structured_status),
        "legacy": family(legacy_status),
    }


def reconcile_agent_turn_outputs(
    output: str,
    structured_result: AgentTurnResult | None,
    *,
    dry_run: bool,
    submission_phase: str,
) -> tuple[str, dict | None, str]:
    """Prefer a typed report, while retaining and cross-checking legacy output."""
    status, evidence, source, _classification = (
        reconcile_agent_turn_outputs_with_diagnostics(
            output,
            structured_result,
            dry_run=dry_run,
            submission_phase=submission_phase,
        )
    )
    return status, evidence, source


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
