from __future__ import annotations

import pytest

from applypilot.apply.failure_taxonomy import classify_failure


@pytest.mark.parametrize(
    ("result", "category", "recoverability", "permanent"),
    [
        ("failed:resume_upload", "resume_upload_failed", "retry_same_application", False),
        (
            "failed:email_route_capability_missing",
            "mailbox_route_unavailable",
            "requires_capability",
            False,
        ),
        (
            "failed:manual_review_required:required_document",
            "required_material_missing",
            "requires_material",
            False,
        ),
        ("failed:site_blocked", "browser_runtime_blocked", "retry_new_session", False),
        (
            "failed:agent_runtime_timeout",
            "agent_runtime_failure",
            "retry_with_larger_runtime_budget",
            False,
        ),
        (
            "failed:provider_submission_error",
            "provider_submission_failure",
            "submission_uncertain",
            False,
        ),
        (
            "failed:post_submit_observer_unavailable",
            "post_submit_observation_failure",
            "submission_uncertain",
            False,
        ),
        (
            "failed:identity_material_missing:passport",
            "exact_identity_material_missing",
            "requires_material",
            False,
        ),
        ("failed:not_eligible_salary", "legacy_policy_rejection", "retry_same_application", False),
        ("expired", "expired", "do_not_retry", True),
        (
            "failed:explicit_do_not_apply",
            "explicit_do_not_apply",
            "do_not_retry",
            True,
        ),
        (
            "submission_uncertain",
            "submission_confirmation_missing",
            "submission_uncertain",
            False,
        ),
    ],
)
def test_failure_taxonomy_separates_policy_material_runtime_and_receipt_states(
    result: str,
    category: str,
    recoverability: str,
    permanent: bool,
) -> None:
    descriptor = classify_failure(result)

    assert descriptor.category == category
    assert descriptor.recoverability == recoverability
    assert descriptor.permanent is permanent
