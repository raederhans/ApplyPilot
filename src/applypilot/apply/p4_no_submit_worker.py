"""Deterministic P4 worker for the isolated full-stack no-submit cohort.

This module deliberately has no SubmissionGate, reservation, receipt, mailbox,
or final-action imports.  It traverses the P1 supervisor/durable broker, P2
semantic control gateway, and P3 episode/event contracts against local Chromium
and one explicitly supplied SQLite database.
"""

from __future__ import annotations

import math
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from applypilot.apply.application_episode import (
    application_command,
    build_job_evidence_bundle,
    command_result,
    create_episode,
    episode_from_job,
    execute_application_command,
    get_episode,
    persist_job_evidence_bundle,
)
from applypilot.apply.application_episode import (
    ensure_schema as ensure_episode_schema,
)
from applypilot.apply.application_sessions import ApplicationSupervisor, EndpointDescriptor
from applypilot.apply.browser_broker import StalePageBinding
from applypilot.apply.contracts import application_actor_id
from applypilot.apply.control_descriptors import (
    PlaywrightSemanticControlDriver,
    SemanticControlAuthorityIssuer,
    SemanticControlRequest,
    execute_semantic_control,
    inspect_form_surfaces,
)
from applypilot.apply.durable_browser_broker import DurableBrowserBroker
from applypilot.apply.runtime_namespace import RuntimeNamespace
from applypilot.storage import runtime_control


class MetricSink(Protocol):
    def record_sqlite_wait(self, wait_ms: float) -> None: ...

    def record_sqlite_busy(self) -> None: ...

    def record_worker_span(self, worker_id: int, name: str, duration_ms: float) -> None: ...


ATTRIBUTION_SCHEMA_VERSION = "capypilot-performance-attribution/v2"
TASK_SPANS = frozenset(
    {
        "browser_lease_acquire_ms",
        "context_create_ms",
        "page_create_ms",
        "route_install_ms",
        "page_navigation_ms",
        "cdp_session_ms",
        "control_inspection_ms",
        "page_epoch_advance_ms",
        "evidence_bundle_ms",
        "episode_persist_ms",
        "sqlite_commit_ms",
        "semantic_authority_ms",
        "semantic_action_ms",
        "finalization_lane_wait_ms",
        "finalization_lane_hold_ms",
        "context_close_ms",
    }
)
AGENT_RUNTIME_SPANS = (
    "codex_spawn_ms",
    "mcp_initialize_ms",
    "first_model_output_ms",
    "first_tool_decision_ms",
    "tool_execution_ms",
)


