"""Repeatable local Chromium session-aging experiment with no submit authority.

The harness is deliberately separate from production application orchestration.
It only serves a local fixture through Playwright interception and records which
browser/context lifetime was used for each synthetic application.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

FIXTURE_SCHEMA_VERSION = "applypilot-session-aging-fixture/v1"
REPORT_SCHEMA_VERSION = "applypilot-session-aging-report/v1"
MODES = ("shared_context", "fresh_context", "fresh_process")
_PROVIDER_DOMAINS = {
    "workday": "tenant.myworkdayjobs.com",
    "smartrecruiters": "jobs.smartrecruiters.com",
}
_UNAVAILABLE_GROUPS = {
    "agent": ["agent.turn", "agent.startup", "model.first_output"],
    "mcp": ["mcp.initialize", "mcp.first_tool_request", "mcp.tool_execution"],
    "recovery": ["recovery.agent", "recovery.retry"],
}
_HTML = """<!doctype html><html><body>
<form id="application"><input id="name" required><button id="next" type="button">Next</button>
<button id="submit" type="submit">Submit</button><output id="status">fixture</output></form>
<script>
window.__submissionAttempts = 0;
document.querySelector('#application').addEventListener('submit', event => {
  event.preventDefault(); window.__submissionAttempts += 1;
  throw new Error('session-aging no-submit harness forbids form submission');
});
</script></body></html>"""


def experiment_definition() -> dict[str, object]:
    """Return the immutable A/B/C lifetime definitions used in every report."""

    return {
        "shared_context": {
            "label": "A",
            "browser_lifetime": "one hot Chromium browser for the cohort",
            "context_lifetime": "one shared BrowserContext for the cohort",
            "page_lifetime": "one fresh page per synthetic application",
        },
        "fresh_context": {
            "label": "B",
            "browser_lifetime": "one hot Chromium browser for the cohort",
            "context_lifetime": "one fresh BrowserContext per synthetic application",
            "page_lifetime": "one page in each fresh context",
        },
        "fresh_process": {
            "label": "C",
            "browser_lifetime": "one newly launched Chromium process per synthetic application",
            "context_lifetime": "one fresh BrowserContext in each process",
            "page_lifetime": "one page in each fresh context",
        },
    }


def counterbalanced_mode_order(application_index: int) -> tuple[str, str, str]:
    """Rotate A/B/C per paired application so each mode occupies every position."""

    if isinstance(application_index, bool) or not isinstance(application_index, int) or application_index < 1:
        raise ValueError("application index must be a positive integer")
    rotation = (application_index - 1) % len(MODES)
    return MODES[rotation:] + MODES[:rotation]


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[min(rank - 1, len(ordered) - 1)]


def load_fixture(path: Path | str) -> tuple[dict[str, Any], str]:
    """Load a small provider-balanced fixture without accepting live URLs."""

    fixture_path = Path(path).expanduser().resolve()
    raw_bytes = fixture_path.read_bytes()
    raw = json.loads(raw_bytes)
    if not isinstance(raw, dict) or raw.get("schema_version") != FIXTURE_SCHEMA_VERSION:
        raise ValueError("unsupported session-aging fixture")
    tasks = raw.get("tasks")
    if not isinstance(tasks, list) or len(tasks) < 2:
        raise ValueError("session-aging fixture requires at least two tasks")
    task_ids: list[str] = []
    providers: set[str] = set()
    pairs: dict[str, set[str]] = {}
    for task in tasks:
        if not isinstance(task, Mapping):
            raise TypeError("session-aging task must be an object")
        task_id = task.get("task_id")
        provider = task.get("provider")
        if not isinstance(task_id, str) or not task_id or not task_id.isascii() or not task_id.replace("-", "").isalnum():
            raise ValueError("session-aging task_id must be non-empty ASCII alphanumeric text")
        if provider not in _PROVIDER_DOMAINS:
            raise ValueError("session-aging provider is unsupported")
        pair_id = task.get("pair_id")
        if not isinstance(pair_id, str) or not pair_id or not pair_id.isascii() or not pair_id.replace("-", "").isalnum():
            raise ValueError("session-aging pair_id must be non-empty ASCII alphanumeric text")
        task_ids.append(task_id)
        providers.add(str(provider))
        pairs.setdefault(pair_id, set()).add(str(provider))
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("session-aging task ids must be unique")
    if providers != set(_PROVIDER_DOMAINS):
        raise ValueError("session-aging fixture must include both supported providers")
    if len(pairs) < 3 or any(pair_providers != set(_PROVIDER_DOMAINS) for pair_providers in pairs.values()):
        raise ValueError("session-aging fixture requires at least three Workday/SmartRecruiters pairs")
    return raw, hashlib.sha256(raw_bytes).hexdigest()


def fixture_expected_cohort(fixture: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    """Derive the only admissible indexed cohort from an already validated fixture."""

    tasks = fixture.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("verified session-aging fixture requires tasks")
    cohort: list[dict[str, object]] = []
    for index, task in enumerate(tasks, start=1):
        if not isinstance(task, Mapping):
            raise TypeError("verified session-aging fixture task must be an object")
        provider = task.get("provider")
        task_id = task.get("task_id")
        if provider not in _PROVIDER_DOMAINS or not isinstance(task_id, str) or not task_id:
            raise ValueError("verified session-aging fixture task is invalid")
        cohort.append(
            {
                "application_index": index,
                "task_id": task_id,
                "provider": provider,
                "domain": _PROVIDER_DOMAINS[provider],
            }
        )
    return tuple(cohort)


def _contains(parent: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(parent)
    except ValueError:
        return False
    return True


def _assert_isolated_root(*, workspace_root: Path, source_root: Path) -> None:
    if not (source_root / "pyproject.toml").is_file():
        raise ValueError("source_root must be the ApplyPilot repository root")
    if workspace_root.exists():
        raise FileExistsError("session-aging workspace already exists")
    if _contains(source_root, workspace_root):
        raise ValueError("session-aging workspace must remain outside the source tree")
    configured_root = os.environ.get("APPLYPILOT_DIR")
    if configured_root:
        live_root = Path(configured_root).expanduser().resolve()
        if _contains(live_root, workspace_root) or _contains(workspace_root, live_root):
            raise ValueError("session-aging workspace must not alias APPLYPILOT_DIR")


def _coverage() -> dict[str, object]:
    groups = {
        **{
            name: {"status": "unavailable", "coverage_ratio": 0.0, "unavailable_spans": spans}
            for name, spans in _UNAVAILABLE_GROUPS.items()
        },
        "observation": {
            "status": "observed",
            "coverage_ratio": 1.0,
            "observed_spans": ["observation.fixture_dom_probe"],
        },
    }
    return {
        "groups": groups,
        "observed_group_count": 1,
        "total_group_count": len(groups),
        "coverage_ratio": round(1 / len(groups), 6),
        "attribution_complete": False,
    }


def compile_report(
    *,
    fixture_sha256: str,
    expected_cohort: Sequence[Mapping[str, object]],
    samples: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Compile a deterministic report from measured synthetic samples.

    Missing Agent/MCP/recovery spans remain unavailable.  This is intentional:
    this browser-only diagnostic cannot observe or fabricate those runtimes.
    """

    expected_matrix = {
        (
            item.get("application_index"),
            item.get("task_id"),
            item.get("provider"),
            item.get("domain"),
        )
        for item in expected_cohort
    }
    expected_indices = {item[0] for item in expected_matrix}
    if (
        not expected_matrix
        or len(expected_matrix) != len(expected_cohort)
        or expected_indices != set(range(1, len(expected_cohort) + 1))
        or any(
            not isinstance(index, int)
            or isinstance(index, bool)
            or not isinstance(task_id, str)
            or not isinstance(provider, str)
            or provider not in _PROVIDER_DOMAINS
            or domain != _PROVIDER_DOMAINS[provider]
            for index, task_id, provider, domain in expected_matrix
        )
    ):
        raise ValueError("expected cohort must be a complete contiguous verified fixture matrix")
    by_mode: dict[str, list[dict[str, object]]] = {mode: [] for mode in MODES}
    for sample in samples:
        mode = sample.get("mode")
        if mode not in by_mode:
            raise ValueError("sample mode is unsupported")
        dimensions = sample.get("dimensions")
        if not isinstance(dimensions, Mapping):
            raise TypeError("sample dimensions are required")
        provider = dimensions.get("provider")
        domain = dimensions.get("domain")
        index = dimensions.get("application_index")
        task_id = dimensions.get("task_id")
        if (
            provider not in _PROVIDER_DOMAINS
            or domain != _PROVIDER_DOMAINS[provider]
            or isinstance(index, bool)
            or not isinstance(index, int)
            or index < 1
        ):
            raise ValueError("sample provider/domain/application index is invalid")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("sample task_id is required")
        observation_ms = sample.get("observation_ms")
        if isinstance(observation_ms, bool) or not isinstance(observation_ms, (int, float)) or observation_ms < 0:
            raise ValueError("sample observation duration is invalid")
        if sample.get("submit_attempts") != 0:
            raise ValueError("session-aging samples must record zero submit attempts")
        end_to_end_ms = sample.get("end_to_end_ms")
        if isinstance(end_to_end_ms, bool) or not isinstance(end_to_end_ms, (int, float)) or end_to_end_ms < 0:
            raise ValueError("sample end-to-end duration is invalid")
        lifecycle = sample.get("lifecycle_spans_ms")
        if not isinstance(lifecycle, Mapping):
            raise TypeError("sample lifecycle spans are required")
        lifecycle_total = 0.0
        for name, value in lifecycle.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"sample lifecycle span is invalid: {name}")
            lifecycle_total += float(value)
        if lifecycle_total + float(observation_ms) > float(end_to_end_ms) + 0.01:
            raise ValueError("sample lifecycle and observation spans exceed end-to-end duration")
        browser_instance = sample.get("browser_instance")
        context_instance = sample.get("context_instance")
        if (
            isinstance(browser_instance, bool)
            or not isinstance(browser_instance, int)
            or browser_instance < 1
            or isinstance(context_instance, bool)
            or not isinstance(context_instance, int)
            or context_instance < 1
        ):
            raise ValueError("sample browser/context instances are invalid")
        execution_position = sample.get("execution_position")
        if execution_position != counterbalanced_mode_order(index).index(str(mode)) + 1:
            raise ValueError("sample does not match counterbalanced execution order")
        by_mode[str(mode)].append(dict(sample))
    for mode, mode_samples in by_mode.items():
        if not mode_samples:
            raise ValueError("every session-aging mode requires a complete cohort")
        matrix = {
            (
                int(sample["dimensions"]["application_index"]),  # type: ignore[index]
                str(sample["dimensions"]["task_id"]),  # type: ignore[index]
                str(sample["dimensions"]["provider"]),  # type: ignore[index]
                str(sample["dimensions"]["domain"]),  # type: ignore[index]
            )
            for sample in mode_samples
        }
        if len(matrix) != len(mode_samples):
            raise ValueError("every mode must contain each cohort sample exactly once")
        if matrix != expected_matrix:
            raise ValueError("session-aging mode cohort must match the verified fixture exactly")
        pairs = {(int(sample["browser_instance"]), int(sample["context_instance"])) for sample in mode_samples}
        if mode == "shared_context" and len(pairs) > 1:
            raise ValueError("shared_context must use one browser and one context")
        if mode == "fresh_context" and any(browser != 1 for browser, _context in pairs):
            raise ValueError("fresh_context must keep one hot browser")
        if mode == "fresh_context" and len({context for _browser, context in pairs}) != len(mode_samples):
            raise ValueError("fresh_context must create one context per application")
        if mode == "fresh_process" and any(context != 1 for _browser, context in pairs):
            raise ValueError("fresh_process must create one context in each process")
        if mode == "fresh_process" and len({browser for browser, _context in pairs}) != len(mode_samples):
            raise ValueError("fresh_process must launch one process per application")
    execution_schedule = [
        {
            "application_index": index,
            "task_id": task_id,
            "provider": provider,
            "domain": domain,
            "mode_order": list(counterbalanced_mode_order(index)),
        }
        for index, task_id, provider, domain in sorted(expected_matrix)
    ]
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "diagnostic_only": True,
        "production_authority": False,
        "submission_authority": False,
        "fixture_sha256": fixture_sha256,
        "definitions": experiment_definition(),
        "attribution_coverage": _coverage(),
        "execution_schedule": execution_schedule,
        "modes": {
            mode: {
                "sample_count": len(mode_samples),
                "samples": sorted(mode_samples, key=lambda sample: int(sample["dimensions"]["application_index"])),  # type: ignore[index]
                "coverage": _coverage(),
                "end_to_end_ms": {
                    "p50": round(_percentile([float(sample["end_to_end_ms"]) for sample in mode_samples], 0.50), 3),
                    "p95": round(_percentile([float(sample["end_to_end_ms"]) for sample in mode_samples], 0.95), 3),
                },
            }
            for mode, mode_samples in by_mode.items()
        },
        "claim_boundary": (
            "local fixture interception only; no live ATS, Submit, SubmissionGate, reservation, receipt, "
            "mailbox, production profile, or production authority"
        ),
    }


