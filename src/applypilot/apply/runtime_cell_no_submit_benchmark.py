"""Paired local 1-Cell versus 2-Cell benchmark with no Submit authority."""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import statistics
import tempfile
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from applypilot.apply.runtime_cell_coordinator import (
    RuntimeCellAdmissionDecision,
    RuntimeCellCoordinator,
    source_manifest_identity,
)
from applypilot.storage import runtime_cells as storage

FIXTURE_SCHEMA = "applypilot-runtime-cell-no-submit-fixture/v1"
REPORT_SCHEMA = "applypilot-runtime-cell-no-submit/v1"
SPEEDUP_LOWER_BOUND_MIN = 1.6


def load_fixture(path: Path | str) -> tuple[dict[str, Any], str]:
    fixture_path = Path(path).expanduser().resolve()
    raw_bytes = fixture_path.read_bytes()
    value = json.loads(raw_bytes)
    if not isinstance(value, dict) or value.get("schema_version") != FIXTURE_SCHEMA:
        raise ValueError("unsupported Runtime Cell no-submit fixture")
    tasks = value.get("tasks")
    if not isinstance(tasks, list) or len(tasks) < 6:
        raise ValueError("Runtime Cell fixture requires at least six tasks")
    ids: set[str] = set()
    domains: set[str] = set()
    for task in tasks:
        if not isinstance(task, Mapping):
            raise TypeError("fixture task must be an object")
        task_id = str(task.get("task_id") or "")
        hostname = storage.normalize_hostname(task.get("hostname"))
        work_ms = task.get("work_ms")
        if not task_id or task_id in ids:
            raise ValueError("fixture task ids must be non-empty and unique")
        if isinstance(work_ms, bool) or not isinstance(work_ms, int) or work_ms < 5:
            raise ValueError("fixture work_ms must be an integer of at least 5")
        ids.add(task_id)
        domains.add(hostname)
    if len(domains) < 2 or len(domains) == len(tasks):
        raise ValueError("fixture must contain repeated same-domain and different-domain tasks")
    return value, hashlib.sha256(raw_bytes).hexdigest()


def _connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=10, check_same_thread=False)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def _run_cohort(
    tasks: tuple[Mapping[str, object], ...],
    *,
    cells: int,
    source_identity: str,
    database_path: Path,
) -> dict[str, object]:
    decision = RuntimeCellAdmissionDecision(
        mode="canary" if cells == 2 else "off",
        requested_workers=cells,
        effective_cells=cells,
        status="ADMITTED" if cells == 2 else "NOT_ADMITTED",
        reasons=() if cells == 2 else ("benchmark_baseline",),
        source_identity=source_identity,
        production_authority=False,
    )
    coordinator = RuntimeCellCoordinator(lambda: _connection(database_path), decision=decision)
    bindings = []
    for index in range(cells):
        bindings.append(
            coordinator.register(
                cell_index=index,
                generation=1,
                runtime_id=f"benchmark-runtime-{cells}-{index}",
                process_id=10_000 + cells * 10 + index,
                process_birth_time=20_000 + cells * 10 + index,
            )
        )

    pending = [dict(task) for task in tasks]
    pending_lock = threading.Lock()
    metrics_lock = threading.Lock()
    active_total = 0
    overall_peak = 0
    domain_active: dict[str, int] = {}
    domain_peak: dict[str, int] = {}
    completed: list[str] = []
    cell_cross_writes = 0
    errors: list[str] = []

    def worker(index: int) -> None:
        nonlocal active_total, overall_peak, cell_cross_writes
        binding = bindings[index]
        connection = _connection(database_path)
        try:
            while True:
                selected: dict[str, object] | None = None
                token = None
                with pending_lock:
                    if not pending:
                        return
                    for candidate in tuple(pending):
                        task_id = str(candidate["task_id"])
                        hostname = str(candidate["hostname"])
                        attempt_id = f"attempt-{cells}-{task_id}"
                        try:
                            token = coordinator.claim(
                                binding,
                                application_id=f"application-{cells}-{task_id}",
                                actor_id=f"application:{attempt_id}",
                                attempt_id=attempt_id,
                                application_url=f"https://{hostname}/apply/{task_id}",
                                ttl_seconds=30,
                                connection=connection,
                            )
                        except storage.RuntimeCellConflictError:
                            continue
                        pending.remove(candidate)
                        selected = candidate
                        break
                if selected is None or token is None:
                    time.sleep(0.001)
                    continue
                hostname = str(selected["hostname"])
                with metrics_lock:
                    active_total += 1
                    overall_peak = max(overall_peak, active_total)
                    domain_active[hostname] = domain_active.get(hostname, 0) + 1
                    domain_peak[hostname] = max(domain_peak.get(hostname, 0), domain_active[hostname])
                    if token.cell_id != binding.cell_id or token.runtime_id != binding.runtime_id:
                        cell_cross_writes += 1
                time.sleep(int(selected["work_ms"]) / 1000.0)
                storage.begin_drain(connection, token, reason="benchmark_task_complete")
                storage.release_after_cleanup(
                    connection,
                    token,
                    agent_stopped=True,
                    context_cleanup_verified=True,
                    residual_resources=0,
                )
                with metrics_lock:
                    active_total -= 1
                    domain_active[hostname] -= 1
                    completed.append(str(selected["task_id"]))
        except BaseException as exc:  # noqa: BLE001 - benchmark records any worker anomaly.
            with metrics_lock:
                errors.append(type(exc).__name__)
        finally:
            connection.close()

    started = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(index,)) for index in range(cells)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.perf_counter() - started
    if errors or len(completed) != len(tasks):
        raise RuntimeError(f"Runtime Cell benchmark worker failure: {errors!r}")
    return {
        "cells": cells,
        "elapsed_seconds": elapsed,
        "throughput_per_second": len(tasks) / elapsed,
        "completed_tasks": len(completed),
        "overall_concurrency_peak": overall_peak,
        "same_domain_peak": max(domain_peak.values(), default=0),
        "domain_peaks": dict(sorted(domain_peak.items())),
        "duplicate_submit_attempts": 0,
        "submit_attempts": 0,
        "effect_attempts": 0,
        "submission_gate_attempts": 0,
        "reservation_attempts": 0,
        "receipt_attempts": 0,
        "cell_cross_writes": cell_cross_writes,
    }


