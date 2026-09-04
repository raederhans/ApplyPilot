"""Current-source-bound local A/B/C Runtime Cell browser diagnostic.

This module has no production job, Submit, reservation, mailbox, receipt, or
ledger imports.  Every navigation is fulfilled by a Playwright route handler.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from applypilot.apply.browser_context_runtime import (
    BrowserContextFeature,
    BrowserStateScope,
    HotBrowserContextRuntime,
    ScopedBrowserState,
)
from applypilot.apply.runtime_cell_coordinator import (
    APP_SERVER_PRODUCTION_CELL_ADMITTED,
    DiagnosticRuntimeCellCoordinator,
    RuntimeCellHost,
    resolve_runtime_cell_admission,
    source_manifest_identity,
)
from applypilot.apply.session_aging_experiment import (
    _HTML,
    _PROVIDER_DOMAINS,
    fixture_expected_cohort,
    load_fixture,
)
from applypilot.storage import runtime_cells

REPORT_SCHEMA_VERSION = "applypilot-runtime-cell-browser-diagnostic/v1"
_SAFETY_COUNTERS = (
    "submit_attempts",
    "submission_gate_attempts",
    "reservation_attempts",
    "receipt_attempts",
    "mailbox_attempts",
    "external_effect_attempts",
)


class _FixtureBrowser:
    """Install the local-only route in every Context created by a Cell host."""

    def __init__(self, browser: Any) -> None:
        self.browser = browser

    def new_context(self, **kwargs: object) -> Any:
        context = self.browser.new_context(service_workers="block", **kwargs)
        context.route(
            "**/*",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body=_HTML,
            ),
        )
        return context

    def close(self) -> None:
        self.browser.close()


def _assert_workspace(root: Path, source_root: Path) -> None:
    if not (source_root / "pyproject.toml").is_file():
        raise ValueError("source_root must be the ApplyPilot repository root")
    if root.exists():
        raise FileExistsError("diagnostic workspace already exists")
    try:
        root.relative_to(source_root)
    except ValueError:
        pass
    else:
        raise ValueError("diagnostic workspace must remain outside the source tree")


def _connection_factory(path: Path):
    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(path, timeout=30, check_same_thread=False)
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.row_factory = sqlite3.Row
        return connection

    return connect


def _observe_page(page: Any, task: Mapping[str, object]) -> dict[str, object]:
    provider = str(task["provider"])
    domain = _PROVIDER_DOMAINS[provider]
    started = time.perf_counter()
    page.goto(
        f"https://{domain}/local-runtime-cell/{task['task_id']}",
        wait_until="load",
    )
    if page.locator("#application").count() != 1:
        raise AssertionError("local fixture application form is missing")
    if page.locator("#submit").count() != 1:
        raise AssertionError("local fixture Submit sentinel is missing")
    submit_attempts = int(page.evaluate("window.__submissionAttempts"))
    if submit_attempts != 0:
        raise AssertionError("no-submit fixture recorded a submission attempt")
    return {
        "task_id": str(task["task_id"]),
        "provider": provider,
        "domain": domain,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "submit_attempts": submit_attempts,
    }


def _scope(task: Mapping[str, object]) -> tuple[BrowserStateScope, ScopedBrowserState]:
    provider = str(task["provider"])
    scope = BrowserStateScope(
        provider=provider,
        host=_PROVIDER_DOMAINS[provider],
        account_id="local-diagnostic",
    )
    return scope, ScopedBrowserState(scope, {"cookies": [], "origins": []})


def _summary(samples: Sequence[Mapping[str, object]], wall_ms: float) -> dict[str, object]:
    values = [float(sample["elapsed_ms"]) for sample in samples]
    return {
        "sample_count": len(samples),
        "wall_clock_ms": round(wall_ms, 3),
        "observation_ms": {
            "median": round(statistics.median(values), 3),
            "max": round(max(values), 3),
        },
        "samples": list(samples),
    }


def _run_existing_single_worker(playwright: Any, tasks: Sequence[Mapping[str, object]]) -> dict[str, object]:
    started = time.perf_counter()
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(service_workers="block")
    context.route(
        "**/*",
        lambda route: route.fulfill(status=200, content_type="text/html", body=_HTML),
    )
    samples: list[dict[str, object]] = []
    for task in tasks:
        page = context.new_page()
        try:
            samples.append(_observe_page(page, task))
        finally:
            page.close()
    context.close()
    residual_pages = len(context.pages)
    browser.close()
    payload = _summary(samples, (time.perf_counter() - started) * 1000)
    payload.update(
        {
            "label": "A",
            "lifecycle": "existing_single_worker_shared_context",
            "effective_cells": 0,
            "context_cleanup": {
                "contexts_created": 1,
                "contexts_closed": 1,
                "active_contexts": 0,
                "pages_after_close": residual_pages,
                "verified": residual_pages == 0,
            },
        }
    )
    return payload


def _run_cell_worker(
    *,
    coordinator: DiagnosticRuntimeCellCoordinator,
    connection_factory: Any,
    cell_index: int,
    tasks: Sequence[Mapping[str, object]],
    process_identity: tuple[int, int],
    binding: Any = None,
) -> dict[str, object]:
    from playwright.sync_api import sync_playwright

    started = time.perf_counter()
    playwright = sync_playwright().start()
    if binding is None:
        binding = coordinator.register_next(
            cell_index=cell_index,
            runtime_id=f"diagnostic-cell-{cell_index}-{time.time_ns()}",
            process_id=process_identity[0],
            process_birth_time=process_identity[1],
        )
    native_browser = playwright.chromium.launch(headless=True)
    context_runtime = HotBrowserContextRuntime(
        feature=BrowserContextFeature(True),
        launch_browser=lambda: _FixtureBrowser(native_browser),
    )
    host = RuntimeCellHost(
        coordinator=coordinator,
        binding=binding,
        context_runtime=context_runtime,
        connection_factory=connection_factory,
    )
    samples: list[dict[str, object]] = []
    try:
        for task in tasks:
            attempt_id = f"diagnostic-{cell_index}-{task['task_id']}"
            scope, state = _scope(task)
            application = host.open_application(
                application_id=f"application-{attempt_id}",
                actor_id=f"application:{attempt_id}",
                attempt_id=attempt_id,
                application_url=f"https://{scope.host}/local-runtime-cell/{task['task_id']}",
                scope=scope,
                state=state,
                agent_stop=lambda: None,
                contain_runtime=lambda: None,
            )
            try:
                page = context_runtime.new_page(application.context_lease)
                samples.append(_observe_page(page, task))
            finally:
                host.close_application(application)
        metrics = context_runtime.metrics
    finally:
        try:
            host.close()
        finally:
            playwright.stop()
    payload = _summary(samples, (time.perf_counter() - started) * 1000)
    payload.update(
        {
            "cell_id": binding.cell_id,
            "generation": binding.generation,
            "context_cleanup": {
                "contexts_created": metrics.contexts_created,
                "contexts_closed": metrics.contexts_closed,
                "active_contexts": metrics.active_contexts,
                "pages_after_close": metrics.pages_after_close,
                "frames_after_close": metrics.frames_after_close,
                "service_workers_after_close": metrics.service_workers_after_close,
                "verified": (
                    metrics.contexts_created == metrics.contexts_closed
                    and metrics.active_contexts == 0
                    and metrics.pages_after_close == 0
                    and metrics.frames_after_close == 0
                    and metrics.service_workers_after_close == 0
                ),
            },
        }
    )
    return payload


def run_runtime_cell_browser_diagnostic(
    fixture_path: Path | str,
    *,
    workspace_root: Path | str,
    source_root: Path | str,
) -> dict[str, object]:
    """Run A existing worker, B one Cell, and C two diagnostic-only Cells."""

    root = Path(workspace_root).expanduser().resolve()
    source = Path(source_root).expanduser().resolve()
    _assert_workspace(root, source)
    fixture, fixture_sha256 = load_fixture(fixture_path)
    tasks = tuple(fixture_expected_cohort(fixture))
    root.mkdir(parents=True)
    db_path = root / "runtime-cells.sqlite3"
    connect = _connection_factory(db_path)
    connection = connect()
    runtime_cells.ensure_schema(connection)
    connection.close()
    source_identity = source_manifest_identity(source)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        mode_a = _run_existing_single_worker(playwright, tasks)

    one_cell = DiagnosticRuntimeCellCoordinator(
        connect,
        source_identity=source_identity,
        cells=1,
    )
    mode_b = _run_cell_worker(
        coordinator=one_cell,
        connection_factory=connect,
        cell_index=0,
        tasks=tasks,
        process_identity=(900001, 1000001),
    )
    mode_b.update({"label": "B", "lifecycle": "one_runtime_cell", "effective_cells": 1})

    two_cell = DiagnosticRuntimeCellCoordinator(
        connect,
        source_identity=source_identity,
        cells=2,
    )
    cell_bindings = [
        two_cell.register_next(
            cell_index=index,
            runtime_id=f"diagnostic-cell-{index}-{time.time_ns()}",
            process_id=900010 + index,
            process_birth_time=1000010 + index,
        )
        for index in range(2)
    ]
    c_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                _run_cell_worker,
                coordinator=two_cell,
                connection_factory=connect,
                cell_index=index,
                tasks=tasks[index::2],
                process_identity=(900010 + index, 1000010 + index),
                binding=cell_bindings[index],
            )
            for index in range(2)
        ]
        cell_results = [future.result() for future in futures]
    c_wall_ms = (time.perf_counter() - c_started) * 1000
    c_samples = [sample for cell in cell_results for sample in cell["samples"]]
    mode_c = _summary(c_samples, c_wall_ms)
    mode_c.update(
        {
            "label": "C",
            "lifecycle": "two_runtime_cells_diagnostic_only",
            "effective_cells": 2,
            "cells": cell_results,
            "context_cleanup": {
                "verified": all(cell["context_cleanup"]["verified"] for cell in cell_results),
                "active_contexts": sum(cell["context_cleanup"]["active_contexts"] for cell in cell_results),
                "pages_after_close": sum(cell["context_cleanup"]["pages_after_close"] for cell in cell_results),
            },
        }
    )

    production_decision = resolve_runtime_cell_admission(
        mode="canary",
        current_source_identity=source_identity,
        requested_workers=2,
        manifest=None,
    )
    all_samples = [*mode_a["samples"], *mode_b["samples"], *mode_c["samples"]]
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "diagnostic_only": True,
        "production_authority": False,
        "submission_authority": False,
        "source_identity": source_identity,
        "source_root": str(source),
        "fixture_sha256": fixture_sha256,
        "production_admission_constant": APP_SERVER_PRODUCTION_CELL_ADMITTED,
        "production_effective_cells": production_decision.effective_cells,
        "process_identity_evidence": "synthetic diagnostic identities; no OS-liveness claim",
        "modes": {"A": mode_a, "B": mode_b, "C": mode_c},
        "safety_counters": {name: sum(int(sample.get(name, 0)) for sample in all_samples) for name in _SAFETY_COUNTERS},
        "context_cleanup_verified": all(mode["context_cleanup"]["verified"] for mode in (mode_a, mode_b, mode_c)),
        "claim_boundary": (
            "local Playwright route fixtures only; no live ATS, production profile, "
            "Submit, SubmissionGate, reservation, receipt, mailbox, ledger, or external effect"
        ),
    }
    report_path = root / "report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report


__all__ = ["REPORT_SCHEMA_VERSION", "run_runtime_cell_browser_diagnostic"]
