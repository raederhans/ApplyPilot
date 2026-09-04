"""Application worker orchestration over an injected runtime port.

The compatibility launcher supplies browser, storage, policy, and dashboard
operations at call time.  This keeps current monkeypatch and shutdown semantics
while removing the orchestration state machine from the launcher facade.
"""

from __future__ import annotations

import math
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from applypilot.apply import application_actor as application_actor_mod
from applypilot.apply import performance_attribution as performance_attribution_mod
from applypilot.apply import recovery_execution as recovery_execution_mod
from applypilot.apply.answer_provenance import build_host_provenance_binding
from applypilot.apply.application_sessions import (
    ApplicationSupervisor,
    BrowserWorkerProcess,
    EndpointUnavailable,
    LoopbackHttpEndpointManager,
    LoopbackHttpEndpointSpec,
    LoopbackPortReservation,
    PerTurnStdioEndpointManager,
    resolve_persistent_playwright_launcher,
)
from applypilot.apply.browser_broker import BrowserBrokerError, BrowserLeaseBundle
from applypilot.apply.contracts import application_actor_id, contract_json
from applypilot.apply.email_routing import (
    normalize_prepared_email_application,
    normalize_sent_receipt,
    reserve_direct_email_send,
)
from applypilot.apply.exception_queue import exception_id_for_command
from applypilot.apply.failure_taxonomy import classify_failure
from applypilot.apply.operator_binding import operator_resume_binding
from applypilot.apply.operator_dispatch import wait_for_requested_resume
from applypilot.apply.operator_runtime import OperatorRuntime, verified_child_execution
from applypilot.apply.run_progress import PreviewTicket, RunProgress
from applypilot.apply.stateful_control_coverage import (
    stateful_control_coverage_error as _stateful_control_coverage_error,
)


class WorkerConfigPort(Protocol):
    APPLY_WORKER_DIR: Path

    def load_profile(self) -> dict: ...


@dataclass(frozen=True, slots=True)
class WorkerHostPorts:
    """Process, configuration, and clock services for one worker."""

    poll_interval: float
    stop_event: threading.Event
    config: WorkerConfigPort
    logger: Any
    datetime: Any
    load_runtime_settings: Callable[..., Any]
    kill_process_tree: Callable[..., Any]
    process_rss_bytes: Callable[..., Any] | None = None


@dataclass(frozen=True, slots=True)
class WorkerBrowserPorts:
    """Browser ownership, routing, and session lifecycle services."""

    acquire_cloak_lane: Callable[..., Any]
    cloak_lane: Any
    allocate_cdp_port: Callable[..., int]
    release_cdp_port: Callable[..., Any]
    launch_chrome: Callable[..., Any]
    cleanup_worker: Callable[..., Any]
    cdp_endpoint_reachable: Callable[[int], bool]
    open_bound_application_target: Callable[..., Any]
    close_bound_application_targets: Callable[..., Any]
    release_application_browser_authority: Callable[..., Any]
    capture_browser_session: Callable[..., Any]
    restore_browser_session: Callable[..., Any]
    cloak_fallback_route: Callable[..., Any]
    computer_use_handoff_allowed: Callable[..., Any]
    initial_route: Callable[..., Any]
    resolve_browser_backend: Callable[..., Any]
    resolve_interaction_mode: Callable[..., Any]


@dataclass(frozen=True, slots=True)
class WorkerJobPorts:
    """Job acquisition and durable lifecycle services."""

    acquire_job: Callable[..., Any]
    add_event: Callable[..., Any]
    get_connection: Callable[..., Any]
    mark_result: Callable[..., Any]
    mark_runtime_cover_not_required: Callable[..., Any]
    record_application_attempt_performance: Callable[..., Any]
    release_lock: Callable[..., Any]
    restore_preview_state: Callable[..., Any]
    update_state: Callable[..., Any]


@dataclass(frozen=True, slots=True)
class WorkerApplicationPorts:
    """Application-agent execution and preparation services."""

    archive_worker_evidence: Callable[..., Any]
    snapshot_worker_evidence: Callable[..., Any]
    attach_control_contract: Callable[..., Any]
    format_failure_error: Callable[..., Any]
    is_permanent_failure: Callable[..., Any]
    resolve_ats_application_binding: Callable[..., Any]
    run_read_only_preflight: Callable[..., Any]
    prepare_ats_fill_plan_repair: Callable[..., Any]
    try_semantic_pre_submit_repair: Callable[..., Any]
    record_ats_fill_plan_feedback: Callable[..., Any]
    prepare_runtime_cover_letter: Callable[..., Any]
    runtime_linkedin_route_gate: Callable[..., Any]
    route_for_phase: Callable[..., Any]
    run_job: Callable[..., Any]


@dataclass(frozen=True, slots=True)
class WorkerPageObservationPorts:
    """Pre-submit, LinkedIn handoff, and post-submit observation services."""

    audit_live_pre_submit_page: Callable[..., Any]
    classify_post_submit_observation: Callable[..., Any]
    click_linkedin_main_apply_causally: Callable[..., Any]
    verify_linkedin_post_login_state: Callable[..., Any]
    observe_post_submit_page: Callable[..., Any]
    observe_linkedin_external_handoff_page: Callable[..., Any]


@dataclass(frozen=True, slots=True)
class WorkerSubmissionPorts:
    """Submission authority, ledger, and receipt services."""

    acquire_submit_writer_lane: Callable[..., Any]
    admit_direct_email_receipt: Callable[..., Any]
    configured_receipt_observers: Callable[..., Any]
    build_receipt_observer_context: Callable[..., Any]
    process_receipt_observer_result: Callable[..., Any]
    reserve_manifest_submission: Callable[..., Any]
    submission_evidence_consistent: Callable[..., Any]
    submission_rate_status: Callable[..., Any]
    submit_writer_lane: Any
    update_submission_ledger: Callable[..., Any]
    has_admitted_submission_receipt: Callable[..., Any]


@dataclass(frozen=True, slots=True)
class WorkerOperatorPorts:
    """Human-review and bounded resume authorization services."""

    issue_manual_resume_authorization: Callable[..., Any]
    consume_manual_resume_authorization: Callable[..., Any]
    heartbeat_operator_handoff: Callable[..., Any]
    runtime_operator_resume_scope: Callable[..., Any]
    runtime_audit_verification_scope: Callable[..., Any]
    runtime_recovery_scope: Callable[..., Any]
    runtime_submit_scope: Callable[..., Any]
    wait_for_manual_captcha: Callable[..., Any]


@dataclass(frozen=True, slots=True)
class WorkerRuntimePorts:
    """Responsibility-grouped runtime boundary consumed by the worker core."""

    host: WorkerHostPorts
    browser: WorkerBrowserPorts
    jobs: WorkerJobPorts
    application: WorkerApplicationPorts
    observation: WorkerPageObservationPorts
    submission: WorkerSubmissionPorts
    operator: WorkerOperatorPorts


@dataclass(frozen=True, slots=True)
class WorkerRunOptions:
    """Immutable configuration for one worker run."""

    worker_id: int = 0
    limit: int = 1
    target_url: str | None = None
    min_score: int = 6
    headless: bool = False
    model: str = "sonnet"
    dry_run: bool = False
    agent_backend: str = "codex"
    manual_captcha_relay: bool = False
    browser_backend: str = "edge"
    interaction_mode: str = "auto"
    authorization_manifest: dict | None = None
    attempted_urls: set[str] | None = None
    attempted_urls_lock: threading.Lock | None = None
    run_progress: RunProgress | None = None


def _prepared_email_application(job: dict) -> dict | None:
    """Return a bounded, verified prepare plan reported by the application agent."""
    observations = job.get("_agent_observations")
    if not isinstance(observations, dict):
        return None
    return normalize_prepared_email_application(
        observations.get("email_application"),
        job,
    )


def _observer_screenshot_name(attempt: int) -> str:
    """Return the semantic-neutral name for one post-submit observation."""
    return (
        "post-submit-observer.png"
        if attempt == 1
        else f"post-submit-observer-attempt-{attempt}.png"
    )


def _url_has_host(url: object, domain: str) -> bool:
    """Return whether a URL is hosted by an exact domain or one of its subdomains."""
    try:
        host = (urlparse(str(url or "")).hostname or "").casefold().rstrip(".")
    except ValueError:
        return False
    normalized_domain = domain.casefold().rstrip(".")
    return host == normalized_domain or host.endswith(f".{normalized_domain}")


def _consume_provenance_repair_artifacts(job: dict, repair_job: Mapping[str, object]) -> str | None:
    """Copy only current-page provenance repair output back to the owning job."""

    repair_observations = repair_job.get("_agent_observations")
    answer_mappings = (
        repair_observations.get("answer_mappings")
        if isinstance(repair_observations, Mapping)
        else None
    )
    repair_binding = repair_job.get("_answer_provenance_binding")
    if answer_mappings is None and not isinstance(repair_binding, Mapping):
        return None
    if answer_mappings is not None and not isinstance(answer_mappings, Mapping):
        return "mapping_not_object"
    attempt_id = str(job.get("_attempt_id") or "")
    if not attempt_id or str(repair_job.get("_attempt_id") or "") != attempt_id:
        return "attempt_mismatch"
    try:
        current_bundle = BrowserLeaseBundle.from_mapping(job["_browser_lease_binding"])
        repair_bundle = BrowserLeaseBundle.from_mapping(
            repair_job["_browser_lease_binding"]  # type: ignore[arg-type]
        )
    except (BrowserBrokerError, KeyError, TypeError, ValueError):
        return "browser_binding_invalid"
    fixed_profile = (
        "lease_id", "resource_kind", "resource_id", "owner_id", "scope_id",
        "attempt_id", "runtime_id", "epoch",
    )
    fixed_page = fixed_profile
    fixed_binding = (
        "page_id", "page_lease_id", "page_lease_epoch", "profile_lease_id",
        "owner_id", "attempt_id", "runtime_id",
    )
    if any(
        getattr(current_bundle.profile, key) != getattr(repair_bundle.profile, key)
        for key in fixed_profile
    ) or any(
        getattr(current_bundle.page, key) != getattr(repair_bundle.page, key)
        for key in fixed_page
    ) or any(
        getattr(current_bundle.page_binding, key)
        != getattr(repair_bundle.page_binding, key)
        for key in fixed_binding
    ):
        return "browser_lease_mismatch"
    if repair_bundle.page_binding.page_epoch < current_bundle.page_binding.page_epoch:
        return "stale_page_epoch"
    if not isinstance(repair_binding, Mapping):
        return "provenance_binding_missing"
    try:
        recomputed = build_host_provenance_binding(repair_job)
    except (TypeError, ValueError):
        return "provenance_binding_invalid"
    if dict(repair_binding) != recomputed:
        return "provenance_binding_mismatch"

    job["_browser_lease_binding"] = deepcopy(repair_job["_browser_lease_binding"])
    job["_answer_provenance_binding"] = deepcopy(dict(repair_binding))
    if isinstance(answer_mappings, Mapping):
        current_observations = job.get("_agent_observations")
        merged = dict(current_observations) if isinstance(current_observations, Mapping) else {}
        merged["answer_mappings"] = deepcopy(dict(answer_mappings))
        job["_agent_observations"] = merged
    return None


def _provenance_only_audit(report: Mapping[str, object]) -> bool:
    """Admit a child only when missing provenance is the sole host finding."""
    issues = report.get("issues")
    repairable = report.get("repairable_issues")
    blockers = report.get("blocking_issues")
    advisories = report.get("advisory_issues")
    form = report.get("ats_adapter_context")
    page_url = str(report.get("page_url") or "").strip()
    return bool(
        report.get("disposition") == "retry_prepare"
        and isinstance(issues, list)
        and issues
        and all(str(issue).startswith("answer_provenance_missing:") for issue in issues)
        and isinstance(repairable, list)
        and set(map(str, repairable)) == set(map(str, issues))
        and blockers == []
        and advisories == []
        and isinstance(form, Mapping)
        and isinstance(form.get("fields"), list)
        and page_url
        and _stateful_control_coverage_error(report) is None
    )


def _enforce_stateful_control_coverage(
    audit_signal: str | None,
    report: Mapping[str, object],
) -> tuple[str | None, dict]:
    """Make the aggregate stateful-control proof mandatory at every host gate."""

    normalized = dict(report)
    error = _stateful_control_coverage_error(normalized)
    if error is None:
        return audit_signal, normalized

    def _append_unique(name: str) -> list[str]:
        values = normalized.get(name)
        result = [str(value) for value in values] if isinstance(values, list) else []
        if error not in result:
            result.append(error)
        return result

    normalized.update(
        {
            "status": "attention",
            "disposition": "block",
            "issues": _append_unique("issues"),
            "blocking_issues": _append_unique("blocking_issues"),
            "submission_gate": False,
        }
    )
    return audit_signal or f"pre_submit_audit:{error}", normalized


