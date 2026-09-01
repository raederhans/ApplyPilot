"""Focused tests for the local replay/synthetic/dry-run benchmark."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from applypilot import config
from applypilot.apply import dry_run_benchmark
from applypilot.apply.dry_run_benchmark import (
    BENCHMARK_LABEL,
    BENCHMARK_SCHEMA_VERSION,
    run_dry_cohort_benchmark,
    write_benchmark_report,
)

FIXTURE = Path(__file__).parent / "fixtures" / "benchmarks" / "apply_cohort_v1.json"


def test_matched_cohorts_are_submit_free_bounded_and_order_invariant(tmp_path: Path) -> None:
    report = run_dry_cohort_benchmark(
        FIXTURE,
        db_root=tmp_path / "db",
        data_root=tmp_path / "data",
    )

    assert report["schema_version"] == BENCHMARK_SCHEMA_VERSION
    assert report["label"] == BENCHMARK_LABEL
    assert report["evidence_labels"] == ["replay", "synthetic", "dry-run"]
    assert report["claim_boundary"].endswith("no real provider benchmark")
    assert report["passed"] is True
    assert report["matched_size"] == 4
    assert report["decision_drift"]["drift_count"] == 0
    assert report["decision_drift"]["passed"] is True
    assert len(set(report["decision_drift"]["forward_digests"].values())) == 1
    assert len(set(report["decision_drift"]["reverse_digests"].values())) == 1
    assert report["cohort_order_invariance"]["passed"] is True

    assert [item["workers_requested"] for item in report["cohorts"]] == [1, 2, 4]
    for cohort in report["cohorts"]:
        workers = cohort["workers_requested"]
        assert cohort["workers_effective"] == workers
        assert cohort["matched_size"] == report["matched_size"]
        assert cohort["unique_job_denominator"] == report["matched_size"]
        assert cohort["jobs_exactly_once"] is True
        assert cohort["parallelism"]["max_active_workers"] == workers
        assert cohort["parallelism"]["bounded_by_requested"] is True
        assert cohort["parallelism"]["task_count"] == report["matched_size"]
        assert cohort["status_counts"] == {"previewed": report["matched_size"]}
        assert cohort["final_submit_attempts"] == 0
        assert cohort["submit_lane_peak"] == 0
        assert cohort["submit_lane_acquisitions"] == 0
        assert cohort["submission_gate_attempts"] == 0
        assert cohort["batch_consumptions"] == 0
        assert cohort["receipt_admissions"] == 0
        assert cohort["receipt_quality"]["false_positives"] == 0
        assert cohort["receipt_quality"]["observed_admitted_receipts"] == 0
        assert cohort["stale"]["writes"] == 0
        assert cohort["profile_cross_talk"]["events"] == 0
        assert cohort["acquisition"] == {"attempts": report["matched_size"], "empty_polls": 0}
        assert cohort["performance"]["provider_measurement"] is False
        assert cohort["performance"]["promotion_eligible"] is False
        assert cohort["guardrails"]["fresh_fixture_deep_copies"] is True
        assert cohort["guardrails"]["harness_boundary_not_reached"] is True
        assert cohort["isolation"]["namespace_only"] is True
        assert cohort["isolation"]["production_safety_proven"] is False
        assert cohort["production_entry_canary"]["status"] == (
            "harness boundary not reachable/imported"
        )

    assert report["fixture_integrity"]["fixture_sha256"] == report["fixture_sha256"]
    assert report["fixture_integrity"]["fixture_sha256_after"] == report["fixture_sha256"]
    assert report["fixture_integrity"]["fixture_bytes_unchanged"] is True


def test_report_can_only_be_written_to_explicit_output_path(tmp_path: Path) -> None:
    output = tmp_path / "reports" / "cohort.json"
    report = run_dry_cohort_benchmark(
        FIXTURE,
        db_root=tmp_path / "db",
        data_root=tmp_path / "data",
        output_path=output,
    )

    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["fixture_sha256"] == report["fixture_sha256"]
    assert loaded["passed"] is True
    with pytest.raises(FileExistsError):
        write_benchmark_report(report, output)
    with pytest.raises(ValueError, match=".json suffix"):
        write_benchmark_report(report, tmp_path / "reports" / "cohort.txt")


def test_output_path_rejects_default_db_and_input_roots(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="isolated temporary root"):
        run_dry_cohort_benchmark(
            FIXTURE,
            db_root=tmp_path / "db-default",
            data_root=tmp_path / "data-default",
            output_path=config.DB_PATH,
        )
    with pytest.raises(ValueError, match="separate from"):
        run_dry_cohort_benchmark(
            FIXTURE,
            db_root=tmp_path / "db-input",
            data_root=tmp_path / "data-input",
            output_path=FIXTURE,
        )


def test_preexisting_nonzero_probe_state_fails_closed_without_reset(tmp_path: Path) -> None:
    db_root = tmp_path / "db"
    db_root.mkdir()
    db_path = db_root / "cohort-1.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE benchmark_side_effects (name TEXT PRIMARY KEY, count INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO benchmark_side_effects VALUES ('final_submit_attempts', 7)"
        )

    with pytest.raises(ValueError, match="pre-existing benchmark side-effect state"):
        run_dry_cohort_benchmark(
            FIXTURE,
            db_root=db_root,
            data_root=tmp_path / "data",
        )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT count FROM benchmark_side_effects WHERE name='final_submit_attempts'"
        ).fetchone() == (7,)


def test_serial_workload_gate_is_observed_as_peak_one_and_fails(tmp_path: Path) -> None:
    gate_lock = threading.Lock()
    report = run_dry_cohort_benchmark(
        FIXTURE,
        db_root=tmp_path / "db",
        data_root=tmp_path / "data",
        workload_gate=lambda: gate_lock,
    )

    assert report["passed"] is False
    two = next(item for item in report["cohorts"] if item["workers_requested"] == 2)
    four = next(item for item in report["cohorts"] if item["workers_requested"] == 4)
    assert two["parallelism"]["max_active_workers"] == 1
    assert four["parallelism"]["max_active_workers"] == 1
    assert two["guardrails"]["bounded_parallelism_observed"] is False
    assert four["guardrails"]["bounded_parallelism_observed"] is False


def test_pre_evaluate_barrier_does_not_inflate_runtime_peak(monkeypatch) -> None:
    raw, _ = dry_run_benchmark._raw_fixture(FIXTURE)
    _baseline, baseline_parallelism = dry_run_benchmark._parallel_replay(raw, workers=2)
    pre_evaluate_barrier = threading.Barrier(2)
    original_evaluate = dry_run_benchmark.evaluate_scenario

    def waiting_evaluate(*args, **kwargs):
        pre_evaluate_barrier.wait(timeout=30)
        return original_evaluate(*args, **kwargs)

    monkeypatch.setattr(dry_run_benchmark, "evaluate_scenario", waiting_evaluate)
    _delayed, delayed_parallelism = dry_run_benchmark._parallel_replay(raw, workers=2)

    assert baseline_parallelism["max_active_workers"] == 2
    assert delayed_parallelism["max_active_workers"] == baseline_parallelism["max_active_workers"]


def test_production_entry_canaries_are_not_reached(monkeypatch, tmp_path: Path) -> None:
    import applypilot.config as app_config
    from applypilot import database

    def unexpected(*args, **kwargs):
        raise AssertionError("production entrypoint reached")

    monkeypatch.setattr(database, "get_connection", unexpected)
    monkeypatch.setattr(app_config, "load_profile", unexpected)
    report = run_dry_cohort_benchmark(
        FIXTURE,
        db_root=tmp_path / "db",
        data_root=tmp_path / "data",
    )

    assert report["production_entry_canary"]["passed"] is True
    assert report["production_entry_canary"]["status"] == (
        "harness boundary not reachable/imported"
    )
    for cohort in report["cohorts"]:
        assert cohort["production_entry_canary"]["passed"] is True


def test_same_status_counts_with_different_scenario_decision_fails_drift_gate(
    monkeypatch,
    tmp_path: Path,
) -> None:
    original_cohort = dry_run_benchmark._cohort
    calls = 0

    def drifted_cohort(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original_cohort(*args, **kwargs)
        if calls == 2:
            result["decision_signature"][0]["status"] = "different_scenario_outcome"
            result["decision_digest"] = "d" * 64
        return result

    monkeypatch.setattr(dry_run_benchmark, "_cohort", drifted_cohort)
    report = run_dry_cohort_benchmark(
        FIXTURE,
        db_root=tmp_path / "db",
        data_root=tmp_path / "data",
    )

    assert report["passed"] is False
    assert report["decision_drift"]["drift_count"] > 0
    assert report["decision_drift"]["passed"] is False


def test_default_applypilot_roots_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="isolated temporary root"):
        run_dry_cohort_benchmark(
            FIXTURE,
            db_root=config.APP_DIR,
            data_root=tmp_path / "data",
        )
    with pytest.raises(ValueError, match="isolated temporary root"):
        run_dry_cohort_benchmark(
            FIXTURE,
            db_root=tmp_path / "db",
            data_root=config.APP_DIR,
        )


def test_small_fixture_is_rejected_before_executor_barrier(tmp_path: Path) -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["scenarios"] = raw["scenarios"][:1]
    small_fixture = tmp_path / "small.json"
    small_fixture.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="at least as many"):
        run_dry_cohort_benchmark(
            small_fixture,
            db_root=tmp_path / "db",
            data_root=tmp_path / "data",
        )


@pytest.mark.parametrize(
    ("field", "guardrail"),
    [
        ("final_submit_attempts", "final_submit_attempts_zero"),
        ("submission_gate_attempts", "submission_gate_zero"),
        ("receipt_admissions", "receipt_admission_zero"),
    ],
)
def test_side_effect_guardrails_fail_closed_on_tampered_fixture(
    tmp_path: Path,
    field: str,
    guardrail: str,
) -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["scenarios"][0]["guardrails"][field] = 1
    tampered = tmp_path / f"tampered-{field}.json"
    tampered.write_text(json.dumps(raw), encoding="utf-8")

    report = run_dry_cohort_benchmark(
        tampered,
        db_root=tmp_path / "db",
        data_root=tmp_path / "data",
    )

    assert report["passed"] is False
    assert report["cohorts"][0]["guardrails"][guardrail] is False


def test_forbidden_submit_activity_or_non_dry_scenario_cannot_pass(
    tmp_path: Path,
) -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["runtimes"]["synthetic-local-replay"]["cohort-workday-multipage"][
        "result"
    ]["observations"]["tool_calls"].append(
        {"name": "browser_submit", "side_effect": "write"}
    )
    forbidden = tmp_path / "forbidden.json"
    forbidden.write_text(json.dumps(raw), encoding="utf-8")
    report = run_dry_cohort_benchmark(
        forbidden,
        db_root=tmp_path / "db-forbidden",
        data_root=tmp_path / "data-forbidden",
    )
    assert report["passed"] is False
    assert report["cohorts"][0]["forbidden_activity_count"] == 1

    raw["scenarios"][0]["dry_run"] = False
    non_dry = tmp_path / "non-dry.json"
    non_dry.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="not dry-run"):
        run_dry_cohort_benchmark(
            non_dry,
            db_root=tmp_path / "db-non-dry",
            data_root=tmp_path / "data-non-dry",
        )