def paired_bootstrap_lower_bound(speedups: list[float], *, samples: int = 5000, seed: int = 7) -> float:
    if len(speedups) < 2:
        raise ValueError("paired bootstrap requires at least two speedups")
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choice(speedups) for _ in speedups) for _ in range(samples))
    return means[max(0, int(0.025 * len(means)) - 1)]


def run_runtime_cell_no_submit_benchmark(
    fixture_path: Path | str,
    *,
    output_path: Path | str,
    source_root: Path | str,
    measured_blocks: int = 10,
    warmup_blocks: int = 2,
) -> dict[str, object]:
    """Run paired blocks and exclusively create one auditable local report."""

    if measured_blocks < 2 or warmup_blocks < 0:
        raise ValueError("benchmark requires at least two measured blocks")
    fixture, fixture_sha = load_fixture(fixture_path)
    tasks = tuple(fixture["tasks"])
    source_identity = source_manifest_identity(source_root)
    blocks: list[dict[str, object]] = []
    total = measured_blocks + warmup_blocks
    with tempfile.TemporaryDirectory(prefix="applypilot-runtime-cell-benchmark-") as temp:
        temp_root = Path(temp)
        for block in range(total):
            cohorts: dict[int, dict[str, object]] = {}
            order = (1, 2) if block % 2 == 0 else (2, 1)
            for cells in order:
                cohorts[cells] = _run_cohort(
                    tasks,
                    cells=cells,
                    source_identity=source_identity,
                    database_path=temp_root / f"block-{block}-cells-{cells}.sqlite3",
                )
            if block >= warmup_blocks:
                one = float(cohorts[1]["elapsed_seconds"])
                two = float(cohorts[2]["elapsed_seconds"])
                blocks.append(
                    {
                        "block": block - warmup_blocks,
                        "order": list(order),
                        "one_cell": cohorts[1],
                        "two_cells": cohorts[2],
                        "paired_speedup": one / two,
                    }
                )
    speedups = [float(block["paired_speedup"]) for block in blocks]
    lower_bound = paired_bootstrap_lower_bound(speedups, seed=int(fixture_sha[:8], 16))
    safety_fields = (
        "duplicate_submit_attempts",
        "submit_attempts",
        "effect_attempts",
        "submission_gate_attempts",
        "reservation_attempts",
        "receipt_attempts",
        "cell_cross_writes",
    )
    safety_zero = all(
        int(block[lane][field]) == 0
        for block in blocks
        for lane in ("one_cell", "two_cells")
        for field in safety_fields
    )
    same_domain_serial = all(
        int(block[lane]["same_domain_peak"]) == 1 for block in blocks for lane in ("one_cell", "two_cells")
    )
    different_domain_parallel = all(int(block["two_cells"]["overall_concurrency_peak"]) == 2 for block in blocks)
    admitted = (
        lower_bound >= SPEEDUP_LOWER_BOUND_MIN and safety_zero and same_domain_serial and different_domain_parallel
    )
    reasons: list[str] = []
    if lower_bound < SPEEDUP_LOWER_BOUND_MIN:
        reasons.append("paired_bootstrap_95_lower_bound_below_1_6x")
    if not safety_zero:
        reasons.append("no_submit_safety_counter_nonzero")
    if not same_domain_serial:
        reasons.append("same_domain_concurrency_exceeded_one")
    if not different_domain_parallel:
        reasons.append("different_domains_did_not_reach_two_cells")
    reasons.append("local_diagnostic_has_no_production_authority")
    report: dict[str, object] = {
        "schema_version": REPORT_SCHEMA,
        "label": "runtime_cell_no_submit_local_fixture",
        "generated_at": datetime.now(UTC).isoformat(),
        "fixture_sha256": fixture_sha,
        "source_identity": source_identity,
        "measured_blocks": measured_blocks,
        "warmup_blocks": warmup_blocks,
        "paired_blocks": blocks,
        "paired_speedup_mean": statistics.fmean(speedups),
        "paired_bootstrap_95_lower_bound": lower_bound,
        "threshold": {"paired_bootstrap_95_lower_bound_min": SPEEDUP_LOWER_BOUND_MIN},
        "gates": {
            "speedup": lower_bound >= SPEEDUP_LOWER_BOUND_MIN,
            "same_domain_serial": same_domain_serial,
            "different_domain_parallel": different_domain_parallel,
            "safety_counters_zero": safety_zero,
        },
        "admission": {
            "status": "ADMITTED" if admitted else "NOT_ADMITTED",
            "production_authority": False,
            "effective_production_cells": 1,
            "canary_enabled": False,
            "reasons": reasons,
        },
        "authority_boundary": (
            "fixed local fixture only; no Submit, effect, SubmissionGate, reservation, "
            "receipt, live ATS, or production authority"
        ),
    }
    report["report_sha256"] = hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    ).hexdigest()
    destination = Path(output_path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return report


__all__ = [
    "FIXTURE_SCHEMA",
    "REPORT_SCHEMA",
    "SPEEDUP_LOWER_BOUND_MIN",
    "load_fixture",
    "paired_bootstrap_lower_bound",
    "run_runtime_cell_no_submit_benchmark",
]