class TaskAttribution:
    """Non-overlapping task timings for the isolated no-submit benchmark."""

    def __init__(
        self,
        *,
        required_spans: Sequence[str],
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        unknown = set(required_spans) - TASK_SPANS
        if unknown:
            raise ValueError(f"unknown required attribution spans: {sorted(unknown)}")
        self._required_spans = tuple(dict.fromkeys(required_spans))
        self._clock = clock
        self._spans_ms: dict[str, float] = {}
        self._active_span: str | None = None

    def record(self, name: str, duration_ms: float) -> None:
        if name not in TASK_SPANS:
            raise ValueError(f"unknown attribution span: {name}")
        value = float(duration_ms)
        if not math.isfinite(value) or value < 0:
            raise ValueError("attribution duration must be finite and non-negative")
        self._spans_ms[name] = self._spans_ms.get(name, 0.0) + value

    @contextmanager
    def measure(self, name: str) -> Iterator[None]:
        if self._active_span is not None:
            raise RuntimeError(f"attribution spans cannot overlap: {self._active_span} -> {name}")
        self._active_span = name
        started = self._clock()
        try:
            yield
        finally:
            elapsed_ms = (self._clock() - started) * 1000
            self._active_span = None
            self.record(name, elapsed_ms)

    def snapshot(self, *, wall_clock_ms: float) -> dict[str, object]:
        wall_clock = float(wall_clock_ms)
        if not math.isfinite(wall_clock) or wall_clock < 0:
            raise ValueError("wall clock must be finite and non-negative")
        observed = sum(self._spans_ms.values())
        if observed > wall_clock + 0.001:
            raise ValueError("attributed spans exceed task wall clock")
        attributed = min(observed, wall_clock)
        unattributed = max(0.0, wall_clock - attributed)
        coverage = 1.0 if wall_clock == 0 else attributed / wall_clock
        missing = sorted(set(self._required_spans) - self._spans_ms.keys())
        return {
            "schema_version": ATTRIBUTION_SCHEMA_VERSION,
            "spans_ms": {name: round(value, 3) for name, value in sorted(self._spans_ms.items())},
            "attributed_wall_clock_ms": round(attributed, 3),
            "unattributed_wall_clock_ms": round(unattributed, 3),
            "attribution_coverage_ratio": round(coverage, 6),
            "missing_required_spans": missing,
            "unavailable_spans": list(AGENT_RUNTIME_SPANS),
            "attribution_complete": not missing and coverage >= 0.90,
        }


_HTML = """
<!doctype html><html><body><form id="application">
  <label>Full name <input id="name" required></label>
  <label>Country <select id="country"><option>Choose</option><option value="sg">Singapore</option></select></label>
  <label><input id="consent" type="checkbox">Routine consent</label>
  <label>Date available <input id="available-date" type="date"></label>
  <button id="next" type="button">Next</button>
  <button id="submit" type="submit">Submit</button>
  <output id="progress">step-one</output>
</form><script>
document.querySelector('#next').addEventListener('click', () => {
  document.querySelector('#progress').textContent = 'step-two';
});
document.querySelector('#application').addEventListener('submit', event => {
  event.preventDefault();
  throw new Error('P4 no-submit cohort forbids form submission');
});
</script></body></html>
"""


class _MeasuredConnection(sqlite3.Connection):
    metric_sink: MetricSink

    def execute(self, sql: str, parameters: Sequence[object] = (), /):  # type: ignore[override]
        statement = str(sql).lstrip().upper()
        started = time.perf_counter() if statement.startswith("BEGIN IMMEDIATE") else None
        try:
            return super().execute(sql, parameters)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).casefold() or "busy" in str(exc).casefold():
                self.metric_sink.record_sqlite_busy()
            raise
        finally:
            if started is not None:
                self.metric_sink.record_sqlite_wait((time.perf_counter() - started) * 1000)


def _connection_provider(db_path: Path, metric_sink: MetricSink) -> Callable[[], sqlite3.Connection]:
    class CohortConnection(_MeasuredConnection):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.metric_sink = metric_sink

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(
            db_path,
            timeout=0.5,
            factory=CohortConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=500")
        return connection

    return connect


def initialize_p4_no_submit_database(db_path: Path) -> None:
    """Initialize the isolated database once before concurrent workers start."""

    connection = sqlite3.connect(db_path, timeout=0.5)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=500")
        runtime_control.ensure_schema(connection)
        ensure_episode_schema(connection)
        connection.commit()
    finally:
        connection.close()


class _ChromiumWorker:
    """Small P1 BrowserWorkerProcess-compatible adapter over local Playwright."""

    def __init__(self, browser: object, worker_id: int, metric_sink: MetricSink) -> None:
        self.worker_id = worker_id
        self._browser = browser
        self._metric_sink = metric_sink
        self._generation = 1
        self._context = None
        self._page = None
        self._target_ids: tuple[str, ...] = ()
        self._applications = 0
        self._active_session: str | None = None
        self._application_spans_ms: dict[str, float] = {}

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def browser_runtime(self) -> str:
        return "chromium"

    @property
    def active_targets(self) -> tuple[str, ...]:
        return self._target_ids

    @property
    def page(self):
        if self._page is None:
            raise RuntimeError("benchmark page is absent")
        return self._page

    def begin_application(
        self,
        *,
        application_session_id: str,
        attempt_id: str,
        actor_id: str,
        start_url: str,
        browser_runtime: str,
        headless: bool,
    ) -> object:
        del attempt_id, actor_id, headless
        if browser_runtime != "chromium" or self._active_session is not None:
            raise RuntimeError("benchmark browser worker lifecycle drifted")
        self._application_spans_ms = {}
        started = time.perf_counter()
        self._context = self._browser.new_context(service_workers="block")
        self._application_spans_ms["context_create_ms"] = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        page = self._context.new_page()
        self._application_spans_ms["page_create_ms"] = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=_HTML))
        self._application_spans_ms["route_install_ms"] = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        page.goto(start_url, wait_until="load")
        self._application_spans_ms["page_navigation_ms"] = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        session = page.context.new_cdp_session(page)
        try:
            target = session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
        finally:
            session.detach()
        self._application_spans_ms["cdp_session_ms"] = (time.perf_counter() - started) * 1000
        self._page = page
        self._target_ids = (str(target),)
        self._active_session = application_session_id
        self._applications += 1
        return page

    def heartbeat(self, *, expected_generation: int) -> EndpointDescriptor:
        if expected_generation != self._generation or self._active_session is None:
            raise RuntimeError("benchmark browser generation is stale")
        return EndpointDescriptor(
            endpoint_id=f"p4-local-{self.worker_id}",
            generation=self._generation,
            transport="stdio",
            address=f"local-chromium-worker:{self.worker_id}",
            reusable=True,
        )

    def end_application(self, application_session_id: str) -> None:
        if application_session_id != self._active_session:
            raise RuntimeError("benchmark application session drifted")
        assert self._context is not None
        started = time.perf_counter()
        self._context.close()
        self._application_spans_ms["context_close_ms"] = (time.perf_counter() - started) * 1000
        self._context = None
        self._page = None
        self._target_ids = ()
        self._active_session = None

    def recycle_idle_generation(self) -> None:
        return None

    def application_spans(self) -> dict[str, float]:
        return dict(self._application_spans_ms)

    def metrics(self) -> dict[str, int]:
        return {
            "browser_generation": self._generation,
            "applications_in_generation": self._applications,
        }