def _prepare_workspace(root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=False)
    (root / "profiles").mkdir()
    logs = root / "logs"
    logs.mkdir()
    (root / "ports.json").write_text(
        json.dumps({"debugging_port": "not_used", "reason": "no remote browser endpoint is allowed"}),
        encoding="utf-8",
    )
    db_path = root / "data" / "session-aging.sqlite3"
    db_path.parent.mkdir()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE sample (mode TEXT, application_index INTEGER, provider TEXT, domain TEXT, observation_ms REAL)"
        )
    return db_path, logs / "events.jsonl"


def end_to_end_attribution(
    *,
    started_at: float,
    lifecycle_spans_ms: Mapping[str, float],
    observation_ms: float,
    clock: Any = time.perf_counter,
) -> dict[str, object]:
    """Close one application span after lifecycle and observation have both completed."""

    end_to_end_ms = (float(clock()) - float(started_at)) * 1000
    accounted_ms = sum(float(value) for value in lifecycle_spans_ms.values()) + float(observation_ms)
    if end_to_end_ms < 0 or accounted_ms > end_to_end_ms + 0.01:
        raise ValueError("lifecycle and observation spans must fit within end-to-end time")
    return {
        "end_to_end_ms": round(end_to_end_ms, 3),
        "lifecycle_spans_ms": {name: round(value, 3) for name, value in sorted(lifecycle_spans_ms.items())},
        "observation_ms": round(observation_ms, 3),
    }


