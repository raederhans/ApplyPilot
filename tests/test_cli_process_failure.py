from __future__ import annotations

import pytest

from applypilot.apply import launcher


def test_prepare_cli_exit_without_result_is_typed_runtime_failure() -> None:
    status, source, context = launcher._resolve_cli_process_failure(
        1,
        "failed:invalid_result_marker",
        "legacy",
        submission_phase="prepare",
        dry_run=False,
        structured_result_present=False,
    )

    assert status == "failed:agent_runtime_process_exit"
    assert source == "runtime_provider_failed"
    assert context == {
        "category": "agent_runtime_failure",
        "recoverability": "retry_new_session",
        "next_action": "inspect_typed_process_exit_before_retry",
    }


def test_submit_cli_exit_without_result_stays_receipt_only() -> None:
    status, source, context = launcher._resolve_cli_process_failure(
        1,
        "failed:invalid_result_marker",
        "legacy",
        submission_phase="submit",
        dry_run=False,
        structured_result_present=False,
    )

    assert status == "submission_uncertain"
    assert source == "runtime_provider_failed"
    assert context == {
        "category": "submission_confirmation_missing",
        "recoverability": "submission_uncertain",
        "next_action": "reconcile_receipt_without_resubmitting",
    }


@pytest.mark.parametrize(
    ("status", "source", "structured_result_present"),
    [
        ("ready_to_submit", "structured", True),
        ("applied", "structured+legacy", True),
        ("applied", "legacy", False),
        ("submission_uncertain", "structured", True),
    ],
)
def test_cli_exit_preserves_admitted_report_or_receipt_result(
    status: str,
    source: str,
    structured_result_present: bool,
) -> None:
    assert launcher._resolve_cli_process_failure(
        1,
        status,
        source,
        submission_phase="submit",
        dry_run=False,
        structured_result_present=structured_result_present,
    ) == (status, source, None)


def test_successful_cli_exit_keeps_result_contract_authoritative() -> None:
    assert launcher._resolve_cli_process_failure(
        0,
        "failed:invalid_result_marker",
        "legacy",
        submission_phase="prepare",
        dry_run=False,
        structured_result_present=False,
    ) == ("failed:invalid_result_marker", "legacy", None)