def _worker_loop_with_port(
    runtime: WorkerRuntimePorts,
    port: int,
    browser_worker: BrowserWorkerProcess,
    options: WorkerRunOptions,
) -> tuple[int, int]:
    """Run jobs until the confirmed-success target is reached or the queue is empty.

    Args:
        worker_id: Numeric worker identifier.
        limit: Confirmed submissions to achieve for a real run; preview jobs for dry-run (0 = continuous).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome headless.
        model: Claude model name.
        dry_run: Don't click Submit.

    Returns:
        Tuple of (applied_count, failed_count).
    """
    host = runtime.host
    browser = runtime.browser
    jobs = runtime.jobs
    application = runtime.application
    observation = runtime.observation
    submission = runtime.submission
    operator = runtime.operator
    worker_id = options.worker_id
    limit = options.limit
    target_url = options.target_url
    min_score = options.min_score
    headless = options.headless
    model = options.model
    dry_run = options.dry_run
    agent_backend = options.agent_backend
    manual_captcha_relay = options.manual_captcha_relay
    browser_backend = options.browser_backend
    interaction_mode = options.interaction_mode
    authorization_manifest = options.authorization_manifest
    attempted_urls = options.attempted_urls
    attempted_urls_lock = options.attempted_urls_lock
    run_progress = options.run_progress
    POLL_INTERVAL = host.poll_interval
    _acquire_cloak_lane = browser.acquire_cloak_lane
    _acquire_submit_writer_lane = submission.acquire_submit_writer_lane
    _archive_worker_evidence = application.archive_worker_evidence
    _snapshot_worker_evidence = application.snapshot_worker_evidence
    _attach_control_contract = application.attach_control_contract
    _audit_live_pre_submit_page = observation.audit_live_pre_submit_page
    _classify_post_submit_observation = observation.classify_post_submit_observation
    _cloak_lane = browser.cloak_lane
    _format_failure_error = application.format_failure_error
    _is_permanent_failure = application.is_permanent_failure
    _mark_runtime_cover_not_required = jobs.mark_runtime_cover_not_required
    _click_linkedin_main_apply_causally = observation.click_linkedin_main_apply_causally
    _verify_linkedin_post_login_state = observation.verify_linkedin_post_login_state
    _observe_post_submit_page = observation.observe_post_submit_page
    _observe_linkedin_external_handoff_page = (
        observation.observe_linkedin_external_handoff_page
    )
    _resolve_ats_application_binding = application.resolve_ats_application_binding
    _run_read_only_preflight = application.run_read_only_preflight
    _prepare_ats_fill_plan_repair = application.prepare_ats_fill_plan_repair
    _try_semantic_pre_submit_repair = application.try_semantic_pre_submit_repair
    _record_ats_fill_plan_feedback = application.record_ats_fill_plan_feedback
    _prepare_runtime_cover_letter = application.prepare_runtime_cover_letter
    _admit_direct_email_receipt = submission.admit_direct_email_receipt
    _configured_receipt_observers = submission.configured_receipt_observers
    _build_receipt_observer_context = submission.build_receipt_observer_context
    _process_receipt_observer_result = submission.process_receipt_observer_result
    _reserve_manifest_submission = submission.reserve_manifest_submission
    _runtime_linkedin_route_gate = application.runtime_linkedin_route_gate
    _runtime_recovery_scope = operator.runtime_recovery_scope
    _runtime_submit_scope = operator.runtime_submit_scope
    _route_for_phase = application.route_for_phase
    _stop_event = host.stop_event
    _submission_evidence_consistent = submission.submission_evidence_consistent
    _submission_rate_status = submission.submission_rate_status
    _submit_writer_lane = submission.submit_writer_lane
    _update_submission_ledger = submission.update_submission_ledger
    _has_admitted_submission_receipt = submission.has_admitted_submission_receipt
    _issue_manual_resume_authorization = operator.issue_manual_resume_authorization
    _consume_manual_resume_authorization = operator.consume_manual_resume_authorization
    _heartbeat_operator_handoff = operator.heartbeat_operator_handoff
    _runtime_operator_resume_scope = operator.runtime_operator_resume_scope
    _runtime_audit_verification_scope = operator.runtime_audit_verification_scope
    _wait_for_manual_captcha = operator.wait_for_manual_captcha
    acquire_job = jobs.acquire_job
    add_event = jobs.add_event
    capture_browser_session = browser.capture_browser_session
    cloak_fallback_route = browser.cloak_fallback_route
    computer_use_handoff_allowed = browser.computer_use_handoff_allowed
    config = host.config
    datetime = host.datetime
    get_connection = jobs.get_connection
    initial_route = browser.initial_route
    logger = host.logger
    mark_result = jobs.mark_result
    release_lock = jobs.release_lock
    record_application_attempt_performance = (
        jobs.record_application_attempt_performance
    )
    resolve_browser_backend = browser.resolve_browser_backend
    resolve_interaction_mode = browser.resolve_interaction_mode
    restore_browser_session = browser.restore_browser_session
    restore_preview_state = jobs.restore_preview_state
    runtime_run_job = application.run_job
    update_state = jobs.update_state
    application_lease_minutes = host.load_runtime_settings().application_lease_minutes

    applied = 0
    failed = 0
    continuous = limit == 0
    jobs_done = 0
    empty_polls = 0
    profile = config.load_profile()
    submission_policy = profile.get("submission_policy", {})
    operator_handoff_timeout_seconds = max(
        60,
        min(
            int(
                submission_policy.get("manual_intervention_timeout_seconds", 1800)
                if isinstance(submission_policy, Mapping)
                else 1800
            ),
            3600,
        ),
    )

    def configured_retry_limit(name: str) -> int:
        configured = (
            submission_policy.get(name, 1)
            if isinstance(submission_policy, dict)
            else 1
        )
        return int(
            isinstance(configured, int)
            and not isinstance(configured, bool)
            and configured > 0
        )

    same_retry_limit = configured_retry_limit("same_application_retry_limit")
    new_session_retry_limit = configured_retry_limit("new_session_retry_limit")
    field_repair_limit = configured_retry_limit("field_repair_limit")
    material_regeneration_limit = configured_retry_limit(
        "material_regeneration_limit"
    )
    requested_browser_backend = resolve_browser_backend(browser_backend)
    requested_interaction_mode = resolve_interaction_mode(interaction_mode)
    run_attempted_urls = attempted_urls if attempted_urls is not None else set()
    application_supervisor: ApplicationSupervisor | None = None

    def run_job(*args, **kwargs):
        kwargs["application_supervisor"] = application_supervisor
        return runtime_run_job(*args, **kwargs)

    while not _stop_event.is_set():
        if run_progress is not None:
            if not run_progress.should_acquire():
                break
        else:
            target_progress = jobs_done if dry_run else applied
            if not continuous and target_progress >= limit:
                break

        update_state(worker_id, status="idle", job_title="", company="",
                     last_action="waiting for job", actions=0)

        if not dry_run:
            allowed, cooldown, rate_reason = _submission_rate_status(
                get_connection(), profile
            )
            if not allowed:
                add_event(f"[W{worker_id}] Rate limit reached: {rate_reason}")
                update_state(worker_id, status="done", last_action=rate_reason)
                break
            if cooldown > 0:
                add_event(f"[W{worker_id}] Submission cooldown: {cooldown:.0f}s")
                update_state(worker_id, status="idle", last_action="submission cooldown")
                if _stop_event.wait(timeout=cooldown):
                    break

        if attempted_urls_lock is None:
            excluded_urls = set(run_attempted_urls)
        else:
            with attempted_urls_lock:
                excluded_urls = set(run_attempted_urls)
        acquisition_attempt: dict[str, object] = {}
        acquire_started = time.perf_counter()
        job = None
        try:
            job = acquire_job(
                target_url=target_url,
                min_score=min_score,
                worker_id=worker_id,
                preview_only=dry_run,
                authorization_manifest=authorization_manifest,
                exclude_urls=excluded_urls,
                application_lease_minutes=application_lease_minutes,
                performance_sink=acquisition_attempt,
            )
        except Exception:
            acquisition_attempt["outcome"] = "error"
            raise
        finally:
            acquisition_attempt["worker_call_ms"] = round(
                (time.perf_counter() - acquire_started) * 1000,
                3,
            )
            acquisition_outcome = str(
                acquisition_attempt.get("outcome")
                or ("acquired" if job is not None else "empty")
            )
            if run_progress is not None:
                try:
                    run_progress.record_acquisition(
                        acquisition_attempt,
                        outcome=acquisition_outcome,
                    )
                except Exception as exc:  # noqa: BLE001 - telemetry is advisory
                    logger.warning("Could not aggregate acquisition performance: %s", exc)
        if not job:
            if run_progress is not None:
                run_progress.mark_manifest_exhausted()
                add_event(f"[W{worker_id}] Manifest exhausted")
                update_state(worker_id, status="done", last_action="manifest exhausted")
                break
            if not continuous:
                add_event(f"[W{worker_id}] Queue empty")
                update_state(worker_id, status="done", last_action="queue empty")
                break
            empty_polls += 1
            update_state(worker_id, status="idle",
                         last_action=f"polling ({empty_polls})")
            if empty_polls == 1:
                add_event(f"[W{worker_id}] Queue empty, polling every {POLL_INTERVAL}s...")
            # Use Event.wait for interruptible sleep
            if _stop_event.wait(timeout=POLL_INTERVAL):
                break  # Stop was requested during wait
            continue

        empty_polls = 0
        worker_application_index = jobs_done + 1
        preview_ticket: PreviewTicket | None = None
        if dry_run and run_progress is not None:
            preview_ticket = run_progress.claim_preview_ticket(job["url"])
            if preview_ticket is None:
                restore_preview_state(job)
                break
        initialization_complete = False
        try:
            job.setdefault(
                "_run_namespace_id",
                (
                    run_progress.run_id
                    if run_progress is not None
                    else f"worker-{worker_id}:{job.get('_attempt_id') or job['url']}"
                ),
            )
            # Each Agent turn receives a fresh output namespace.  The runtime
            # compatibility port returns an empty baseline without inspecting
            # or resetting a prior run/session directory.
            job["_evidence_baseline"] = _snapshot_worker_evidence(worker_id)
            if attempted_urls_lock is None:
                run_attempted_urls.add(str(job["url"]))
            else:
                with attempted_urls_lock:
                    run_attempted_urls.add(str(job["url"]))
            initialization_complete = True
        finally:
            if not initialization_complete:
                if dry_run:
                    restore_preview_state(job)
                else:
                    release_lock(job["url"], job.get("_attempt_id"))
                if preview_ticket is not None and run_progress is not None:
                    run_progress.release_preview_ticket(preview_ticket)

        chrome_proc = None
        application_supervisor = None
        submission_started = False
        submitted_at = None
        email_application = None
        verification_relay_used = False
        provenance_verification_child_used = False
        provenance_audit_page_url: str | None = None
        cover_material_retries_remaining = material_regeneration_limit
        field_repair_retries_remaining = field_repair_limit
        ats_fill_plan_feedback: dict[str, object] | None = None
        ledger_reserved = False
        submission_evidence: dict | None = None
        cloak_lane_held = False
        submit_writer_held = False
        submit_lane_state: dict[str, object] = {"held": False, "held_at": None}
        cloak_fallback_used = False
        route_history: list[dict[str, object]] = []
        progress_submit_claimed = False
        progress_outcome: tuple[str, bool] | None = None
        worker_generation_tainted = False
        pre_submit_audit_failure: dict[str, object] | None = None
        raw_acquisition_metrics = acquisition_attempt
        acquisition_metrics: dict[str, float | int] = {}
        if isinstance(raw_acquisition_metrics, dict):
            for key in (
                "stale_recovery_ms",
                "profile_load_ms",
                "eligibility_refresh_ms",
                "transaction_wait_ms",
                "candidate_fetch_ms",
                "candidate_rows",
                "admission_scan_ms",
                "admission_rows_scanned",
                "total_ms",
                "worker_call_ms",
            ):
                value = raw_acquisition_metrics.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                numeric = float(value)
                if math.isfinite(numeric) and numeric >= 0:
                    acquisition_metrics[key] = round(
                        min(numeric, 86_400_000.0),
                        3,
                    )
        orchestration_metrics: dict[str, float] = {
            "pre_submit_audit_ms": 0.0,
            "submission_gate_wait_ms": 0.0,
            "submit_agent_ms": 0.0,
            "post_submit_observer_ms": 0.0,
            "mailbox_receipt_observer_ms": 0.0,
            "prepare_repair_agent_ms": 0.0,
            "validation_repair_agent_ms": 0.0,
            "submit_lane_wait_ms": 0.0,
            "submit_lane_hold_ms": 0.0,
            "submit_lane_acquisitions": 0.0,
            "submit_lane_peak": 0.0,
        }

        def performance_snapshot(
            *,
            metrics=orchestration_metrics,
            acquisition=acquisition_metrics,
            lane_state=submit_lane_state,
            current_job=job,
        ) -> dict[str, object]:
            bounded = {
                key: round(max(0.0, float(value)), 3)
                for key, value in metrics.items()
            }
            held_at = lane_state.get("held_at")
            if lane_state.get("held") is True and isinstance(held_at, float):
                bounded["submit_lane_hold_ms"] = round(
                    bounded["submit_lane_hold_ms"]
                    + (time.perf_counter() - held_at) * 1000,
                    3,
                )
            snapshot: dict[str, object] = {
                "version": 1,
                "metrics": bounded,
                "acquisition": acquisition,
            }
            attribution = performance_attribution_mod.safe_attribution_snapshot(current_job)
            if attribution is not None:
                snapshot["attribution"] = attribution
            route_binding = performance_attribution_mod.safe_route_binding_snapshot(current_job)
            if route_binding is not None:
                snapshot["attribution_route"] = route_binding
            return snapshot

        def attach_performance(
            evidence: dict | None = None,
            *,
            snapshot=performance_snapshot,
        ) -> dict:
            attached = dict(evidence or {})
            attached["orchestration_performance"] = snapshot()
            return attached

        def acquire_submit_lane(
            *,
            metrics=orchestration_metrics,
            lane_state=submit_lane_state,
        ) -> bool:
            nonlocal submit_writer_held
            if submit_writer_held:
                return True
            add_event(f"[W{worker_id}] Waiting for final submit lane")
            lane_wait_started = time.perf_counter()
            acquired = _acquire_submit_writer_lane(worker_id)
            metrics["submit_lane_wait_ms"] += (
                time.perf_counter() - lane_wait_started
            ) * 1000
            if not acquired:
                return False
            metrics["submit_lane_acquisitions"] += 1
            metrics["submit_lane_peak"] = max(metrics["submit_lane_peak"], 1.0)
            submit_writer_held = True
            lane_state["held"] = True
            lane_state["held_at"] = time.perf_counter()
            return True

        def release_submit_lane(
            *,
            metrics=orchestration_metrics,
            lane_state=submit_lane_state,
        ) -> None:
            nonlocal submit_writer_held
            if not submit_writer_held:
                return
            held_at = lane_state.get("held_at")
            if isinstance(held_at, float):
                metrics["submit_lane_hold_ms"] += (
                    time.perf_counter() - held_at
                ) * 1000
            lane_state["held"] = False
            lane_state["held_at"] = None
            _submit_writer_lane.release()
            submit_writer_held = False

        try:
            read_only_preflight = _run_read_only_preflight(job)
        except Exception as exc:  # noqa: BLE001 - final atomic gate remains authoritative
            logger.warning(
                "Read-only preflight failed for %s: %s",
                job.get("url"),
                exc,
            )
            runtime_profile = profile.get("agent_runtime", {})
            orchestration_profile = (
                runtime_profile.get("orchestration", {})
                if isinstance(runtime_profile, dict)
                else {}
            )
            configured_material_mode = (
                orchestration_profile.get("material_specialist_mode", "shadow")
                if isinstance(orchestration_profile, dict)
                else "shadow"
            )
            material_mode = str(
                job.get("_material_specialist_mode") or configured_material_mode
            ).casefold().strip()
            fail_closed_mode = material_mode not in {"off", "shadow"}
            read_only_preflight = {
                "task_statuses": {},
                "error_type": type(exc).__name__,
                "material_specialist_mode": material_mode,
                "material_enforced_block": fail_closed_mode,
                "material_readiness": {
                    "state": "blocked",
                    "ready": False,
                    "missing_kinds": ["material_specialist_unavailable"],
                    "error_type": type(exc).__name__,
                },
            }
        job["_read_only_preflight"] = read_only_preflight
        duplicate_snapshot = read_only_preflight.get("duplicate")
        if (
            isinstance(duplicate_snapshot, dict)
            and duplicate_snapshot.get("clear") is False
        ):
            reason = str(
                duplicate_snapshot.get("reason") or "duplicate_submission_identity"
            )
            mark_result(
                job["url"],
                "skipped",
                reason,
                permanent=True,
                task_id=job.get("_attempt_id"),
                evidence={"duplicate_snapshot": duplicate_snapshot},
            )
            if run_progress is not None:
                run_progress.record_terminal(job["url"], "skipped")
            if preview_ticket is not None:
                run_progress.release_preview_ticket(preview_ticket)
                preview_ticket = None
            jobs_done += 1
            add_event(f"[W{worker_id}] Exact duplicate skipped before browser launch")
            update_state(
                worker_id,
                status="skipped",
                last_action="exact duplicate receipt/status",
                jobs_done=jobs_done,
            )
            continue
        if read_only_preflight.get("material_enforced_block"):
            material_readiness = read_only_preflight.get("material_readiness")
            material_readiness = (
                material_readiness if isinstance(material_readiness, dict) else {}
            )
            material_state = str(
                material_readiness.get("state") or "blocked"
            ).casefold()
            reason_codes = []
            for key in (
                "missing_kinds",
                "human_reason_codes",
                "unknown_required_labels",
            ):
                values = material_readiness.get(key)
                if isinstance(values, (list, tuple)):
                    reason_codes.extend(str(value) for value in values if str(value))
            reason = f"material_readiness_{material_state}"
            if reason_codes:
                reason += ":" + ",".join(sorted(set(reason_codes)))
            if dry_run:
                restore_preview_state(job)
            else:
                mark_result(
                    job["url"],
                    "failed",
                    reason,
                    permanent=False,
                    task_id=job.get("_attempt_id"),
                    evidence={
                        "material_readiness": material_readiness,
                        "material_task_id": read_only_preflight.get("material_task_id"),
                        "material_proposal_id": read_only_preflight.get(
                            "material_proposal_id"
                        ),
                    },
                )
            if run_progress is not None:
                run_progress.record_terminal(job["url"], "failed")
            if preview_ticket is not None:
                run_progress.release_preview_ticket(preview_ticket)
                preview_ticket = None
            failed += 1
            jobs_done += 1
            add_event(
                f"[W{worker_id}] Material specialist blocked browser launch: {reason}"
            )
            update_state(
                worker_id,
                status="failed",
                last_action=reason,
                jobs_done=jobs_done,
            )
            continue

        ats_binding = read_only_preflight.get("ats_binding")
        if (
            read_only_preflight.get("provider") == "smartrecruiters"
            and "ats_binding" not in read_only_preflight
        ):
            ats_binding = _resolve_ats_application_binding(job)
        if isinstance(ats_binding, dict):
            job["_ats_application_binding"] = ats_binding
        admitted_provider = (
            ats_binding.get("provider") or read_only_preflight.get("provider")
            if isinstance(ats_binding, Mapping)
            else read_only_preflight.get("provider")
        )
        admitted_target_url = (
            ats_binding.get("application_url") or ats_binding.get("url")
            if isinstance(ats_binding, Mapping)
            else job.get("application_url") or job.get("url")
        )
        def bind_attribution_target(
            target_url: object = admitted_target_url,
            current_job=job,
            current_provider=admitted_provider,
            current_index=worker_application_index,
            current_worker_id=worker_id,
        ) -> None:
            performance_attribution_mod.safe_bind_attempt_route(
                current_job,
                provider=current_provider,
                target_url=target_url,
                worker_application_index=current_index,
                worker_id=f"worker-{current_worker_id}",
            )

        bind_attribution_target()
        if read_only_preflight.get("provider") == "smartrecruiters" and not (
            isinstance(ats_binding, dict)
            and ats_binding.get("provider") == "smartrecruiters"
            and ats_binding.get("resolved") is True
        ):
            identity_reason = (
                str(ats_binding.get("reason") or "unavailable")
                if isinstance(ats_binding, dict)
                else "unavailable"
            )
            reason = (
                "smartrecruiters_provider_identity_unresolved:"
                f"{identity_reason}"
            )
            if dry_run:
                restore_preview_state(job)
            else:
                mark_result(
                    job["url"],
                    "failed",
                    reason,
                    permanent=False,
                    task_id=job.get("_attempt_id"),
                    evidence=attach_performance({
                        "ats_application_binding": (
                            dict(ats_binding)
                            if isinstance(ats_binding, dict)
                            else None
                        ),
                        "submit_started": False,
                    }),
                )
            if run_progress is not None:
                run_progress.record_terminal(job["url"], "failed")
            if preview_ticket is not None:
                run_progress.release_preview_ticket(preview_ticket)
                preview_ticket = None
            failed += 1
            jobs_done += 1
            add_event(
                f"[W{worker_id}] SmartRecruiters identity blocked before browser: "
                f"{identity_reason[:35]}"
            )
            update_state(
                worker_id,
                status="failed",
                last_action="SmartRecruiters identity unresolved",
                jobs_done=jobs_done,
            )
            continue
        try:
            active_route = initial_route(requested_browser_backend, phase="prepare")
            active_browser_backend = active_route.browser_runtime
            if active_browser_backend == "cloak":
                cloak_lane_held = _acquire_cloak_lane(worker_id)
            _attach_control_contract(
                job,
                active_route,
                interaction_mode=requested_interaction_mode,
                resume_existing_page=False,
            )
            route_history.append({
                **active_route.as_dict(),
                "event": "route_selected",
                "submit_started": False,
            })
            add_event(f"[W{worker_id}] Launching {active_browser_backend} browser...")
            start_url = str(job.get("application_url") or job["url"])
            initial_linkedin_entry = _url_has_host(start_url, "linkedin.com")
            attempt_id = str(
                job.get("_attempt_id")
                or f"worker-{worker_id}:{job.get('url') or start_url}"
            )
            application_supervisor = ApplicationSupervisor(
                browser_worker,
                attempt_id=attempt_id,
                actor_id=application_actor_id(attempt_id),
                start_url=start_url,
                browser_runtime=active_browser_backend,
                headless=headless,
                release_browser_authority=lambda current_job=job: (
                    browser.release_application_browser_authority(current_job)
                ),
            )
            chrome_proc = application_supervisor.process
            root_ids = set(browser_worker.active_targets)
            job["_browser_root_target_ids"] = sorted(root_ids)
            job["_browser_root_runtime"] = active_browser_backend

            submission_phase = "submit" if dry_run else "prepare"
            result = ""
            duration_ms = 0

            def record_entry_failure(
                stage: str,
                reason: object,
                current_job=job,
                current_route_history=route_history,
            ) -> None:
                reason_code = str(reason or "linkedin_entry_unknown")[:200]
                current_job["_preview_attempt_evidence"] = {
                    "version": 1,
                    "stage": str(stage or "linkedin_entry")[:80],
                    "reason_code": reason_code,
                    "submit_started": False,
                }
                current_route_history.append({
                    "event": "linkedin_entry_rejected",
                    "stage": str(stage or "linkedin_entry")[:80],
                    "reason_code": reason_code,
                    "submit_started": False,
                })
                add_event(
                    f"[W{worker_id}] LinkedIn entry blocked: {reason_code[:90]}"
                )

            def execute_entry_turn(
                current_job=job,
                *,
                linkedin_entry=initial_linkedin_entry,
                entry_phase=submission_phase,
            ) -> tuple[str, int]:
                if not linkedin_entry:
                    return run_job(
                        current_job,
                        port=port,
                        worker_id=worker_id,
                        model=model,
                        dry_run=dry_run,
                        agent_backend=agent_backend,
                        manual_captcha_relay=manual_captcha_relay,
                        submission_phase=entry_phase,
                    )
                click_signal, click_observation = (
                    _click_linkedin_main_apply_causally(
                        port, worker_id, current_job
                    )
                )
                if click_signal:
                    record_entry_failure("first_apply", click_signal)
                    return f"failed:manual_review_required:{click_signal}", 0
                disposition = str(click_observation.get("disposition") or "")
                if disposition == "linkedin_external_handoff":
                    return "linkedin_external_handoff", 0
                if disposition == "linkedin_native_apply_opened":
                    return run_job(
                        current_job,
                        port=port,
                        worker_id=worker_id,
                        model=model,
                        dry_run=dry_run,
                        agent_backend=agent_backend,
                        manual_captcha_relay=manual_captcha_relay,
                        resume_existing_page=True,
                        submission_phase=entry_phase,
                    )
                if disposition != "linkedin_login_required":
                    record_entry_failure(
                        "first_apply", "linkedin_apply_click_unknown"
                    )
                    return "failed:manual_review_required:linkedin_apply_click_unknown", 0

                current_job["_linkedin_login_only"] = True
                login_result, login_duration = run_job(
                    current_job,
                    port=port,
                    worker_id=worker_id,
                    model=model,
                    dry_run=dry_run,
                    agent_backend=agent_backend,
                    manual_captcha_relay=manual_captcha_relay,
                    resume_existing_page=True,
                    submission_phase="prepare",
                )
                current_job.pop("_linkedin_login_only", None)
                if login_result != "linkedin_login_completed":
                    return login_result, login_duration
                login_verified, login_reason = _verify_linkedin_post_login_state(
                    port, worker_id, current_job
                )
                if not login_verified:
                    record_entry_failure("post_login_guard", login_reason)
                    return f"failed:manual_review_required:{login_reason}", login_duration
                second_signal, second_observation = (
                    _click_linkedin_main_apply_causally(
                        port, worker_id, current_job
                    )
                )
                if second_signal:
                    record_entry_failure("second_apply", second_signal)
                    return (
                        f"failed:manual_review_required:{second_signal}",
                        login_duration,
                    )
                second_disposition = str(
                    second_observation.get("disposition") or ""
                )
                if second_disposition == "linkedin_external_handoff":
                    return "linkedin_external_handoff", login_duration
                if second_disposition == "linkedin_native_apply_opened":
                    normal_result, normal_duration = run_job(
                        current_job,
                        port=port,
                        worker_id=worker_id,
                        model=model,
                        dry_run=dry_run,
                        agent_backend=agent_backend,
                        manual_captcha_relay=manual_captcha_relay,
                        resume_existing_page=True,
                        submission_phase=entry_phase,
                    )
                    return normal_result, login_duration + normal_duration
                record_entry_failure(
                    "second_apply", "linkedin_second_apply_unknown"
                )
                return (
                    "failed:manual_review_required:linkedin_second_apply_unknown",
                    login_duration,
                )

            job["_application_actor_same_application_retries_remaining"] = (
                same_retry_limit
            )
            job["_application_actor_new_session_retries_remaining"] = min(
                new_session_retry_limit,
                int(
                    requested_browser_backend == "auto"
                    and active_browser_backend == "edge"
                    and not cloak_fallback_used
                ),
            )
            result, duration_ms = execute_entry_turn()

            raw_turn = job.get("_agent_turn_result")
            run_id = (
                str(raw_turn.get("run_id") or "")
                if isinstance(raw_turn, dict)
                else ""
            ) or f"worker-{worker_id}:prepare"
            actor_attempt_id = str(
                job.get("_attempt_id") or job.get("url") or f"worker-{worker_id}:attempt"
            )
            actor_state = application_actor_mod.ApplicationActorState(
                run_id=run_id,
                attempt_id=actor_attempt_id,
                application_id=str(job.get("url") or actor_attempt_id),
                page_id=str(job.get("application_url") or job.get("url") or actor_attempt_id),
                write_owner=str(getattr(active_route, "submit_owner", "playwright")),
                phase="verify",
                submission_uncertain=str(result).casefold() == "submission_uncertain",
                same_application_retries_remaining=int(
                    job["_application_actor_same_application_retries_remaining"]
                ),
                new_session_retries_remaining=int(
                    job["_application_actor_new_session_retries_remaining"]
                ),
            )
            actor_decision = application_actor_mod.decision_for_status(actor_state, result)
            job["_application_actor_decision"] = contract_json(actor_decision)
            recovery_action = actor_decision.recovery_action
            recovery_admission = None
            recovery_command = None
            if recovery_action is not None:
                recovery_admission = recovery_execution_mod.admit_recovery_decision(
                    actor_decision,
                    submit_started=submission_started,
                    operator_context=operator_resume_binding(actor_decision, job),
                )
                job["_application_recovery_admission"] = contract_json(
                    recovery_admission
                )
                recovery_command = recovery_admission.command
                if recovery_command is not None:
                    job["_application_recovery_command"] = contract_json(
                        recovery_command
                    )
            fallback_route = None
            if (
                recovery_command is not None
                and recovery_command.command == "retry_new_session"
            ):
                fallback_route = cloak_fallback_route(
                    result,
                    requested_browser_backend=requested_browser_backend,
                    phase="prepare",
                    current_runtime=active_browser_backend,
                    fallback_already_used=cloak_fallback_used,
                )

            def execute_browser_recovery(
                _command,
                current_job=job,
                current_fallback_route=fallback_route,
                current_route_history=route_history,
                current_start_url=start_url,
                current_supervisor=application_supervisor,
            ):
                nonlocal active_browser_backend
                nonlocal active_route
                nonlocal chrome_proc
                nonlocal cloak_fallback_used
                nonlocal cloak_lane_held
                nonlocal duration_ms
                nonlocal result

                if current_fallback_route is None:
                    raise RuntimeError("browser_recovery_route_unavailable")
                edge_block_result = result
                cloak_fallback_used = True
                cloak_lane_held = _acquire_cloak_lane(worker_id)
                active_route = current_fallback_route
                active_browser_backend = active_route.browser_runtime
                _attach_control_contract(
                    current_job,
                    active_route,
                    interaction_mode=requested_interaction_mode,
                    resume_existing_page=False,
                )
                add_event(
                    f"[W{worker_id}] Explicit browser block detected; "
                    "retrying once with CloakBrowser"
                )
                update_state(
                    worker_id,
                    status="retrying",
                    last_action="switching to CloakBrowser",
                )
                try:
                    edge_session = capture_browser_session(
                        port,
                        [
                            str(current_job.get("url") or ""),
                            str(current_job.get("application_url") or ""),
                        ],
                    )
                    if current_supervisor is None:
                        raise RuntimeError("application_supervisor_unavailable")
                    chrome_proc = current_supervisor.restart_browser(
                        active_browser_backend,
                        headless=headless,
                    )
                    restored_cookies = restore_browser_session(
                        port,
                        edge_session,
                        current_start_url,
                    )
                    root_ids = set(browser_worker.active_targets)
                    current_job["_browser_root_target_ids"] = sorted(root_ids)
                    current_job["_browser_root_runtime"] = active_browser_backend
                    logger.info(
                        "[worker-%d] Bridged %d browser cookies into CloakBrowser",
                        worker_id,
                        restored_cookies,
                    )
                    current_route_history.append({
                        **active_route.as_dict(),
                        "event": "runtime_transition",
                        "from": "playwright/edge",
                        "to": "playwright/cloak",
                        "trigger_result": edge_block_result,
                        "session_cookies_restored": restored_cookies,
                        "submit_started": False,
                    })
                    current_job["_application_actor_new_session_retries_remaining"] = 0
                    with _runtime_recovery_scope(_command):
                        result, stealth_duration = execute_entry_turn()
                    duration_ms += stealth_duration
                except Exception as exc:
                    logger.exception("CloakBrowser fallback failed")
                    current_route_history.append({
                        **active_route.as_dict(),
                        "event": "runtime_transition_failed",
                        "from": "playwright/edge",
                        "to": "playwright/cloak",
                        "trigger_result": edge_block_result,
                        "error_type": type(exc).__name__,
                        "submit_started": False,
                    })
                    result = f"failed:cloak_backend_unavailable:{type(exc).__name__}"
                    raise
                logger.info(
                    "[worker-%d] Browser fallback edge_result=%s cloak_result=%s",
                    worker_id,
                    edge_block_result,
                    result,
                )
                return {
                    "browser_runtime": active_browser_backend,
                    "fallback_applied": True,
                    "recovery_turn_completed": bool(
                        current_job.get("_parent_agent_checkpoint_id")
                        and current_job.get("_parent_agent_run_id") != _command.turn_id
                    ),
                    "result_category": classify_failure(result).category,
                }

            def execute_same_application_recovery(
                _command,
                current_job=job,
                current_submit_started=submission_started,
                current_route=active_route,
                current_metrics=orchestration_metrics,
            ):
                """Re-observe and repair the same page once before any Submit."""
                nonlocal duration_ms
                nonlocal result

                if current_submit_started:
                    raise RuntimeError("same_application_retry_forbidden_after_submit")
                previous_result = result
                failure = classify_failure(previous_result)
                current_job["_application_actor_same_application_retries_remaining"] = 0
                current_job["_browser_observation"] = {
                    "recovery_mode": "same_application",
                    "signal": failure.category,
                    "next_action": failure.next_action,
                    "submit_started": False,
                    "submission_gate": False,
                }
                if failure.recoverability == "retry_with_larger_runtime_budget":
                    current_job["_agent_timeout_multiplier"] = 2
                _attach_control_contract(
                    current_job,
                    current_route,
                    interaction_mode=requested_interaction_mode,
                    resume_existing_page=True,
                )
                add_event(
                    f"[W{worker_id}] One same-page recovery: "
                    f"{failure.category[:45]}"
                )
                update_state(
                    worker_id,
                    status="retrying",
                    last_action="one bounded same-page repair",
                )
                with _runtime_recovery_scope(_command):
                    result, retry_duration = run_job(
                        current_job,
                        port=port,
                        worker_id=worker_id,
                        model=model,
                        dry_run=dry_run,
                        agent_backend=agent_backend,
                        manual_captcha_relay=manual_captcha_relay,
                        resume_existing_page=True,
                        submission_phase="prepare",
                    )
                duration_ms += retry_duration
                current_metrics["prepare_repair_agent_ms"] += max(
                    0, retry_duration
                )
                return {
                    "same_application_retry": True,
                    "submit_started": False,
                    "recovery_turn_completed": bool(
                        current_job.get("_parent_agent_checkpoint_id")
                        and current_job.get("_parent_agent_run_id") != _command.turn_id
                    ),
                    "previous_category": failure.category,
                    "result_category": classify_failure(result).category,
                }

            recovery_result = None
            if recovery_command is not None:
                if dry_run:
                    if recovery_command.command == "retry_new_session":
                        try:
                            execute_browser_recovery(recovery_command)
                        except Exception:
                            logger.debug("Dry-run browser recovery failed closed", exc_info=True)
                else:
                    recovery_handler = None
                    if recovery_command.command == "retry_new_session":
                        recovery_handler = execute_browser_recovery
                    elif recovery_command.command == "retry_same_application":
                        recovery_handler = execute_same_application_recovery
                    recovery_verifier = None
                    if recovery_command.command == "retry_new_session":
                        def verify_browser_recovery(_command, details):
                            return (
                                details.get("browser_runtime") == "cloak"
                                and details.get("fallback_applied") is True
                                and details.get("recovery_turn_completed") is True
                            )

                        recovery_verifier = verify_browser_recovery
                    elif recovery_command.command == "retry_same_application":
                        def verify_same_application_recovery(_command, details):
                            return (
                                details.get("same_application_retry") is True
                                and details.get("submit_started") is False
                                and details.get("recovery_turn_completed") is True
                            )

                        recovery_verifier = verify_same_application_recovery
                    try:
                        recovery_result = recovery_execution_mod.execute_recovery_command(
                            get_connection(),
                            recovery_command,
                            handler=recovery_handler,
                            verifier=recovery_verifier,
                        )
                        job["_application_recovery_execution"] = contract_json(
                            recovery_result
                        )
                    except Exception as exc:  # noqa: BLE001 - persistence must fail closed
                        logger.warning(
                            "Recovery command persistence failed for %s: %s",
                            actor_attempt_id,
                            type(exc).__name__,
                        )
                        job["_application_recovery_execution"] = {
                            "stage": "failed",
                            "outcome": "recovery_control_persistence_failed",
                            "error_type": type(exc).__name__,
                        }

            operator_exception_id = None
            operator_request_id = None
            operator_handoff_used = False
            if (
                not dry_run
                and recovery_command is not None
                and recovery_command.command == "enqueue_human_handoff"
                and recovery_result is not None
                and recovery_result.stage == "verified"
                and recovery_result.terminal_status == "completed"
                and set(recovery_command.payload) >= {
                    "request_id",
                    "checkpoint_id",
                    "job_url",
                    "profile_id",
                    "browser_lease_id",
                    "browser_lease_epoch",
                    "page_target_id",
                    "page_epoch",
                }
            ):
                operator_exception_id = exception_id_for_command(
                    recovery_command.command_id
                )
                operator_request_id = str(recovery_command.payload["request_id"])

            def execute_operator_resume(
                operator_command,
                resume_context,
                current_job=job,
                current_route=active_route,
            ):
                """Run the sole page-owning, prepare-only child for one response."""
                nonlocal duration_ms
                nonlocal result

                checkpoint_id = str(resume_context.get("checkpoint_ref") or "")
                _attach_control_contract(
                    current_job,
                    current_route,
                    interaction_mode=requested_interaction_mode,
                    resume_existing_page=True,
                )
                with _runtime_operator_resume_scope(
                    operator_command,
                    checkpoint_id=checkpoint_id,
                    resume_context=resume_context,
                ):
                    result, resumed_duration = run_job(
                        current_job,
                        port=port,
                        worker_id=worker_id,
                        model=model,
                        dry_run=False,
                        agent_backend=agent_backend,
                        manual_captcha_relay=manual_captcha_relay,
                        resume_existing_page=True,
                        submission_phase="prepare",
                    )
                duration_ms += resumed_duration
                child_turn_id = str(current_job.get("_parent_agent_run_id") or "")
                return verified_child_execution(
                    get_connection(),
                    actor_id=operator_command.actor_id,
                    attempt_id=operator_command.attempt_id,
                    parent_turn_id=operator_command.run_id,
                    child_turn_id=child_turn_id,
                )

            if initial_linkedin_entry:
                route_signal, route_observation = _observe_linkedin_external_handoff_page(
                    port,
                    worker_id,
                    job,
                )
                observed_external_route = (
                    isinstance(route_observation, dict)
                    and route_observation.get("disposition")
                    == "linkedin_external_handoff"
                    and bool(str(route_observation.get("page_url") or "").strip())
                )
                if observed_external_route:
                    route_allowed, route_reason = _runtime_linkedin_route_gate(
                        job,
                        route_observation,
                        profile,
                        persist_external_handoff=not dry_run,
                    )
                    if (
                        not route_allowed
                        and route_reason
                        in {
                            "linkedin_external_handoff_reauthorized",
                            "linkedin_external_handoff_preview_verified",
                        }
                    ):
                        external_url = str(
                            job.get("_discovered_application_url") or ""
                        ).strip()
                        runtime_binding = job.get("_linkedin_runtime_route_binding")
                        binding_attestation = (
                            runtime_binding.get("causal_apply_attestation")
                            if isinstance(runtime_binding, dict)
                            else None
                        )
                        private_attestation = job.get(
                            "_linkedin_causal_apply_attestation"
                        )
                        route_can_continue = (
                            bool(external_url)
                            and isinstance(runtime_binding, dict)
                            and runtime_binding.get("lineage_verified") is True
                            and runtime_binding.get("target_application_url")
                            == external_url
                            and isinstance(binding_attestation, dict)
                            and binding_attestation.get("verified") is True
                            and isinstance(private_attestation, dict)
                            and binding_attestation.get("target_id_digest")
                            == private_attestation.get("target_id_digest")
                        )
                        if route_can_continue:
                            job["application_url"] = external_url
                            ats_binding = _resolve_ats_application_binding(job)
                            if ats_binding is not None:
                                job["_ats_application_binding"] = ats_binding
                            if _url_has_host(
                                external_url, "smartrecruiters.com"
                            ) and not (
                                isinstance(ats_binding, dict)
                                and ats_binding.get("provider") == "smartrecruiters"
                                and ats_binding.get("resolved") is True
                            ):
                                identity_reason = (
                                    str(ats_binding.get("reason") or "unavailable")
                                    if isinstance(ats_binding, dict)
                                    else "unavailable"
                                )
                                result = (
                                    "deferred:smartrecruiters_provider_identity_"
                                    f"unresolved:{identity_reason}"
                                )
                                route_can_continue = False
                                add_event(
                                    f"[W{worker_id}] SmartRecruiters route saved; "
                                    "provider identity must resolve before resume"
                                )

                        if route_can_continue:
                            _attach_control_contract(
                                job,
                                active_route,
                                interaction_mode=requested_interaction_mode,
                                resume_existing_page=True,
                            )
                            add_event(
                                f"[W{worker_id}] Continuing verified LinkedIn ATS application"
                            )
                            result, resumed_duration = run_job(
                                job,
                                port=port,
                                worker_id=worker_id,
                                model=model,
                                dry_run=dry_run,
                                agent_backend=agent_backend,
                                manual_captcha_relay=manual_captcha_relay,
                                resume_existing_page=True,
                                submission_phase=submission_phase,
                            )
                            duration_ms += resumed_duration
                        elif external_url and not result.startswith("deferred:"):
                            result = f"deferred:{route_reason}"
                            add_event(
                                f"[W{worker_id}] LinkedIn ATS route saved without a unique "
                                "same-attempt lineage; retrying from the exact ATS target"
                            )
                        elif not external_url:
                            result = (
                                "failed:manual_review_required:"
                                "linkedin_external_target_missing"
                            )
                    elif not route_allowed:
                        result = f"failed:manual_review_required:{route_reason}"
                    else:
                        result = (
                            "failed:manual_review_required:"
                            "linkedin_external_handoff_marker_without_external_route"
                        )
                    if route_signal:
                        logger.info(
                            "[worker-%d] LinkedIn handoff observation signal=%s reason=%s",
                            worker_id,
                            route_signal,
                            route_reason,
                        )
                elif result == "linkedin_external_handoff":
                    result = (
                        "failed:manual_review_required:"
                        "linkedin_external_handoff_marker_without_external_route"
                    )
                    if route_signal:
                        logger.info(
                            "[worker-%d] LinkedIn handoff marker lacked external page: %s",
                            worker_id,
                            route_signal,
                        )

            if computer_use_handoff_allowed(
                result,
                interaction_mode=requested_interaction_mode,
                phase="prepare",
                submit_started=submission_started,
            ):
                route_history.append({
                    "contract_version": 1,
                    "interaction_driver": "computer_use",
                    "browser_runtime": active_browser_backend,
                    "phase": "prepare",
                    "reason_code": "external_visual_handoff_requested",
                    "event": "handoff_requested",
                    "submit_started": False,
                    "requires_fresh_observation": True,
                })
                add_event(
                    f"[W{worker_id}] Computer Use handoff requested; "
                    "preserving fail-closed result"
                )

            while True:
                if result == "prepared_for_audit":
                    if dry_run or provenance_verification_child_used:
                        result = "failed:prepared_for_audit_not_admitted"
                        break
                    if _prepared_email_application(job) is not None:
                        result = "failed:prepared_for_audit_not_admitted"
                        break
                    audit_started = time.perf_counter()
                    audit_signal, audit_report = _audit_live_pre_submit_page(
                        port, worker_id, job
                    )
                    audit_signal, audit_report = _enforce_stateful_control_coverage(
                        audit_signal, audit_report
                    )
                    bind_attribution_target(
                        audit_report.get("page_url") or admitted_target_url
                    )
                    audit_duration_ms = (time.perf_counter() - audit_started) * 1000
                    orchestration_metrics["pre_submit_audit_ms"] += audit_duration_ms
                    performance_attribution_mod.safe_record_job_span(
                        job, "audit.pre_submit", audit_duration_ms
                    )
                    if not _provenance_only_audit(audit_report):
                        pre_submit_audit_failure = dict(audit_report)
                        result = (
                            "failed:manual_review_required:"
                            f"{audit_signal or 'prepared_for_audit_not_provenance_only'}"
                        )
                        break
                    provenance_verification_child_used = True
                    provenance_audit_page_url = str(audit_report["page_url"])
                    verification_job = dict(job)
                    for protected_key in (
                        "_browser_lease_binding",
                        "_answer_provenance_binding",
                        "_agent_observations",
                    ):
                        if protected_key in verification_job:
                            verification_job[protected_key] = deepcopy(
                                verification_job[protected_key]
                            )
                    verification_job["_answer_provenance_verification_child"] = True
                    verification_job["_browser_observation"] = {
                        **audit_report,
                        "signal": audit_signal,
                        "advisory_only": False,
                        "submission_gate": False,
                    }
                    _attach_control_contract(
                        verification_job,
                        active_route,
                        interaction_mode=requested_interaction_mode,
                        resume_existing_page=True,
                    )
                    with _runtime_audit_verification_scope(verification_job):
                        result, verification_duration = run_job(
                            verification_job,
                            port=port,
                            worker_id=worker_id,
                            model=model,
                            dry_run=False,
                            agent_backend=agent_backend,
                            manual_captcha_relay=False,
                            resume_existing_page=True,
                            submission_phase="prepare",
                        )
                    duration_ms += verification_duration
                    provenance_error = _consume_provenance_repair_artifacts(
                        job, verification_job
                    )
                    if provenance_error is not None:
                        result = (
                            "failed:manual_review_required:"
                            f"answer_provenance_verification:{provenance_error}"
                        )
                        break
                    for parent_key in (
                        "_parent_agent_run_id",
                        "_parent_agent_checkpoint_id",
                    ):
                        if verification_job.get(parent_key):
                            job[parent_key] = verification_job[parent_key]
                    if result != "ready_to_submit":
                        result = (
                            "failed:answer_provenance_verification:"
                            f"{result}"
                        )
                        break

                if result in {"cover_not_required", "cover_letter_required"}:
                    if cover_material_retries_remaining <= 0:
                        result = "failed:cover_material_discovery_loop"
                        break
                    cover_material_retries_remaining -= 1
                    try:
                        if result == "cover_not_required":
                            job = _mark_runtime_cover_not_required(job)
                            add_event(f"[W{worker_id}] ATS confirmed no cover letter is required")
                        else:
                            add_event(f"[W{worker_id}] ATS requires a cover letter; generating it")
                            update_state(
                                worker_id,
                                status="preparing_material",
                                last_action="generating validated cover letter",
                            )
                            job = _prepare_runtime_cover_letter(job)
                        # Runtime cover resolution reloads the database row.
                        # Re-attach the non-persisted browser policy before the
                        # agent resumes on the existing Cloak/Edge page.
                        _attach_control_contract(
                            job,
                            active_route,
                            interaction_mode=requested_interaction_mode,
                            resume_existing_page=True,
                        )
                        result, resumed_duration = run_job(
                            job,
                            port=port,
                            worker_id=worker_id,
                            model=model,
                            dry_run=False,
                            agent_backend=agent_backend,
                            manual_captcha_relay=manual_captcha_relay,
                            resume_existing_page=True,
                            submission_phase="prepare",
                        )
                        duration_ms += resumed_duration
                        continue
                    except Exception as exc:
                        logger.exception("Runtime cover-letter resolution failed")
                        result = f"failed:manual_review_required:cover_letter_generation:{type(exc).__name__}"
                        break

                if result == "captcha":
                    resume_authorization = (
                        _issue_manual_resume_authorization(
                            job,
                            submit_started=False,
                        )
                        if manual_captcha_relay and not verification_relay_used
                        else None
                    )
                    manual_relay_succeeded = (
                        resume_authorization is not None
                        and _wait_for_manual_captcha(
                            port,
                            worker_id,
                            attempt_id=job.get("_attempt_id"),
                            submit_started=False,
                            root_target_ids=set(job.get("_browser_root_target_ids") or []),
                            application_lease_minutes=application_lease_minutes,
                        )
                        and _consume_manual_resume_authorization(
                            job,
                            resume_authorization,
                            submit_started=False,
                        )
                    )
                    if manual_relay_succeeded:
                        if operator_exception_id and operator_request_id:
                            try:
                                OperatorRuntime(get_connection()).expire_resume_request(
                                    operator_exception_id,
                                    request_id=operator_request_id,
                                )
                                operator_handoff_used = True
                            except Exception as exc:  # noqa: BLE001 - fail closed
                                logger.warning(
                                    "Could not expire superseded operator handoff %s: %s",
                                    operator_exception_id,
                                    type(exc).__name__,
                                )
                                result = (
                                    "failed:manual_review_required:"
                                    "operator_handoff_expiry_failed"
                                )
                                break
                        verification_relay_used = True
                        resumed_job = dict(job)
                        resumed_job["_browser_observation"] = {
                            "verification_resume": True,
                            "signal": "manual_verification_cleared",
                            "resume_authorization_id": resume_authorization.get(
                                "authorization_id"
                            ),
                            "submission_gate": True,
                        }
                        _attach_control_contract(
                            resumed_job,
                            active_route,
                            interaction_mode=requested_interaction_mode,
                            resume_existing_page=True,
                        )
                        result, resumed_duration = run_job(
                            resumed_job,
                            port=port,
                            worker_id=worker_id,
                            model=model,
                            dry_run=dry_run,
                            agent_backend=agent_backend,
                            manual_captcha_relay=manual_captcha_relay,
                            resume_existing_page=True,
                            submission_phase=submission_phase,
                        )
                        duration_ms += resumed_duration
                        continue
                    if resume_authorization is not None:
                        if operator_exception_id and operator_request_id:
                            try:
                                OperatorRuntime(get_connection()).expire_resume_request(
                                    operator_exception_id,
                                    request_id=operator_request_id,
                                )
                                operator_handoff_used = True
                            except Exception:
                                logger.warning(
                                    "Manual relay ended with an unexpired operator handoff",
                                    exc_info=True,
                                )
                                result = (
                                    "failed:manual_review_required:"
                                    "operator_handoff_expiry_failed"
                                )
                        break
                    if not operator_exception_id or not operator_request_id:
                        break

                if (
                    operator_exception_id is not None
                    and operator_request_id is not None
                    and not operator_handoff_used
                ):
                    operator_handoff_used = True
                    add_event(
                        f"[W{worker_id}] Waiting for one exact operator response; "
                        "browser lease remains owned"
                    )
                    update_state(
                        worker_id,
                        status="human_wait",
                        last_action="waiting for exact operator response",
                    )
                    try:
                        handoff = wait_for_requested_resume(
                            get_connection(),
                            exception_id=operator_exception_id,
                            request_id=operator_request_id,
                            resume_owner=execute_operator_resume,
                            heartbeat=lambda current_job=job: _heartbeat_operator_handoff(
                                current_job,
                                lease_minutes=application_lease_minutes,
                            ),
                            stop_wait=lambda seconds: _stop_event.wait(
                                timeout=seconds
                            ),
                            timeout_seconds=operator_handoff_timeout_seconds,
                        )
                    except Exception as exc:  # noqa: BLE001 - dispatcher fails closed
                        logger.warning(
                            "Operator handoff failed closed for %s: %s",
                            operator_exception_id,
                            type(exc).__name__,
                        )
                        try:
                            OperatorRuntime(get_connection()).expire_resume_request(
                                operator_exception_id,
                                request_id=operator_request_id,
                            )
                        except Exception:
                            logger.warning(
                                "Operator handoff could not expire after dispatcher error",
                                exc_info=True,
                            )
                        result = (
                            "failed:manual_review_required:"
                            f"operator_handoff_error:{type(exc).__name__}"
                        )
                        break
                    job["_operator_handoff_result"] = {
                        "status": handoff.status,
                        "exception_id": operator_exception_id,
                        "request_id": operator_request_id,
                        "submit_authority": False,
                    }
                    if handoff.status == "resumed":
                        continue
                    result = (
                        "failed:manual_review_required:"
                        f"operator_resume_{handoff.status}"
                    )
                    break

                if result == "ready_to_submit" and not dry_run:
                    audit_started = time.perf_counter()
                    email_application = _prepared_email_application(job)
                    reported_observations = job.get("_agent_observations")
                    reported_email_application = (
                        reported_observations.get("email_application")
                        if isinstance(reported_observations, dict)
                        else None
                    )
                    if (
                        email_application is None
                        and isinstance(reported_email_application, dict)
                        and reported_email_application.get("route") == "direct_email"
                    ):
                        result = "failed:email_plan_unverified"
                        break
                    if email_application is not None:
                        audit_signal = None
                        audit_report = {
                            "status": "clear",
                            "disposition": "clear",
                            "page_url": str(job.get("application_url") or job.get("url") or ""),
                            "issues": [],
                            "blocking_issues": [],
                            "repairable_issues": [],
                            "advisory_issues": [],
                            "lossy_answer_mappings": [],
                            "advisory_only": False,
                            "submission_gate": True,
                            "email_application": email_application,
                        }
                    else:
                        audit_signal, audit_report = _audit_live_pre_submit_page(
                            port, worker_id, job
                        )
                        audit_signal, audit_report = _enforce_stateful_control_coverage(
                            audit_signal, audit_report
                        )
                        if (
                            provenance_audit_page_url is not None
                            and str(audit_report.get("page_url") or "")
                            != provenance_audit_page_url
                        ):
                            audit_signal = "answer_provenance_verification:page_drift"
                            audit_report = {
                                **audit_report,
                                "disposition": "block",
                                "blocking_issues": ["page_drift"],
                            }
                    if email_application is not None:
                        # The normalized plan proves the mailbox channel and the
                        # admitted posting/application target remains the durable
                        # attribution target for this terminal attempt.
                        performance_attribution_mod.safe_bind_attempt_route(
                            job,
                            provider="direct_email",
                            target_url=admitted_target_url,
                            worker_application_index=worker_application_index,
                            worker_id=f"worker-{worker_id}",
                        )
                    else:
                        bind_attribution_target(
                            audit_report.get("page_url") or admitted_target_url
                        )
                    audit_duration_ms = (time.perf_counter() - audit_started) * 1000
                    orchestration_metrics["pre_submit_audit_ms"] += audit_duration_ms
                    performance_attribution_mod.safe_record_job_span(
                        job, "audit.pre_submit", audit_duration_ms
                    )
                    observation_label = audit_signal or "clear"
                    add_event(
                        f"[W{worker_id}] Browser observation: {observation_label[:45]}"
                    )
                    update_state(
                        worker_id,
                        status="observed",
                        last_action=f"browser signal: {observation_label[:25]}",
                    )
                    if ats_fill_plan_feedback is not None:
                        _record_ats_fill_plan_feedback(
                            ats_fill_plan_feedback,
                            event="changed_decision",
                            audit_report=audit_report,
                        )
                        ats_fill_plan_feedback = None
                    if audit_report.get("disposition") == "retry_prepare":
                        repairable = [
                            str(issue)
                            for issue in audit_report.get("repairable_issues", [])
                        ]
                        if field_repair_retries_remaining <= 0:
                            reason = repairable[0] if repairable else "pre_submit_state"
                            result = f"failed:pre_submit_not_ready:{reason}"
                            break
                        field_repair_retries_remaining -= 1
                        resume_repair_only = bool(repairable) and all(
                            issue in {
                                "resume_not_uploaded",
                                "resume_state_unconfirmed",
                            }
                            for issue in repairable
                        )
                        if resume_repair_only:
                            try:
                                semantic_repair = _try_semantic_pre_submit_repair(
                                    port,
                                    worker_id,
                                    job,
                                    authorization_manifest,
                                    audit_report,
                                )
                            except Exception as exc:  # noqa: BLE001 - fail closed
                                logger.warning(
                                    "Semantic pre-submit repair failed for %s: %s",
                                    job.get("url"),
                                    exc,
                                )
                                result = (
                                    "failed:manual_review_required:"
                                    f"semantic_resume_repair:{type(exc).__name__}"
                                )
                                break
                            semantic_status = str(
                                semantic_repair.get("status")
                                if isinstance(semantic_repair, dict)
                                else "invalid_result"
                            )
                            if semantic_status in {"verified", "replayed"}:
                                add_event(
                                    f"[W{worker_id}] Semantic resume repair: "
                                    f"{semantic_status}"
                                )
                                continue
                            safe_legacy_fallback = bool(
                                isinstance(semantic_repair, dict)
                                and semantic_repair.get("legacy_fallback_safe") is True
                            )
                            if not (
                                semantic_status == "not_applicable"
                                and safe_legacy_fallback
                            ):
                                result = (
                                    "failed:manual_review_required:"
                                    f"semantic_resume_repair:{semantic_status}"
                                )
                                break
                        specialist_repair: dict[str, object] | None = None
                        fill_snapshot = audit_report.get("ats_fill_plan_snapshot")
                        ordinary_dynamic_repair = any(
                            issue.startswith("required_field_empty:")
                            for issue in repairable
                        )
                        snapshot_target = (
                            fill_snapshot.get("target_url")
                            if isinstance(fill_snapshot, dict)
                            else None
                        )
                        if (
                            isinstance(fill_snapshot, dict)
                            and ordinary_dynamic_repair
                            and not _url_has_host(snapshot_target, "linkedin.com")
                        ):
                            try:
                                specialist_repair = _prepare_ats_fill_plan_repair(
                                    job, audit_report
                                )
                            except Exception as exc:  # noqa: BLE001 - fail closed
                                logger.warning(
                                    "ATS fill-plan repair failed for %s: %s",
                                    job.get("url"),
                                    exc,
                                )
                                result = (
                                    "failed:manual_review_required:"
                                    f"ats_fill_plan_specialist:{type(exc).__name__}"
                                )
                                break
                        repair_job = dict(job)
                        for protected_key in (
                            "_browser_lease_binding",
                            "_answer_provenance_binding",
                            "_agent_observations",
                        ):
                            if protected_key in repair_job:
                                repair_job[protected_key] = deepcopy(
                                    repair_job[protected_key]
                                )
                        repair_job["_browser_observation"] = {
                            **audit_report,
                            "signal": audit_signal,
                            "repair_prepare": True,
                            "advisory_only": False,
                            "submission_gate": False,
                        }
                        if specialist_repair is not None:
                            repair_context = specialist_repair.get("context")
                            repair_feedback = specialist_repair.get("feedback")
                            if not isinstance(repair_context, dict) or not isinstance(
                                repair_feedback, dict
                            ):
                                result = (
                                    "failed:manual_review_required:"
                                    "ats_fill_plan_contract"
                                )
                                break
                            repair_job["_ats_fill_plan_context"] = repair_context
                            ats_fill_plan_feedback = repair_feedback
                        _attach_control_contract(
                            repair_job,
                            active_route,
                            interaction_mode=requested_interaction_mode,
                            resume_existing_page=True,
                        )
                        add_event(
                            f"[W{worker_id}] One prepare repair: "
                            f"{','.join(repairable[:2])[:45]}"
                        )
                        result, repair_duration = run_job(
                            repair_job,
                            port=port,
                            worker_id=worker_id,
                            model=model,
                            dry_run=False,
                            agent_backend=agent_backend,
                            manual_captcha_relay=manual_captcha_relay,
                            resume_existing_page=True,
                            submission_phase="prepare",
                        )
                        duration_ms += repair_duration
                        orchestration_metrics["prepare_repair_agent_ms"] += max(
                            0, repair_duration
                        )
                        provenance_repair_error = _consume_provenance_repair_artifacts(
                            job, repair_job
                        )
                        if provenance_repair_error is not None:
                            result = (
                                "failed:manual_review_required:"
                                f"answer_provenance_repair:{provenance_repair_error}"
                            )
                            break
                        if ats_fill_plan_feedback is not None:
                            accepted = repair_job.get("_ats_fill_plan_consumed")
                            binding_matches = (
                                isinstance(accepted, dict)
                                and accepted.get("accepted") is True
                                and all(
                                    str(accepted.get(key) or "")
                                    == str(ats_fill_plan_feedback.get(key) or "")
                                    for key in (
                                        "snapshot_ref",
                                        "snapshot_sha256",
                                        "plan_sha256",
                                    )
                                )
                            )
                            if not binding_matches:
                                ats_fill_plan_feedback = None
                                result = (
                                    "failed:manual_review_required:"
                                    "ats_fill_plan_not_consumed"
                                )
                                break
                            _record_ats_fill_plan_feedback(
                                ats_fill_plan_feedback,
                                event="consumed",
                            )
                        continue
                    if audit_signal:
                        pre_submit_audit_failure = dict(audit_report)
                        result = f"failed:manual_review_required:{audit_signal}"
                        break
                    # Expensive page inspection and repair stay outside the
                    # process-global lane.  The lane begins only at the final
                    # lease/capacity/reservation check that can admit Submit.
                    if not acquire_submit_lane():
                        result = "deferred:operator_stop_before_submit_lane"
                        break
                    from applypilot.database import update_application_attempt

                    attempt_id = job.get("_attempt_id")
                    lease_held = not attempt_id or update_application_attempt(
                        attempt_id,
                        phase="reservation",
                        submit_started=False,
                        evidence={
                            "pre_submit_audit": audit_report.get("disposition", "clear"),
                            "lossy_answer_mapping_count": len(
                                audit_report.get("lossy_answer_mappings", [])
                            ),
                        },
                        lease_minutes=application_lease_minutes,
                    )
                    if not lease_held:
                        result = "failed:stale_application_attempt"
                        break
                    if run_progress is not None:
                        submit_decision = run_progress.before_submit(job["url"])
                        if not submit_decision.allowed:
                            result = f"deferred:{submit_decision.reason}"
                            break
                        progress_submit_claimed = True
                    reserved, reservation_reason = _reserve_manifest_submission(
                        authorization_manifest,
                        job,
                        audit_report,
                        success_target=(
                            run_progress.success_target
                            if run_progress is not None
                            else None
                        ),
                    )
                    if (
                        not reserved
                        and reservation_reason in {
                            "minimum_submission_gap",
                            "submit_writer_busy",
                        }
                    ):
                        gate_state = job.get("_submission_gate", {})
                        retry_after = (
                            float(gate_state.get("retry_after_seconds") or 0)
                            if isinstance(gate_state, dict)
                            else 0.0
                        )
                        retry_after = max(0.0, min(retry_after, 120.0))
                        # A durable reservation was not granted, so no Submit
                        # authority exists while waiting.  Release the lane and
                        # reacquire only after a fresh page audit.
                        release_submit_lane()
                        add_event(
                            f"[W{worker_id}] Atomic submit gate wait: "
                            f"{retry_after:.1f}s"
                        )
                        update_state(
                            worker_id,
                            status="idle",
                            last_action="atomic submission gap",
                        )
                        gate_wait_started = time.perf_counter()
                        if _stop_event.wait(timeout=retry_after):
                            orchestration_metrics["submission_gate_wait_ms"] += (
                                time.perf_counter() - gate_wait_started
                            ) * 1000
                            result = "deferred:operator_stop_before_submission_gate"
                            break
                        orchestration_metrics["submission_gate_wait_ms"] += (
                            time.perf_counter() - gate_wait_started
                        ) * 1000
                        if email_application is None:
                            audit_started = time.perf_counter()
                            audit_signal, audit_report = _audit_live_pre_submit_page(
                                port,
                                worker_id,
                                job,
                            )
                            audit_signal, audit_report = _enforce_stateful_control_coverage(
                                audit_signal, audit_report
                            )
                            bind_attribution_target(
                                audit_report.get("page_url") or admitted_target_url
                            )
                            audit_duration_ms = (time.perf_counter() - audit_started) * 1000
                            orchestration_metrics["pre_submit_audit_ms"] += audit_duration_ms
                            performance_attribution_mod.safe_record_job_span(
                                job, "audit.pre_submit", audit_duration_ms
                            )
                            if audit_signal or audit_report.get("disposition") != "clear":
                                pre_submit_audit_failure = dict(audit_report)
                                result = (
                                    "failed:manual_review_required:"
                                    f"{audit_signal or 'page_changed_during_submission_gap'}"
                                )
                                break
                        if not acquire_submit_lane():
                            result = "deferred:operator_stop_before_submit_lane"
                            break
                        lease_held = not attempt_id or update_application_attempt(
                            attempt_id,
                            phase="reservation",
                            submit_started=False,
                            evidence={"submission_gate_wait_seconds": retry_after},
                            lease_minutes=application_lease_minutes,
                        )
                        if not lease_held:
                            result = "failed:stale_application_attempt"
                            break
                        reserved, reservation_reason = _reserve_manifest_submission(
                            authorization_manifest,
                            job,
                            audit_report,
                            success_target=(
                                run_progress.success_target
                                if run_progress is not None
                                else None
                            ),
                        )
                    if not reserved:
                        if reservation_reason in {
                            "minimum_submission_gap",
                            "rolling_hour_submission_cap",
                            "submit_writer_busy",
                            "linkedin_external_handoff_reauthorized",
                            "run_success_target_reached",
                            "authorization_batch_capacity_exhausted",
                            "job_already_reserved",
                        }:
                            result = f"deferred:{reservation_reason}"
                        else:
                            result = f"failed:manual_review_required:{reservation_reason}"
                        break
                    ledger_reserved = authorization_manifest is not None
                    observed_job = dict(job)
                    observed_job["_browser_observation"] = {
                        **audit_report,
                        "signal": audit_signal,
                        "advisory_only": bool(audit_report.get("advisory_only")),
                        "submission_gate": True,
                    }
                    if email_application is not None:
                        observed_job["_direct_email_send_reservation"] = (
                            reserve_direct_email_send(job, email_application)
                        )
                    submission_phase = "submit"
                    lease_held = not attempt_id or update_application_attempt(
                        attempt_id,
                        phase="submit",
                        submit_started=True,
                        evidence={"pre_submit_audit": "clear"},
                        lease_minutes=application_lease_minutes,
                    )
                    if not lease_held:
                        result = "submission_uncertain"
                        submission_evidence = {
                            "submit_started": False,
                            "reason": "attempt_lease_lost_after_reservation",
                        }
                        break
                    submission_started = True
                    submitted_at = datetime.now().astimezone()
                    active_route = _route_for_phase(
                        active_route,
                        "submit",
                        (
                            "email_plan_verified"
                            if email_application is not None
                            else "pre_submit_audit_clear"
                        ),
                        interaction_driver=(
                            "mailbox" if email_application is not None else None
                        ),
                        submit_owner=(
                            "mailbox" if email_application is not None else None
                        ),
                    )
                    _attach_control_contract(
                        observed_job,
                        active_route,
                        interaction_mode=requested_interaction_mode,
                        resume_existing_page=True,
                    )
                    route_history.append({
                        **active_route.as_dict(),
                        "event": "phase_transition",
                        "submit_started": True,
                        "pre_submit_page_url": audit_report.get("page_url"),
                    })
                    with _runtime_submit_scope(observed_job):
                        result, submit_duration = run_job(
                            observed_job,
                            port=port,
                            worker_id=worker_id,
                            model=model,
                            dry_run=False,
                            agent_backend=agent_backend,
                            manual_captcha_relay=manual_captcha_relay,
                            resume_existing_page=True,
                            submission_phase="submit",
                        )
                    duration_ms += submit_duration
                    orchestration_metrics["submit_agent_ms"] += max(
                        0, submit_duration
                    )
                    # The single-writer lane protects reservation plus the
                    # only Submit-capable Agent turn.  Independent observation,
                    # CAPTCHA parking, evidence archiving, and receipt
                    # reconciliation never authorize another Submit and run
                    # after this release.
                    release_submit_lane()
                    agent_evidence = observed_job.get("_agent_submission_evidence")
                    if email_application is not None:
                        runtime_mailbox = observed_job.get(
                            "_mailbox_runtime_evidence", {}
                        )
                        if not isinstance(runtime_mailbox, dict):
                            runtime_mailbox = {}
                        sent_receipt = normalize_sent_receipt(
                            runtime_mailbox.get("sent_receipt"),
                            email_application,
                        )
                        observer_evidence = {
                            "channel": "direct_email",
                            "confirmed": bool(
                                sent_receipt is not None
                                and runtime_mailbox.get("send_request_bound") is True
                                and runtime_mailbox.get("send_call_completed") is True
                                and runtime_mailbox.get("post_send_search_completed") is True
                                and runtime_mailbox.get("post_send_read_completed") is True
                            ),
                            "send_call_completed": bool(
                                runtime_mailbox.get("send_call_completed")
                            ),
                            "send_request_bound": bool(
                                runtime_mailbox.get("send_request_bound")
                            ),
                            "post_send_search_completed": bool(
                                runtime_mailbox.get("post_send_search_completed")
                            ),
                            "post_send_read_completed": bool(
                                runtime_mailbox.get("post_send_read_completed")
                            ),
                            "recipient": email_application.get("recipient"),
                            "subject": email_application.get("subject"),
                            "attachment_names": email_application.get(
                                "attachment_names", []
                            ),
                            "confirmation_text": "mailbox send and Sent-copy verification tools completed",
                            "sent_receipt": sent_receipt,
                        }
                        disposition = (
                            "confirmed"
                            if observer_evidence["confirmed"]
                            else "inconclusive"
                        )
                    else:
                        observer_started = time.perf_counter()
                        observer_evidence = _observe_post_submit_page(
                            port, worker_id, job, attempt=1
                        )
                        orchestration_metrics["post_submit_observer_ms"] += (
                            time.perf_counter() - observer_started
                        ) * 1000
                        disposition = _classify_post_submit_observation(
                            observer_evidence
                        )
                        observer_reason = str(observer_evidence.get("reason") or "")
                        if (
                            disposition == "uncertain"
                            and observer_reason.startswith(
                                (
                                    "post_submit_observer_error:",
                                    "post_submit_no_bound_application_page",
                                )
                            )
                            and not _stop_event.wait(timeout=0.75)
                        ):
                            add_event(
                                f"[W{worker_id}] Reconnecting observer once; "
                                "never repeating Submit"
                            )
                            observer_started = time.perf_counter()
                            retry_observation = _observe_post_submit_page(
                                port,
                                worker_id,
                                job,
                                attempt=1,
                            )
                            orchestration_metrics["post_submit_observer_ms"] += (
                                time.perf_counter() - observer_started
                            ) * 1000
                            retry_observation["observer_reconnect_attempts"] = 1
                            retry_observation["initial_observer_reason"] = observer_reason
                            observer_evidence = retry_observation
                            disposition = _classify_post_submit_observation(
                                observer_evidence
                            )
                    attempts = [{
                        "agent": agent_evidence,
                        "observer": observer_evidence,
                        "disposition": disposition,
                    }]

                    # Once a Submit-capable turn has started, visible validation
                    # errors are parked for review.  They never authorize a
                    # second Agent subprocess or another possible Submit.
                    if (
                        email_application is None
                        and disposition == "validation_blocked_repairable"
                    ):
                        observer_evidence["agent_repair_spawned"] = False
                        observer_evidence["next_action"] = (
                            "manual_review_without_resubmission"
                        )
                        add_event(
                            f"[W{worker_id}] Post-submit validation parked; "
                            "no second Agent turn"
                        )
                        update_state(
                            worker_id,
                            status="submission_uncertain",
                            last_action="validation parked without resubmission",
                        )
                    elif (
                        email_application is None
                        and disposition == "verification_required"
                        and manual_captcha_relay
                        and not verification_relay_used
                    ):
                        resume_authorization = _issue_manual_resume_authorization(
                            observed_job,
                            submit_started=True,
                        )
                        if (
                            resume_authorization is not None
                            and _wait_for_manual_captcha(
                                port,
                                worker_id,
                                attempt_id=job.get("_attempt_id"),
                                submit_started=True,
                                root_target_ids=set(
                                    job.get("_browser_root_target_ids") or []
                                ),
                                application_lease_minutes=application_lease_minutes,
                            )
                            and _consume_manual_resume_authorization(
                                observed_job,
                                resume_authorization,
                                submit_started=True,
                            )
                        ):
                            verification_relay_used = True
                            # Submit may already have reached the provider.  A
                            # cleared challenge therefore authorizes only a
                            # fresh observation, never another submit turn.
                            observer_started = time.perf_counter()
                            observer_evidence = _observe_post_submit_page(
                                port, worker_id, job, attempt=2
                            )
                            orchestration_metrics["post_submit_observer_ms"] += (
                                time.perf_counter() - observer_started
                            ) * 1000
                            observer_evidence["resume_authorization_id"] = (
                                resume_authorization.get("authorization_id")
                            )
                            observer_evidence["submit_replayed"] = False
                            disposition = _classify_post_submit_observation(
                                observer_evidence
                            )
                            attempts.append({
                                "agent": agent_evidence,
                                "observer": observer_evidence,
                                "disposition": disposition,
                            })

                    submission_evidence = {
                        "browser_backend": active_browser_backend,
                        "interaction_driver": active_route.interaction_driver,
                        "browser_runtime": active_route.browser_runtime,
                        "submit_owner": active_route.submit_owner,
                        "route_contract_version": active_route.contract_version,
                        "route_history": route_history,
                        "fallback_from_edge": (
                            requested_browser_backend == "auto"
                            and active_browser_backend == "cloak"
                        ),
                        "agent": agent_evidence,
                        "observer": observer_evidence,
                        "attempts": attempts,
                    }
                    if disposition == "provider_submission_error":
                        submission_evidence["technical_failure"] = classify_failure(
                            "failed:provider_submission_error"
                        ).as_dict()
                    elif disposition == "historical_duplicate":
                        submission_evidence["historical_duplicate"] = True
                        submission_evidence["historical_duplicate_text"] = str(
                            observer_evidence.get("historical_duplicate_text")
                            or observer_evidence.get("confirmation_text")
                            or ""
                        )[:500]
                    elif (
                        disposition == "uncertain"
                        and str(observer_evidence.get("reason") or "").startswith(
                            (
                                "post_submit_observer_error:",
                                "post_submit_no_bound_application_page",
                            )
                        )
                    ):
                        submission_evidence["technical_failure"] = classify_failure(
                            "failed:post_submit_observer_unavailable"
                        ).as_dict()
                    if disposition == "confirmed":
                        sent_receipt = observer_evidence.get("sent_receipt")
                        direct_receipt_agrees = bool(
                            email_application is None
                            or (
                                isinstance(agent_evidence, dict)
                                and isinstance(sent_receipt, dict)
                                and str(agent_evidence.get("provider_message_id") or "")
                                == str(sent_receipt.get("provider_message_id") or "")
                            )
                        )
                        if (
                            result != "applied"
                            or not direct_receipt_agrees
                            or not _submission_evidence_consistent(
                                agent_evidence, observer_evidence
                            )
                        ):
                            result = "submission_uncertain"
                        elif email_application is not None:
                            admission = _admit_direct_email_receipt(
                                job,
                                observer_evidence.get("sent_receipt"),
                            )
                            if admission.get("status") not in {
                                "admitted",
                                "already_admitted",
                            }:
                                observer_evidence["receipt_admission"] = admission
                                result = "submission_uncertain"
                    elif disposition == "historical_duplicate":
                        # A provider-side historical marker is a permanent
                        # already-applied outcome, not evidence of a new
                        # submission and not an ordinary uncertain result.
                        result = "already_applied"
                    elif disposition == "verification_required":
                        result = "captcha"
                    elif disposition == "validation_blocked_manual":
                        result = "failed:manual_review_required:submission_validation"
                    elif disposition in {
                        "validation_blocked_repairable",
                        "provider_submission_error",
                    }:
                        result = "submission_uncertain"
                    else:
                        result = "submission_uncertain"

                    # Archive only after the independent observer and receipt
                    # admission gates have resolved the final outcome.  The
                    # retention layer receives explicit semantics rather than
                    # inferring success from a screenshot filename.
                    archived = (
                        []
                        if email_application is not None
                        else _archive_worker_evidence(
                            Path(
                                str(
                                    observed_job.get("_runtime_output_root")
                                    or config.APPLY_WORKER_DIR / f"worker-{worker_id}"
                                )
                            ),
                            job,
                            worker_id,
                            datetime.now().astimezone().strftime("%Y%m%d_%H%M%S"),
                            disposition=disposition,
                            receipt_admitted=(
                                result == "applied" and disposition == "confirmed"
                            ),
                        )
                    )
                    archived_by_name = {path.name: path for path in archived}
                    for index, attempt_evidence in enumerate(attempts, start=1):
                        filename = _observer_screenshot_name(index)
                        archived_observer = archived_by_name.get(filename)
                        if archived_observer is not None:
                            attempt_evidence["observer"]["screenshot_path"] = str(
                                archived_observer
                            )
                    final_archive = archived_by_name.get(
                        _observer_screenshot_name(len(attempts))
                    )
                    if final_archive is not None:
                        observer_evidence["screenshot_path"] = str(final_archive)
                    break
                break

            if (
                result == "submission_uncertain"
                and submission_started
                and submitted_at is not None
                and email_application is None
            ):
                receipt_attempts: list[dict[str, object]] = []
                configured_observers = _configured_receipt_observers(profile)
                receipt_gate_ready = bool(
                    configured_observers
                    and ledger_reserved
                    and _update_submission_ledger(
                        authorization_manifest,
                        job,
                        "submission_uncertain",
                        submission_evidence,
                    )
                )
                configured_observers = (
                    configured_observers if receipt_gate_ready else []
                )
                for provider, _mailbox_spec in configured_observers:
                    observer_context = _build_receipt_observer_context(
                        job,
                        provider=provider,
                        submitted_at=submitted_at,
                    )
                    add_event(
                        f"[W{worker_id}] Queued deterministic {provider} receipt reconciliation"
                    )
                    receipt_attempts.append(
                        {
                            "provider": provider,
                            "turn_status": "queued",
                            "status": "pending_reconciliation",
                            "watermark_advanced": False,
                            "search_after": observer_context.get("search_after"),
                        }
                    )
                if receipt_attempts:
                    job["_receipt_reconciliation_pending"] = receipt_attempts
                    submission_evidence = dict(submission_evidence or {})
                    submission_evidence["mailbox_receipt_observers"] = receipt_attempts

            if (
                submission_started
                and result not in {"applied", "submission_uncertain", "already_applied"}
                and "submission_validation" not in result
            ):
                submission_evidence = submission_evidence or {
                    "submit_started": True,
                    "reason": f"post_submit_result_requires_review:{result}",
                }
                result = "submission_uncertain"
            if job.get("_bound_submission_materials"):
                submission_evidence = dict(submission_evidence or {})
                submission_evidence["material_binding"] = job[
                    "_bound_submission_materials"
                ]
            if submission_evidence is not None:
                submission_evidence = attach_performance(submission_evidence)
            if dry_run:
                preview_evidence = job.get("_preview_attempt_evidence")
                preview_evidence = (
                    dict(preview_evidence)
                    if isinstance(preview_evidence, dict)
                    else {
                        "version": 1,
                        "stage": "dry_run",
                        "reason_code": str(result or "unknown")[:200],
                        "submit_started": False,
                    }
                )
                preview_evidence["orchestration_performance"] = performance_snapshot()
                job["_preview_attempt_evidence"] = preview_evidence

            if result.startswith("deferred:"):
                if dry_run:
                    restore_preview_state(job)
                else:
                    release_lock(job["url"], job.get("_attempt_id"))
                reason = result.split(":", 1)[1]
                add_event(f"[W{worker_id}] Deferred without failure: {reason[:45]}")
                update_state(
                    worker_id,
                    status="idle",
                    last_action=f"deferred: {reason[:25]}",
                )
                progress_outcome = ("deferred", False)
            elif dry_run and result != "previewed":
                if not isinstance(job.get("_preview_attempt_evidence"), dict):
                    job["_preview_attempt_evidence"] = {
                        "version": 1,
                        "stage": "dry_run",
                        "reason_code": str(result or "unknown")[:200],
                        "submit_started": False,
                    }
                restore_preview_state(job)
                if result == "skipped":
                    add_event(f"[W{worker_id}] Preview skipped: {job['title'][:30]}")
                    progress_outcome = ("skipped", False)
                    continue
                failed += 1
                update_state(worker_id, jobs_failed=failed, jobs_done=applied + failed)
                progress_outcome = ("failed", False)
            elif result == "skipped":
                release_lock(job["url"], job.get("_attempt_id"))
                add_event(f"[W{worker_id}] Skipped: {job['title'][:30]}")
                progress_outcome = ("skipped", False)
                continue
            elif result == "applied":
                bound_manifest = (
                    authorization_manifest if ledger_reserved else None
                )
                durable_receipt_admitted = _has_admitted_submission_receipt(
                    bound_manifest,
                    job,
                )
                if not durable_receipt_admitted:
                    uncertainty_evidence = attach_performance({
                        "submit_started": True,
                        "reason": "durable_submission_receipt_not_admitted",
                        "submission_evidence": submission_evidence,
                    })
                    _update_submission_ledger(
                        bound_manifest,
                        job,
                        "submission_uncertain",
                        uncertainty_evidence,
                    )
                    mark_result(
                        job["url"],
                        "submission_uncertain",
                        "browser outcome lacked an admitted durable receipt",
                        duration_ms=duration_ms,
                        task_id=job.get("_attempt_id"),
                        evidence=uncertainty_evidence,
                    )
                    add_event(
                        f"[W{worker_id}] Durable submission receipt was not admitted"
                    )
                    update_state(
                        worker_id,
                        status="submission_uncertain",
                        last_action="durable receipt missing",
                        jobs_done=applied + failed + 1,
                    )
                    progress_outcome = ("submission_uncertain", False)
                else:
                    ledger_updated = _update_submission_ledger(
                        bound_manifest,
                        job,
                        "applied",
                        submission_evidence,
                    )
                    if not ledger_updated:
                        uncertainty_evidence = attach_performance({
                            "submit_started": True,
                            "reason": "submission_ledger_update_failed",
                            "submission_evidence": submission_evidence,
                        })
                        mark_result(
                            job["url"],
                            "submission_uncertain",
                            "submission ledger could not record the admitted receipt",
                            duration_ms=duration_ms,
                            task_id=job.get("_attempt_id"),
                            evidence=uncertainty_evidence,
                        )
                        add_event(
                            f"[W{worker_id}] Admitted receipt ledger update failed"
                        )
                        update_state(
                            worker_id,
                            status="submission_uncertain",
                            last_action="ledger update failed",
                            jobs_done=applied + failed + 1,
                        )
                        progress_outcome = ("submission_uncertain", False)
                    else:
                        mark_result(
                            job["url"],
                            "applied",
                            duration_ms=duration_ms,
                            task_id=job.get("_attempt_id"),
                            evidence=submission_evidence,
                        )
                        applied += 1
                        progress_outcome = ("applied", True)
                        update_state(
                            worker_id,
                            jobs_applied=applied,
                            jobs_done=applied + failed,
                        )
            elif result == "submission_uncertain":
                uncertainty_evidence = attach_performance(
                    submission_evidence
                    or {
                        "submit_started": submission_started,
                        "reason": "agent_or_observer_confirmation_inconclusive",
                    }
                )
                _update_submission_ledger(
                    authorization_manifest if ledger_reserved else None,
                    job,
                    "submission_uncertain",
                    uncertainty_evidence,
                )
                mark_result(
                    job["url"],
                    "submission_uncertain",
                    "browser did not show a decisive receipt after the final action",
                    duration_ms=duration_ms,
                    task_id=job.get("_attempt_id"),
                    evidence=uncertainty_evidence,
                )
                add_event(f"[W{worker_id}] Submission state uncertain; status recorded")
                update_state(
                    worker_id,
                    status="submission_uncertain",
                    last_action="status recorded for agent review",
                    jobs_done=applied + failed + 1,
                )
                progress_outcome = ("submission_uncertain", False)
            elif result == "already_applied":
                existing_application_evidence = attach_performance(
                    submission_evidence
                    or {
                        "submit_started": submission_started,
                        "reason": "provider reported an existing application",
                    }
                )
                _update_submission_ledger(
                    authorization_manifest if ledger_reserved else None,
                    job,
                    "already_applied",
                    existing_application_evidence,
                )
                mark_result(
                    job["url"],
                    "already_applied",
                    "provider reported an existing application",
                    permanent=True,
                    duration_ms=duration_ms,
                    task_id=job.get("_attempt_id"),
                    evidence=existing_application_evidence,
                )
                add_event(
                    f"[W{worker_id}] Historical application detected; no new submission recorded"
                )
                update_state(
                    worker_id,
                    status="already_applied",
                    last_action="historical application recorded",
                    jobs_done=applied + failed + 1,
                )
                progress_outcome = ("already_applied", False)
            elif result == "previewed":
                mark_result(
                    job["url"],
                    "previewed",
                    duration_ms=duration_ms,
                    task_id=job.get("_attempt_id"),
                    evidence=attach_performance(),
                )
                if preview_ticket is not None and run_progress is not None:
                    run_progress.consume_preview_ticket(preview_ticket)
                    preview_ticket = None
                progress_outcome = ("previewed", False)
                update_state(worker_id, jobs_done=applied + failed + 1)
            else:
                reason = result.split(":", 1)[-1] if ":" in result else result
                reason = _format_failure_error(reason, job.pop("_failure_context", None))
                if submission_started and ledger_reserved:
                    _update_submission_ledger(
                        authorization_manifest,
                        job,
                        "failed",
                        {
                            "reason": reason,
                            "submission_evidence": submission_evidence,
                        },
                    )
                mark_result(
                    job["url"],
                    "failed",
                    reason,
                    permanent=_is_permanent_failure(result),
                    duration_ms=duration_ms,
                    task_id=job.get("_attempt_id"),
                    evidence=attach_performance(
                        {
                            "pre_submit_audit": pre_submit_audit_failure,
                            "submit_started": False,
                        }
                        if pre_submit_audit_failure is not None
                        else None
                    ),
                )
                failed += 1
                progress_outcome = ("failed", False)
                update_state(worker_id, jobs_failed=failed,
                             jobs_done=applied + failed)

        except KeyboardInterrupt:
            if dry_run:
                restore_preview_state(job)
                progress_outcome = ("skipped", False)
            elif submission_started:
                uncertainty_evidence = attach_performance({
                    "submit_started": True,
                    "reason": "operator_interrupt_after_submit_phase_started",
                })
                _update_submission_ledger(
                    authorization_manifest if ledger_reserved else None,
                    job,
                    "submission_uncertain",
                    uncertainty_evidence,
                )
                mark_result(
                    job["url"],
                    "submission_uncertain",
                    "operator interrupt after submit phase started",
                    task_id=job.get("_attempt_id"),
                    evidence=uncertainty_evidence,
                )
                progress_outcome = ("submission_uncertain", False)
            else:
                release_lock(job["url"], job.get("_attempt_id"))
                progress_outcome = ("skipped", False)
            if _stop_event.is_set():
                break
            add_event(f"[W{worker_id}] Job skipped (Ctrl+C)")
            continue
        except Exception as e:
            worker_generation_tainted = True
            logger.exception("Worker %d launcher error", worker_id)
            add_event(f"[W{worker_id}] Launcher error: {str(e)[:40]}")
            if dry_run:
                restore_preview_state(job)
                failed += 1
                update_state(worker_id, jobs_failed=failed)
                progress_outcome = ("failed", False)
            elif submission_started:
                uncertainty_evidence = attach_performance({
                    "submit_started": True,
                    "reason": f"launcher_error:{type(e).__name__}",
                })
                _update_submission_ledger(
                    authorization_manifest if ledger_reserved else None,
                    job,
                    "submission_uncertain",
                    uncertainty_evidence,
                )
                mark_result(
                    job["url"],
                    "submission_uncertain",
                    "launcher error after submit phase started",
                    task_id=job.get("_attempt_id"),
                    evidence=uncertainty_evidence,
                )
                update_state(worker_id, status="submission_uncertain")
                progress_outcome = ("submission_uncertain", False)
            else:
                release_lock(job["url"], job.get("_attempt_id"))
                failed += 1
                update_state(worker_id, jobs_failed=failed)
                progress_outcome = ("failed", False)
        finally:
            if run_progress is not None and progress_outcome is not None:
                progress_status, receipt_confirmed = progress_outcome
                if progress_submit_claimed and not ledger_reserved:
                    progress_status = "cancelled_before_action"
                    receipt_confirmed = False
                run_progress.record_terminal(
                    job["url"],
                    progress_status,
                    receipt_confirmed=receipt_confirmed,
                )
            if preview_ticket is not None and run_progress is not None:
                run_progress.release_preview_ticket(preview_ticket)
            if application_supervisor is not None:
                application_supervisor.close_application(
                    recycle_worker=worker_generation_tainted
                )
                application_supervisor = None
                chrome_proc = None
            if submit_writer_held:
                release_submit_lane()
            if run_progress is not None:
                try:
                    run_progress.record_performance(
                        performance_snapshot()["metrics"]
                    )
                except Exception as exc:  # noqa: BLE001 - telemetry is advisory
                    logger.warning("Could not aggregate job performance: %s", exc)
            try:
                record_application_attempt_performance(
                    job.get("_attempt_id"),
                    performance_snapshot(),
                )
            except Exception as exc:  # noqa: BLE001 - telemetry is advisory
                logger.warning(
                    "Could not persist final orchestration performance for %s: %s",
                    job.get("_attempt_id"),
                    exc,
                )
            if cloak_lane_held:
                _cloak_lane.release()

        jobs_done += 1
        if target_url:
            break

    update_state(worker_id, status="done", last_action="finished")
    return applied, failed


