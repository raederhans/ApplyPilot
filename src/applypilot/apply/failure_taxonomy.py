"""Structured, provider-neutral failure classification for application turns."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

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
    if any(token in reason for token in ("site_blocked", "cloudflare", "cloak_backend")):
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
    if any(
        token in reason
        for token in (
            "biometric",
            "financial_identity",
            "identity_material_authorization_unconfirmed",
        )
    ):
        return FailureDescriptor(
            "sensitive_identity_boundary",
            "requires_human_boundary",
            "obtain_explicit_authorization_or_complete_sensitive_check_manually",
        )
    if any(
        token in reason
        for token in (
            "protected_identifier_source_required",
            "identity_material_missing",
        )
    ):
        return FailureDescriptor(
            "exact_identity_material_missing",
            "requires_material",
            "supply_exact_verified_and_authorized_identity_material",
            missing_material="requested_identity_material",
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