def _measure_application(*, context: object, task: Mapping[str, object]) -> tuple[float, int, dict[str, float]]:
    provider = str(task["provider"])
    domain = _PROVIDER_DOMAINS[provider]
    started = time.perf_counter()
    page = context.new_page()  # type: ignore[attr-defined]
    try:
        page_create_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        page.goto(f"https://{domain}/local-fixture/{task['task_id']}", wait_until="load")
        navigation_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        form_count = page.locator("#application").count()
        submit_count = page.locator("#submit").count()
        submit_attempts = page.evaluate("window.__submissionAttempts")
        observation_ms = (time.perf_counter() - started) * 1000
        if form_count != 1 or submit_count != 1 or submit_attempts != 0:
            raise AssertionError("no-submit fixture invariant failed")
    finally:
        started = time.perf_counter()
        page.close()
        page_close_ms = (time.perf_counter() - started) * 1000
    return observation_ms, int(submit_attempts), {
        "page_create_ms": page_create_ms,
        "page_navigation_ms": navigation_ms,
        "page_close_ms": page_close_ms,
    }


def _new_context(browser: object) -> tuple[object, float]:
    started = time.perf_counter()
    context = browser.new_context(service_workers="block")  # type: ignore[attr-defined]
    _install_local_fixture_route(context)
    return context, (time.perf_counter() - started) * 1000


