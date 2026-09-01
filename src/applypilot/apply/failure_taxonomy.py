"""Structured, provider-neutral failure classification for application turns."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Literal

from applypilot.apply.contracts import FailureObservation

Recoverability = Literal[
    "retry_same_application",
    "retry_new_session",
    "retry_with_larger_runtime_budget",
    "requires_capability",
    "requires_material",
    "requires_human_boundary",
    "do_not_retry",
    "submission_uncertain",
]


@dataclass(frozen=True, slots=True)
class FailureDescriptor:
    category: str
    recoverability: Recoverability
    next_action: str
    missing_capability: str | None = None
    missing_material: str | None = None
    permanent: bool = False

    def as_dict(self) -> dict[str, object]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def _reason(result: str) -> str:
    text = str(result or "unknown").strip().casefold()
    if text.startswith("result:"):
        text = text.split(":", 1)[1]
    if text.startswith("failed:"):
        text = text.split(":", 1)[1]
    return text


def classify_failure(result: str) -> FailureDescriptor:
    """Describe what is missing without conflating technical and policy stops."""
    reason = _reason(result)
    if reason in {"submission_uncertain", "uncertain"}:
        return FailureDescriptor(
            "submission_confirmation_missing",
            "submission_uncertain",
            "reconcile_receipt_without_resubmitting",
        )
    if reason in {"expired", "already_applied", "duplicate", "not_a_job_application"}:
        return FailureDescriptor(
            reason,
            "do_not_retry",
            "exclude_from_active_queue",
            permanent=True,
        )
    if any(token in reason for token in ("do_not_apply", "do-not-apply", "will_not_be_considered")):
        return FailureDescriptor(
            "explicit_do_not_apply",
            "do_not_retry",
            "exclude_from_active_queue",
            permanent=True,
        )
    if any(
        token in reason
        for token in (
            "mfa",
            "multi_factor",
            "security_challenge",
            "identity_provider_security_code",
            "identity_provider_verification_code",
            "mfa_code",
            "security_challenge_code",
            "account_recovery",
        )
    ):
        return FailureDescriptor(
            "security_challenge_required",
            "requires_human_boundary",
            "complete_security_challenge_manually_then_reobserve",
            missing_capability="authorized_human_security_handoff",
        )
    if reason in {"captcha", "login_issue"} or "captcha" in reason:
        return FailureDescriptor(
            "human_verification_required",
            "requires_human_boundary",
            "resume_after_authorized_verification",
            missing_capability="human_verification_relay",
        )
    if "assessment" in reason:
        return FailureDescriptor(
            "assessment_required",
            "requires_human_boundary",
            "route_to_assessment_workflow",
            missing_capability="assessment_owner",
        )
    if any(
        token in reason
        for token in (
            "biometric",
            "financial_identity",
            "financial_material",
            "financial_document",
            "bank_statement",
            "tax_document",
            "identity_material_authorization_unconfirmed",
            "identity_material_missing",
            "protected_identifier_source_required",
        )
    ) or reason == "financial":
        return FailureDescriptor(
            "sensitive_identity_boundary",
            "requires_human_boundary",
            "complete_sensitive_identity_or_financial_material_step_manually",
        )
    if any(
        token in reason
        for token in (
            "unsupported_legal_declaration",
            "legal_declaration_unanswerable",
            "cannot_answer_truthfully",
            "no_truthful_legal_option",
        )
    ):
        return FailureDescriptor(
            "unsupported_legal_declaration",
            "requires_human_boundary",
            "resolve_legal_declaration_truthfully_with_human_review",
        )
    if "required_document" in reason:
        return FailureDescriptor(
            "required_material_missing",
            "requires_material",
            "supply_exact_required_document",
            missing_material="required_application_document",
        )
    if "cover_letter" in reason:
        return FailureDescriptor(
            "cover_letter_missing",
            "requires_material",
            "generate_and_validate_cover_letter",
            missing_material="validated_cover_letter",
        )
    if "resume_upload" in reason or "resume_not_uploaded" in reason:
        return FailureDescriptor(
            "resume_upload_failed",
            "retry_same_application",
            "repair_bound_resume_upload_once",
            missing_capability="browser_file_upload_or_site_adapter",
        )
    if "credential_relay" in reason:
        return FailureDescriptor(
            "credential_relay_missing",
            "requires_capability",
            "configure_scoped_credential_relay",
            missing_capability="credential_relay",
        )
    if "mailbox" in reason or "email_route_capability" in reason:
        return FailureDescriptor(
            "mailbox_route_unavailable",
            "requires_capability",
            "configure_authorized_mailbox_search_read_send",
            missing_capability="mailbox_route",
        )
    if "computer_use_handoff" in reason or "visual_only_control" in reason:
        return FailureDescriptor(
            "visual_control_unavailable",
            "requires_capability",
            "route_to_authorized_visual_control_before_submit",
            missing_capability="visual_control",
        )
    if "browser_interaction_unavailable" in reason:
        return FailureDescriptor(
            "browser_interaction_unavailable",
            "requires_capability",
            "inspect_page_state_or_route_to_authorized_app_browser",
            missing_capability="site_specific_browser_interaction_or_app_handoff",
        )
    if "browser_mcp_unavailable" in reason:
        return FailureDescriptor(
            "browser_mcp_unavailable",
            "requires_capability",
            "repair_or_attach_playwright_mcp_before_retry",
            missing_capability="playwright_mcp",
        )
    if any(
        token in reason
        for token in (
            "site_blocked",
            "cloudflare",
            "automation_blocked",
            "bot_detected",
            "browser_challenge",
            "cloak_backend",
        )
    ):
        return FailureDescriptor(
            "browser_runtime_blocked",
            "retry_new_session",
            "retry_once_with_available_browser_runtime",
            missing_capability="compatible_browser_runtime",
        )
    if "agent_runtime_timeout" in reason:
        return FailureDescriptor(
            "agent_runtime_failure",
            "retry_with_larger_runtime_budget",
            "increase_agent_timeout_or_reduce_turn_scope",
            missing_capability="agent_runtime_budget",
        )
    if "provider_submission_error" in reason:
        return FailureDescriptor(
            "provider_submission_failure",
            "submission_uncertain",
            "review_or_reconcile_provider_response_without_resubmitting",
            missing_capability="provider_submission_diagnostics_or_adapter",
        )
    if any(
        token in reason
        for token in (
            "post_submit_observer",
            "post_submit_no_bound_application_page",
            "browser_runtime_lost_after_submit",
        )
    ):
        return FailureDescriptor(
            "post_submit_observation_failure",
            "submission_uncertain",
            "reconnect_observer_then_reconcile_receipt_without_resubmitting",
            missing_capability="post_submit_observer_or_receipt_reconciliation",
        )
    if any(token in reason for token in ("page_error", "timeout", "stuck")):
        return FailureDescriptor(
            "page_or_progress_failure",
            "retry_new_session",
            "retry_with_fresh_observation",
            missing_capability="site_state_or_adapter_progress",
        )
    if any(token in reason for token in ("pre_submit_not_ready", "required_field_empty", "validation")):
        return FailureDescriptor(
            "form_resolution_incomplete",
            "retry_same_application",
            "resolve_visible_validation_or_add_adapter_support",
            missing_capability="answer_resolution_or_form_adapter",
        )
    if any(token in reason for token in ("unknown_required", "unsupported_skill", "manual_salary")):
        return FailureDescriptor(
            "answer_policy_needs_relaxed_retry",
            "retry_same_application",
            "apply_selection_order_and_continue",
            missing_capability="answer_resolution",
        )
    if reason.startswith("not_eligible_"):
        return FailureDescriptor(
            "legacy_policy_rejection",
            "retry_same_application",
            "answer_truthfully_and_let_employer_decide",
            missing_capability="relaxed_application_policy",
        )
    if "unsafe_" in reason or "hard_answer_mismatch" in reason:
        return FailureDescriptor(
            "truth_or_security_boundary",
            "requires_human_boundary",
            "correct_directly_false_or_sensitive_answer",
        )
    return FailureDescriptor(
        "unclassified_application_failure",
        "retry_new_session",
        "inspect_bounded_failure_context_and_add_capability",
    )


_TYPED_REASON_BY_CODE = {
    "agent_runtime_timeout": "agent_runtime_timeout",
    "already_applied": "already_applied",
    "answer_policy_unresolved": "unknown_required",
    "assessment_required": "assessment_required",
    "browser_interaction_unavailable": "browser_interaction_unavailable",
    "browser_mcp_unavailable": "browser_mcp_unavailable",
    "browser_runtime_blocked": "site_blocked",
    "captcha_required": "captcha",
    "cover_letter_missing": "cover_letter_missing",
    "credential_relay_missing": "credential_relay_missing",
    "duplicate": "duplicate",
    "expired": "expired",
    "explicit_do_not_apply": "explicit_do_not_apply",
    "form_resolution_incomplete": "required_field_empty",
    "mailbox_route_unavailable": "email_route_capability_missing",
    "not_a_job_application": "not_a_job_application",
    "page_or_progress_failure": "page_error",
    "post_submit_observer_unavailable": "post_submit_observer_unavailable",
    "provider_submission_error": "provider_submission_error",
    "required_document_missing": "required_document_missing",
    "resume_upload_failed": "resume_upload_failed",
    "security_challenge_required": "security_challenge_required",
    "sensitive_identity_material_required": "identity_material_missing",
    "submission_uncertain": "submission_uncertain",
    "truth_or_security_boundary": "unsafe_answer",
    "unknown": "unknown",
    "unsupported_legal_declaration": "unsupported_legal_declaration",
    "visual_control_unavailable": "computer_use_handoff_unavailable",
}


def classify_failure_observation(observation: FailureObservation) -> FailureDescriptor:
    """Adapt bounded failure facts to the existing recovery policy taxonomy."""
    if not isinstance(observation, FailureObservation):
        raise TypeError("observation must be a FailureObservation")
    if observation.submit_started:
        descriptor = classify_failure("submission_uncertain")
    else:
        reason = _TYPED_REASON_BY_CODE.get(observation.code)
        if reason is None:
            return FailureDescriptor(
                "unclassified_application_failure",
                "do_not_retry",
                "park_unknown_typed_failure_for_bounded_diagnosis",
            )
        descriptor = classify_failure(reason)
    return replace(
        descriptor,
        missing_capability=(
            observation.missing_capability or descriptor.missing_capability
        ),
        missing_material=observation.missing_material or descriptor.missing_material,
    )
