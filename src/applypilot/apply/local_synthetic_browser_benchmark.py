"""Real local-Chromium, offline and submit-free matched-cohort benchmark.

This is synthetic smoke evidence only.  It never loads an ApplyPilot profile or
database, never acquires submission authority, and is not a provider benchmark.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

LOCAL_SYNTHETIC_LABEL: Final = "local_synthetic_chromium_not_a_provider_benchmark"


class LocalChromiumUnavailable(RuntimeError):
    """The required local Playwright Chromium executable could not start."""


class SyntheticBrowserSafetyViolation(RuntimeError):
    """Synthetic page code attempted a forbidden submission operation."""


@dataclass(frozen=True, slots=True)
class SyntheticBrowserTask:
    task_id: str
    scenario: str


@dataclass(frozen=True, slots=True)
class SyntheticTaskResult:
    task_id: str
    scenario: str
    outcome: str
    observation: str
    external_network_requests: int
    submit_events: int
    form_submit_calls: int
    request_submit_calls: int
    submit_button_clicks: int


@dataclass(frozen=True, slots=True)
class SyntheticCohortReport:
    workers: int
    task_count: int
    execution_counts: tuple[tuple[str, int], ...]
    max_active: int
    wall_clock_ms: float
    fixture_digest: str
    result_digest: str
    results: tuple[SyntheticTaskResult, ...]


@dataclass(frozen=True, slots=True)
class LocalSyntheticBrowserReport:
    label: str
    fixture_digest: str
    cohorts: tuple[SyntheticCohortReport, ...]
    provider_benchmark: bool = False
    promotion_authority: bool = False
    profile_accesses: int = 0
    default_database_accesses: int = 0
    submission_gate_touches: int = 0
    reservation_touches: int = 0
    receipt_admission_touches: int = 0


FIXED_TASKS: Final = (
    SyntheticBrowserTask("task-text", "text"),
    SyntheticBrowserTask("task-select", "select"),
    SyntheticBrowserTask("task-file", "file"),
    SyntheticBrowserTask("task-segmented-date", "segmented_date"),
    SyntheticBrowserTask("task-dynamic-validation", "dynamic_validation"),
    SyntheticBrowserTask("task-stale-rerender", "stale_dom_rerender"),
    SyntheticBrowserTask("task-session-restart", "session_restart"),
    SyntheticBrowserTask("task-synthetic-receipt", "synthetic_receipt_confirmation"),
)

_HTML: Final = """
<!doctype html>
<html><body>
  <form id="application-form">
    <label>Name <input id="name" autocomplete="off"></label>
    <label>Country <select id="country">
      <option value="">Choose</option><option value="sg">Singapore</option>
    </select></label>
    <label>Resume <input id="resume" type="file" accept="application/pdf"></label>
    <fieldset id="date">
      <input id="day" inputmode="numeric"><select id="month"><option value="09">09</option></select>
      <input id="year" inputmode="numeric">
    </fieldset>
    <label>Code <input id="code"><span id="validation">pending</span></label>
    <section id="rerender"><input id="stale-field" data-generation="1"></section>
    <button id="rerender-button" type="button">Rerender</button>
    <button id="receipt-button" type="button">Show synthetic confirmation</button>
    <output id="receipt"></output>
    <button id="submit-button" type="submit">Submit</button>
  </form>
  <script>
    document.querySelector('#code').addEventListener('input', event => {
      document.querySelector('#validation').textContent =
        event.target.value === 'SAFE-42' ? 'valid' : 'invalid';
    });
    document.querySelector('#rerender-button').addEventListener('click', () => {
      document.querySelector('#rerender').innerHTML =
        '<input id="stale-field" data-generation="2">';
    });
    document.querySelector('#receipt-button').addEventListener('click', () => {
      document.querySelector('#receipt').textContent = 'SYNTHETIC CONFIRMATION ONLY';
    });
  </script>