def worker_loop(
    runtime: WorkerRuntimePorts,
    options: WorkerRunOptions,
) -> tuple[int, int]:
    """Own the CDP claim and execute one typed worker run."""
    if not isinstance(runtime, WorkerRuntimePorts):
        raise TypeError("worker runtime must be a grouped WorkerRuntimePorts instance")
    if not isinstance(options, WorkerRunOptions):
        raise TypeError("worker options must be a WorkerRunOptions instance")
    host = runtime.host
    browser = runtime.browser
    worker_id = options.worker_id
    port = browser.allocate_cdp_port(worker_id)
    browser_worker: BrowserWorkerProcess | None = None
    endpoint_manager = None
    try:
        profile = host.config.load_profile()
        policy = profile.get("submission_policy", {})
        runtime_profile = profile.get("agent_runtime", {})
        persistent_endpoint = (
            runtime_profile.get("persistent_playwright_mcp", {})
            if isinstance(runtime_profile, Mapping)
            else {}
        )
        persistent_endpoint_enabled = bool(
            isinstance(persistent_endpoint, Mapping)
            and persistent_endpoint.get("enabled") is True
        )
        if persistent_endpoint_enabled and options.agent_backend != "codex":
            raise ValueError(
                "persistent Playwright MCP HTTP transport currently requires Codex"
            )

        def positive_policy_integer(name: str, default: int) -> int:
            raw = policy.get(name, default) if isinstance(policy, Mapping) else default
            return (
                raw
                if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0
                else default
            )

        rss_reader = host.process_rss_bytes
        if persistent_endpoint_enabled:
            endpoint_launcher = resolve_persistent_playwright_launcher()
            endpoint_reservation = LoopbackPortReservation.reserve(
                host.config.APPLY_WORKER_DIR,
                worker_id=worker_id,
            )
            try:
                endpoint_spec = LoopbackHttpEndpointSpec(
                    worker_id=worker_id,
                    port=endpoint_reservation.port,
                    cdp_port=port,
                    launcher=endpoint_launcher,
                )
                raw_endpoint_timeout = persistent_endpoint.get(
                    "startup_timeout_seconds", 60
                )
                endpoint_timeout = (
                    raw_endpoint_timeout
                    if isinstance(raw_endpoint_timeout, int)
                    and not isinstance(raw_endpoint_timeout, bool)
                    and raw_endpoint_timeout > 0
                    else 60
                )

                def stop_endpoint_process(process) -> None:
                    if process.poll() is None:
                        host.kill_process_tree(process.pid)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired as exc:
                        raise EndpointUnavailable(
                            "persistent MCP process-tree termination timed out"
                        ) from exc
                    if process.poll() is None:
                        raise EndpointUnavailable(
                            "persistent MCP process-tree termination is unconfirmed"
                        )

                endpoint_manager = LoopbackHttpEndpointManager(
                    endpoint_spec,
                    endpoint_reservation,
                    stop_process=stop_endpoint_process,
                    startup_timeout_seconds=endpoint_timeout,
                )
            finally:
                if endpoint_manager is None:
                    endpoint_reservation.release()
        else:
            endpoint_manager = PerTurnStdioEndpointManager(worker_id)
        browser_worker = BrowserWorkerProcess(
            worker_id=worker_id,
            port=port,
            run_id=(
                options.run_progress.run_id
                if options.run_progress is not None
                else f"worker-{worker_id}-{time.time_ns()}"
            ),
            namespace_root=host.config.APPLY_WORKER_DIR,
            launch_browser=browser.launch_chrome,
            cleanup_browser=browser.cleanup_worker,
            browser_health_probe=lambda: browser.cdp_endpoint_reachable(port),
            open_target=browser.open_bound_application_target,
            close_targets=browser.close_bound_application_targets,
            endpoint_manager=endpoint_manager,
            rss_reader=rss_reader if callable(rss_reader) else None,
            max_applications=positive_policy_integer(
                "persistent_browser_max_applications", 8
            ),
            max_rss_bytes=positive_policy_integer(
                "persistent_browser_max_rss_bytes", 1_500_000_000
            ),
        )
        return _worker_loop_with_port(
            runtime,
            port,
            browser_worker,
            options,
        )
    finally:
        try:
            if browser_worker is not None:
                browser_worker.close()
            elif endpoint_manager is not None:
                endpoint_manager.shutdown()
        finally:
            browser.release_cdp_port(worker_id)
