"""Matched full-stack 1/2/4-worker local no-submit cohort evidence.

The runner uses deterministic fixtures, local Chromium, isolated roots, and a
shared SQLite database per cohort.  Its report is diagnostic and can never
grant production worker authority.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import random
import statistics
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from applypilot.apply import agent_runtime
from applypilot.apply.p4_no_submit_worker import ATTRIBUTION_SCHEMA_VERSION
from applypilot.apply.performance_governor import (
    DEFAULT_THRESHOLDS,
    REPORT_SCHEMA_VERSION,
    AdmissionManifest,
    PerformanceGovernor,
)

SUPPORTED_WORKERS = (1, 2, 4)
FIXTURE_SCHEMA_VERSION = "applypilot-full-stack-fixture/v1"


class CohortUnavailable(RuntimeError):
    """The required local Chromium/runtime dependency is unavailable."""


class CohortMetricCollector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.sqlite_waits_ms: list[float] = []
        self.sqlite_busy_errors = 0
        self.worker_spans_ms: dict[int, dict[str, float]] = {}

    def record_sqlite_wait(self, wait_ms: float) -> None:
        with self._lock:
            self.sqlite_waits_ms.append(max(0.0, float(wait_ms)))

    def record_sqlite_busy(self) -> None:
        with self._lock:
            self.sqlite_busy_errors += 1

    def record_worker_span(self, worker_id: int, name: str, duration_ms: float) -> None:
        value = float(duration_ms)
        if not math.isfinite(value) or value < 0:
            raise ValueError("worker span duration must be finite and non-negative")
        if name not in {"playwright_start_ms", "browser_launch_ms", "browser_close_ms"}:
            raise ValueError(f"unknown worker attribution span: {name}")
        with self._lock:
            spans = self.worker_spans_ms.setdefault(int(worker_id), {})
            spans[name] = spans.get(name, 0.0) + value

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "sqlite_lock_wait_ms": list(self.sqlite_waits_ms),
                "sqlite_busy_errors": self.sqlite_busy_errors,
                "worker_lifecycle_spans_ms": {
                    str(worker_id): {name: round(value, 3) for name, value in sorted(spans.items())}
                    for worker_id, spans in sorted(self.worker_spans_ms.items())
                },
            }


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(item) for item in values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[min(rank - 1, len(ordered) - 1)]


def _paired_lower_bound(
    baseline: Sequence[float],
    candidate: Sequence[float],
    *,
    resamples: int = 2000,
) -> float:
    if len(baseline) != len(candidate) or not baseline:
        return 0.0
    ratios = [right / left if left > 0 else 0.0 for left, right in zip(baseline, candidate)]
    rng = random.Random(20260902)
    estimates = []
    for _ in range(resamples):
        sample = [ratios[rng.randrange(len(ratios))] for _ in ratios]
        estimates.append(statistics.fmean(sample))
    return sorted(estimates)[max(0, math.floor(0.025 * len(estimates)) - 1)]


def _windows_process_tree(root_pid: int) -> tuple[int, ...]:
    if platform.system() != "Windows":
        return (root_pid,)
    try:
        import ctypes
        from ctypes import wintypes

        th32cs_snapprocess = 0x00000002
        invalid_handle = ctypes.c_void_p(-1).value

        class ProcessEntry32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        snapshot = kernel32.CreateToolhelp32Snapshot(th32cs_snapprocess, 0)
        if snapshot == invalid_handle:
            return (root_pid,)
        parents: dict[int, int] = {}
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        try:
            ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        selected = {root_pid}
        changed = True
        while changed:
            changed = False
            for pid, parent in parents.items():
                if parent in selected and pid not in selected:
                    selected.add(pid)
                    changed = True
        return tuple(sorted(selected))
    except (AttributeError, OSError, ValueError):
        return (root_pid,)


def _process_tree_rss_bytes() -> int:
    return sum(agent_runtime.process_rss_bytes(pid) for pid in _windows_process_tree(os.getpid()))


def _available_memory_bytes() -> int:
    if platform.system() == "Windows":
        try:
            import ctypes

            class MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatusEx()
            status.dwLength = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullAvailPhys)
        except (AttributeError, OSError, ValueError):
            return 0
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        pages = os.sysconf("SC_AVPHYS_PAGES")
        return int(page_size * pages)
    except (AttributeError, OSError, ValueError):
        return 0


class _RssSampler:
    def __init__(self, interval_seconds: float = 0.1) -> None:
        self.interval_seconds = interval_seconds
        self.peak_bytes = 0
        self.samples = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        def sample() -> None:
            while not self._stop.is_set():
                self.peak_bytes = max(self.peak_bytes, _process_tree_rss_bytes())
                self.samples += 1
                self._stop.wait(self.interval_seconds)

        self._thread = threading.Thread(target=sample, name="p4-rss-sampler", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.peak_bytes = max(self.peak_bytes, _process_tree_rss_bytes())
        self.samples += 1


def load_fixture(path: Path | str) -> tuple[dict[str, Any], str]:
    fixture_path = Path(path).expanduser().resolve()
    raw_bytes = fixture_path.read_bytes()
    raw = json.loads(raw_bytes)
    if not isinstance(raw, dict) or raw.get("schema_version") != FIXTURE_SCHEMA_VERSION:
        raise ValueError("unsupported full-stack cohort fixture")
    tasks = raw.get("tasks")
    if not isinstance(tasks, list) or len(tasks) < 4:
        raise ValueError("cohort fixture requires at least four tasks")
    ids = [str(item.get("task_id") or "") for item in tasks if isinstance(item, Mapping)]
    if len(ids) != len(tasks) or any(not item for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("cohort task ids must be non-empty and unique")
    for item in tasks:
        if item.get("provider") not in {"workday", "smartrecruiters"}:
            raise ValueError("cohort provider must be Workday or SmartRecruiters")
        if item.get("scenario") not in {"routine", "stale", "human_only", "crash_recovery"}:
            raise ValueError("unsupported cohort scenario")
    return raw, hashlib.sha256(raw_bytes).hexdigest()


def _source_manifest_sha256(source_root: Path) -> str:
    paths = sorted((source_root / "src" / "applypilot" / "apply").glob("*.py"))
    manifest = [
        {
            "path": path.relative_to(source_root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in paths
    ]
    return _canonical_digest(manifest)


def _contains(parent: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(parent)
    except ValueError:
        return False
    return True


def _assert_isolated_paths(
    *,
    fixture_path: Path,
    workspace_root: Path,
    report_path: Path,
    source_root: Path,
) -> None:
    if not (source_root / "pyproject.toml").is_file():
        raise ValueError("source_root must be the ApplyPilot repository root")
    if not fixture_path.is_file():
        raise FileNotFoundError(f"cohort fixture does not exist: {fixture_path}")
    if workspace_root.exists():
        raise FileExistsError(f"benchmark workspace already exists: {workspace_root}")
    if report_path.exists():
        raise FileExistsError(f"benchmark report already exists: {report_path}")
    if _contains(source_root, workspace_root) or _contains(source_root, report_path):
        raise ValueError("benchmark workspace and report must remain outside the source tree")

    configured_root = os.environ.get("APPLYPILOT_DIR")
    if configured_root:
        live_root = Path(configured_root).expanduser().resolve()
        if any(
            _contains(live_root, candidate) or _contains(candidate, live_root)
            for candidate in (workspace_root, report_path)
        ):
            raise ValueError("benchmark paths must not alias the configured APPLYPILOT_DIR")


def _quality_projection(results: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "task_id": item["task_id"],
            "provider": item["provider"],
            "scenario": item["scenario"],
            "outcome": item["outcome"],
            "episode_state": item["episode_state"],
            "replay_verified": item["replay_verified"],
            "stale_rejections": item["stale_rejections"],
        }
        for item in sorted(results, key=lambda value: str(value["task_id"]))
    ]


def _attribution_summary(results: Sequence[Mapping[str, object]]) -> dict[str, object]:
    phase_values: dict[str, list[float]] = {}
    coverage: list[float] = []
    unavailable: set[str] = set()
    complete = 0
    for result in results:
        raw = result.get("performance_attribution")
        if not isinstance(raw, Mapping) or raw.get("schema_version") != ATTRIBUTION_SCHEMA_VERSION:
            coverage.append(0.0)
            continue
        raw_spans = raw.get("spans_ms")
        if isinstance(raw_spans, Mapping):
            for name, value in raw_spans.items():
                phase_values.setdefault(str(name), []).append(float(value))
        coverage.append(float(raw.get("attribution_coverage_ratio") or 0.0))
        raw_unavailable = raw.get("unavailable_spans")
        if isinstance(raw_unavailable, Sequence) and not isinstance(raw_unavailable, (str, bytes)):
            unavailable.update(str(item) for item in raw_unavailable)
        complete += int(raw.get("attribution_complete") is True)
    task_count = len(results)
    return {
        "schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "task_count": task_count,
        "complete_tasks": complete,
        "all_tasks_complete": task_count > 0 and complete == task_count,
        "coverage_ratio_min": round(min(coverage, default=0.0), 6),
        "coverage_ratio_p50": round(_percentile(coverage, 0.50), 6),
        "phase_p50_ms": {name: round(_percentile(values, 0.50), 3) for name, values in sorted(phase_values.items())},
        "phase_p95_ms": {name: round(_percentile(values, 0.95), 3) for name, values in sorted(phase_values.items())},
        "unavailable_spans": sorted(unavailable),
    }


def _cohort(
    tasks: Sequence[Mapping[str, object]],
    *,
    workers: int,
    cohort_id: str,
    root: Path,
) -> dict[str, object]:
    from applypilot.apply import launcher

    data_root = root / "data"
    profile_root = root / "profiles"
    output_root = root / "output"
    for path in (data_root, profile_root, output_root):
        path.mkdir(parents=True, exist_ok=False)
    (profile_root / "profile.json").write_text(
        json.dumps({"submission_policy": {"maximum_workers": 1, "submit": False}}),
        encoding="utf-8",
    )
    db_path = data_root / "cohort.sqlite3"
    collector = CohortMetricCollector()
    launcher.initialize_p4_no_submit_database(db_path)
    submit_lane = threading.Semaphore(1)
    partitions = [list(tasks[index::workers]) for index in range(workers)]
    sampler = _RssSampler()
    available_memory = _available_memory_bytes()
    started = time.perf_counter()
    sampler.start()
    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"p4-cohort-{workers}") as pool:
            futures = [
                pool.submit(
                    launcher.run_p4_no_submit_worker,
                    worker_id=worker_id,
                    tasks=partition,
                    cohort_id=cohort_id,
                    root=root,
                    db_path=db_path,
                    submit_lane=submit_lane,
                    metric_sink=collector,
                )
                for worker_id, partition in enumerate(partitions)
            ]
            results = [item for future in futures for item in future.result()]
    except Exception as exc:
        if type(exc).__module__.startswith("playwright"):
            raise CohortUnavailable(f"local Chromium unavailable: {exc}") from exc
        raise
    finally:
        sampler.close()
    wall_clock_ms = (time.perf_counter() - started) * 1000
    results.sort(key=lambda item: str(item["task_id"]))
    for item in results:
        item["cohort_id"] = cohort_id
        item["workers"] = workers
    sqlite_metrics = collector.snapshot()
    quality = _quality_projection(results)
    return {
        "cohort_id": cohort_id,
        "workers": workers,
        "task_count": len(tasks),
        "wall_clock_ms": round(wall_clock_ms, 3),
        "throughput_jobs_per_second": round(len(tasks) / (wall_clock_ms / 1000), 6),
        "process_tree_rss_peak_bytes": sampler.peak_bytes,
        "rss_samples": sampler.samples,
        "available_memory_bytes_at_start": available_memory,
        "latency_ms": [float(item["latency_ms"]) for item in results],
        "submit_lane_wait_ms": [float(item["submit_lane_wait_ms"]) for item in results],
        "submit_lane_hold_ms": [float(item["submit_lane_hold_ms"]) for item in results],
        **sqlite_metrics,
        "quality_digest": _canonical_digest(quality),
        "quality": quality,
        "results": results,
    }


def _aggregate(reports: Sequence[Mapping[str, object]]) -> dict[str, object]:
    results = [item for report in reports for item in report["results"]]  # type: ignore[index]
    latency = [float(item) for report in reports for item in report["latency_ms"]]  # type: ignore[index]
    sqlite_wait = [
        float(item)
        for report in reports
        for item in report["sqlite_lock_wait_ms"]  # type: ignore[index]
    ]
    submit_wait = [
        float(item)
        for report in reports
        for item in report["submit_lane_wait_ms"]  # type: ignore[index]
    ]
    submit_hold = [
        float(item)
        for report in reports
        for item in report["submit_lane_hold_ms"]  # type: ignore[index]
    ]
    side_effect_fields = (
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
    )
    attribution = _attribution_summary(results)
    return {
        "blocks": len(reports),
        "throughput_by_block": [float(item["throughput_jobs_per_second"]) for item in reports],
        "throughput_jobs_per_second": round(
            sum(int(item["task_count"]) for item in reports)
            / (sum(float(item["wall_clock_ms"]) for item in reports) / 1000),
            6,
        ),
        "job_latency_p50_ms": round(_percentile(latency, 0.50), 3),
        "job_latency_p95_ms": round(_percentile(latency, 0.95), 3),
        "process_tree_rss_peak_bytes": max(int(item["process_tree_rss_peak_bytes"]) for item in reports),
        "available_memory_bytes_min": min(int(item["available_memory_bytes_at_start"]) for item in reports),
        "sqlite_busy_errors": sum(int(item["sqlite_busy_errors"]) for item in reports),
        "sqlite_lock_wait_p95_ms": round(_percentile(sqlite_wait, 0.95), 3),
        "sqlite_lock_wait_max_ms": round(max(sqlite_wait, default=0.0), 3),
        "submit_lane_wait_p95_ms": round(_percentile(submit_wait, 0.95), 3),
        "submit_lane_wait_max_ms": round(max(submit_wait, default=0.0), 3),
        "submit_lane_hold_p95_ms": round(_percentile(submit_hold, 0.95), 3),
        "quality_digests": sorted({str(item["quality_digest"]) for item in reports}),
        "task_samples": results,
        "completed_jobs": len(results),
        "side_effects": {field: sum(int(item[field]) for item in results) for field in side_effect_fields},
        "all_replays_verified": all(bool(item["replay_verified"]) for item in results),
        "performance_attribution": attribution,
        "attribution_complete": bool(attribution["all_tasks_complete"]),
        "worker_lifecycle_samples": [item.get("worker_lifecycle_spans_ms", {}) for item in reports],
    }


def _evaluate_admission(
    aggregates: Mapping[int, Mapping[str, object]],
    *,
    rss_budget_bytes: int,
) -> tuple[int, dict[str, object]]:
    baseline = aggregates[1]
    baseline_throughput = list(baseline["throughput_by_block"])  # type: ignore[arg-type]
    speedups: dict[str, object] = {}
    gates: dict[str, dict[str, bool]] = {}
    eligible = 1
    for workers, required in ((2, 1.6), (4, 2.6)):
        current = aggregates[workers]
        lower = _paired_lower_bound(
            baseline_throughput,
            list(current["throughput_by_block"]),  # type: ignore[arg-type]
        )
        ratio = float(current["throughput_jobs_per_second"]) / float(baseline["throughput_jobs_per_second"])
        side_effects = current["side_effects"]
        assert isinstance(side_effects, Mapping)
        available = int(current["available_memory_bytes_min"])
        worker_gates = {
            "speedup_lower_bound": lower >= required,
            "job_p95": float(current["job_latency_p95_ms"])
            <= DEFAULT_THRESHOLDS["job_p95_ratio_max"] * float(baseline["job_latency_p95_ms"]),
            "rss_budget": int(current["process_tree_rss_peak_bytes"]) <= rss_budget_bytes,
            "rss_available_fraction": available > 0
            and int(current["process_tree_rss_peak_bytes"])
            <= available * DEFAULT_THRESHOLDS["available_memory_fraction_max"],
            "sqlite_busy_zero": int(current["sqlite_busy_errors"]) == 0,
            "sqlite_lock_wait_p95": float(current["sqlite_lock_wait_p95_ms"])
            <= DEFAULT_THRESHOLDS["sqlite_lock_wait_p95_ms_max"],
            "sqlite_lock_wait_max": float(current["sqlite_lock_wait_max_ms"])
            <= DEFAULT_THRESHOLDS["sqlite_lock_wait_max_ms_max"],
            "submit_lane_wait_p95": float(current["submit_lane_wait_p95_ms"])
            <= DEFAULT_THRESHOLDS["submit_lane_wait_p95_ms_max"],
            "submit_lane_wait_max": float(current["submit_lane_wait_max_ms"])
            <= DEFAULT_THRESHOLDS["submit_lane_wait_max_ms_max"],
            "submit_lane_hold_p95": float(current["submit_lane_hold_p95_ms"])
            <= DEFAULT_THRESHOLDS["submit_lane_hold_p95_ms_max"],
            "quality_digest_equal": current["quality_digests"] == baseline["quality_digests"]
            and len(current["quality_digests"]) == 1,
            "all_jobs_complete": int(current["completed_jobs"]) == int(current["blocks"]) * 24,
            "replay_verified": bool(current["all_replays_verified"]),
            "all_side_effect_authorities_zero": all(int(value) == 0 for value in side_effects.values()),
        }
        gates[str(workers)] = worker_gates
        speedups[str(workers)] = {
            "point_estimate": round(ratio, 6),
            "paired_bootstrap_95_lower_bound": round(lower, 6),
            "required_lower_bound": required,
        }
        if workers == 2 and all(worker_gates.values()):
            eligible = 2
        elif workers == 4 and eligible == 2 and all(worker_gates.values()):
            eligible = 4
    return eligible, {"speedups": speedups, "gates": gates}


def run_full_stack_no_submit_benchmark(
    fixture_path: Path | str,
    *,
    workspace_root: Path | str,
    output_path: Path | str,
    source_root: Path | str,
    measured_blocks: int = 10,
    warmup_blocks: int = 2,
    rss_budget_bytes: int = 6_000_000_000,
) -> dict[str, object]:
    """Run matched cohorts and write one immutable diagnostic report."""

    if measured_blocks < 2 or warmup_blocks < 0:
        raise ValueError("benchmark requires at least two measured blocks")
    fixture_file = Path(fixture_path).expanduser().resolve()
    root = Path(workspace_root).expanduser().resolve()
    report_path = Path(output_path).expanduser().resolve()
    source = Path(source_root).expanduser().resolve()
    _assert_isolated_paths(
        fixture_path=fixture_file,
        workspace_root=root,
        report_path=report_path,
        source_root=source,
    )
    root.mkdir(parents=True, exist_ok=False)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    fixture, fixture_sha256 = load_fixture(fixture_file)
    tasks = tuple(fixture["tasks"])
    if len(tasks) != 24:
        raise ValueError("promotion fixture must contain exactly 24 matched tasks")

    measured: dict[int, list[dict[str, object]]] = {workers: [] for workers in SUPPORTED_WORKERS}
    total_blocks = warmup_blocks + measured_blocks
    for block in range(total_blocks):
        rotation = block % len(SUPPORTED_WORKERS)
        order = SUPPORTED_WORKERS[rotation:] + SUPPORTED_WORKERS[:rotation]
        for workers in order:
            cohort = _cohort(
                tasks,
                workers=workers,
                cohort_id=f"block-{block:02d}-workers-{workers}",
                root=root / f"block-{block:02d}" / f"workers-{workers}",
            )
            if block >= warmup_blocks:
                measured[workers].append(cohort)

    aggregates = {workers: _aggregate(reports) for workers, reports in measured.items()}
    eligible_workers, evaluation = _evaluate_admission(
        aggregates,
        rss_budget_bytes=rss_budget_bytes,
    )
    source_sha = _source_manifest_sha256(source)
    local_manifest = AdmissionManifest.local_diagnostic(
        source_manifest_sha256=source_sha,
        fixture_sha256=fixture_sha256,
        eligible_workers=eligible_workers,
    )
    governor = PerformanceGovernor(
        local_manifest,
        requested_workers=4,
        explicit_four_workers=True,
        rss_budget_bytes=rss_budget_bytes,
        available_memory_bytes=min(
            int(aggregates[workers]["available_memory_bytes_min"]) for workers in SUPPORTED_WORKERS
        ),
    )
    failed = [
        f"workers_{workers}:{name}"
        for workers, gate_set in evaluation["gates"].items()  # type: ignore[union-attr]
        for name, passed in gate_set.items()
        if not passed
    ]
    local_gate_status = "QUALIFIED" if eligible_workers == 4 else "NOT_QUALIFIED"
    reasons = [*failed, "local_fixture_only_no_production_authority"]
    report: dict[str, object] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "label": "local_full_stack_no_submit_not_live_provider_throughput",
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": {
            "platform": platform.platform(),
            "python": sys.version,
            "logical_cpu_count": os.cpu_count(),
        },
        "fixture_sha256": fixture_sha256,
        "fixture_task_count": len(tasks),
        "source_manifest_sha256": source_sha,
        "thresholds": dict(DEFAULT_THRESHOLDS),
        "rss_budget_bytes": rss_budget_bytes,
        "warmup_blocks": warmup_blocks,
        "measured_blocks": measured_blocks,
        "cohorts": {str(key): value for key, value in aggregates.items()},
        **evaluation,
        "admission": {
            "status": "NOT_ADMITTED",
            "local_gate_status": local_gate_status,
            "eligible_workers": eligible_workers,
            "production_admitted_workers": 1,
            "production_authority": False,
            "reasons": reasons,
        },
        "local_diagnostic_manifest": local_manifest.as_dict(),
        "governor": asdict(governor.decision()),
        "claim_boundary": (
            "local deterministic fixtures and Chromium only; no live provider, production "
            "profile, Submit, SubmissionGate, reservation, receipt, or production-cap authority"
        ),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


__all__ = [
    "FIXTURE_SCHEMA_VERSION",
    "SUPPORTED_WORKERS",
    "CohortMetricCollector",
    "CohortUnavailable",
    "load_fixture",
    "run_full_stack_no_submit_benchmark",
]