</body></html>
"""

_SAFETY_SCRIPT: Final = """
() => {
  window.__applypilotSyntheticSafety = {
    submitEvents: 0,
    formSubmitCalls: 0,
    requestSubmitCalls: 0,
    submitButtonClicks: 0
  };
  const form = document.querySelector('#application-form');
  form.addEventListener('submit', event => {
    window.__applypilotSyntheticSafety.submitEvents += 1;
    event.preventDefault();
    throw new Error('synthetic benchmark forbids submit events');
  }, true);
  const nativeSubmit = HTMLFormElement.prototype.submit;
  const nativeRequestSubmit = HTMLFormElement.prototype.requestSubmit;
  HTMLFormElement.prototype.submit = function() {
    window.__applypilotSyntheticSafety.formSubmitCalls += 1;
    throw new Error('synthetic benchmark forbids form.submit');
  };
  HTMLFormElement.prototype.requestSubmit = function() {
    window.__applypilotSyntheticSafety.requestSubmitCalls += 1;
    throw new Error('synthetic benchmark forbids requestSubmit');
  };
  document.querySelector('#submit-button').addEventListener('click', () => {
    window.__applypilotSyntheticSafety.submitButtonClicks += 1;
  }, true);
  window.__applypilotNativeSubmit = Boolean(nativeSubmit);
  window.__applypilotNativeRequestSubmit = Boolean(nativeRequestSubmit);
}
"""


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _fixture_digest(tasks: tuple[SyntheticBrowserTask, ...]) -> str:
    return _canonical_digest([asdict(task) for task in sorted(tasks, key=lambda item: item.task_id)])


class _OverlapTracker:
    def __init__(self, target: int) -> None:
        self.target = target
        self.active = 0
        self.max_active = 0
        self.ready = asyncio.Event()

        self.arrivals = 0

    async def rendezvous(self) -> None:
        self.arrivals += 1
        if self.arrivals >= self.target:
            self.ready.set()
        await asyncio.wait_for(self.ready.wait(), timeout=10)

    def start_exercise(self) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)

    def exit(self) -> None:
        self.active -= 1


async def _new_page(browser: object, network_attempts: list[str]):
    context = await browser.new_context(service_workers="block")

    async def abort_route(route: object) -> None:
        url = str(route.request.url)
        if url.startswith(("http://", "https://")):
            network_attempts.append(url)
        await route.abort()

    await context.route("**/*", abort_route)
    page = await context.new_page()
    await page.set_content(_HTML, wait_until="domcontentloaded")
    await page.evaluate(_SAFETY_SCRIPT)
    return context, page


async def _exercise_task(browser: object, task: SyntheticBrowserTask) -> SyntheticTaskResult:
    network_attempts: list[str] = []
    context, page = await _new_page(browser, network_attempts)
    observation = ""
    try:
        if task.scenario == "text":
            await page.locator("#name").fill("Synthetic Candidate")
            observation = await page.locator("#name").input_value()
        elif task.scenario == "select":
            await page.locator("#country").select_option("sg")
            observation = await page.locator("#country").input_value()
        elif task.scenario == "file":
            with tempfile.TemporaryDirectory(prefix="applypilot-synthetic-") as directory:
                artifact = Path(directory) / "synthetic-resume.pdf"
                artifact.write_bytes(b"%PDF-1.4\n% local synthetic fixture\n%%EOF")
                await page.locator("#resume").set_input_files(str(artifact))
                observation = await page.locator("#resume").evaluate(
                    "element => element.files[0].name"
                )
        elif task.scenario == "segmented_date":
            await page.locator("#day").fill("01")
            await page.locator("#month").select_option("09")
            await page.locator("#year").fill("2026")
            observation = "-".join(
                [
                    await page.locator("#year").input_value(),
                    await page.locator("#month").input_value(),
                    await page.locator("#day").input_value(),
                ]
            )
        elif task.scenario == "dynamic_validation":
            await page.locator("#code").fill("SAFE-42")
            observation = await page.locator("#validation").text_content() or ""
        elif task.scenario == "stale_dom_rerender":
            stale = page.locator("#stale-field")
            await stale.fill("before")
            await page.locator("#rerender-button").click()
            fresh = page.locator("#stale-field")
            await fresh.fill("after")
            observation = ":".join(
                [await fresh.get_attribute("data-generation") or "", await fresh.input_value()]
            )
        elif task.scenario == "session_restart":
            await page.evaluate("window.__syntheticSessionMarker = 'old-session'")
            await context.close()
            context, page = await _new_page(browser, network_attempts)
            observation = await page.evaluate(
                "typeof window.__syntheticSessionMarker === 'undefined' ? 'fresh' : 'reused'"
            )
        elif task.scenario == "synthetic_receipt_confirmation":
            await page.locator("#receipt-button").click()
            observation = await page.locator("#receipt").text_content() or ""
        elif task.scenario.startswith("negative_"):
            from playwright.async_api import Error as PlaywrightError

            action = task.scenario.removeprefix("negative_")
            try:
                if action == "submit_button":
                    await page.locator("#submit-button").click()
                elif action == "form_submit":
                    await page.evaluate("document.querySelector('#application-form').submit()")
                elif action == "request_submit":
                    await page.evaluate(
                        "document.querySelector('#application-form').requestSubmit()"
                    )
                elif action == "submit_event":
                    await page.evaluate(
                        "document.querySelector('#application-form').dispatchEvent("
                        "new Event('submit', {bubbles: true, cancelable: true}))"
                    )
                else:  # pragma: no cover - negative tasks are test-owned
                    raise ValueError(f"unsupported negative submit trap: {action}")
            except PlaywrightError:
                # The JS trap deliberately throws.  The durable proof is the
                # corresponding page-owned counter checked below.
                pass
            safety = await page.evaluate("window.__applypilotSyntheticSafety")
            expected_counter = {
                "submit_button": "submitButtonClicks",
                "form_submit": "formSubmitCalls",
                "request_submit": "requestSubmitCalls",
                "submit_event": "submitEvents",
            }[action]
            if int(safety[expected_counter]) < 1:
                raise AssertionError(f"synthetic submit trap did not activate: {action}")
            raise SyntheticBrowserSafetyViolation(
                f"forbidden synthetic submission operation: {action}"
            )
        else:  # pragma: no cover - fixed tasks are exhaustive
            raise ValueError(f"unsupported synthetic scenario: {task.scenario}")
        safety = await page.evaluate("window.__applypilotSyntheticSafety")
        if any(int(value) != 0 for value in safety.values()):
            raise SyntheticBrowserSafetyViolation(
                f"forbidden synthetic submission activity: {task.task_id}"
            )
    finally:
        await context.close()
    return SyntheticTaskResult(
        task_id=task.task_id,
        scenario=task.scenario,
        outcome="passed",
        observation=str(observation),
        external_network_requests=len(network_attempts),
        submit_events=int(safety["submitEvents"]),
        form_submit_calls=int(safety["formSubmitCalls"]),
        request_submit_calls=int(safety["requestSubmitCalls"]),
        submit_button_clicks=int(safety["submitButtonClicks"]),
    )


async def _run_cohort(
    browser: object,
    tasks: tuple[SyntheticBrowserTask, ...],
    workers: int,
) -> SyntheticCohortReport:
    semaphore = asyncio.Semaphore(workers)
    tracker = _OverlapTracker(min(workers, len(tasks)))
    counts = {task.task_id: 0 for task in tasks}

    async def run_one(task: SyntheticBrowserTask) -> SyntheticTaskResult:
        async with semaphore:
            await tracker.rendezvous()
            tracker.start_exercise()
            counts[task.task_id] += 1
            try:
                return await _exercise_task(browser, task)
            finally:
                tracker.exit()

    started = time.perf_counter()
    results = tuple(await asyncio.gather(*(run_one(task) for task in tasks)))
    elapsed_ms = (time.perf_counter() - started) * 1000
    ordered_results = tuple(sorted(results, key=lambda item: item.task_id))
    semantic = [
        {
            "task_id": result.task_id,
            "scenario": result.scenario,
            "outcome": result.outcome,
            "observation": result.observation,
            "external_network_requests": result.external_network_requests,
            "submit_events": result.submit_events,
            "form_submit_calls": result.form_submit_calls,
            "request_submit_calls": result.request_submit_calls,
            "submit_button_clicks": result.submit_button_clicks,
        }
        for result in ordered_results
    ]
    return SyntheticCohortReport(
        workers=workers,
        task_count=len(tasks),
        execution_counts=tuple(sorted(counts.items())),
        max_active=tracker.max_active,
        wall_clock_ms=round(elapsed_ms, 3),
        fixture_digest=_fixture_digest(tasks),
        result_digest=_canonical_digest(semantic),
        results=ordered_results,
    )


async def _run(
    worker_counts: tuple[int, ...],
    tasks: tuple[SyntheticBrowserTask, ...],
) -> LocalSyntheticBrowserReport:
    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - test environment must provide Playwright
        raise LocalChromiumUnavailable(
            "local Chromium benchmark requires Playwright"
        ) from exc

    fixture_digest = _fixture_digest(tasks)
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except PlaywrightError as exc:
            raise LocalChromiumUnavailable(
                "local headless Chromium is required for this benchmark"
            ) from exc
        try:
            cohorts = tuple(
                [await _run_cohort(browser, tasks, workers) for workers in worker_counts]
            )
        finally:
            await browser.close()
    return LocalSyntheticBrowserReport(
        label=LOCAL_SYNTHETIC_LABEL,
        fixture_digest=fixture_digest,
        cohorts=cohorts,
    )


def run_local_synthetic_browser_benchmark(
    *,
    worker_counts: tuple[int, ...] = (1, 2, 4),
    tasks: tuple[SyntheticBrowserTask, ...] = FIXED_TASKS,
) -> LocalSyntheticBrowserReport:
    """Run matched local Chromium cohorts; missing Chromium is a hard failure."""
    if not worker_counts or any(
        isinstance(workers, bool) or not isinstance(workers, int) or workers < 1
        for workers in worker_counts
    ):
        raise ValueError("worker_counts must contain positive integers")
    if not tasks or len({task.task_id for task in tasks}) != len(tasks):
        raise ValueError("synthetic tasks must be non-empty with unique task ids")
    return asyncio.run(_run(worker_counts, tuple(tasks)))


__all__ = [
    "FIXED_TASKS",
    "LOCAL_SYNTHETIC_LABEL",
    "LocalChromiumUnavailable",
    "LocalSyntheticBrowserReport",
    "SyntheticBrowserSafetyViolation",
    "SyntheticBrowserTask",
    "SyntheticCohortReport",
    "SyntheticTaskResult",
    "run_local_synthetic_browser_benchmark",
]
