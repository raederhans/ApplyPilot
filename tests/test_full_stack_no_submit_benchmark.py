from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from applypilot import cli
from applypilot.apply import launcher
from applypilot.apply.full_stack_no_submit_benchmark import (
    CohortMetricCollector,
    _evaluate_admission,
    load_fixture,
    run_full_stack_no_submit_benchmark,
)
from applypilot.apply.performance_governor import (
    DEFAULT_THRESHOLDS,
    REPORT_SCHEMA_VERSION,
    AdmissionManifest,
    PerformanceGovernor,
)

FIXTURE = Path(__file__).parent / "fixtures" / "benchmarks" / "applypilot_full_stack_no_submit_v1.json"


def _aggregate(workers: int, throughput: float) -> dict[str, object]:
    return {
        "blocks": 3,
        "throughput_by_block": [throughput] * 3,
        "throughput_jobs_per_second": throughput,
        "job_latency_p95_ms": 100.0,
        "process_tree_rss_peak_bytes": workers * 100_000_000,
        "available_memory_bytes_min": 10_000_000_000,
        "sqlite_busy_errors": 0,
        "sqlite_lock_wait_p95_ms": 1.0,
        "sqlite_lock_wait_max_ms": 2.0,
        "submit_lane_wait_p95_ms": 1.0,
        "submit_lane_wait_max_ms": 2.0,
        "submit_lane_hold_p95_ms": 1.0,
        "quality_digests": ["same"],
        "completed_jobs": 72,
        "all_replays_verified": True,
        "side_effects": {
            "duplicate_submit_attempts": 0,
            "actor_cross_talk": 0,
            "profile_cross_talk": 0,
            "page_cross_talk": 0,
            "admitted_stale_writes": 0,
            "repeated_admitted_actions": 0,
            "final_submit_calls": 0,
            "submission_gate_calls": 0,
            "reservation_calls": 0,
            "receipt_calls": 0,
            "receipt_identity_drift": 0,
        },
    }


def test_fixture_is_exact_matched_workday_smartrecruiters_cohort() -> None:
    fixture, digest = load_fixture(FIXTURE)

    assert len(digest) == 64
    assert len(fixture["tasks"]) == 24
    assert {item["provider"] for item in fixture["tasks"]} == {
        "workday",
        "smartrecruiters",
    }
    assert {item["scenario"] for item in fixture["tasks"]} == {
        "routine",
        "stale",
        "human_only",
        "crash_recovery",
    }


@pytest.mark.browser
def test_launcher_worker_crosses_p1_p2_p3_without_submit_authority(tmp_path: Path) -> None:
    root = tmp_path / "isolated"
    root.mkdir()
    tasks = (
        {"task_id": "routine", "provider": "workday", "scenario": "routine"},
        {"task_id": "stale", "provider": "smartrecruiters", "scenario": "stale"},
        {"task_id": "human", "provider": "workday", "scenario": "human_only"},
        {"task_id": "crash", "provider": "smartrecruiters", "scenario": "crash_recovery"},
    )

    results = launcher.run_p4_no_submit_worker(
        worker_id=0,
        tasks=tasks,
        cohort_id="focused",
        root=root,
        db_path=root / "cohort.sqlite3",
        submit_lane=threading.Semaphore(1),
        metric_sink=CohortMetricCollector(),
    )

    assert [item["task_id"] for item in results] == ["routine", "stale", "human", "crash"]
    assert next(item for item in results if item["task_id"] == "stale")["stale_rejections"] == 1
    assert next(item for item in results if item["task_id"] == "human")["episode_state"] == "human_required"
    assert next(item for item in results if item["task_id"] == "crash")["replay_verified"] is True
    forbidden = {
        "duplicate_submit_attempts",
        "actor_cross_talk",
        "profile_cross_talk",
        "page_cross_talk",
        "admitted_stale_writes",
        "repeated_admitted_actions",
        "final_submit_calls",
        "submission_gate_calls",
        "reservation_calls",
        "receipt_calls",
        "receipt_identity_drift",
    }
    assert all(item[field] == 0 for item in results for field in forbidden)


