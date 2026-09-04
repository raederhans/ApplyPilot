"""Repeatable local Chromium session-aging experiment with no submit authority.

The harness is deliberately separate from production application orchestration.
It only serves a local fixture through Playwright interception and records which
browser/context lifetime was used for each synthetic application.
"""

from __future__ import annotations

import hashlib
import json
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
    for task in tasks:
        if not isinstance(task, Mapping):
            raise TypeError("session-aging task must be an object")
        task_id = task.get("task_id")
        provider = task.get("provider")
        if not isinstance(task_id, str) or not task_id or not task_id.isascii() or not task_id.replace("-", "").isalnum():
            raise ValueError("session-aging task_id must be non-empty ASCII alphanumeric text")
        if provider not in _PROVIDER_DOMAINS:
            raise ValueError("session-aging provider is unsupported")
        task_ids.append(task_id)
        providers.add(str(provider))
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("session-aging task ids must be unique")
    if providers != set(_PROVIDER_DOMAINS):
        raise ValueError("session-aging fixture must include both supported providers")
    return raw, hashlib.sha256(raw_bytes).hexdigest()


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
    samples: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Compile a deterministic report from measured synthetic samples.

    Missing Agent/MCP/recovery spans remain unavailable.  This is intentional:
    this browser-only diagnostic cannot observe or fabricate those runtimes.
    """

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
        if (
            provider not in _PROVIDER_DOMAINS
            or domain != _PROVIDER_DOMAINS[provider]
            or isinstance(index, bool)
            or not isinstance(index, int)
            or index < 1
        ):
            raise ValueError("sample provider/domain/application index is invalid")
        observation_ms = sample.get("observation_ms")
        if isinstance(observation_ms, bool) or not isinstance(observation_ms, (int, float)) or observation_ms < 0:
            raise ValueError("sample observation duration is invalid")
        if sample.get("submit_attempts") != 0:
            raise ValueError("session-aging samples must record zero submit attempts")
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
        by_mode[str(mode)].append(dict(sample))
    for mode, mode_samples in by_mode.items():
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
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "diagnostic_only": True,
        "production_authority": False,
        "submission_authority": False,
        "fixture_sha256": fixture_sha256,
        "definitions": experiment_definition(),
        "attribution_coverage": _coverage(),
        "modes": {
            mode: {
                "sample_count": len(mode_samples),
                "samples": mode_samples,
                "coverage": _coverage(),
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


def _measure_application(*, context: object, task: Mapping[str, object], index: int) -> tuple[float, int]:
    provider = str(task["provider"])
    domain = _PROVIDER_DOMAINS[provider]
    page = context.new_page()  # type: ignore[attr-defined]
    try:
        started = time.perf_counter()
        page.goto(f"https://{domain}/local-fixture/{task['task_id']}", wait_until="load")
        form_count = page.locator("#application").count()
        submit_count = page.locator("#submit").count()
        submit_attempts = page.evaluate("window.__submissionAttempts")
        observation_ms = (time.perf_counter() - started) * 1000
        if form_count != 1 or submit_count != 1 or submit_attempts != 0:
            raise AssertionError("no-submit fixture invariant failed")
        return observation_ms, int(submit_attempts)
    finally:
        page.close()


def _new_context(browser: object) -> object:
    context = browser.new_context(service_workers="block")  # type: ignore[attr-defined]
    context.route(  # type: ignore[attr-defined]
        "**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=_HTML)
    )
    return context


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
        def record(mode: str, task: Mapping[str, object], index: int, browser_instance: int, context_instance: int, context: object) -> None:
            observation_ms, submit_attempts = _measure_application(context=context, task=task, index=index)
            provider = str(task["provider"])
            sample = {
                "mode": mode,
                "dimensions": {
                    "provider": provider,
                    "domain": _PROVIDER_DOMAINS[provider],
                    "application_index": index,
                },
                "browser_instance": browser_instance,
                "context_instance": context_instance,
                "observation_ms": round(observation_ms, 3),
                "submit_attempts": submit_attempts,
            }
            samples.append(sample)
            connection.execute(
                "INSERT INTO sample VALUES (?, ?, ?, ?, ?)",
                (mode, index, provider, _PROVIDER_DOMAINS[provider], observation_ms),
            )
            log.write(json.dumps(sample, sort_keys=True) + "\n")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = _new_context(browser)
                try:
                    for index, task in enumerate(tasks, start=1):
                        record("shared_context", task, index, 1, 1, context)
                finally:
                    context.close()
                for index, task in enumerate(tasks, start=1):
                    context = _new_context(browser)
                    try:
                        record("fresh_context", task, index, 1, index, context)
                    finally:
                        context.close()
            finally:
                browser.close()
        for index, task in enumerate(tasks, start=1):
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    context = _new_context(browser)
                    try:
                        record("fresh_process", task, index, index, 1, context)
                    finally:
                        context.close()
                finally:
                    browser.close()
        connection.commit()
    report = compile_report(fixture_sha256=fixture_sha256, samples=samples)
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
    "experiment_definition",
    "load_fixture",
    "run_session_aging_experiment",
]
