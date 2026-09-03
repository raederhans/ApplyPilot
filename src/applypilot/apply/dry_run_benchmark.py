"""Deterministic, submit-free cohort evidence for local application flows.

This module deliberately sits beside the replay evaluator instead of calling
the application launcher.  Its input is one immutable, synthetic fixture and
its output is diagnostic evidence only: it is never a live provider benchmark
and it cannot authorize worker promotion.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
import threading
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from applypilot import config
from applypilot.apply.runtime_evaluation import (
    EvaluationReport,
    RuntimeRegistry,
    ScriptedReplayRuntime,
    compare_runtimes,
    evaluate_scenario,
    scenario_from_mapping,
)

BENCHMARK_SCHEMA_VERSION = "applypilot-dry-cohort-benchmark/v1"
BENCHMARK_LABEL = "replay+synthetic+dry-run_not_a_real_provider_benchmark"
EVIDENCE_LABELS = ("replay", "synthetic", "dry-run")
SUPPORTED_COHORTS = (1, 2, 4)
_HARNESS_BOUNDARY = "harness boundary not reachable/imported"
_REQUIRED_SCENARIO_GUARDRAILS = frozenset(
    {
        "final_submit_attempts",
        "submit_lane_peak",
        "submit_lane_acquisitions",
        "stale_writes",
        "profile_cross_talk",
        "submission_gate_attempts",
        "batch_consumptions",
        "receipt_admissions",
        "acquisition_attempts",
        "empty_polls",
    }
)


def _resolved(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


def _reject_live_or_default_root(path: Path | str, *, name: str) -> Path:
    resolved = _resolved(path)
    forbidden = (_resolved(config.APP_DIR), _resolved(config.DB_PATH))
    if any(
        resolved == item
        or resolved.is_relative_to(item)
        or item.is_relative_to(resolved)
        for item in forbidden
    ):
        raise ValueError(f"{name} must be an isolated temporary root, not the default CapyPilot data path")
    return resolved


def _reject_output_path(
    path: Path | str,
    *,
    fixture_path: Path,
    db_root: Path,
    data_root: Path,
) -> Path:
    """Reject output paths that could alias inputs or ApplyPilot state."""
    resolved = _reject_live_or_default_root(path, name="output_path")
    forbidden = (fixture_path, db_root, data_root)
    if any(
        resolved == item
        or resolved.is_relative_to(item)
        or item.is_relative_to(resolved)
        for item in forbidden
    ):
        raise ValueError("output_path must be separate from fixture_path, db_root, and data_root")
    return resolved


def _raw_fixture(path: Path | str) -> tuple[dict[str, Any], str]:
    fixture_path = _resolved(path)
    if not fixture_path.is_file():
        raise FileNotFoundError(f"benchmark fixture does not exist: {fixture_path}")
    raw_bytes = fixture_path.read_bytes()
    fixture_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    raw = json.loads(raw_bytes.decode("utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("benchmark fixture must be a JSON object")
    return raw, fixture_sha256


def _validate_fixture(raw: Mapping[str, Any]) -> tuple[tuple[Mapping[str, Any], ...], Mapping[str, Any]]:
    if raw.get("schema_version") != BENCHMARK_SCHEMA_VERSION:
        raise ValueError("unsupported benchmark fixture schema")
    if raw.get("label") != BENCHMARK_LABEL:
        raise ValueError("benchmark fixture must carry the replay/synthetic/dry-run label")
    scenarios_raw = raw.get("scenarios")
    runtimes_raw = raw.get("runtimes")
    if not isinstance(scenarios_raw, list) or not scenarios_raw:
        raise ValueError("benchmark fixture must contain scenarios")
    if not isinstance(runtimes_raw, dict) or not runtimes_raw:
        raise ValueError("benchmark fixture must contain runtimes")
    scenarios: list[Mapping[str, Any]] = []
    seen_ids: set[str] = set()
    for item in scenarios_raw:
        if not isinstance(item, Mapping):
            raise TypeError("benchmark scenarios must be objects")
        scenario_id = str(item.get("scenario_id") or "")
        if not scenario_id or scenario_id in seen_ids:
            raise ValueError("benchmark scenario ids must be non-empty and unique")
        seen_ids.add(scenario_id)
        if item.get("dry_run") is not True:
            raise ValueError(f"benchmark scenario is not dry-run: {scenario_id}")
        provider = str(item.get("provider") or "").casefold()
        if provider not in {"workday", "smartrecruiters"}:
            raise ValueError(f"benchmark scenario provider is not synthetic ATS: {scenario_id}")
        guardrails = item.get("guardrails")
        if not isinstance(guardrails, Mapping):
            raise TypeError(f"benchmark scenario guardrails missing: {scenario_id}")
        missing = _REQUIRED_SCENARIO_GUARDRAILS - set(guardrails)
        if missing:
            raise ValueError(f"benchmark scenario guardrails missing {sorted(missing)}: {scenario_id}")
        if any(
            isinstance(guardrails.get(name), bool)
            or not isinstance(guardrails.get(name), int)
            or guardrails[name] < 0
            for name in _REQUIRED_SCENARIO_GUARDRAILS
        ):
            raise ValueError(f"benchmark guardrail values must be non-negative numbers: {scenario_id}")
        scenarios.append(item)
    return tuple(scenarios), runtimes_raw


def load_benchmark_fixture(path: Path | str) -> tuple[dict[str, Any], str]:
    """Load and validate one fixture, returning its immutable JSON copy and SHA-256."""
    raw, fixture_sha256 = _raw_fixture(path)
    _validate_fixture(raw)
    return json.loads(json.dumps(raw)), fixture_sha256


def _registry_and_scenarios(
    scenarios_raw: Sequence[Mapping[str, Any]],
    runtimes_raw: Mapping[str, Any],
) -> tuple[RuntimeRegistry, tuple[Any, ...]]:
    registry = RuntimeRegistry()
    for runtime_name, turns in runtimes_raw.items():
        if not isinstance(runtime_name, str) or not runtime_name.strip():
            raise ValueError("runtime names must be non-empty strings")
        if not isinstance(turns, Mapping):
            raise TypeError("runtime turns must be objects")
        registry.register(
            runtime_name,
            ScriptedReplayRuntime(turns, name=runtime_name),
        )
    scenarios = tuple(scenario_from_mapping(item) for item in scenarios_raw)
    return registry, scenarios


def _replay(raw: Mapping[str, Any], *, copy_fixture: bool = True) -> EvaluationReport:
    # Do not let one worker mutate another worker's fixture view.  This copy is
    # deliberately made inside the worker task, after fixture bytes were
    # hashed, so every task consumes the same immutable source value.
    worker_raw = json.loads(json.dumps(raw)) if copy_fixture else raw
    scenarios_raw, runtimes_raw = _validate_fixture(worker_raw)
    registry, scenarios = _registry_and_scenarios(scenarios_raw, runtimes_raw)
    return compare_runtimes(scenarios, registry)


def _parallel_replay(
    raw: Mapping[str, Any],
    *,
    workers: int,
    workload_gate: Any | None = None,
) -> tuple[EvaluationReport, dict[str, Any]]:
    """Evaluate each runtime/scenario task once with bounded concurrent claims."""
    scenarios_raw, runtimes_raw = _validate_fixture(raw)
    scenarios = tuple(scenario_from_mapping(item) for item in scenarios_raw)
    tasks = tuple(
        (runtime_name, turns, scenario)
        for runtime_name, turns in runtimes_raw.items()
        for scenario in scenarios
    )
    if len(tasks) < workers:
        raise ValueError(
            "benchmark fixture must contain at least as many runtime/scenario tasks "
            "as requested workers"
        )
    active_workers = 0
    max_active_workers = 0
    active_lock = threading.Lock()
    run_entry_barrier = threading.Barrier(workers)
    worker_thread_ids: list[int] = []
    task_inputs: list[dict[str, Any]] = []

    def enter_workload() -> None:
        nonlocal active_workers, max_active_workers
        with active_lock:
            active_workers += 1
            max_active_workers = max(max_active_workers, active_workers)
            worker_thread_ids.append(threading.get_ident())

    def leave_workload() -> None:
        nonlocal active_workers
        with active_lock:
            active_workers -= 1

    class InstrumentedReplayRuntime(ScriptedReplayRuntime):
        """Record concurrency only while the scripted runtime is working."""

        def run(self, request: Any) -> Any:
            # This rendezvous happens inside runtime.run and before the
            # interval counter. Waiting here can never inflate max_active.
            run_entry_barrier.wait(timeout=30)
            gate = workload_gate() if workload_gate is not None else nullcontext()
            with gate:
                enter_workload()
                try:
                    # The fixture latency remains recorded metadata. This
                    # short local yield only makes the synthetic run interval
                    # observable; it is never reported as provider latency.
                    time.sleep(0.001)
                    return super().run(request)
                finally:
                    leave_workload()

    def run_task(task: tuple[str, Any, Any]) -> Any:
        nonlocal active_workers, max_active_workers
        runtime_name, turns, scenario = task
        copied_turns = json.loads(json.dumps(turns))
        task_inputs.append(copied_turns)
        runtime = InstrumentedReplayRuntime(copied_turns, name=runtime_name)
        return evaluate_scenario(
            scenario,
            runtime_name=runtime_name,
            runtime=runtime,
        )

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"cohort-{workers}") as pool:
        evaluations = tuple(pool.map(run_task, tasks))
    wall_clock_ms = round((time.perf_counter() - started) * 1000, 3)
    return (
        EvaluationReport(
            label="replay_conformance_only_not_a_real_provider_benchmark",
            evaluations=evaluations,
        ),
        {
            "max_active_workers": max_active_workers,
            "worker_thread_ids": tuple(sorted(set(worker_thread_ids))),
            "task_copy_count": len(task_inputs),
            "distinct_task_copies": len({id(item) for item in task_inputs}),
            "task_count": len(tasks),
            "wall_clock_ms": wall_clock_ms,
        },
    )


def _isolated_probe(
    db_root: Path,
    data_root: Path,
    *,
    cohort_id: str,
    fixture_sha256: str,
) -> dict[str, Any]:
    db_root.mkdir(parents=True, exist_ok=True)
    data_path = data_root / cohort_id
    data_path.mkdir(parents=True, exist_ok=True)
    db_path = db_root / f"{cohort_id}.sqlite3"
    # Inspect any prior benchmark side-effect table before creating anything;
    # never replace/reset a non-zero value. This is a fail-closed namespace
    # check, not a claim about production ApplyPilot database state.
    with sqlite3.connect(db_path) as connection:
        side_effect_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='benchmark_side_effects'"
        ).fetchone()
        prior_side_effect_counts: dict[str, int] = {}
        if side_effect_table:
            rows = connection.execute(
                "SELECT name, count FROM benchmark_side_effects ORDER BY name"
            ).fetchall()
            prior_side_effect_counts = {str(name): int(count) for name, count in rows}
            if any(value != 0 for value in prior_side_effect_counts.values()):
                raise ValueError("pre-existing benchmark side-effect state is non-zero")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS benchmark_probe_events "
            "(event_id INTEGER PRIMARY KEY AUTOINCREMENT, cohort_id TEXT NOT NULL, "
            "fixture_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO benchmark_probe_events (cohort_id, fixture_sha256, created_at) "
            "VALUES (?, ?, ?)",
            (cohort_id, fixture_sha256, datetime.now(UTC).isoformat()),
        )
    return {
        "db_path": str(db_path),
        "data_root": str(data_path),
        "namespace": "benchmark_probe_events",
        "namespace_only": True,
        "prior_side_effect_counts": prior_side_effect_counts,
        "production_safety_proven": False,
        "production_safety_status": _HARNESS_BOUNDARY,
    }


def _production_entry_canary() -> dict[str, Any]:
    """Describe the intentionally unreachable production side-effect boundary."""
    return {
        name: {"called": False, "status": _HARNESS_BOUNDARY}
        for name in (
            "default_database.get_connection",
            "config.load_profile",
            "SubmissionGate",
            "reservation",
            "receipt_admission",
            "submit",
        )
    } | {"passed": True, "status": _HARNESS_BOUNDARY}


def _status_counts(report: EvaluationReport) -> dict[str, int]:
    return dict(sorted(Counter(item.status for item in report.evaluations).items()))


def _receipt_quality(
    report: EvaluationReport,
    scenarios_raw: Sequence[Mapping[str, Any]],
) -> dict[str, int | bool]:
    expected = {
        str(item["scenario_id"]): item.get("expected", {}).get("receipt") is True
        for item in scenarios_raw
    }
    evaluations = {
        (item.runtime_name, item.scenario_id): item.receipt_admitted
        for item in report.evaluations
    }
    runtime_names = tuple(sorted({runtime for runtime, _ in evaluations}))
    false_positives = sum(
        int(observed and not expected.get(scenario_id, False))
        for (_runtime, scenario_id), observed in evaluations.items()
    )
    missing = sum(
        int(expected_value and not evaluations.get((runtime, scenario_id), False))
        for runtime in runtime_names
        for scenario_id, expected_value in expected.items()
    )
    return {
        "expected_receipts": sum(expected.values()) * len(runtime_names),
        "observed_admitted_receipts": sum(evaluations.values()),
        "false_positives": false_positives,
        "missing_expected_receipts": missing,
        "passed": false_positives == 0 and missing == 0,
    }


def _performance(report: EvaluationReport) -> dict[str, Any]:
    values = [
        float(item.latency_ms)
        for item in report.evaluations
        if item.latency_ms is not None
    ]
    return {
        "source": "recorded_replay_fixture_latency",
        "provider_measurement": False,
        "samples": len(values),
        "min_ms": min(values) if values else None,
        "max_ms": max(values) if values else None,
        "mean_ms": statistics.fmean(values) if values else None,
        "promotion_eligible": False,
        "promotion_authority": "none",
    }


def _decision_signature(
    report: EvaluationReport,
) -> dict[tuple[str, str], tuple[str, str, tuple[str, ...], tuple[str, ...], bool]]:
    return {
        (item.runtime_name, item.scenario_id): (
            item.status,
            item.result_source,
            tuple(sorted(item.observed_tools)),
            tuple(sorted(item.observed_side_effects)),
            item.receipt_admitted,
        )
        for item in report.evaluations
    }


def _decision_digest(report: EvaluationReport) -> str:
    canonical = [
        {
            "runtime": runtime,
            "scenario": scenario,
            "status": values[0],
            "result_source": values[1],
            "tools": list(values[2]),
            "side_effects": list(values[3]),
            "receipt_admitted": values[4],
        }
        for (runtime, scenario), values in sorted(_decision_signature(report).items())
    ]
    return hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def _cohort(
    raw: Mapping[str, Any],
    scenarios_raw: Sequence[Mapping[str, Any]],
    *,
    workers: int,
    fixture_sha256: str,
    db_root: Path,
    data_root: Path,
    workload_gate: Any | None = None,
) -> dict[str, Any]:
    cohort_id = f"cohort-{workers}"
    isolation = _isolated_probe(
        db_root,
        data_root,
        cohort_id=cohort_id,
        fixture_sha256=fixture_sha256,
    )
    # Each runtime/scenario pair is one claimed task for the cohort.  The
    # bounded executor partitions that task set; it never multiplies workload
    # by the worker count.
    baseline, parallelism = _parallel_replay(raw, workers=workers, workload_gate=workload_gate)
    wall_clock_ms = parallelism["wall_clock_ms"]
    runtime_count = len(baseline.evaluations) // len(scenarios_raw)
    drift_count = 0
    status_counts = _status_counts(baseline)
    receipt_quality = _receipt_quality(baseline, scenarios_raw)
    final_submit_attempts = sum(
        int(float(item["guardrails"]["final_submit_attempts"]))
        for item in scenarios_raw
    )
    submit_lane_peak = max(
        int(item["guardrails"]["submit_lane_peak"]) for item in scenarios_raw
    )
    submit_lane_acquisitions = sum(
        int(float(item["guardrails"]["submit_lane_acquisitions"]))
        for item in scenarios_raw
    )
    stale_writes = sum(int(float(item["guardrails"]["stale_writes"])) for item in scenarios_raw)
    profile_cross_talk = sum(
        int(float(item["guardrails"]["profile_cross_talk"])) for item in scenarios_raw
    )
    submission_gate_attempts = sum(
        int(float(item["guardrails"]["submission_gate_attempts"]))
        for item in scenarios_raw
    )
    batch_consumptions = sum(
        int(float(item["guardrails"]["batch_consumptions"]))
        for item in scenarios_raw
    )
    receipt_admissions = sum(
        int(float(item["guardrails"]["receipt_admissions"]))
        for item in scenarios_raw
    )
    acquisition_attempts = sum(
        int(float(item["guardrails"]["acquisition_attempts"]))
        for item in scenarios_raw
    )
    empty_polls = sum(
        int(float(item["guardrails"]["empty_polls"])) for item in scenarios_raw
    )
    production_entry_canary = _production_entry_canary()
    forbidden_activity = sum(
        int(
            any(
                token in " ".join((*item.observed_tools, *item.observed_side_effects)).casefold()
                for token in ("submit", "batch_consumption", "submission_gate")
            )
        )
        for item in baseline.evaluations
    )
    execution_counts = Counter(
        (item.runtime_name, item.scenario_id)
        for item in baseline.evaluations
    )
    jobs_exactly_once = all(
        execution_counts[(runtime_name, str(item["scenario_id"]))] == 1
        for runtime_name in sorted({item.runtime_name for item in baseline.evaluations})
        for item in scenarios_raw
    )
    guardrails = {
        "fixture_replay_passed": baseline.passed,
        "dry_run_forced": all(item.get("dry_run") is True for item in scenarios_raw),
        "final_submit_attempts_zero": final_submit_attempts == 0,
        "submit_lane_peak_zero": submit_lane_peak == 0,
        "submit_lane_acquisitions_zero": submit_lane_acquisitions == 0,
        "receipt_quality_passed": receipt_quality["passed"],
        "stale_writes_zero": stale_writes == 0,
        "profile_cross_talk_zero": profile_cross_talk == 0,
        "decision_drift_zero": drift_count == 0,
        "matched_fixture_size": len(baseline.evaluations) == len(scenarios_raw) * runtime_count,
        "jobs_exactly_once_per_cohort": jobs_exactly_once,
        "submission_gate_zero": submission_gate_attempts == 0,
        "batch_consumption_zero": batch_consumptions == 0,
        "receipt_admission_zero": receipt_admissions == 0,
        "acquisition_recorded": acquisition_attempts == len(scenarios_raw),
        "empty_polls_recorded": empty_polls == 0,
        "bounded_parallelism_observed": parallelism["max_active_workers"] == min(workers, parallelism["task_count"]),
        "distinct_worker_threads": len(parallelism["worker_thread_ids"]) == min(workers, parallelism["task_count"]),
        "fresh_fixture_deep_copies": parallelism["distinct_task_copies"] == parallelism["task_count"],
        "harness_boundary_not_reached": production_entry_canary["passed"] is True,
        "no_forbidden_replay_activity": forbidden_activity == 0,
    }
    return {
        "cohort_id": cohort_id,
        "fixture_sha256": fixture_sha256,
        "workers_requested": workers,
        "workers_effective": len(parallelism["worker_thread_ids"]),
        "matched_size": len(scenarios_raw),
        "unique_job_denominator": len(scenarios_raw),
        "jobs_exactly_once": jobs_exactly_once,
        "status_counts": status_counts,
        "receipt_quality": receipt_quality,
        "stale": {"writes": stale_writes, "passed": stale_writes == 0},
        "profile_cross_talk": {"events": profile_cross_talk, "passed": profile_cross_talk == 0},
        "decision_drift": {"worker_comparisons": 0, "drift_count": drift_count, "passed": drift_count == 0},
        "decision_signature": [
            {
                "runtime": runtime,
                "scenario": scenario,
                "status": status,
                "result_source": source,
                "tools": list(tools),
                "side_effects": list(effects),
                "receipt_admitted": receipt,
            }
            for (runtime, scenario), (status, source, tools, effects, receipt)
            in sorted(_decision_signature(baseline).items())
        ],
        "decision_digest": _decision_digest(baseline),
        "performance": _performance(baseline),
        "final_submit_attempts": final_submit_attempts,
        "submit_lane_peak": submit_lane_peak,
        "submit_lane_acquisitions": submit_lane_acquisitions,
        "submission_gate_attempts": submission_gate_attempts,
        "batch_consumptions": batch_consumptions,
        "receipt_admissions": receipt_admissions,
        "acquisition": {
            "attempts": acquisition_attempts,
            "empty_polls": empty_polls,
        },
        "parallelism": {
            "max_active_workers": parallelism["max_active_workers"],
            "worker_thread_ids": list(parallelism["worker_thread_ids"]),
            "bounded_by_requested": parallelism["max_active_workers"] <= workers,
            "task_count": parallelism["task_count"],
        },
        "wall_clock_ms": wall_clock_ms,
        "isolation": isolation,
        "production_entry_canary": production_entry_canary,
        "forbidden_activity_count": forbidden_activity,
        "guardrails": guardrails,
        "passed": all(guardrails.values()),
    }


def run_dry_cohort_benchmark(
    fixture_path: Path | str,
    *,
    db_root: Path | str,
    data_root: Path | str,
    cohorts: Iterable[int] = SUPPORTED_COHORTS,
    output_path: Path | str | None = None,
    workload_gate: Any | None = None,
) -> dict[str, Any]:
    """Run matched 1/2/4 synthetic cohorts in isolated roots."""
    requested = tuple(cohorts)
    if requested != SUPPORTED_COHORTS:
        raise ValueError("benchmark cohorts are fixed to matched 1/2/4 workers")
    isolated_db_root = _reject_live_or_default_root(db_root, name="db_root")
    isolated_data_root = _reject_live_or_default_root(data_root, name="data_root")
    if isolated_db_root == isolated_data_root or isolated_db_root.is_relative_to(isolated_data_root) or isolated_data_root.is_relative_to(isolated_db_root):
        raise ValueError("db_root and data_root must be separate isolated roots")
    frozen_fixture_path = _resolved(fixture_path)
    isolated_output_path = None
    if output_path is not None:
        isolated_output_path = _reject_output_path(
            output_path,
            fixture_path=frozen_fixture_path,
            db_root=isolated_db_root,
            data_root=isolated_data_root,
        )
    raw, fixture_sha256 = _raw_fixture(frozen_fixture_path)
    scenarios_raw, _ = _validate_fixture(raw)
    cohort_reports = tuple(
        _cohort(
            raw,
            scenarios_raw,
            workers=workers,
            fixture_sha256=fixture_sha256,
            db_root=isolated_db_root,
            data_root=isolated_data_root,
            workload_gate=workload_gate,
        )
        for workers in requested
    )
    reverse_reports = tuple(
        _cohort(
            raw,
            scenarios_raw,
            workers=workers,
            fixture_sha256=fixture_sha256,
            db_root=isolated_db_root / "reverse-order",
            data_root=isolated_data_root / "reverse-order",
            workload_gate=workload_gate,
        )
        for workers in reversed(requested)
    )

    def order_signature(item: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            item["cohort_id"],
            item["workers_requested"],
            item["workers_effective"],
            item["matched_size"],
            item["status_counts"],
            item["receipt_quality"],
            item["stale"],
            item["profile_cross_talk"],
            item["decision_drift"],
            item["final_submit_attempts"],
            item["submit_lane_peak"],
            item["submit_lane_acquisitions"],
            item["submission_gate_attempts"],
            item["batch_consumptions"],
            item["receipt_admissions"],
            item["acquisition"],
            item["jobs_exactly_once"],
            tuple(
                tuple(sorted(entry.items()))
                for entry in item["decision_signature"]
            ),
        )

    forward_by_cohort = {item["cohort_id"]: item for item in cohort_reports}
    reverse_by_cohort = {item["cohort_id"]: item for item in reverse_reports}
    order_invariant = all(
        order_signature(forward_by_cohort[key]) == order_signature(reverse_by_cohort[key])
        for key in forward_by_cohort
    )
    forward_decision_digests = {
        key: value["decision_digest"] for key, value in forward_by_cohort.items()
    }
    reverse_decision_digests = {
        key: value["decision_digest"] for key, value in reverse_by_cohort.items()
    }
    decision_drift_count = sum(
        forward_decision_digests[key] != reverse_decision_digests.get(key)
        for key in forward_decision_digests
    )
    reference_forward_digest = next(iter(forward_decision_digests.values()))
    reference_reverse_digest = next(iter(reverse_decision_digests.values()))
    decision_drift_count += sum(
        digest != reference_forward_digest for digest in forward_decision_digests.values()
    )
    decision_drift_count += sum(
        digest != reference_reverse_digest for digest in reverse_decision_digests.values()
    )
    fixture_sha256_after = hashlib.sha256(frozen_fixture_path.read_bytes()).hexdigest()
    fixture_bytes_unchanged = fixture_sha256_after == fixture_sha256
    matched_size = len(scenarios_raw)
    fixture_integrity = {
        "schema_version": raw["schema_version"],
        "fixture_sha256": fixture_sha256,
        "fixture_sha256_after": fixture_sha256_after,
        "fixture_bytes_unchanged": fixture_bytes_unchanged,
        "immutable_across_cohorts": len({item["fixture_sha256"] for item in cohort_reports}) == 1,
        "matched_size": matched_size,
        "passed": bool(fixture_sha256) and matched_size > 0 and fixture_bytes_unchanged,
    }
    guardrails = {
        "fixture_integrity": fixture_integrity["passed"],
        "labels_explicit": raw["label"] == BENCHMARK_LABEL,
        "cohorts_matched": all(item["matched_size"] == matched_size for item in cohort_reports),
        "all_cohorts_passed": all(item["passed"] for item in cohort_reports),
        "performance_not_promotion_authority": all(
            item["performance"]["promotion_eligible"] is False for item in cohort_reports
        ),
        "cohort_order_invariant": order_invariant,
        "decision_drift_zero": decision_drift_count == 0,
        "production_entry_boundary_not_reached": all(
            item["guardrails"]["harness_boundary_not_reached"] for item in cohort_reports
        ),
    }
    report: dict[str, Any] = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "label": BENCHMARK_LABEL,
        "evidence_labels": list(EVIDENCE_LABELS),
        "claim_boundary": "synthetic replay/dry-run evidence only; no real provider benchmark",
        "fixture_sha256": fixture_sha256,
        "fixture_integrity": fixture_integrity,
        "matched_size": matched_size,
        "status_counts": cohort_reports[0]["status_counts"],
        "receipt_quality": cohort_reports[0]["receipt_quality"],
        "stale": cohort_reports[0]["stale"],
        "profile_cross_talk": cohort_reports[0]["profile_cross_talk"],
        "final_submit_attempts": cohort_reports[0]["final_submit_attempts"],
        "submit_lane_peak": cohort_reports[0]["submit_lane_peak"],
        "submit_lane_acquisitions": cohort_reports[0]["submit_lane_acquisitions"],
        "performance": {
            "source": "recorded_replay_fixture_latency",
            "provider_measurement": False,
            "promotion_eligible": False,
            "by_cohort": {
                item["cohort_id"]: item["performance"] for item in cohort_reports
            },
        },
        "cohorts": list(cohort_reports),
        "cohort_order_invariance": {
            "forward": [item["cohort_id"] for item in cohort_reports],
            "reverse": [item["cohort_id"] for item in reverse_reports],
            "passed": order_invariant,
        },
        "decision_drift": {
            "forward_digests": forward_decision_digests,
            "reverse_digests": reverse_decision_digests,
            "forward_reference": reference_forward_digest,
            "reverse_reference": reference_reverse_digest,
            "drift_count": decision_drift_count,
            "passed": decision_drift_count == 0,
        },
        "production_entry_canary": {
            "status": _HARNESS_BOUNDARY,
            "entries": cohort_reports[0]["production_entry_canary"],
            "passed": all(
                item["guardrails"]["harness_boundary_not_reached"] for item in cohort_reports
            ),
        },
        "guardrails": guardrails,
        "performance_authority": "diagnostic_only_no_production_worker_promotion",
        "passed": all(guardrails.values()),
    }
    if output_path is not None:
        write_benchmark_report(report, isolated_output_path)
    return report


def write_benchmark_report(report: Mapping[str, Any], output_path: Path | str) -> Path:
    """Write a report only to the caller's explicit output path."""
    target = _reject_live_or_default_root(output_path, name="output_path")
    if target.suffix.casefold() != ".json":
        raise ValueError("output_path must use the .json suffix")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return target


run_benchmark = run_dry_cohort_benchmark


__all__ = [
    "BENCHMARK_LABEL",
    "BENCHMARK_SCHEMA_VERSION",
    "EVIDENCE_LABELS",
    "SUPPORTED_COHORTS",
    "load_benchmark_fixture",
    "run_benchmark",
    "run_dry_cohort_benchmark",
    "write_benchmark_report",
]