def test_admission_requires_every_two_and_four_worker_gate() -> None:
    eligible, evidence = _evaluate_admission(
        {
            1: _aggregate(1, 10.0),
            2: _aggregate(2, 17.0),
            4: _aggregate(4, 28.0),
        },
        rss_budget_bytes=1_000_000_000,
    )

    assert eligible == 4
    assert evidence["speedups"]["2"]["paired_bootstrap_95_lower_bound"] >= 1.6
    assert evidence["speedups"]["4"]["paired_bootstrap_95_lower_bound"] >= 2.6
    assert all(all(gates.values()) for gates in evidence["gates"].values())


def test_governor_clamps_downshifts_never_upshifts_and_memory_opens_circuit() -> None:
    local = AdmissionManifest.local_diagnostic(
        source_manifest_sha256="a" * 64,
        fixture_sha256="b" * 64,
        eligible_workers=4,
    )
    assert PerformanceGovernor(local, requested_workers=4, explicit_four_workers=True).effective_workers == 1

    admitted = AdmissionManifest(
        source_manifest_sha256="a" * 64,
        fixture_sha256="b" * 64,
        thresholds_sha256="c" * 64,
        admitted_workers=4,
        default_workers=1,
        production_authority=True,
        authority_ref="test-only-authority",
    )
    governor = PerformanceGovernor(
        admitted,
        requested_workers=4,
        explicit_four_workers=True,
        rss_budget_bytes=1_000,
        available_memory_bytes=2_000,
    )
    assert governor.observe({"sqlite_busy_errors": 1}).effective_workers == 2
    assert governor.observe({}).effective_workers == 2
    decision = governor.observe({"process_tree_rss_bytes": 1_201})
    assert decision.effective_workers == 0
    assert decision.circuit_open is True
    assert "circuit_open:rss_budget_exceeded" in decision.reasons
    assert "circuit_open:available_memory_fraction_exceeded" in decision.reasons


def test_benchmark_rejects_source_or_configured_workspace_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = Path(__file__).parents[1]
    monkeypatch.setenv("APPLYPILOT_DIR", str(tmp_path / "live"))

    with pytest.raises(ValueError, match="source tree"):
        run_full_stack_no_submit_benchmark(
            FIXTURE,
            workspace_root=source_root / "forbidden-workspace",
            output_path=tmp_path / "report.json",
            source_root=source_root,
            measured_blocks=2,
            warmup_blocks=0,
        )

    with pytest.raises(ValueError, match="APPLYPILOT_DIR"):
        run_full_stack_no_submit_benchmark(
            FIXTURE,
            workspace_root=tmp_path / "live" / "cohort",
            output_path=tmp_path / "report.json",
            source_root=source_root,
            measured_blocks=2,
            warmup_blocks=0,
        )


def test_runs_cohort_is_read_only_and_exposes_explicit_admission_reasons(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "schema_version": REPORT_SCHEMA_VERSION,
                "fixture_sha256": "a" * 64,
                "source_manifest_sha256": "b" * 64,
                "speedups": {"2": {"paired_bootstrap_95_lower_bound": 1.7}},
                "gates": {"2": {"speedup_lower_bound": True}},
                "admission": {
                    "status": "NOT_ADMITTED",
                    "local_gate_status": "QUALIFIED",
                    "eligible_workers": 4,
                    "production_admitted_workers": 1,
                    "reasons": ["local_fixture_only_no_production_authority"],
                },
                "governor": {
                    "effective_workers": 1,
                    "reasons": ["local_diagnostic_has_no_production_authority"],
                },
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli.app,
        ["runs", "cohort", "--report-path", str(report_path)],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["evidence"]["read_only"] is True
    assert payload["evidence"]["status"] == "NOT_ADMITTED"
    assert payload["evidence"]["local_gate_status"] == "QUALIFIED"
    assert payload["evidence"]["reasons"] == ["local_fixture_only_no_production_authority"]


def test_thresholds_match_the_promotion_contract() -> None:
    assert DEFAULT_THRESHOLDS == {
        "two_worker_speedup_lower_bound": 1.6,
        "four_worker_speedup_lower_bound": 2.6,
        "job_p95_ratio_max": 1.2,
        "available_memory_fraction_max": 0.60,
        "sqlite_lock_wait_p95_ms_max": 50.0,
        "sqlite_lock_wait_max_ms_max": 500.0,
        "submit_lane_wait_p95_ms_max": 250.0,
        "submit_lane_wait_max_ms_max": 1000.0,
        "submit_lane_hold_p95_ms_max": 200.0,
    }
