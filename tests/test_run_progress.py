from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from applypilot.apply.run_progress import RunProgress


def test_preview_ticket_claims_are_strictly_bounded_under_race() -> None:
    progress = RunProgress(
        dry_run=True,
        success_target=3,
        preview_target=2,
        authorization_slot_cap=5,
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        tickets = list(pool.map(progress.claim_preview_ticket, range(20)))

    claimed = [ticket for ticket in tickets if ticket is not None]
    assert len(claimed) == 2
    assert progress.should_acquire() is False
    assert progress.consume_preview_ticket(claimed[0]) is True
    assert progress.consume_preview_ticket(claimed[0]) is False
    assert progress.release_preview_ticket(claimed[1]) is True
    assert progress.should_acquire() is True


def test_real_success_target_and_authorization_capacity_are_independent() -> None:
    progress = RunProgress(
        dry_run=False,
        success_target=1,
        preview_target=4,
        authorization_slot_cap=2,
    )

    first = progress.before_submit("job:uncertain")
    second = progress.before_submit("job:success")
    assert first.allowed is True
    assert second.allowed is True
    assert progress.before_submit("job:third").reason == (
        "authorization_batch_capacity_exhausted"
    )

    assert progress.record_terminal(
        "job:uncertain", "submission_uncertain", receipt_confirmed=False
    )
    assert progress.record_terminal(
        "job:success", "applied", receipt_confirmed=True
    )
    assert progress.record_terminal(
        "job:success", "failed", receipt_confirmed=False
    ) is False
    assert progress.before_submit("job:success").allowed is True
    assert progress.before_submit("job:other").reason == "run_success_target_reached"

    snapshot = progress.snapshot()
    assert snapshot["receipt_confirmed_successes"] == 1
    assert snapshot["authorization_slots_used"] == 2
    assert snapshot["submission_uncertain"] == 1
    assert snapshot["target_reached"] is True


def test_manifest_exhaustion_reports_partial_without_conflating_targets() -> None:
    progress = RunProgress(
        dry_run=False,
        success_target=3,
        preview_target=7,
        authorization_slot_cap=5,
    )
    assert progress.should_acquire() is True
    assert progress.record_terminal("job:failed", "failed") is True
    assert progress.record_terminal("job:failed", "applied", receipt_confirmed=True) is False

    progress.mark_manifest_exhausted()

    assert progress.should_acquire() is False
    snapshot = progress.snapshot()
    assert snapshot["manifest_exhausted"] is True
    assert snapshot["partial"] is True
    assert snapshot["success_target"] == 3
    assert snapshot["preview_target"] == 7
    assert snapshot["authorization_slot_cap"] == 5


def test_dry_run_never_allocates_real_submission_slots() -> None:
    progress = RunProgress(
        dry_run=True,
        success_target=1,
        preview_target=1,
        authorization_slot_cap=1,
    )

    decision = progress.before_submit("job:dry")

    assert decision.allowed is False
    assert decision.reason == "dry_run_submission_forbidden"
    assert progress.snapshot()["authorization_slots_used"] == 0


def test_performance_samples_are_bounded_thread_safe_and_decision_neutral() -> None:
    progress = RunProgress(
        dry_run=False,
        success_target=2,
        preview_target=2,
        authorization_slot_cap=3,
    )

    samples = [
        {
            "submit_lane_wait_ms": index,
            "submit_lane_hold_ms": 100,
            "submit_lane_peak": 1,
            "unknown": 999,
            "post_submit_observer_ms": -1,
        }
        for index in range(8)
    ]
    acquisition_samples = [
        {
            "worker_call_ms": 10 + index,
            "candidate_rows": 3,
            "admission_rows_scanned": 2,
            "unknown": 999,
        }
        for index in range(8)
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        recorded = list(pool.map(progress.record_performance, samples))
    with ThreadPoolExecutor(max_workers=8) as pool:
        acquired = list(
            pool.map(
                lambda item: progress.record_acquisition(
                    item[1],
                    outcome="acquired" if item[0] < 4 else "empty",
                ),
                enumerate(acquisition_samples),
            )
        )

    snapshot = progress.snapshot()
    performance = snapshot["performance"]
    assert recorded == [True] * 8
    assert acquired == [True] * 8
    assert performance["job_sample_count"] == 8
    assert performance["totals"]["submit_lane_wait_ms"] == 28
    assert performance["totals"]["submit_lane_hold_ms"] == 800
    assert performance["totals"]["submit_lane_peak"] == 1
    assert performance["maxima"]["submit_lane_peak"] == 1
    assert performance["acquisition"]["attempt_count"] == 8
    assert performance["acquisition"]["outcomes"] == {"acquired": 4, "empty": 4}
    assert performance["acquisition"]["totals"]["worker_call_ms"] == 108
    assert performance["acquisition"]["totals"]["candidate_rows"] == 24
    assert performance["acquisition"]["maxima"]["worker_call_ms"] == 17
    assert "unknown" not in performance["totals"]
    assert "unknown" not in performance["acquisition"]["totals"]
    assert "post_submit_observer_ms" not in performance["totals"]
    assert snapshot["authorization_slots_used"] == 0
    assert snapshot["receipt_confirmed_successes"] == 0