def _job(task: Mapping[str, object], *, attempt_id: str) -> dict[str, object]:
    provider = str(task["provider"])
    host = "tenant.myworkdayjobs.com" if provider == "workday" else "jobs.smartrecruiters.com"
    url = f"https://{host}/apply/{task['task_id']}"
    return {
        "url": url,
        "application_url": url,
        "source_url": url,
        "site": provider,
        "source_site": provider,
        "title": "Synthetic Data Intern",
        "company_name": "CapyPilot Fixture",
        "full_description": "Deterministic local no-submit benchmark role",
        "tailored_resume_sha256": "b" * 64,
        "_bound_submission_materials": {"materials": [{"kind": "resume", "sha256": "b" * 64, "state": "bound"}]},
        "_control_contract": {"version": 1, "scope": "current-page-only", "submit": False},
        "_answer_provenance_binding": {
            "opaque_binding_seed": "a" * 64,
            "fact_scopes": ["global:candidate"],
        },
        "_attempt_id": attempt_id,
    }


def run_p4_no_submit_worker(
    *,
    worker_id: int,
    tasks: Sequence[Mapping[str, object]],
    cohort_id: str,
    root: Path,
    db_path: Path,
    submit_lane: threading.Semaphore,
    metric_sink: MetricSink,
) -> list[dict[str, object]]:
    """Execute one persistent local Chromium worker over its task partition."""

    from playwright.sync_api import sync_playwright

    results: list[dict[str, object]] = []
    connect = _connection_provider(db_path, metric_sink)
    connection = connect()
    try:
        started = time.perf_counter()
        with sync_playwright() as playwright:
            metric_sink.record_worker_span(worker_id, "playwright_start_ms", (time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            browser = playwright.chromium.launch(headless=True)
            metric_sink.record_worker_span(worker_id, "browser_launch_ms", (time.perf_counter() - started) * 1000)
            browser_worker = _ChromiumWorker(browser, worker_id, metric_sink)
            for task in tasks:
                started = time.perf_counter()
                task_id = str(task["task_id"])
                scenario = str(task["scenario"])
                provider = str(task["provider"])
                attempt_id = f"{cohort_id}:attempt:{task_id}"
                actor_id = application_actor_id(attempt_id)
                scope_id = f"{cohort_id}:worker:{worker_id}"
                runtime_id = f"p4-local-chromium:{worker_id}"
                profile_id = f"p4-profile:{worker_id}"
                required_spans = [
                    "context_create_ms",
                    "page_create_ms",
                    "route_install_ms",
                    "page_navigation_ms",
                    "cdp_session_ms",
                    "browser_lease_acquire_ms",
                    "control_inspection_ms",
                    "evidence_bundle_ms",
                    "episode_persist_ms",
                    "sqlite_commit_ms",
                    "finalization_lane_wait_ms",
                    "finalization_lane_hold_ms",
                    "context_close_ms",
                ]
                if scenario != "human_only":
                    required_spans.extend(("semantic_authority_ms", "semantic_action_ms"))
                if scenario == "stale":
                    required_spans.append("page_epoch_advance_ms")
                attribution = TaskAttribution(required_spans=required_spans)
                supplied_job = _job(task, attempt_id=attempt_id)
                supervisor = ApplicationSupervisor(
                    browser_worker,  # type: ignore[arg-type]
                    attempt_id=attempt_id,
                    actor_id=actor_id,
                    start_url=str(supplied_job["application_url"]),
                    browser_runtime="chromium",
                    headless=True,
                    release_browser_authority=lambda: None,
                    session_id_factory=lambda task_id=task_id: f"{cohort_id}:session:{task_id}",
                )
                for name, duration_ms in browser_worker.application_spans().items():
                    attribution.record(name, duration_ms)
                target_id = browser_worker.active_targets[0]
                broker = DurableBrowserBroker(
                    lambda: connection,
                    process_identity_provider=lambda: (os.getpid(), 1),
                    close_connections=False,
                )
                with attribution.measure("browser_lease_acquire_ms"):
                    bundle = broker.acquire_bundle(
                        profile_id=profile_id,
                        page_id=target_id,
                        owner_id=actor_id,
                        scope_id=scope_id,
                        attempt_id=attempt_id,
                        runtime_id=runtime_id,
                    )
                supervisor._release_browser_authority = (  # type: ignore[attr-defined]
                    lambda current_broker=broker, current_scope_id=scope_id: current_broker.release_scope(
                        current_scope_id
                    )
                )
                supervisor.bind_browser_authority(bundle)
                namespace = RuntimeNamespace(
                    root=root,
                    run_id=cohort_id,
                    session_id=f"runtime:{cohort_id}:{worker_id}",
                    profile_id=profile_id,
                )
                context = supervisor.context_bundle(
                    namespace=namespace,
                    phase="prepare",
                    runtime_backend="deterministic-local",
                )
                page = browser_worker.page
                with attribution.measure("control_inspection_ms"):
                    inspection = inspect_form_surfaces(page, context, provider=provider)  # type: ignore[arg-type]
                stale_rejections = 0
                if scenario == "stale":
                    old_binding = bundle.page_binding
                    with attribution.measure("page_epoch_advance_ms"):
                        bundle = broker.advance_page(
                            bundle,
                            expected_page_epoch=bundle.page_binding.page_epoch,
                        )
                    try:
                        broker.validate_page(old_binding)
                    except StalePageBinding:
                        stale_rejections = 1
                    else:
                        raise AssertionError("stale page binding was admitted")
                    supervisor.bind_browser_authority(bundle)
                    context = supervisor.context_bundle(
                        namespace=namespace,
                        phase="prepare",
                        runtime_backend="deterministic-local",
                    )
                    with attribution.measure("control_inspection_ms"):
                        inspection = inspect_form_surfaces(page, context, provider=provider)  # type: ignore[arg-type]

                supplied_job["_browser_lease_binding"] = bundle.as_dict()
                supplied_job["_application_session_id"] = context.application_session_id
                required_materials = ("transcript",) if scenario == "human_only" else ("resume",)
                with attribution.measure("evidence_bundle_ms"):
                    evidence = build_job_evidence_bundle(
                        supplied_job,
                        {},
                        attempt_id=attempt_id,
                        inspection=inspection,
                        required_material_kinds=required_materials,
                    )
                executor_calls = 0
                result: dict[str, object] | None = None
                try:
                    with attribution.measure("episode_persist_ms"):
                        persist_job_evidence_bundle(connection, evidence)
                        episode = episode_from_job(
                            supplied_job,
                            run_id=f"{cohort_id}:turn:{task_id}",
                            evidence=evidence,
                        )
                        create_episode(connection, episode)
                    with attribution.measure("sqlite_commit_ms"):
                        connection.commit()
                    if scenario == "human_only":
                        outcome = "human_required"
                        final_state = get_episode(connection, episode.episode_id).state  # type: ignore[union-attr]
                    else:
                        descriptor = next(item for item in inspection.controls if item.kind == "text")
                        request = SemanticControlRequest(
                            descriptor=descriptor,
                            operation="set_text",
                            value=f"Synthetic Candidate {task_id}",
                        )
                        command = application_command(
                            episode,
                            kind="browser_control",
                            action="set_text",
                            descriptor=descriptor,
                            value_ref=f"fixture:{task_id}",
                        )

                        def execute(
                            current,
                            *,
                            current_request=request,
                            current_broker=broker,
                            current_page=page,
                            current_inspection=inspection,
                            current_supervisor=supervisor,
                            current_attribution=attribution,
                        ):
                            nonlocal executor_calls, bundle, context
                            executor_calls += 1
                            issuer = SemanticControlAuthorityIssuer()
                            with current_attribution.measure("semantic_authority_ms"):
                                authority = issuer.issue(
                                    context=context,
                                    bundle=bundle,
                                    request=current_request,
                                    submit_started=False,
                                )
                            with current_attribution.measure("semantic_action_ms"):
                                semantic = execute_semantic_control(
                                    current_broker,
                                    PlaywrightSemanticControlDriver(current_page, current_inspection),
                                    issuer,
                                    bundle=bundle,
                                    context=context,
                                    authority=authority,
                                    request=current_request,
                                )
                            bundle = semantic.bundle
                            current_supervisor.bind_browser_authority(bundle)
                            context = replace(context, page_binding=bundle.page_binding.as_dict())
                            return command_result(
                                current,
                                status="verified",
                                outcome="semantic_control_verified",
                                effect_applied=True,
                                resulting_page_epoch=bundle.page_binding.page_epoch,
                            )

                        first = execute_application_command(connection, command, execute)
                        replayed = False
                        if scenario == "crash_recovery":
                            replay = execute_application_command(connection, command, execute)
                            replayed = replay.replayed
                        outcome = first.outcome
                        final_state = get_episode(connection, episode.episode_id).state  # type: ignore[union-attr]

                    lane_started = time.perf_counter()
                    if not submit_lane.acquire(timeout=1.0):
                        raise TimeoutError("no-submit finalization lane timed out")
                    lane_acquired = time.perf_counter()
                    try:
                        time.sleep(0.0005)
                    finally:
                        lane_released = time.perf_counter()
                        submit_lane.release()
                    attribution.record("finalization_lane_wait_ms", (lane_acquired - lane_started) * 1000)
                    attribution.record("finalization_lane_hold_ms", (lane_released - lane_acquired) * 1000)

                    cross_talk = {
                        "actor": int(context.actor_id != actor_id or context.attempt_id != attempt_id),
                        "profile": int(context.browser_profile_id != profile_id),
                        "page": int(
                            context.application_session_id != f"{cohort_id}:session:{task_id}"
                            or target_id not in context.root_target_ids
                        ),
                    }
                    result = {
                        "task_id": task_id,
                        "provider": provider,
                        "scenario": scenario,
                        "outcome": outcome,
                        "episode_state": final_state,
                        "executor_calls": executor_calls,
                        "replay_verified": bool(scenario != "crash_recovery" or replayed),
                        "stale_rejections": stale_rejections,
                        "submit_lane_wait_ms": round((lane_acquired - lane_started) * 1000, 3),
                        "submit_lane_hold_ms": round((lane_released - lane_acquired) * 1000, 3),
                        "actor_cross_talk": cross_talk["actor"],
                        "profile_cross_talk": cross_talk["profile"],
                        "page_cross_talk": cross_talk["page"],
                        "duplicate_submit_attempts": 0,
                        "admitted_stale_writes": 0,
                        "repeated_admitted_actions": int(executor_calls > 1),
                        "final_submit_calls": 0,
                        "submission_gate_calls": 0,
                        "reservation_calls": 0,
                        "receipt_calls": 0,
                        "receipt_identity_drift": 0,
                    }
                finally:
                    supervisor.close_application()
                    attribution.record(
                        "context_close_ms",
                        browser_worker.application_spans().get("context_close_ms", 0.0),
                    )
                    broker.close()
                if result is not None:
                    wall_clock_ms = (time.perf_counter() - started) * 1000
                    result["latency_ms"] = round(wall_clock_ms, 3)
                    result["performance_attribution"] = attribution.snapshot(wall_clock_ms=wall_clock_ms)
                    results.append(result)
            started = time.perf_counter()
            browser.close()
            metric_sink.record_worker_span(worker_id, "browser_close_ms", (time.perf_counter() - started) * 1000)
    finally:
        connection.close()
    return results


__all__ = [
    "AGENT_RUNTIME_SPANS",
    "ATTRIBUTION_SCHEMA_VERSION",
    "TaskAttribution",
    "initialize_p4_no_submit_database",
    "run_p4_no_submit_worker",
]