def _install_local_fixture_route(context: object) -> float:
    started = time.perf_counter()
    context.route(  # type: ignore[attr-defined]
        "**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=_HTML)
    )
    return (time.perf_counter() - started) * 1000


def run_session_aging_experiment(
    fixture_path: Path | str,
    *,
    workspace_root: Path | str,
    source_root: Path | str,
) -> dict[str, object]:
    """Run A/B/C against local intercepted pages and persist one report in ``workspace_root``."""

    fixture_file = Path(fixture_path).expanduser().resolve()
    root = Path(workspace_root).expanduser().resolve()
    source = Path(source_root).expanduser().resolve()
    _assert_isolated_root(workspace_root=root, source_root=source)
    fixture, fixture_sha256 = load_fixture(fixture_file)
    tasks = tuple(fixture["tasks"])
    db_path, event_log = _prepare_workspace(root)
    from playwright.sync_api import sync_playwright

    samples: list[dict[str, object]] = []
    with sqlite3.connect(db_path) as connection, event_log.open("x", encoding="utf-8") as log:
        started = time.perf_counter()
        playwright = sync_playwright().start()
        run_lifecycle = {"playwright_driver_start_ms": (time.perf_counter() - started) * 1000}
        hot_states: dict[str, dict[str, object]] = {"shared_context": {}, "fresh_context": {}}
        mode_lifecycle: dict[str, dict[str, float]] = {mode: {} for mode in MODES}

        def add_span(target: dict[str, float], name: str, duration_ms: float) -> None:
            target[name] = target.get(name, 0.0) + duration_ms

        def start_browser(*, mode: str, browser_instance: int) -> tuple[dict[str, object], float]:
            profile = root / "profiles" / f"{mode}-{browser_instance}"
            profile.mkdir()
            started = time.perf_counter()
            bootstrap_context = playwright.chromium.launch_persistent_context(str(profile), headless=True)
            launch_ms = (time.perf_counter() - started) * 1000
            browser = bootstrap_context.browser
            if browser is None:
                raise RuntimeError("persistent Chromium context did not expose its browser")
            return {"browser": browser, "bootstrap_context": bootstrap_context, "profile": profile}, launch_ms

        def close_browser(state: Mapping[str, object]) -> float:
            bootstrap_context = state.get("bootstrap_context")
            if bootstrap_context is None:
                return 0.0
            started = time.perf_counter()
            bootstrap_context.close()  # type: ignore[attr-defined]
            return (time.perf_counter() - started) * 1000

        def close_context(context: object) -> float:
            started = time.perf_counter()
            context.close()  # type: ignore[attr-defined]
            return (time.perf_counter() - started) * 1000

        def save_sample(
            *,
            mode: str,
            task: Mapping[str, object],
            index: int,
            execution_position: int,
            browser_instance: int,
            context_instance: int,
            profile: Path,
            lifecycle: Mapping[str, float],
            observation_ms: float,
            submit_attempts: int,
            started_at: float,
        ) -> None:
            provider = str(task["provider"])
            timing = end_to_end_attribution(
                started_at=started_at,
                lifecycle_spans_ms=lifecycle,
                observation_ms=observation_ms,
            )
            sample = {
                "mode": mode,
                "dimensions": {
                    "provider": provider,
                    "domain": _PROVIDER_DOMAINS[provider],
                    "application_index": index,
                    "task_id": str(task["task_id"]),
                },
                "browser_instance": browser_instance,
                "context_instance": context_instance,
                "execution_position": execution_position,
                "profile": str(profile),
                "submit_attempts": submit_attempts,
                **timing,
            }
            samples.append(sample)
            connection.execute(
                "INSERT INTO sample VALUES (?, ?, ?, ?, ?)",
                (mode, index, provider, _PROVIDER_DOMAINS[provider], observation_ms),
            )
            log.write(json.dumps(sample, sort_keys=True) + "\n")

        try:
            for index, task in enumerate(tasks, start=1):
                for execution_position, mode in enumerate(counterbalanced_mode_order(index), start=1):
                    if mode == "fresh_process":
                        started_at = time.perf_counter()
                        state, launch_ms = start_browser(mode=mode, browser_instance=index)
                        context = state["bootstrap_context"]
                        lifecycle = {
                            "browser_process_and_profile_context_create_ms": launch_ms,
                            "route_install_ms": _install_local_fixture_route(context),
                        }
                        observation_ms, submit_attempts, page_lifecycle = _measure_application(context=context, task=task)
                        lifecycle.update(page_lifecycle)
                        lifecycle["browser_process_and_profile_context_close_ms"] = close_browser(state)
                        save_sample(
                            mode=mode,
                            task=task,
                            index=index,
                            execution_position=execution_position,
                            browser_instance=index,
                            context_instance=1,
                            profile=state["profile"],
                            lifecycle=lifecycle,
                            observation_ms=observation_ms,
                            submit_attempts=submit_attempts,
                            started_at=started_at,
                        )
                        continue
                    state = hot_states[mode]
                    if not state:
                        state, launch_ms = start_browser(mode=mode, browser_instance=1)
                        hot_states[mode] = state
                        add_span(mode_lifecycle[mode], "browser_process_and_profile_context_create_ms", launch_ms)
                        if mode == "shared_context":
                            state["context"] = state["bootstrap_context"]
                            add_span(mode_lifecycle[mode], "route_install_ms", _install_local_fixture_route(state["context"]))
                    started_at = time.perf_counter()
                    context_instance = 1
                    lifecycle: dict[str, float] = {}
                    if mode == "fresh_context":
                        context, context_ms = _new_context(state["browser"])
                        lifecycle["context_create_ms"] = context_ms
                        context_instance = index
                    else:
                        context = state["context"]
                    observation_ms, submit_attempts, page_lifecycle = _measure_application(context=context, task=task)
                    lifecycle.update(page_lifecycle)
                    if mode == "fresh_context":
                        lifecycle["context_close_ms"] = close_context(context)
                    save_sample(
                        mode=mode,
                        task=task,
                        index=index,
                        execution_position=execution_position,
                        browser_instance=1,
                        context_instance=context_instance,
                        profile=state["profile"],
                        lifecycle=lifecycle,
                        observation_ms=observation_ms,
                        submit_attempts=submit_attempts,
                        started_at=started_at,
                    )
        finally:
            for mode, state in hot_states.items():
                if state:
                    add_span(
                        mode_lifecycle[mode],
                        "browser_process_and_profile_context_close_ms",
                        close_browser(state),
                    )
            started = time.perf_counter()
            playwright.stop()
            run_lifecycle["playwright_driver_stop_ms"] = (time.perf_counter() - started) * 1000
        connection.commit()
    report = compile_report(
        fixture_sha256=fixture_sha256,
        expected_cohort=fixture_expected_cohort(fixture),
        samples=samples,
    )
    for mode, payload in report["modes"].items():
        lifecycle = mode_lifecycle[mode]
        lifecycle_total = sum(lifecycle.values())
        sample_count = int(payload["sample_count"])
        application_total = sum(float(sample["end_to_end_ms"]) for sample in payload["samples"])
        payload["shared_lifecycle_ms"] = {name: round(value, 3) for name, value in sorted(lifecycle.items())}
        payload["shared_lifecycle_total_ms"] = round(lifecycle_total, 3)
        payload["amortized_shared_lifecycle_ms_per_application"] = round(lifecycle_total / sample_count, 3)
        payload["active_cohort_wall_clock_ms"] = round(application_total + lifecycle_total, 3)
    report["timing_model"] = {
        "run_level_setup_excluded_from_application_end_to_end": {
            name: round(value, 3) for name, value in sorted(run_lifecycle.items())
        },
        "application_end_to_end_includes": "page lifecycle plus per-application context/process teardown",
        "shared_lifecycle_reporting": "A/B hot browser/profile context setup and teardown are separate and amortized",
    }
    report["workspace"] = {
        "sqlite": str(db_path),
        "profiles": str(root / "profiles"),
        "logs": str(event_log),
        "ports": str(root / "ports.json"),
    }
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


__all__ = [
    "FIXTURE_SCHEMA_VERSION",
    "MODES",
    "REPORT_SCHEMA_VERSION",
    "compile_report",
    "counterbalanced_mode_order",
    "end_to_end_attribution",
    "experiment_definition",
    "fixture_expected_cohort",
    "load_fixture",
    "run_session_aging_experiment",
]
