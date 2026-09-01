from __future__ import annotations

import inspect
from dataclasses import asdict

import pytest

from applypilot.apply import local_synthetic_browser_benchmark as benchmark_module
from applypilot.apply.local_synthetic_browser_benchmark import (
    FIXED_TASKS,
    LOCAL_SYNTHETIC_LABEL,
    SyntheticBrowserSafetyViolation,
    SyntheticBrowserTask,
    run_local_synthetic_browser_benchmark,
)


def test_real_local_chromium_matched_cohorts_are_offline_and_submit_free() -> None:
    report = run_local_synthetic_browser_benchmark()

    assert report.label == LOCAL_SYNTHETIC_LABEL
    assert report.provider_benchmark is False
    assert report.promotion_authority is False
    assert (
        report.profile_accesses,
        report.default_database_accesses,
        report.submission_gate_touches,
        report.reservation_touches,
        report.receipt_admission_touches,
    ) == (0, 0, 0, 0, 0)
    assert tuple(cohort.workers for cohort in report.cohorts) == (1, 2, 4)
    assert {cohort.fixture_digest for cohort in report.cohorts} == {report.fixture_digest}
    assert len({cohort.result_digest for cohort in report.cohorts}) == 1

    expected_ids = tuple(sorted(task.task_id for task in FIXED_TASKS))
    expected_scenarios = {task.scenario for task in FIXED_TASKS}
    assert expected_scenarios == {
        "text",
        "select",
        "file",
        "segmented_date",
        "dynamic_validation",
        "stale_dom_rerender",
        "session_restart",
        "synthetic_receipt_confirmation",
    }
    expected_observations = {
        "dynamic_validation": "valid",
        "file": "synthetic-resume.pdf",
        "segmented_date": "2026-09-01",
        "select": "sg",
        "session_restart": "fresh",
        "stale_dom_rerender": "2:after",
        "synthetic_receipt_confirmation": "SYNTHETIC CONFIRMATION ONLY",
        "text": "Synthetic Candidate",
    }
    for cohort in report.cohorts:
        assert cohort.task_count == len(FIXED_TASKS)
        assert cohort.max_active == cohort.workers
        assert cohort.execution_counts == tuple((task_id, 1) for task_id in expected_ids)
        assert tuple(result.task_id for result in cohort.results) == expected_ids
        assert all(result.outcome == "passed" for result in cohort.results)
        assert all(result.external_network_requests == 0 for result in cohort.results)
        assert all(result.submit_events == 0 for result in cohort.results)
        assert all(result.form_submit_calls == 0 for result in cohort.results)
        assert all(result.request_submit_calls == 0 for result in cohort.results)
        assert all(result.submit_button_clicks == 0 for result in cohort.results)
        assert {result.scenario: result.observation for result in cohort.results} == (
            expected_observations
        )


def test_fixture_order_does_not_change_local_synthetic_results() -> None:
    forward = run_local_synthetic_browser_benchmark(worker_counts=(2,))
    reversed_report = run_local_synthetic_browser_benchmark(
        worker_counts=(2,),
        tasks=tuple(reversed(FIXED_TASKS)),
    )

    assert forward.fixture_digest == reversed_report.fixture_digest
    assert forward.cohorts[0].result_digest == reversed_report.cohorts[0].result_digest
    assert forward.cohorts[0].execution_counts == reversed_report.cohorts[0].execution_counts
    assert [asdict(result) for result in forward.cohorts[0].results] == [
        asdict(result) for result in reversed_report.cohorts[0].results
    ]


def test_report_contract_denies_provider_and_promotion_claims() -> None:
    report = run_local_synthetic_browser_benchmark(worker_counts=(1,))

    assert report.label == "local_synthetic_chromium_not_a_provider_benchmark"
    assert "provider_benchmark" in asdict(report)
    assert asdict(report)["provider_benchmark"] is False
    assert asdict(report)["promotion_authority"] is False
    assert report.cohorts[0].wall_clock_ms >= 0
    source = inspect.getsource(benchmark_module)
    assert "applypilot.database" not in source
    assert "applypilot.config" not in source
    assert "SubmissionGate" not in source
    assert "reserve_batch_submission" not in source
    assert "reconcile_submission_receipt" not in source
    assert "load_profile" not in source
    assert "get_connection" not in source


@pytest.mark.parametrize(
    "operation",
    ["submit_button", "form_submit", "request_submit", "submit_event"],
)
def test_every_submit_trap_actively_fails_closed(operation: str) -> None:
    task = SyntheticBrowserTask(f"negative-{operation}", f"negative_{operation}")

    with pytest.raises(
        SyntheticBrowserSafetyViolation,
        match=f"forbidden synthetic submission operation: {operation}",
    ):
        run_local_synthetic_browser_benchmark(worker_counts=(1,), tasks=(task,))
