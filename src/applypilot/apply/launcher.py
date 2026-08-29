"""Apply orchestration: acquire jobs, spawn Claude Code sessions, track results.

This is the main entry point for the apply pipeline. It pulls jobs from
the database, launches Chrome + Claude Code for each one, parses the
result, and updates the database. Supports parallel workers via --workers.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

from rich.console import Console
from rich.live import Live

from applypilot import config
from applypilot.apply import agent_output as agent_output_mod
from applypilot.apply import agent_runtime as agent_runtime_mod
from applypilot.apply import application_jobs as application_jobs_mod
from applypilot.apply import ats as ats_mod
from applypilot.apply import orchestration as orchestration_mod
from applypilot.apply import page_observation as page_observation_mod
from applypilot.apply import prompt as prompt_mod
from applypilot.apply import worker_orchestration as worker_orchestration_mod
from applypilot.apply.agent_report_mcp import REPORT_PATH_ENV, RUN_ID_ENV
from applypilot.apply.ats_tools_mcp import ATS_CONTEXT_PATH_ENV
from applypilot.apply.capabilities import (
    CapabilityRegistry,
    McpPackageSpec,
    compose_runtime_capabilities,
    resolve_capability_registry,
    resolve_playwright_mcp_spec,
    scope_capability_registry,
)
from applypilot.apply.chrome import (
    BASE_CDP_PORT,
    _kill_process_tree,
    allocate_cdp_port,  # noqa: F401 - injected worker port
    capture_browser_session,  # noqa: F401 - injected worker port
    cleanup_on_exit,
    cleanup_worker,  # noqa: F401 - injected worker port
    kill_all_chrome,
    launch_chrome,  # noqa: F401 - injected worker port
    release_cdp_port,  # noqa: F401 - injected worker port
    reset_worker_dir,
    resolve_browser_backend,
    restore_browser_session,  # noqa: F401 - injected worker port
)
from applypilot.apply.contracts import (
    AgentCheckpoint,
    AgentRunRequest,
    AgentTurnResult,
    ApplicationEvent,
    HumanRequest,
    ResourceClaim,
    TaskResult,
    TaskSpec,
    contract_json,
)
from applypilot.apply.dashboard import (
    add_event,
    get_state,
    get_totals,
    init_worker,
    render_full,
    update_state,
)
from applypilot.apply.email_routing import (
    MailboxMcpSpec,
    direct_email_send_is_reserved,
    mailbox_prepare_duplicate_receipt,
    mailbox_read_input_matches_message,
    mailbox_search_message_id,
    mailbox_send_input_matches_plan,
    mailbox_sent_search_input_matches_plan,
    normalize_mailbox_read_receipt,
    normalize_sent_receipt,
    resolve_mailbox_mcp_spec,
)
from applypilot.apply.failure_taxonomy import classify_failure
from applypilot.apply.retention import (
    archive_new_evidence,
    mark_owned_directory,
    reclaim_terminal_artifacts,
    snapshot_files,
)
from applypilot.apply.router import (
    ControlRoute,
    cloak_fallback_route,
    computer_use_handoff_allowed,  # noqa: F401 - injected worker port
    initial_route,  # noqa: F401 - injected worker port
    prompt_control_contract,
    resolve_interaction_mode,
)
from applypilot.database import get_connection
from applypilot.runtime_settings import load_runtime_settings

# Document the compatibility surface consumed by the extracted worker. Tests
# and callers may still replace these ports before ``worker_loop``.
_WORKER_RUNTIME_EXPORTS = worker_orchestration_mod.WORKER_RUNTIME_PORTS

_format_failure_error = agent_output_mod.format_failure_error
_interpret_agent_output = agent_output_mod.interpret_agent_output
_load_agent_turn_report = agent_output_mod.load_agent_turn_report
_parse_failure_context = agent_output_mod.parse_failure_context
_parse_result_line = agent_output_mod.parse_result_line
_parse_unanswered_questions = agent_output_mod.parse_unanswered_questions
_result_status = agent_output_mod.result_status
_validate_preview_audit = agent_output_mod.validate_preview_audit
_validate_submission_evidence = agent_output_mod.validate_submission_evidence
_reconcile_agent_turn_outputs = agent_output_mod.reconcile_agent_turn_outputs
_application_fact_value = page_observation_mod._application_fact_value
_audit_live_pre_submit_page = page_observation_mod._audit_live_pre_submit_page
_bound_application_pages = page_observation_mod._bound_application_pages
_captcha_response_present = page_observation_mod._captcha_response_present
_classify_post_submit_observation = page_observation_mod._classify_post_submit_observation
_expected_screening_answer = page_observation_mod._expected_screening_answer
_observe_post_submit_page = page_observation_mod._observe_post_submit_page
_selected_matches_boolean = page_observation_mod._selected_matches_boolean
_select_application_frame = page_observation_mod._select_application_frame
_select_application_page = page_observation_mod._select_application_page
_submission_evidence_consistent = page_observation_mod._submission_evidence_consistent
_validate_pre_submit_snapshot = page_observation_mod._validate_pre_submit_snapshot
_verification_clear_state_stable = page_observation_mod._verification_clear_state_stable
_visible_captcha_overlay = page_observation_mod._visible_captcha_overlay
_visible_verification_gate = page_observation_mod._visible_verification_gate
_work_authorization_answers = page_observation_mod._work_authorization_answers
_yes_no_value = page_observation_mod._yes_no_value


def _make_mcp_config(
    cdp_port: int,
    *,
    playwright_mcp: McpPackageSpec | dict[str, object] | str | None = None,
    capability_registry: CapabilityRegistry | None = None,
    runtime_metadata: dict | None = None,
    mailbox_mcp: MailboxMcpSpec | dict[str, object] | None = None,
    direct_email_send_authorized: bool = False,
) -> dict:
    return agent_runtime_mod.make_mcp_config(
        cdp_port,
        playwright_mcp=playwright_mcp,
        capability_registry=capability_registry,
        runtime_metadata=runtime_metadata,
        python_executable=sys.executable,
        mailbox_mcp=mailbox_mcp,
        direct_email_send_authorized=direct_email_send_authorized,
    )


def _resolve_claude_command() -> list[str]:
    return agent_runtime_mod.resolve_claude_command()


def _resolve_codex_command() -> list[str]:
    return agent_runtime_mod.resolve_codex_command()


_toml_value = agent_runtime_mod._toml_value
_toml_skill_config = agent_runtime_mod._toml_skill_config


def _start_timeout_watchdog(
    proc: subprocess.Popen, timeout_seconds: float
) -> tuple[threading.Event, threading.Timer]:
    return agent_runtime_mod.start_timeout_watchdog(
        proc,
        timeout_seconds,
        kill_process_tree=_kill_process_tree,
    )


def _runtime_timeout_status(*, submission_phase: str, dry_run: bool) -> str:
    """Keep an interrupted real submit uncertain; identify all other budget exhaustion."""
    if submission_phase == "submit" and not dry_run:
        return "submission_uncertain"
    return "failed:agent_runtime_timeout"


def _build_agent_command(
    backend: str,
    model: str,
    port: int,
    worker_dir: Path,
    mcp_config_path: Path,
    *,
    credential_relay_authorized: bool = False,
    playwright_mcp: McpPackageSpec | dict[str, object] | str | None = None,
    capability_registry: CapabilityRegistry | None = None,
    runtime_metadata: dict | None = None,
    mailbox_mcp: MailboxMcpSpec | dict[str, object] | None = None,
    direct_email_send_authorized: bool = False,
    workload_class: str | None = None,
    reasoning_efforts: dict[str, str] | None = None,
) -> tuple[list[str], Path | None]:
    return agent_runtime_mod.build_agent_command(
        backend,
        model,
        port,
        worker_dir,
        mcp_config_path,
        resolve_claude=_resolve_claude_command,
        resolve_codex=_resolve_codex_command,
        python_executable=sys.executable,
        credential_relay_authorized=credential_relay_authorized,
        playwright_mcp=playwright_mcp,
        capability_registry=capability_registry,
        runtime_metadata=runtime_metadata,
        mailbox_mcp=mailbox_mcp,
        direct_email_send_authorized=direct_email_send_authorized,
        workload_class=workload_class,
        reasoning_efforts=reasoning_efforts,
    )


def _resolve_agent_tool_surface(
    profile: dict,
    job: dict,
    *,
    environ: dict[str, str],
) -> tuple[McpPackageSpec, CapabilityRegistry]:
    """Resolve one portable tool surface for both MCP config and Agent CLI."""
    runtime_config = profile.get("agent_runtime", {})
    if not isinstance(runtime_config, dict):
        runtime_config = {}
    configured_spec = runtime_config.get("playwright_mcp")
    if configured_spec is None:
        servers = runtime_config.get("mcp_servers", {})
        if isinstance(servers, dict):
            configured_spec = servers.get("playwright")
    configured_mapping = configured_spec if isinstance(configured_spec, dict) else None
    explicit_spec = job.get("_playwright_mcp_spec")
    if explicit_spec is None and isinstance(configured_spec, str):
        explicit_spec = configured_spec
    spec = resolve_playwright_mcp_spec(
        explicit_spec,
        environ=environ,
        configured=configured_mapping,
    )

    explicit_registry = job.get("_agent_capability_registry")
    if isinstance(explicit_registry, CapabilityRegistry):
        return spec, explicit_registry
    configured_tools = runtime_config.get("tools")
    inherit_defaults = True
    if isinstance(configured_tools, dict) and "definitions" in configured_tools:
        inherit_defaults = bool(configured_tools.get("inherit_defaults", True))
        configured_tools = configured_tools.get("definitions")
    return spec, resolve_capability_registry(
        configured_tools,
        inherit_defaults=inherit_defaults,
    )


def _resolve_mailbox_tool_surface(
    profile: dict,
    job: dict,
    *,
    environ: dict[str, str],
) -> MailboxMcpSpec:
    """Resolve a replaceable mailbox server without binding to one provider."""
    runtime_config = profile.get("agent_runtime", {})
    if not isinstance(runtime_config, dict):
        runtime_config = {}
    configured = runtime_config.get("mailbox_mcp")
    if configured is None:
        servers = runtime_config.get("mcp_servers", {})
        if isinstance(servers, dict):
            configured = servers.get("mailbox") or servers.get("gmail")
    configured_mapping = configured if isinstance(configured, dict) else None
    explicit = job.get("_mailbox_mcp_spec")
    if explicit is None and isinstance(configured, MailboxMcpSpec):
        explicit = configured
    return resolve_mailbox_mcp_spec(
        explicit,
        environ=environ,
        configured=configured_mapping,
    )


def _persist_agent_turn_started(
    request: AgentRunRequest,
    *,
    backend: str,
    model: str,
    runtime_metadata: dict,
) -> None:
    """Best-effort control telemetry; never becomes application authority."""
    from applypilot.database import append_agent_event

    event = ApplicationEvent(
        event_id=f"{request.run_id}:started",
        attempt_id=request.attempt_id,
        run_id=request.run_id,
        phase=request.phase,
        actor=request.agent_role,
        event_type="agent.turn.started",
        payload={
            "backend": backend,
            "model": model,
            "concurrency_mode": request.concurrency_mode,
            "available_tools": list(request.available_tools),
            "runtime": runtime_metadata,
        },
        idempotency_key=f"{request.run_id}:started",
    )
    try:
        append_agent_event(event)
    except Exception as exc:  # noqa: BLE001 - advisory telemetry must not alter apply outcome
        logger.warning("Could not persist Agent turn start %s: %s", request.run_id, exc)


def _persist_agent_turn_completed(
    request: AgentRunRequest,
    result: AgentTurnResult,
    *,
    application_status: str,
    duration_ms: int,
    source: str,
    metrics: Mapping[str, object] | None = None,
) -> None:
    """Atomically save the terminal control event and resumable checkpoint."""
    from applypilot.database import record_agent_turn_control

    raw_evidence_refs = result.observations.get("evidence_refs", ())
    evidence_ref_count = (
        len(raw_evidence_refs) if isinstance(raw_evidence_refs, (list, tuple)) else 0
    )
    bounded_metrics: dict[str, int | float] = {}
    for key, value in (metrics or {}).items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        bounded_metrics[_bounded_control_text(key, maximum=80)] = max(0, value)
    event = ApplicationEvent(
        event_id=f"{request.run_id}:completed",
        attempt_id=request.attempt_id,
        run_id=request.run_id,
        phase=request.phase,
        actor=request.agent_role,
        event_type="agent.turn.completed",
        payload={
            "reported_status": result.status,
            "application_status": application_status,
            # Free-form Agent/page text stays in the transient turn result. The
            # durable control plane records only bounded workflow metadata.
            "summary_length": len(result.summary),
            "duration_ms": max(0, duration_ms),
            "source": source,
            "proposal_ids": [proposal.proposal_id for proposal in result.proposals],
            "evidence_ref_count": evidence_ref_count,
            "metrics": bounded_metrics,
        },
        evidence_refs=(),
        idempotency_key=f"{request.run_id}:completed",
    )
    checkpoint = AgentCheckpoint(
        checkpoint_id=f"{request.run_id}:checkpoint:1",
        run_id=request.run_id,
        attempt_id=request.attempt_id,
        phase=request.phase,
        sequence=1,
        state={
            "application_status": application_status,
            "result": _durable_agent_result(result),
            "source": source,
        },
    )
    human_request = None
    if result.requested_human_input:
        human_request = HumanRequest(
            request_id=f"{request.run_id}:human:1",
            run_id=request.run_id,
            attempt_id=request.attempt_id,
            request_type="agent_clarification",
            prompt="Agent requested human review; inspect the run evidence before continuing.",
            context={
                "phase": request.phase,
                "application_status": application_status,
                "requested_input_length": len(result.requested_human_input),
            },
        )
    try:
        record_agent_turn_control(event, checkpoint, human_request)
    except Exception as exc:  # noqa: BLE001 - advisory telemetry must not alter apply outcome
        logger.warning("Could not persist Agent turn completion %s: %s", request.run_id, exc)


def _bounded_control_text(value: object, *, maximum: int = 200) -> str:
    """Normalize identifier-like control metadata without retaining page text."""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value)).strip()
    return text[:maximum]


def _durable_agent_result(result: AgentTurnResult) -> dict[str, object]:
    """Project a turn result onto an allowlisted, resumable control summary."""
    return {
        "schema_version": "1",
        "run_id": _bounded_control_text(result.run_id),
        "status": _bounded_control_text(result.status),
        "summary_length": len(result.summary),
        "proposals": [
            {
                "proposal_id": _bounded_control_text(proposal.proposal_id),
                "kind": _bounded_control_text(proposal.kind, maximum=100),
                "depends_on": [
                    _bounded_control_text(dependency)
                    for dependency in proposal.depends_on
                ],
                "concurrency_mode": _bounded_control_text(
                    proposal.concurrency_mode,
                    maximum=100,
                ),
                "concurrency_key": (
                    None
                    if proposal.concurrency_key is None
                    else _bounded_control_text(proposal.concurrency_key)
                ),
                "priority": proposal.priority,
            }
            for proposal in result.proposals
        ],
        "observation_keys": sorted(
            _bounded_control_text(key, maximum=100) for key in result.observations
        ),
        "requested_human_input": result.requested_human_input is not None,
    }


def _proposal_orchestration_settings(
    profile: Mapping[str, object],
    job: Mapping[str, object],
) -> tuple[int, tuple[str, ...]]:
    """Resolve open orchestration hints; explicit job settings override profile config."""
    runtime_config = profile.get("agent_runtime", {})
    profile_config: Mapping[str, object] = {}
    if isinstance(runtime_config, Mapping):
        configured = runtime_config.get("orchestration", {})
        if isinstance(configured, Mapping):
            profile_config = configured
    job_config = job.get("_agent_orchestration", {})
    if not isinstance(job_config, Mapping):
        job_config = {}

    raw_workers = job_config.get(
        "max_workers",
        profile_config.get("max_workers", 1),
    )
    try:
        max_workers = int(raw_workers)
    except (TypeError, ValueError):
        max_workers = 1
    max_workers = max(max_workers, 1)

    raw_modes = job_config.get(
        "parallel_modes",
        profile_config.get(
            "parallel_modes",
            ("parallel", "parallel_safe", "adaptive"),
        ),
    )
    if isinstance(raw_modes, str):
        parallel_modes = (raw_modes,)
    elif isinstance(raw_modes, (list, tuple, set, frozenset)):
        parallel_modes = tuple(
            str(mode) for mode in raw_modes if str(mode).strip()
        )
    else:
        parallel_modes = ("parallel", "parallel_safe", "adaptive")
    return max_workers, parallel_modes


def _execute_agent_proposals(
    profile: Mapping[str, object],
    job: dict,
    result: AgentTurnResult,
) -> tuple[dict[str, dict[str, object]], int]:
    """Run proposed specialist work only when the caller injects a runner.

    The runner is provider-neutral and may route to Codex, Claude, an SDK
    runtime, or a local plugin. With no runner, proposals remain pending and the
    existing single browser Agent behavior is unchanged.
    """
    if not result.proposals:
        return {}, 1
    runner = job.get("_agent_proposal_runner")
    if not callable(runner):
        job["_agent_proposals_pending"] = tuple(
            proposal.proposal_id for proposal in result.proposals
        )
        return {}, 1

    max_workers, parallel_modes = _proposal_orchestration_settings(profile, job)

    mode_set = {str(mode).casefold() for mode in parallel_modes}
    share_rule = (
        job.get("_agent_proposal_can_share_wave")
        if callable(job.get("_agent_proposal_can_share_wave"))
        else None
    )
    pair_claims: dict[str, list[str]] = {
        proposal.proposal_id: [] for proposal in result.proposals
    }
    if share_rule is not None:
        for index, left in enumerate(result.proposals):
            for right in result.proposals[index + 1 :]:
                if not share_rule(left, right):
                    key = f"proposal-pair:{left.proposal_id}:{right.proposal_id}"
                    pair_claims[left.proposal_id].append(key)
                    pair_claims[right.proposal_id].append(key)

    proposals_by_id = {
        proposal.proposal_id: proposal for proposal in result.proposals
    }
    tasks: list[TaskSpec] = []
    resource_capacities: dict[str, int] = {"proposal-lane": max_workers}
    for proposal in result.proposals:
        parallel_safe = proposal.concurrency_mode.casefold() in mode_set
        claims = [
            ResourceClaim(
                "proposal-lane",
                1 if parallel_safe else max_workers,
            )
        ]
        if proposal.concurrency_key:
            resource_key = f"proposal-key:{proposal.concurrency_key}"
            claims.append(ResourceClaim(resource_key))
            resource_capacities[resource_key] = 1
        for resource_key in pair_claims[proposal.proposal_id]:
            claims.append(ResourceClaim(resource_key))
            resource_capacities[resource_key] = 1
        tasks.append(
            TaskSpec(
                task_id=proposal.proposal_id,
                kind=proposal.kind,
                objective=proposal.summary,
                inputs=proposal.payload,
                depends_on=proposal.depends_on,
                required_results=proposal.depends_on,
                effect_class="read",
                resource_claims=tuple(claims),
                retry_budget=0,
                idempotency_key=proposal.proposal_id,
                priority=proposal.priority,
            )
        )

    def task_runner(task: TaskSpec, _context: object) -> TaskResult:
        proposal = proposals_by_id[task.task_id]
        value = runner(proposal)
        return TaskResult(
            task_id=task.task_id,
            status="completed",
            output={"value": value},
        )

    def reduce_result(
        state: dict[str, object],
        task: TaskSpec,
        task_result: TaskResult,
    ) -> None:
        reduced = state.setdefault("proposal_results", {})
        if isinstance(reduced, dict):
            reduced[task.task_id] = {
                "status": task_result.status,
                "failure_category": task_result.failure_category,
            }

    try:
        coordinator = orchestration_mod.execute_task_graph(
            tasks,
            task_runner,
            reduce_result,
            max_workers=max_workers,
            resource_capacities=resource_capacities,
        )
    except (RuntimeError, TypeError, ValueError) as exc:
        logger.warning("Could not plan Agent proposals for %s: %s", result.run_id, exc)
        job["_agent_proposal_error"] = str(exc)[:300]
        return {}, max_workers
    outcomes: dict[str, dict[str, object]] = {}
    specialist_context: list[dict[str, object]] = []
    for proposal in result.proposals:
        task_result = coordinator.results[proposal.proposal_id]
        if task_result.succeeded:
            value = task_result.output.get("value")
            outcome = {"status": "completed", "value": value}
            context_item: dict[str, object] = {
                "proposal_id": proposal.proposal_id,
                "kind": proposal.kind,
                "status": "completed",
            }
            if isinstance(value, str):
                context_item["summary"] = value[:500]
            elif isinstance(value, Mapping):
                for key in ("summary", "facts", "recommendation", "evidence_refs"):
                    if key in value:
                        context_item[key] = value[key]
            specialist_context.append(context_item)
        elif task_result.status == "blocked":
            outcome = {
                "status": "blocked",
                "blocked_by": list(task_result.output.get("blocked_by", [])),
            }
        else:
            outcome = {
                "status": "failed",
                "error_type": str(
                    task_result.output.get("error_type")
                    or task_result.failure_category
                    or "specialist_failure"
                )[:100],
                "error": str(task_result.output.get("error") or "")[:300],
            }
        outcomes[proposal.proposal_id] = outcome
    job["_agent_proposal_results"] = outcomes
    if specialist_context:
        job["_agent_specialist_context"] = specialist_context
    job.pop("_agent_proposals_pending", None)
    return outcomes, max_workers


def _proposal_dispatch_allowed(
    *,
    result_source: str,
    phase: str,
    dry_run: bool,
) -> bool:
    """Keep untrusted or submit-critical proposals out of synchronous dispatch."""
    return (
        result_source in {"structured", "structured+legacy"}
        and (phase.casefold() != "submit" or dry_run)
    )


def _persist_agent_proposal_outcomes(
    request: AgentRunRequest,
    outcomes: Mapping[str, Mapping[str, object]],
    *,
    max_workers: int,
) -> None:
    """Persist outcome metadata, never specialist return values or page content."""
    from applypilot.database import append_agent_event

    completed = [
        _bounded_control_text(proposal_id)
        for proposal_id, outcome in outcomes.items()
        if outcome.get("status") == "completed"
    ]
    failed = [
        _bounded_control_text(proposal_id)
        for proposal_id, outcome in outcomes.items()
        if outcome.get("status") == "failed"
    ]
    blocked = [
        _bounded_control_text(proposal_id)
        for proposal_id, outcome in outcomes.items()
        if outcome.get("status") == "blocked"
    ]
    event = ApplicationEvent(
        event_id=f"{request.run_id}:proposals:1",
        attempt_id=request.attempt_id,
        run_id=request.run_id,
        phase=request.phase,
        actor="agent-orchestrator",
        event_type="agent.proposals.executed",
        payload={
            "completed_ids": completed,
            "failed_ids": failed,
            "blocked_ids": blocked,
            "max_workers": max_workers,
        },
        idempotency_key=f"{request.run_id}:proposals:1",
    )
    try:
        append_agent_event(event)
    except Exception as exc:  # noqa: BLE001 - advisory telemetry must not alter apply outcome
        logger.warning("Could not persist Agent proposal outcomes %s: %s", request.run_id, exc)


def acquire_job(
    target_url: str | None = None,
    min_score: int = 6,
    worker_id: int = 0,
    preview_only: bool = False,
    authorization_manifest: dict | None = None,
    exclude_urls: set[str] | None = None,
    application_lease_minutes: int | None = None,
) -> dict | None:
    if application_lease_minutes is None:
        application_lease_minutes = load_runtime_settings().application_lease_minutes
    return application_jobs_mod.acquire_job(
        get_connection(),
        target_url=target_url,
        min_score=min_score,
        worker_id=worker_id,
        preview_only=preview_only,
        authorization_manifest=authorization_manifest,
        exclude_urls=exclude_urls,
        load_blocked=_load_blocked,
        application_lease_minutes=application_lease_minutes,
    )


def mark_result(
    url: str,
    status: str,
    error: str | None = None,
    permanent: bool = False,
    duration_ms: int | None = None,
    task_id: str | None = None,
    evidence: dict | None = None,
) -> None:
    application_jobs_mod.mark_result(
        get_connection(),
        url,
        status,
        error,
        permanent,
        duration_ms,
        task_id,
        evidence,
    )


def release_lock(url: str, task_id: str | None = None) -> None:
    application_jobs_mod.release_lock(get_connection(), url, task_id)


def restore_preview_state(job: dict) -> None:
    application_jobs_mod.restore_preview_state(get_connection(), job)


def _mark_runtime_cover_not_required(job: dict) -> dict:
    return application_jobs_mod.mark_runtime_cover_not_required(get_connection(), job)


def mark_job(url: str, status: str, reason: str | None = None) -> None:
    application_jobs_mod.mark_job(get_connection(), url, status, reason)


def reset_failed() -> int:
    return application_jobs_mod.reset_failed(get_connection())


def worker_loop(
    worker_id: int = 0,
    limit: int = 1,
    target_url: str | None = None,
    min_score: int = 6,
    headless: bool = False,
    model: str = "sonnet",
    dry_run: bool = False,
    agent_backend: str = "codex",
    manual_captcha_relay: bool = False,
    browser_backend: str = "edge",
    interaction_mode: str = "auto",
    authorization_manifest: dict | None = None,
    attempted_urls: set[str] | None = None,
    attempted_urls_lock: threading.Lock | None = None,
) -> tuple[int, int]:
    return worker_orchestration_mod.worker_loop(
        sys.modules[__name__],
        worker_id=worker_id,
        limit=limit,
        target_url=target_url,
        min_score=min_score,
        headless=headless,
        model=model,
        dry_run=dry_run,
        agent_backend=agent_backend,
        manual_captcha_relay=manual_captcha_relay,
        browser_backend=browser_backend,
        interaction_mode=interaction_mode,
        authorization_manifest=authorization_manifest,
        attempted_urls=attempted_urls,
        attempted_urls_lock=attempted_urls_lock,
    )

logger = logging.getLogger(__name__)

# Blocked sites loaded from config/sites.yaml
def _load_blocked():
    from applypilot.config import load_blocked_sites
    return load_blocked_sites()


def _open_bound_application_target(port: int, start_url: str) -> set[str]:
    """Create and navigate the exact CDP target owned by this application."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        if not browser.contexts:
            raise RuntimeError("Browser exposed no default context for application binding")
        page = browser.contexts[0].new_page()
        info = page.context.new_cdp_session(page).send("Target.getTargetInfo")[
            "targetInfo"
        ]
        target_id = str(info.get("targetId") or "")
        if not target_id:
            raise RuntimeError("Browser did not expose the new application target id")
        page.goto(start_url, wait_until="domcontentloaded", timeout=60_000)
        return {target_id}


def _default_ats_binding_transport(url: str) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "ApplyPilot/0.1 (+application-identity-binding)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return {
                "status_code": int(getattr(response, "status", 200)),
                "json": json.loads(response.read().decode("utf-8")),
            }
    except urllib.error.HTTPError as error:
        return {"status_code": int(error.code), "json": None}
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return {
            "status_code": 0,
            "json": None,
            "error": type(error).__name__,
        }


def _resolve_ats_application_binding(
    job: Mapping[str, object],
    *,
    transport=None,
) -> dict[str, object] | None:
    """Resolve an immutable provider job-to-application identity when needed."""
    target_url = str(job.get("application_url") or job.get("url") or "")
    if ats_mod.detect_ats_site(target_url) != "smartrecruiters":
        return None
    parsed = urlparse(target_url)
    parts = [part for part in parsed.path.split("/") if part]
    tenant = parts[0] if len(parts) >= 2 else ""
    posting_id = parts[1].split("-", 1)[0] if len(parts) >= 2 else ""
    unresolved = {
        "provider": "smartrecruiters",
        "tenant": tenant,
        "posting_id": posting_id,
        "resolved": False,
    }
    if not tenant or not posting_id or tenant.casefold() == "oneclick-ui":
        return {**unresolved, "reason": "public_posting_identity_missing"}

    detail_url = (
        "https://api.smartrecruiters.com/v1/companies/"
        f"{quote(tenant, safe='')}/postings/{quote(posting_id, safe='')}"
    )
    response = (transport or _default_ats_binding_transport)(detail_url)
    if not isinstance(response, Mapping):
        return {**unresolved, "reason": "identity_response_invalid"}
    try:
        status_code = int(response.get("status_code") or 0)
    except (TypeError, ValueError):
        status_code = 0
    payload = response.get("json")
    if status_code != 200 or not isinstance(payload, Mapping):
        return {
            **unresolved,
            "reason": f"identity_lookup_http_{status_code or 'unavailable'}",
        }
    company = payload.get("company")
    company = company if isinstance(company, Mapping) else {}
    publication_id = str(payload.get("uuid") or "").strip()
    try:
        publication_id = str(uuid.UUID(publication_id))
    except (ValueError, AttributeError):
        return {**unresolved, "reason": "publication_identity_invalid"}
    if (
        str(payload.get("id") or "").strip() != posting_id
        or str(company.get("identifier") or "").strip().casefold()
        != tenant.casefold()
    ):
        return {**unresolved, "reason": "identity_response_mismatch"}
    return {
        "provider": "smartrecruiters",
        "tenant": tenant,
        "posting_id": posting_id,
        "publication_id": publication_id,
        "resolved": True,
    }


def _run_read_only_preflight(job: Mapping[str, object]) -> dict[str, object]:
    """Run independent duplicate and provider-identity reads before browser work."""
    provider = ats_mod.detect_ats_site(
        str(job.get("application_url") or job.get("url") or "")
    )
    if provider != "smartrecruiters":
        return {"provider": provider, "task_statuses": {}}
    tasks = [
        TaskSpec(
            task_id="duplicate-snapshot",
            kind="duplicate-check",
            objective="Read the durable application ledger for an exact duplicate.",
            inputs={"job_url": str(job.get("url") or "")},
            effect_class="read",
            resource_claims=(ResourceClaim("database-read"),),
        )
    ]
    tasks.append(
        TaskSpec(
            task_id="ats-identity",
            kind="ats-identity",
            objective="Resolve the immutable public posting identity.",
            inputs={"provider": provider},
            effect_class="read",
            resource_claims=(ResourceClaim("network-read"),),
        )
    )

    def runner(task: TaskSpec, _context: object) -> TaskResult:
        started = time.perf_counter()
        if task.task_id == "ats-identity":
            binding = _resolve_ats_application_binding(job)
            return TaskResult(
                task_id=task.task_id,
                status="completed",
                output={"ats_binding": binding},
                metrics={
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3)
                },
            )
        connection = get_connection()
        try:
            if connection.in_transaction:
                raise RuntimeError("duplicate snapshot connection is already in a transaction")
            connection.execute("BEGIN")
            duplicate = application_jobs_mod.revalidate_duplicate_before_submit(
                connection,
                str(job.get("url") or ""),
            )
            connection.rollback()
            return TaskResult(
                task_id=task.task_id,
                status="completed",
                output={"duplicate": duplicate},
                metrics={
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3)
                },
            )
        finally:
            if getattr(connection, "in_transaction", False):
                connection.rollback()
            close = getattr(connection, "close", None)
            if callable(close):
                close()

    def reducer(
        state: dict[str, object],
        task: TaskSpec,
        task_result: TaskResult,
    ) -> None:
        statuses = state.setdefault("task_statuses", {})
        if isinstance(statuses, dict):
            statuses[task.task_id] = {
                "status": task_result.status,
                "failure_category": task_result.failure_category,
                "metrics": dict(task_result.metrics),
            }

    outcome = orchestration_mod.execute_task_graph(
        tasks,
        runner,
        reducer,
        max_workers=len(tasks),
        resource_capacities={"database-read": 1, "network-read": 1},
    )
    result: dict[str, object] = {
        "provider": provider,
        "task_statuses": outcome.reduced_state.get("task_statuses", {}),
    }
    duplicate_result = outcome.results["duplicate-snapshot"]
    if duplicate_result.succeeded:
        result["duplicate"] = duplicate_result.output.get("duplicate")
    binding_result = outcome.results.get("ats-identity")
    if binding_result is not None and binding_result.succeeded:
        result["ats_binding"] = binding_result.output.get("ats_binding")
    return result

# How often to poll the DB when the queue is empty (seconds)
POLL_INTERVAL = config.DEFAULTS["poll_interval"]

# Thread-safe shutdown coordination
_stop_event = threading.Event()
_cloak_lane = threading.Semaphore(1)

# Track active Claude Code processes for skip (Ctrl+C) handling
_claude_procs: dict[int, subprocess.Popen] = {}
_claude_lock = threading.Lock()


def _acquire_cloak_lane(worker_id: int) -> bool:
    """Acquire the single licensed Cloak lane while leaving Edge workers free."""
    if os.environ.get("APPLYPILOT_CLOAK_ALLOW_CONCURRENCY") == "1":
        return False
    update_state(worker_id, status="waiting", last_action="waiting for CloakBrowser lane")
    while not _stop_event.is_set():
        if _cloak_lane.acquire(timeout=0.5):
            return True
    raise InterruptedError("CloakBrowser lane wait interrupted")


def _route_for_phase(
    route: ControlRoute,
    phase: str,
    reason_code: str,
    *,
    interaction_driver: str | None = None,
    submit_owner: str | None = None,
) -> ControlRoute:
    return ControlRoute(
        interaction_driver=interaction_driver or route.interaction_driver,
        browser_runtime=route.browser_runtime,
        phase=phase,
        reason_code=reason_code,
        single_writer=route.single_writer,
        submit_owner=submit_owner or route.submit_owner,
        requires_fresh_observation=route.requires_fresh_observation,
    )


def _attach_control_contract(
    job: dict,
    route: ControlRoute,
    *,
    interaction_mode: str,
    resume_existing_page: bool,
) -> None:
    job["_browser_backend"] = route.browser_runtime
    job["_control_contract"] = prompt_control_contract(
        route,
        interaction_mode=interaction_mode,
        resume_existing_page=resume_existing_page,
    )


def _resolve_worker_count(
    requested: int,
    profile_cap: int,
    browser_backend: str,
    *,
    cloak_concurrency_allowed: bool,
) -> tuple[int, bool]:
    """Keep Edge/auto parallel while constraining explicit Cloak runs."""
    workers = min(max(1, requested), max(1, profile_cap))
    reduced_for_cloak = (
        browser_backend == "cloak" and workers > 1 and not cloak_concurrency_allowed
    )
    return (1 if reduced_for_cloak else workers), reduced_for_cloak


def _workers_for_target(workers: int, effective_limit: int) -> int:
    """Do not encode a finite worker's zero allocation as continuous mode."""
    return min(workers, effective_limit) if effective_limit > 0 else workers

# Register cleanup on exit
atexit.register(cleanup_on_exit)
if platform.system() != "Windows":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


# ---------------------------------------------------------------------------
# MCP config
# ---------------------------------------------------------------------------













def _record_worker_action(worker_id: int, description: str) -> None:
    """Keep interactive and piped CLI runs visibly moving during browser work."""
    ws = get_state(worker_id)
    cur_actions = ws.actions if ws else 0
    update_state(
        worker_id,
        actions=cur_actions + 1,
        last_action=description[:35],
    )
    add_event(f"[W{worker_id}] {description[:80]}")
    if not sys.stderr.isatty():
        logger.info("[worker-%d] %s", worker_id, description)


def _submission_rate_status(
    conn, profile: dict, now: datetime | None = None
) -> tuple[bool, float, str]:
    """Return whether another submission may start and any short cooldown."""
    policy = profile.get("submission_policy", {})
    hourly_max = int(policy.get("maximum_verified_submissions_per_rolling_hour", 15))
    minimum_gap = float(policy.get("minimum_seconds_between_verified_submissions", 20))
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    cutoff = (current - timedelta(hours=1)).isoformat()
    rows = conn.execute(
        "SELECT applied_at FROM jobs WHERE applied_at IS NOT NULL AND applied_at >= ? "
        "ORDER BY applied_at DESC",
        (cutoff,),
    ).fetchall()
    if hourly_max > 0 and len(rows) >= hourly_max:
        return False, 0.0, "rolling_hour_submission_cap"
    if rows and minimum_gap > 0:
        try:
            latest = datetime.fromisoformat(rows[0]["applied_at"])
            if latest.tzinfo is None:
                latest = latest.replace(tzinfo=UTC)
            remaining = minimum_gap - (current - latest).total_seconds()
            if remaining > 0:
                return True, remaining, "minimum_submission_gap"
        except (TypeError, ValueError):
            logger.warning("Ignoring an invalid applied_at timestamp for rate limiting")
    return True, 0.0, "ready"












def _safe_log_slug(value: object, max_length: int = 40) -> str:
    """Return a bounded cross-platform filename component for runtime evidence."""
    slug = re.sub(r"[^\w.-]+", "_", str(value or "")).strip(" ._")
    return slug[:max_length] or "unknown"


_WORKER_EVIDENCE_NAMES = (
    "final-preview.png",
    "pre-submit-review.png",
    "submission-confirmation.png",
    "submission-confirmation-observer.png",
    "submission-confirmation-observer-attempt-2.png",
    "captcha-blocked.png",
)
_evidence_retention_lock = threading.Lock()
_evidence_retention_checked = False


def _snapshot_worker_evidence(worker_id: int) -> dict[str, tuple[int, int]]:
    worker_dir = config.APPLY_WORKER_DIR / f"worker-{worker_id}"
    return snapshot_files(worker_dir, _WORKER_EVIDENCE_NAMES)


def _record_evidence_retention(
    destination: Path,
    archived: list[Path],
    job: Mapping[str, object],
) -> None:
    """Mark preview artifacts as transient; never let maintenance alter an apply result."""
    global _evidence_retention_checked
    try:
        persistent_receipt = any(
            path.name.startswith("submission-confirmation") for path in archived
        )
        mark_owned_directory(
            destination,
            root=config.LOG_DIR / "application-evidence",
            kind=("application_evidence" if persistent_receipt else "job_transient"),
            owner_id=str(job.get("_attempt_id") or job.get("url") or "preview"),
            state=("applied" if persistent_receipt else "previewed"),
            completed_at=time.time(),
        )
        with _evidence_retention_lock:
            should_reclaim = not _evidence_retention_checked
            _evidence_retention_checked = True
        if not should_reclaim:
            return
        try:
            retention_days = max(
                7.0,
                float(
                    os.environ.get(
                        "APPLYPILOT_TRANSIENT_EVIDENCE_RETENTION_DAYS",
                        "30",
                    )
                ),
            )
        except ValueError:
            retention_days = 30.0
        reclaim_terminal_artifacts(
            config.LOG_DIR / "application-evidence",
            minimum_age_seconds=retention_days * 24 * 60 * 60,
            execute=True,
        )
    except (OSError, PermissionError, RuntimeError, TypeError, ValueError):
        logger.warning("Evidence retention maintenance failed", exc_info=True)


def _archive_worker_evidence(
    worker_dir: Path,
    job: dict,
    worker_id: int,
    timestamp: str,
) -> list[Path]:
    """Preserve browser evidence before the next run resets the worker directory."""
    company = _safe_log_slug(job.get("company_name") or "unknown", 40)
    title = _safe_log_slug(job.get("title") or "job", 60)
    destination = (
        config.LOG_DIR
        / "application-evidence"
        / f"{timestamp}_w{worker_id}_{company}_{title}"
    )
    baseline = job.get("_evidence_baseline")
    archived = archive_new_evidence(
        worker_dir,
        destination,
        _WORKER_EVIDENCE_NAMES,
        baseline=(baseline if isinstance(baseline, dict) else None),
    )
    job["_evidence_baseline"] = snapshot_files(
        worker_dir,
        _WORKER_EVIDENCE_NAMES,
    )
    if archived:
        _record_evidence_retention(destination, archived, job)
    return archived
























def _submission_audit_fingerprint(
    job: Mapping[str, object],
    audit_report: Mapping[str, object] | None,
) -> str:
    """Bind a submission claim to compact page/job facts, not mutable page text."""
    audit = audit_report if isinstance(audit_report, Mapping) else {}
    payload = {
        "job_url": str(job.get("url") or ""),
        "application_url": str(job.get("application_url") or ""),
        "page_url": str(audit.get("page_url") or ""),
        "disposition": str(audit.get("disposition") or ""),
        "submit_control_count": int(audit.get("submit_control_count") or 0),
        "required_unfilled_count": len(audit.get("required_unfilled", []))
        if isinstance(audit.get("required_unfilled"), list)
        else 0,
        "binding_provider": str(
            (job.get("_ats_application_binding") or {}).get("provider") or ""
        )
        if isinstance(job.get("_ats_application_binding"), Mapping)
        else "",
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _reserve_manifest_submission(
    manifest: dict | None,
    job: dict,
    audit_report: Mapping[str, object] | None = None,
) -> tuple[bool, str]:
    """Re-authorize bytes and atomically claim final submission authority."""
    if manifest is None:
        return False, "authorization_manifest_required"
    try:
        expires_at = datetime.fromisoformat(str(manifest.get("expires_at") or ""))
        if expires_at.tzinfo is None or datetime.now(UTC) >= expires_at:
            return False, "authorization_manifest_expired"
        from applypilot.apply.authorization import authorize_job, freeze_submission_materials
        from applypilot.database import claim_submission_gate, reserve_batch_submission

        if authorize_job(manifest, job) is None:
            return False, "authorization_manifest_job_mismatch"
        profile = config.load_profile()
        material_binding = freeze_submission_materials(job, profile)
        job["_bound_submission_materials"] = material_binding
        attempt_id = str(job.get("_attempt_id") or "").strip()
        if attempt_id:
            policy = profile.get("submission_policy", {})
            if not isinstance(policy, Mapping):
                policy = {}
            fingerprint = _submission_audit_fingerprint(job, audit_report)
            connection = get_connection()
            if connection.in_transaction:
                return False, "submission_gate_transaction_busy"
            connection.execute("BEGIN IMMEDIATE")
            try:
                duplicate_check = application_jobs_mod.revalidate_duplicate_before_submit(
                    connection,
                    str(job.get("url") or ""),
                )
                job["_duplicate_revalidation"] = dict(duplicate_check)
                if duplicate_check.get("clear") is not True:
                    connection.rollback()
                    return False, str(
                        duplicate_check.get("reason") or "duplicate_revalidation_failed"
                    )
                claim = claim_submission_gate(
                    str(manifest.get("batch_id") or ""),
                    str(job.get("url") or ""),
                    int(manifest.get("max_submissions") or 0),
                    attempt_id,
                    hourly_maximum=int(
                        policy.get("maximum_verified_submissions_per_rolling_hour", 15)
                    ),
                    minimum_gap_seconds=float(
                        policy.get("minimum_seconds_between_verified_submissions", 20)
                    ),
                    audit_fingerprint=fingerprint,
                    conn=connection,
                )
                job["_submission_gate"] = dict(claim)
                if claim.get("claimed") is not True:
                    connection.rollback()
                    return False, str(claim.get("reason") or "submission_gate_denied")
                connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise
            return True, str(claim.get("reason") or "submission_gate_claimed")
        reserved = reserve_batch_submission(
            str(manifest.get("batch_id") or ""),
            str(job.get("url") or ""),
            int(manifest.get("max_submissions") or 0),
        )
        if reserved is not True:
            return False, "authorization_batch_reservation_denied"
        return True, "reserved"
    except Exception as exc:
        logger.exception("Batch submission reservation failed")
        return False, f"authorization_batch_reservation_error:{type(exc).__name__}"


def _repair_requires_resume_upload(observation: Mapping[str, object]) -> bool:
    """Expose file upload only for a visible, repairable Resume/CV error."""
    if observation.get("repair_mode") is not True:
        return False
    validation_errors = observation.get("validation_errors")
    if not isinstance(validation_errors, list):
        return False
    for error in validation_errors:
        if not isinstance(error, Mapping) or error.get("repairable") is not True:
            continue
        label = str(error.get("label") or error.get("field_label") or "")
        message = str(error.get("message") or "")
        field_type = str(error.get("field_type") or "").casefold()
        evidence = f"{label} {message}"
        if not re.search(
            r"\b(?:resume|curriculum vitae|cv)\b", evidence, re.IGNORECASE
        ):
            continue
        if field_type == "file" or re.search(
            r"\b(?:upload|attach|file|required|missing|invalid)\b",
            evidence,
            re.IGNORECASE,
        ):
            return True
    return False


def _update_submission_ledger(
    manifest: dict | None,
    job: dict,
    status: str,
    evidence: dict | None = None,
) -> bool:
    if manifest is None:
        return True
    try:
        from applypilot.database import (
            update_batch_submission_status,
            update_submission_gate_state,
        )

        ledger_evidence = dict(evidence or {})
        if job.get("_bound_submission_materials"):
            ledger_evidence["material_binding"] = job["_bound_submission_materials"]
        update_batch_submission_status(
            str(manifest.get("batch_id") or ""),
            str(job.get("url") or ""),
            status,
            evidence=ledger_evidence,
        )
        attempt_id = str(job.get("_attempt_id") or "").strip()
        if attempt_id:
            update_submission_gate_state(
                attempt_id,
                status,
                {
                    "receipt_confirmed": status == "applied",
                    "submit_started": bool(
                        isinstance(evidence, Mapping)
                        and evidence.get("submit_started", True)
                    ),
                },
            )
        return True
    except Exception:
        logger.exception("Batch submission ledger update failed")
        return False


def _admit_direct_email_receipt(job: dict, receipt: object) -> dict[str, object]:
    """Admit one provider message id before a direct-email success is recorded."""
    if not isinstance(receipt, dict):
        return {"status": "rejected", "reason": "sent_receipt_required"}
    from applypilot.database import admit_direct_email_sent_receipt

    return admit_direct_email_sent_receipt(
        str(job.get("url") or ""),
        receipt,
    )


def _reported_sent_receipt(
    output: str,
    structured_result: AgentTurnResult | None,
) -> dict[str, object] | None:
    """Extract a structured Sent receipt without copying free-form mail text."""
    candidates: list[object] = []
    if structured_result is not None:
        observations = structured_result.observations
        candidates.extend(
            (
                observations.get("sent_receipt"),
                observations.get("submission_evidence"),
            )
        )
        structured_evidence = observations.get("submission_evidence")
        if isinstance(structured_evidence, dict):
            candidates.append(structured_evidence.get("sent_receipt"))
    marker = re.search(r"SUBMISSION_EVIDENCE\s*:?\s*", output)
    if marker:
        payload = output[marker.end():].lstrip()
        try:
            raw, _ = json.JSONDecoder().raw_decode(payload)
            candidates.extend((raw, raw.get("sent_receipt") if isinstance(raw, dict) else None))
        except (json.JSONDecodeError, TypeError):
            pass
    for candidate in candidates:
        if isinstance(candidate, dict) and {
            "folder",
            "recipient",
            "subject",
            "attachment_names",
            "body_sha256",
            "provider_message_id",
        }.issubset(candidate):
            return dict(candidate)
    return None


def _redacted_agent_log_line(_raw_text: object = None) -> str:
    """Return the only log representation allowed for agent-authored text."""
    return "  >> agent_text_redacted\n"


def _wait_for_manual_captcha(
    port: int,
    worker_id: int,
    timeout_seconds: int | None = None,
    *,
    attempt_id: str | None = None,
    submit_started: bool = False,
    root_target_ids: set[str] | None = None,
    application_lease_minutes: int | None = None,
) -> bool:
    """Keep Edge alive until the applicant clears a visible verification gate."""
    from playwright.sync_api import sync_playwright

    if timeout_seconds is None:
        timeout_seconds = int(
            config.load_profile().get("submission_policy", {}).get(
                "manual_intervention_timeout_seconds", 1800
            )
        )
    if application_lease_minutes is None:
        application_lease_minutes = load_runtime_settings().application_lease_minutes
    timeout_seconds = max(60, min(timeout_seconds, 3600))
    grace_seconds = max(
        4,
        min(
            int(
                config.load_profile().get("submission_policy", {}).get(
                    "automatic_verification_grace_seconds", 12
                )
            ),
            30,
        ),
    )
    marker = config.LOG_DIR / f"manual-captcha-relay-worker-{worker_id}.json"
    marker.write_text(
        json.dumps(
            {
                "status": "observing_transient_verification",
                "port": port,
                "timeout_seconds": timeout_seconds,
                "automatic_grace_seconds": grace_seconds,
            }
        ),
        encoding="utf-8",
    )
    add_event(f"[W{worker_id}] Re-observing verification gate for {grace_seconds}s")
    update_state(worker_id, status="observing", last_action="checking transient verification")

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        browser_session = browser.new_browser_cdp_session()
        deadline = time.monotonic() + timeout_seconds
        started_waiting = time.monotonic()
        applicant_alerted = False
        clear_polls = 0
        last_lease_renewal = 0.0
        while time.monotonic() < deadline and not _stop_event.is_set():
            if attempt_id and time.monotonic() - last_lease_renewal >= 20:
                from applypilot.database import update_application_attempt

                if not update_application_attempt(
                    attempt_id,
                    phase="manual_verification",
                    submit_started=submit_started,
                    lease_minutes=application_lease_minutes,
                ):
                    marker.write_text(
                        json.dumps({"status": "attempt_lease_lost", "port": port}),
                        encoding="utf-8",
                    )
                    return False
                last_lease_renewal = time.monotonic()
            pages = [page for context in browser.contexts for page in context.pages]
            bound_pages = []
            target_infos = {
                str(info.get("targetId") or ""): info
                for info in browser_session.send("Target.getTargets").get("targetInfos", [])
                if info.get("targetId")
            }
            from applypilot.apply.credential_relay import _target_descends_from

            for page in pages:
                try:
                    info = page.context.new_cdp_session(page).send("Target.getTargetInfo")[
                        "targetInfo"
                    ]
                except Exception:  # noqa: BLE001, S112 - page can detach during navigation
                    continue
                target_id = str(info.get("targetId") or "")
                target_infos[target_id] = info
                if root_target_ids and _target_descends_from(
                    target_id, root_target_ids, target_infos
                ):
                    bound_pages.append(page)
            if not bound_pages:
                clear_polls = 0
                if _stop_event.wait(2):
                    break
                continue
            visible = any(_visible_verification_gate(page) for page in bound_pages)
            stable_clear_state = any(
                _verification_clear_state_stable(page) for page in bound_pages
            )
            if visible or not stable_clear_state:
                clear_polls = 0
                if (
                    not applicant_alerted
                    and time.monotonic() - started_waiting >= grace_seconds
                ):
                    applicant_alerted = True
                    marker.write_text(
                        json.dumps(
                            {
                                "status": "waiting_for_applicant",
                                "port": port,
                                "timeout_seconds": timeout_seconds,
                            }
                        ),
                        encoding="utf-8",
                    )
                    add_event(
                        f"[W{worker_id}] MANUAL VERIFICATION: Edge is waiting for the applicant"
                    )
                    update_state(
                        worker_id,
                        status="captcha",
                        last_action="waiting for manual verification",
                    )
                    try:
                        if platform.system() == "Windows":
                            import winsound

                            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                        else:
                            Console().print("\a", end="")
                    except Exception:
                        logger.debug(
                            "Could not emit manual-intervention alert", exc_info=True
                        )
            else:
                clear_polls += 1
                if clear_polls >= 2:
                    marker.write_text(
                        json.dumps(
                            {
                                "status": (
                                    "cleared_after_applicant_intervention"
                                    if applicant_alerted
                                    else "transient_gate_cleared"
                                ),
                                "port": port,
                            }
                        ),
                        encoding="utf-8",
                    )
                    add_event(f"[W{worker_id}] Manual verification cleared; resuming agent")
                    return True
            if _stop_event.wait(2):
                break
    except Exception as exc:
        logger.exception("Manual CAPTCHA relay failed")
        marker.write_text(
            json.dumps({"status": "relay_error", "error": str(exc)[:200]}),
            encoding="utf-8",
        )
        return False
    finally:
        playwright.stop()

    marker.write_text(
        json.dumps({"status": "timeout", "port": port}),
        encoding="utf-8",
    )
    return False













def _prepare_runtime_cover_letter(job: dict) -> dict:
    """Generate, validate, render, and approve one cover letter under standing policy."""
    policy = config.load_profile().get("submission_policy", {})
    if not policy.get("allow_agent_validated_cover_letter", False):
        raise PermissionError("Standing policy does not allow agent-validated cover letters")

    from applypilot.scoring.pdf import convert_to_pdf
    from applypilot.single_job import prepare_cover_letter_for_url

    text_path = Path(str(job.get("cover_letter_path") or ""))
    if job.get("cover_letter_status") != "machine_validated" or not text_path.is_file():
        report = prepare_cover_letter_for_url(
            str(job["url"]),
            str(job.get("company_name") or "").strip(),
            validation_mode="strict",
            resume_path=str(job.get("tailored_resume_path") or ""),
        )
        text_path = Path(str(report["text_path"]))
    pdf_path = text_path.with_suffix(".pdf")
    if not pdf_path.is_file():
        convert_to_pdf(text_path, output_path=pdf_path)
    if not pdf_path.is_file():
        raise FileNotFoundError(f"Validated cover-letter PDF was not rendered: {pdf_path}")

    conn = get_connection()
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "UPDATE jobs SET cover_letter_path=?, cover_letter_status='agent_validated', "
        "cover_letter_error=NULL, cover_letter_approved_at=?, "
        "cover_letter_approved_by='standing_policy_agent' WHERE url=?",
        (str(text_path), now, job["url"]),
    )
    conn.commit()
    refreshed = conn.execute("SELECT * FROM jobs WHERE url=?", (job["url"],)).fetchone()
    if refreshed is None:
        raise ValueError("Exact job disappeared while preparing its cover letter")
    refreshed_job = dict(refreshed)
    refreshed_job.update({key: value for key, value in job.items() if key.startswith("_")})
    return refreshed_job


# ---------------------------------------------------------------------------
# Utility modes (--gen, --mark-applied, --mark-failed, --reset-failed)
# ---------------------------------------------------------------------------

def gen_prompt(target_url: str, min_score: int = 6,
               model: str = "sonnet", worker_id: int = 0) -> Path | None:
    """Generate a prompt file and print the Claude CLI command for manual debugging.

    Returns:
        Path to the generated prompt file, or None if no job found.
    """
    job = acquire_job(
        target_url=target_url,
        min_score=min_score,
        worker_id=worker_id,
        preview_only=True,
    )
    if not job:
        return None

    # Read resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    worker_dir = reset_worker_dir(worker_id)
    prompt = prompt_mod.build_prompt(
        job=job,
        tailored_resume=resume_text,
        dry_run=True,
        worker_id=worker_id,
        worker_dir=worker_dir,
    )

    # Release the lock so the job stays available
    release_lock(job["url"], job.get("_attempt_id"))

    # Write prompt file
    config.ensure_dirs()
    site_slug = _safe_log_slug(job.get("company_name") or "unknown", 20)
    title_slug = _safe_log_slug(job.get("title") or "job", 30)
    prompt_file = config.LOG_DIR / f"prompt_{site_slug}_{title_slug}.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    # Write MCP config for reference
    port = BASE_CDP_PORT + worker_id
    mcp_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    return prompt_file


def _credential_relay_allowed(profile: dict, job: dict) -> bool:
    authentication = profile.get("authentication", {})
    return bool(
        isinstance(authentication, dict)
        and authentication.get("ats_account_creation_authorized", False)
        and job.get("_browser_backend") != "cloak"
    )


def _safe_ats_target_url(job: Mapping[str, object]) -> str:
    """Return a query-free application target suitable for Agent context."""
    raw = str(job.get("application_url") or job.get("url") or "")
    parsed = urlparse(raw)
    hostname = (parsed.hostname or "").casefold()
    if not hostname:
        return ""
    port = f":{parsed.port}" if parsed.port else ""
    scheme = parsed.scheme if parsed.scheme in {"http", "https"} else "https"
    return f"{scheme}://{hostname}{port}{parsed.path or '/'}"


def _available_semantic_fact_names(profile: Mapping[str, object], job: Mapping[str, object]) -> list[str]:
    """List confirmed semantic source names without copying applicant values."""
    facts: set[str] = set()
    personal = profile.get("personal")
    personal = personal if isinstance(personal, Mapping) else {}
    if personal.get("full_name"):
        facts.update({"full_name", "first_name", "last_name"})
    semantic_personal = {
        "email": ("email",),
        "phone": ("phone", "phone_number"),
        "linkedin": ("linkedin", "linkedin_url"),
        "website": ("website", "portfolio", "github", "github_url"),
        "location": ("location", "city", "country", "address"),
    }
    for semantic, keys in semantic_personal.items():
        if any(personal.get(key) for key in keys):
            facts.add(semantic)
    if job.get("tailored_resume_path") or job.get("_staged_resume_path"):
        facts.add("resume")
    if job.get("cover_letter_path") or job.get("_staged_cover_letter_path"):
        facts.add("cover_letter")
    if isinstance(profile.get("work_authorization"), Mapping):
        facts.update({"work_authorization", "sponsorship"})
    application_facts = profile.get("application_facts")
    if isinstance(application_facts, list):
        for fact in application_facts:
            if not isinstance(fact, Mapping):
                continue
            key = str(fact.get("key") or "").strip().casefold()
            if key and len(key) <= 120 and re.fullmatch(r"[a-z0-9_.-]+", key):
                facts.add(key)
    return sorted(facts)


def _build_ats_application_context(
    job: Mapping[str, object], profile: Mapping[str, object]
) -> dict[str, object]:
    """Build the bounded read/proposal-only context shared with ATS tools."""
    target_url = _safe_ats_target_url(job)
    adapter = ats_mod.detect_ats_site(target_url)
    context: dict[str, object] = {
        "schema_version": ats_mod.ATS_SCHEMA_VERSION,
        "adapter": adapter,
        "target_url": target_url,
        "guidance": list(ats_mod.adapter_prompt_guidance(target_url)),
        "available_fact_names": _available_semantic_fact_names(profile, job),
        "side_effect": "proposal-only",
    }
    observation = job.get("_browser_observation")
    if isinstance(observation, Mapping):
        form_context = observation.get("ats_adapter_context")
        if isinstance(form_context, Mapping):
            context["observed_form"] = dict(form_context)
            context["adapter"] = str(form_context.get("adapter") or adapter)
        workday_context = observation.get("workday_state")
        if isinstance(workday_context, Mapping):
            context["workday_state"] = dict(workday_context)
    return context





def run_job(job: dict, port: int, worker_id: int = 0,
            model: str = "sonnet", dry_run: bool = False,
            agent_backend: str = "codex",
            manual_captcha_relay: bool = False,
            resume_existing_page: bool = False,
            submission_phase: str = "submit") -> tuple[str, int]:
    """Spawn a Claude Code session for one job application.

    Returns:
        Tuple of (status_string, duration_ms). Status is one of:
        'applied', 'submission_uncertain', 'expired', 'captcha', 'login_issue',
        'failed:reason', or 'skipped'.
    """
    setup_started = time.perf_counter()
    setup_metrics: dict[str, int | float] = {}
    runtime_settings = load_runtime_settings()
    agent_timeout_seconds = runtime_settings.agent_timeout_seconds
    profile = config.load_profile()
    authentication = profile.get("authentication", {})
    credential_relay_authorized = _credential_relay_allowed(profile, job)
    playwright_mcp, capability_registry = _resolve_agent_tool_surface(
        profile,
        job,
        environ=dict(runtime_settings.environ),
    )
    setup_metrics["tool_surface_ms"] = round(
        (time.perf_counter() - setup_started) * 1000,
        3,
    )
    mailbox_mcp = _resolve_mailbox_tool_surface(
        profile,
        job,
        environ=dict(runtime_settings.environ),
    )
    mailbox_access_authorized = bool(
        authentication.get(
            "mailbox_read_authorized",
            authentication.get("gmail_verification_authorized", False),
        )
        and (
            authentication.get("mailbox")
            or authentication.get("gmail_verification_mailbox")
        )
    )
    direct_email_send_authorized = bool(
        not dry_run
        and direct_email_send_is_reserved(job, submission_phase=submission_phase)
        and profile.get("submission_policy", {}).get(
            "direct_email_application_authorized", False
        )
    )
    runtime_state = {submission_phase}
    observation = job.get("_browser_observation")
    if isinstance(observation, Mapping):
        if observation.get("repair_mode") is True:
            runtime_state.add("repair")
            if _repair_requires_resume_upload(observation):
                runtime_state.add("repair_resume_upload")
        if observation.get("verification_resume") is True:
            runtime_state.add("verification_resumed")
        if observation.get("submission_gate") is True:
            runtime_state.add("reserved")
    if submission_phase == "submit" and job.get("_submission_gate"):
        runtime_state.add("reserved")
    runtime_ats_adapter = ats_mod.detect_ats_site(_safe_ats_target_url(job))
    runtime_state.add(
        "ats_unknown"
        if runtime_ats_adapter in {"", "generic"}
        else f"ats_{runtime_ats_adapter.casefold()}"
    )
    runtime_route = "direct_email" if direct_email_send_authorized else "browser"
    runtime_capabilities = scope_capability_registry(
        compose_runtime_capabilities(capability_registry),
        phase=submission_phase,
        route=runtime_route,
        state=runtime_state,
    )
    semantic_email_tools: list[str] = []
    if mailbox_mcp.enabled and mailbox_access_authorized:
        semantic_email_tools.extend(("mailbox_search", "mailbox_get_message"))
    if mailbox_mcp.enabled and direct_email_send_authorized:
        semantic_email_tools.append("direct_email_send")
    job["_available_tools"] = [*runtime_capabilities.names(), *semantic_email_tools]
    job["_agent_orchestration_available"] = callable(
        job.get("_agent_proposal_runner")
    )
    setup_metrics["exposed_tool_count"] = len(job["_available_tools"])
    # Read tailored resume text
    resume_path = job.get("tailored_resume_path")
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    if resume_existing_page:
        worker_dir = config.APPLY_WORKER_DIR / f"worker-{worker_id}"
        worker_dir.mkdir(parents=True, exist_ok=True)
    else:
        worker_dir = reset_worker_dir(worker_id)

    run_id = f"agent-{uuid.uuid4()}"
    attempt_id = str(job.get("_attempt_id") or f"preview-{run_id}")
    report_path = worker_dir / "agent-turn-report.json"
    ats_context_path = worker_dir / "ats-application-context.json"
    if report_path.exists():
        report_path.unlink()
    if ats_context_path.exists():
        ats_context_path.unlink()

    workspace_root = config.APP_DIR.parent
    secure_fill_script = workspace_root / "fill-ats-credentials.ps1"
    if credential_relay_authorized and secure_fill_script.is_file():
        shutil.copy2(secure_fill_script, worker_dir / secure_fill_script.name)

    # Build the prompt and stage attachments only inside this worker directory.
    job["_agent_backend"] = agent_backend
    job["_agent_reporting_enabled"] = True
    ats_context = _build_ats_application_context(job, profile)
    job["_ats_adapter_context"] = ats_context
    prompt_started = time.perf_counter()
    agent_prompt = prompt_mod.build_prompt(
        job=job,
        tailored_resume=resume_text,
        dry_run=dry_run,
        worker_id=worker_id,
        worker_dir=worker_dir,
        manual_captcha_relay=manual_captcha_relay,
        resume_existing_page=resume_existing_page,
        submission_phase=submission_phase,
    )
    setup_metrics["prompt_build_ms"] = round(
        (time.perf_counter() - prompt_started) * 1000,
        3,
    )
    setup_metrics["prompt_chars"] = len(agent_prompt)
    setup_metrics["prompt_bytes"] = len(agent_prompt.encode("utf-8"))
    selected_fragments = job.get("_selected_prompt_fragments")
    setup_metrics["prompt_fragment_count"] = (
        len(selected_fragments) if isinstance(selected_fragments, list) else 0
    )

    # Write per-worker MCP config
    runtime_metadata: dict = {}
    mcp_config_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_config_path.write_text(
        json.dumps(
            _make_mcp_config(
                port,
                playwright_mcp=playwright_mcp,
                capability_registry=runtime_capabilities,
                runtime_metadata=runtime_metadata,
                mailbox_mcp=mailbox_mcp,
                direct_email_send_authorized=direct_email_send_authorized,
            )
        ),
        encoding="utf-8",
    )

    cmd, final_message_path = _build_agent_command(
        backend=agent_backend,
        model=model,
        port=port,
        worker_dir=worker_dir,
        mcp_config_path=mcp_config_path,
        credential_relay_authorized=credential_relay_authorized,
        playwright_mcp=playwright_mcp,
        capability_registry=runtime_capabilities,
        runtime_metadata=runtime_metadata,
        mailbox_mcp=mailbox_mcp,
        direct_email_send_authorized=direct_email_send_authorized,
        workload_class=(
            "submit_repair"
            if submission_phase == "submit" and "repair" in runtime_state
            else submission_phase
        ),
        reasoning_efforts=(
            dict(profile.get("agent_runtime", {}).get("reasoning_efforts", {}))
            if isinstance(profile.get("agent_runtime"), Mapping)
            and isinstance(
                profile.get("agent_runtime", {}).get("reasoning_efforts"),
                Mapping,
            )
            else None
        ),
    )
    setup_metrics["turn_setup_ms"] = round(
        (time.perf_counter() - setup_started) * 1000,
        3,
    )
    runtime_metadata["performance"] = dict(setup_metrics)
    if final_message_path and final_message_path.exists():
        final_message_path.unlink()

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    agent_runtime_mod.apply_mcp_process_environment(env, playwright_mcp)
    agent_runtime_mod.apply_mcp_process_environment(env, mailbox_mcp)
    env["APPLYPILOT_CDP_PORT"] = str(port)
    env["APPLYPILOT_WORKSPACE_ROOT"] = str(workspace_root)
    env[REPORT_PATH_ENV] = str(report_path)
    env[RUN_ID_ENV] = run_id
    env[ATS_CONTEXT_PATH_ENV] = str(ats_context_path)
    for key in (
        "APPLYPILOT_ATS_CREDENTIAL_FILE",
        "APPLYPILOT_CREDENTIAL_ALLOWED_HOSTS",
        "APPLYPILOT_CREDENTIAL_PASSWORD_ALLOWED_HOSTS",
        "APPLYPILOT_CREDENTIAL_ALLOW_KNOWN_ATS_REDIRECT",
        "APPLYPILOT_CREDENTIAL_ROOT_TARGET_IDS",
        "APPLYPILOT_CREDENTIAL_RELAY_AUTHORIZED",
    ):
        env.pop(key, None)
    allowed_hosts = {
        (urlparse(str(url)).hostname or "").lower()
        for url in (job.get("url"), job.get("application_url"))
        if url
    }
    if credential_relay_authorized:
        configured_password_hosts = authentication.get(
            "ats_credential_allowed_hosts", []
        )
        if not isinstance(configured_password_hosts, list):
            configured_password_hosts = []
        env["APPLYPILOT_CREDENTIAL_RELAY_AUTHORIZED"] = "1"
        env["APPLYPILOT_ATS_CREDENTIAL_FILE"] = str(
            config.APP_DIR / "credentials" / "ats-signup.json"
        )
        env["APPLYPILOT_CREDENTIAL_ALLOWED_HOSTS"] = ",".join(
            sorted(host for host in allowed_hosts if host)
        )
        env["APPLYPILOT_CREDENTIAL_PASSWORD_ALLOWED_HOSTS"] = ",".join(
            sorted(
                str(host).strip().casefold()
                for host in configured_password_hosts
                if str(host).strip()
            )
        )
        # Permit a legitimate employer-page -> known ATS redirect, but the relay
        # still requires exactly one eligible browser tab before filling anything.
        env["APPLYPILOT_CREDENTIAL_ALLOW_KNOWN_ATS_REDIRECT"] = "1"
        current_runtime = str(job.get("_browser_backend") or "")
        credential_root_ids = set(job.get("_browser_root_target_ids") or [])
        if job.get("_browser_root_runtime") != current_runtime:
            credential_root_ids = set()
        env["APPLYPILOT_CREDENTIAL_ROOT_TARGET_IDS"] = ",".join(
            sorted(credential_root_ids)
        )

    agent_request = AgentRunRequest(
        run_id=run_id,
        attempt_id=attempt_id,
        agent_role=str(job.get("_agent_role") or "browser-application-agent"),
        phase=submission_phase,
        objective=(
            "Prepare and preview the application"
            if dry_run
            else f"Complete the {submission_phase} application turn"
        ),
        context={
            "worker_id": worker_id,
            "browser_backend": str(job.get("_browser_backend") or "unknown"),
            "resume_existing_page": resume_existing_page,
            "dry_run": dry_run,
            "ats_adapter": str(ats_context.get("adapter") or "generic"),
            "ats_context_schema_version": str(
                ats_context.get("schema_version") or ats_mod.ATS_SCHEMA_VERSION
            ),
            **(
                {"workday_state": ats_context["workday_state"]}
                if isinstance(ats_context.get("workday_state"), Mapping)
                else {}
            ),
        },
        available_tools=tuple(job["_available_tools"]),
        parent_run_id=(
            str(job["_parent_agent_run_id"])
            if job.get("_parent_agent_run_id")
            else None
        ),
        concurrency_mode=str(job.get("_agent_concurrency_mode") or "serial_per_page"),
    )

    update_state(worker_id, status="applying", job_title=job["title"],
                 company=job.get("company_name", ""), score=job.get("fit_score", 0),
                 start_time=time.time(), actions=0, last_action="starting")
    add_event(f"[W{worker_id}] Starting: {job['title'][:40]} @ {job.get('company_name', '')}")

    worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
    ts_header = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    log_header = (
        f"\n{'=' * 60}\n"
        f"[{ts_header}] {job['title']} @ {job.get('site', '')}\n"
        f"URL: {job.get('application_url') or job['url']}\n"
        f"Score: {job.get('fit_score', 'N/A')}/10\n"
        f"{'=' * 60}\n"
    )

    start = time.time()
    ats_context_path.write_text(
        json.dumps(ats_context, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    stats: dict = {}
    proc = None
    watchdog: threading.Timer | None = None
    timed_out = threading.Event()
    agent_process_started = False
    turn_result: AgentTurnResult | None = None
    turn_application_status: str | None = None
    turn_duration_ms: int | None = None
    turn_source = "runtime"
    pending_mailbox_tools: dict[str, dict[str, object]] = {}
    mailbox_runtime_evidence = {
        "send_call_completed": False,
        "post_send_search_completed": False,
        "post_send_read_completed": False,
        "send_request_bound": False,
    }
    searched_message_id: str | None = None
    provider_sent_receipt: dict[str, object] | None = None
    process_spawned_at: float | None = None
    first_output_at: float | None = None
    first_tool_at: float | None = None
    last_tool_at: float | None = None
    tool_call_count = 0
    unique_tools: set[str] = set()
    prepare_search_events: list[tuple[object, object]] = []
    if submission_phase == "prepare":
        job.pop("_mailbox_prepare_duplicate_receipt", None)

    def record_mailbox_completion(
        tool_name: str,
        *,
        succeeded: bool,
        tool_input: object = None,
        tool_output: object = None,
    ) -> None:
        nonlocal searched_message_id, provider_sent_receipt
        if not succeeded:
            return
        observation = job.get("_browser_observation")
        plan = (
            observation.get("email_application")
            if isinstance(observation, dict)
            else None
        )
        if not isinstance(plan, dict):
            return
        if tool_name == mailbox_mcp.send_tool:
            bound = mailbox_send_input_matches_plan(
                tool_input,
                plan,
            )
            mailbox_runtime_evidence["send_request_bound"] = bound
            mailbox_runtime_evidence["send_call_completed"] = bound
        elif (
            tool_name == mailbox_mcp.search_tool
            and submission_phase == "prepare"
            and not mailbox_runtime_evidence["send_call_completed"]
        ):
            prepare_search_events.append((tool_input, tool_output))
        elif mailbox_runtime_evidence["send_call_completed"]:
            if tool_name == mailbox_mcp.search_tool:
                if mailbox_sent_search_input_matches_plan(tool_input, plan):
                    searched_message_id = mailbox_search_message_id(tool_output)
                    mailbox_runtime_evidence["post_send_search_completed"] = bool(
                        searched_message_id
                    )
            elif (
                tool_name == mailbox_mcp.read_tool
                and searched_message_id
                and mailbox_read_input_matches_message(tool_input, searched_message_id)
            ):
                provider_sent_receipt = normalize_mailbox_read_receipt(
                    tool_output,
                    plan,
                    searched_message_id,
                )
                mailbox_runtime_evidence["post_send_read_completed"] = bool(
                    provider_sent_receipt
                )

    try:
        process_spawn_started = time.perf_counter()
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(worker_dir),
        )
        process_spawned_at = time.perf_counter()
        setup_metrics["process_spawn_ms"] = round(
            (process_spawned_at - process_spawn_started) * 1000,
            3,
        )
        with _claude_lock:
            _claude_procs[worker_id] = proc
        agent_process_started = True
        timed_out, watchdog = _start_timeout_watchdog(proc, agent_timeout_seconds)
        _persist_agent_turn_started(
            agent_request,
            backend=agent_backend,
            model=model,
            runtime_metadata=runtime_metadata,
        )

        try:
            proc.stdin.write(agent_prompt)
            proc.stdin.close()
        except BrokenPipeError as exc:
            startup_output = proc.stdout.read() if proc.stdout else ""
            proc.wait(timeout=5)
            raise RuntimeError(
                "Agent exited before accepting the prompt: "
                + startup_output.strip()[:500]
            ) from exc

        text_parts: list[str] = []
        with open(worker_log, "a", encoding="utf-8") as lf:
            lf.write(log_header)

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                if first_output_at is None:
                    first_output_at = time.perf_counter()
                try:
                    msg = json.loads(line)
                    msg_type = msg.get("type")
                    if msg_type == "assistant":
                        for block in msg.get("message", {}).get("content", []):
                            bt = block.get("type")
                            if bt == "text":
                                text_parts.append(block["text"])
                                lf.write(_redacted_agent_log_line(block["text"]))
                            elif bt == "tool_use":
                                raw_name = block.get("name", "")
                                now_tool = time.perf_counter()
                                if first_tool_at is None:
                                    first_tool_at = now_tool
                                last_tool_at = now_tool
                                tool_call_count += 1
                                unique_tools.add(str(raw_name))
                                name = (
                                    raw_name
                                    .replace("mcp__playwright__", "")
                                    .replace("mcp__gmail__", "gmail:")
                                )
                                mailbox_prefix = f"mcp__{mailbox_mcp.server_name}__"
                                if raw_name.startswith(mailbox_prefix):
                                    pending_mailbox_tools[str(block.get("id") or "")] = {
                                        "name": raw_name.removeprefix(mailbox_prefix),
                                        "input": dict(block.get("input") or {}),
                                    }
                                inp = block.get("input", {})
                                if "url" in inp:
                                    desc = f"{name} {inp['url'][:60]}"
                                elif "ref" in inp:
                                    desc = f"{name} {inp.get('element', inp.get('text', ''))}"[:50]
                                elif "fields" in inp:
                                    desc = f"{name} ({len(inp['fields'])} fields)"
                                elif "paths" in inp:
                                    desc = f"{name} upload"
                                else:
                                    desc = name

                                lf.write(f"  >> {desc}\n")
                                _record_worker_action(worker_id, desc)
                    elif msg_type == "user":
                        for block in msg.get("message", {}).get("content", []):
                            if block.get("type") != "tool_result":
                                continue
                            pending = pending_mailbox_tools.pop(
                                str(block.get("tool_use_id") or ""),
                                {},
                            )
                            tool_name = str(pending.get("name") or "")
                            if tool_name:
                                record_mailbox_completion(
                                    tool_name,
                                    succeeded=block.get("is_error") is not True,
                                    tool_input=pending.get("input"),
                                    tool_output=block.get("content"),
                                )
                    elif msg_type == "result":
                        stats = {
                            "input_tokens": msg.get("usage", {}).get("input_tokens", 0),
                            "output_tokens": msg.get("usage", {}).get("output_tokens", 0),
                            "cache_read": msg.get("usage", {}).get("cache_read_input_tokens", 0),
                            "cache_create": msg.get("usage", {}).get("cache_creation_input_tokens", 0),
                            "cost_usd": msg.get("total_cost_usd", 0),
                            "turns": msg.get("num_turns", 0),
                        }
                        text_parts.append(msg.get("result", ""))
                    elif msg_type == "item.completed":
                        item = msg.get("item", {})
                        item_type = item.get("type")
                        if item_type == "agent_message":
                            text = item.get("text", "")
                            if text:
                                text_parts.append(text)
                                lf.write(_redacted_agent_log_line(text))
                        elif item_type in {"mcp_tool_call", "tool_call"}:
                            server = item.get("server", "playwright")
                            tool = item.get("tool", item.get("name", "tool"))
                            now_tool = time.perf_counter()
                            if first_tool_at is None:
                                first_tool_at = now_tool
                            last_tool_at = now_tool
                            tool_call_count += 1
                            unique_tools.add(f"{server}:{tool}")
                            desc = f"{server}:{tool}"
                            lf.write(f"  >> {desc}\n")
                            _record_worker_action(worker_id, desc)
                            if server == mailbox_mcp.server_name:
                                record_mailbox_completion(
                                    str(tool),
                                    succeeded=(
                                        item.get("error") in (None, "")
                                        and str(item.get("status") or "completed").casefold()
                                        in {"completed", "success", "succeeded"}
                                    ),
                                    tool_input=item.get("input", item.get("arguments")),
                                    tool_output=item.get(
                                        "result",
                                        item.get("output", item.get("content")),
                                    ),
                                )
                    elif msg_type == "turn.completed":
                        usage = msg.get("usage", {})
                        stats = {
                            "input_tokens": usage.get("input_tokens", 0),
                            "output_tokens": usage.get("output_tokens", 0),
                            "cache_read": usage.get("cached_input_tokens", 0),
                            "cache_create": usage.get("cache_write_input_tokens", 0),
                            "cost_usd": 0,
                            "turns": 1,
                        }
                except json.JSONDecodeError:
                    text_parts.append(line)
                    lf.write(_redacted_agent_log_line(line))

        if watchdog is not None:
            watchdog.cancel()
        proc.wait(timeout=5)
        returncode = proc.returncode
        proc = None
        job["_mailbox_runtime_evidence"] = dict(mailbox_runtime_evidence)

        if timed_out.is_set():
            duration_ms = int((time.time() - start) * 1000)
            elapsed = int(time.time() - start)
            add_event(f"[W{worker_id}] TIMEOUT ({elapsed}s)")
            turn_application_status = _runtime_timeout_status(
                submission_phase=submission_phase,
                dry_run=dry_run,
            )
            uncertain = turn_application_status == "submission_uncertain"
            status = "submission_uncertain" if uncertain else "failed"
            update_state(worker_id, status=status, last_action=f"TIMEOUT ({elapsed}s)")
            turn_duration_ms = duration_ms
            turn_source = "runtime_timeout"
            turn_result = AgentTurnResult(
                run_id=run_id,
                status=turn_application_status,
                summary=f"Agent process timed out after {elapsed}s",
            )
            return turn_application_status, duration_ms

        if returncode and returncode < 0:
            status = "submission_uncertain" if submission_phase == "submit" and not dry_run else "skipped"
            turn_application_status = status
            turn_duration_ms = int((time.time() - start) * 1000)
            turn_source = "runtime_interrupted"
            turn_result = AgentTurnResult(
                run_id=run_id,
                status=status,
                summary="Agent process was interrupted",
            )
            return status, turn_duration_ms

        if final_message_path and final_message_path.exists():
            final_text = final_message_path.read_text(encoding="utf-8").strip()
            if final_text and final_text not in text_parts:
                text_parts.append(final_text)
            final_message_path.unlink()
        output = "\n".join(text_parts)
        elapsed = int(time.time() - start)
        duration_ms = int((time.time() - start) * 1000)

        unanswered = _parse_unanswered_questions(output)
        if unanswered is not None:
            from applypilot.database import record_unanswered_questions
            record_unanswered_questions(job["url"], unanswered)

        ts = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        site_slug = _safe_log_slug(
            job.get("site") or job.get("source_site") or "unknown",
            20,
        )
        job_log = config.LOG_DIR / f"agent_{ts}_w{worker_id}_{site_slug}.txt"
        job_log.write_text(
            "Agent output redacted; authoritative status is stored in the application ledger.\n",
            encoding="utf-8",
        )
        archived_evidence = (
            _archive_worker_evidence(worker_dir, job, worker_id, ts)
            if submission_phase == "prepare" or dry_run
            else []
        )
        if archived_evidence:
            logger.info(
                "[worker-%d] Archived %d browser evidence file(s) to %s",
                worker_id,
                len(archived_evidence),
                archived_evidence[0].parent,
            )

        if stats:
            cost = stats.get("cost_usd", 0)
            ws = get_state(worker_id)
            prev_cost = ws.total_cost if ws else 0.0
            update_state(worker_id, total_cost=prev_cost + cost)

        auth_markers = (
            "failed to authenticate",
            "oauth access token has been revoked",
            "not logged in",
            "unauthorized",
        )
        if any(marker in output.casefold() for marker in auth_markers):
            add_event(f"[W{worker_id}] AUTHENTICATION FAILED ({elapsed}s)")
            uncertain = submission_phase == "submit" and not dry_run
            status = "submission_uncertain" if uncertain else "failed:authentication"
            update_state(
                worker_id,
                status="submission_uncertain" if uncertain else "failed",
                last_action="authentication failed",
            )
            turn_application_status = status
            turn_duration_ms = duration_ms
            turn_source = "runtime_authentication"
            turn_result = AgentTurnResult(
                run_id=run_id,
                status=status,
                summary="Agent runtime authentication failed",
            )
            return status, duration_ms

        structured_result = None
        structured_report_invalid = False
        if report_path.exists():
            try:
                structured_result = _load_agent_turn_report(
                    report_path,
                    expected_run_id=run_id,
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                structured_report_invalid = True
                logger.warning("Ignoring invalid Agent report for %s: %s", run_id, exc)
        status, evidence, result_source = _reconcile_agent_turn_outputs(
            output,
            structured_result,
            dry_run=dry_run,
            submission_phase=submission_phase,
        )
        observation = job.get("_browser_observation")
        email_plan = (
            observation.get("email_application")
            if isinstance(observation, dict)
            else None
        )
        if submission_phase == "prepare" and structured_result is not None:
            reported_plan = structured_result.observations.get("email_application")
            if isinstance(reported_plan, dict):
                for search_input, search_output in prepare_search_events:
                    duplicate_receipt = mailbox_prepare_duplicate_receipt(
                        search_input,
                        search_output,
                        reported_plan,
                    )
                    if duplicate_receipt is not None:
                        job["_mailbox_prepare_duplicate_receipt"] = duplicate_receipt
                        break
        if (
            isinstance(email_plan, dict)
            and mailbox_runtime_evidence["send_call_completed"]
            and mailbox_runtime_evidence["post_send_search_completed"]
            and mailbox_runtime_evidence["post_send_read_completed"]
        ):
            reported_receipt = normalize_sent_receipt(
                _reported_sent_receipt(output, structured_result),
                email_plan,
            )
            if reported_receipt == provider_sent_receipt:
                mailbox_runtime_evidence["sent_receipt"] = provider_sent_receipt
            job["_mailbox_runtime_evidence"] = dict(mailbox_runtime_evidence)
        if structured_report_invalid:
            result_source = f"legacy_after_invalid_structured:{result_source}"
        failure_context = _parse_failure_context(output)
        if failure_context is not None:
            job["_failure_context"] = failure_context
        if evidence is not None:
            job["_agent_submission_evidence"] = evidence
        observations: dict[str, object] = {}
        if evidence is not None:
            observations["submission_evidence"] = evidence
        if failure_context is not None:
            observations["failure_context"] = failure_context
        turn_result = structured_result or AgentTurnResult(
            run_id=run_id,
            status=status,
            summary=f"Legacy Agent output resolved to {status}",
            observations=observations,
        )
        turn_application_status = status
        turn_duration_ms = duration_ms
        turn_source = result_source
        job["_agent_turn_result"] = contract_json(turn_result)
        job["_agent_observations"] = dict(turn_result.observations)
        job["_agent_turn_source"] = result_source
        if _proposal_dispatch_allowed(
            result_source=result_source,
            phase=submission_phase,
            dry_run=dry_run,
        ):
            proposal_outcomes, proposal_workers = _execute_agent_proposals(
                profile,
                job,
                turn_result,
            )
            if proposal_outcomes:
                _persist_agent_proposal_outcomes(
                    agent_request,
                    proposal_outcomes,
                    max_workers=proposal_workers,
                )
        elif turn_result.proposals:
            proposal_ids = tuple(
                proposal.proposal_id for proposal in turn_result.proposals
            )
            if result_source in {"structured", "structured+legacy"}:
                job["_agent_proposals_pending"] = proposal_ids
                job["_agent_proposal_deferred_reason"] = "submit_critical_path"
            else:
                job["_agent_proposals_rejected"] = proposal_ids
                job["_agent_proposal_deferred_reason"] = "untrusted_result"
        display_status = status.split(":", 1)[0]
        add_event(f"[W{worker_id}] {display_status.upper()} ({elapsed}s): {job['title'][:30]}")
        update_state(
            worker_id,
            status=display_status,
            last_action=f"{display_status.upper()} ({elapsed}s)",
        )
        return status, duration_ms

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start) * 1000)
        elapsed = int(time.time() - start)
        add_event(f"[W{worker_id}] TIMEOUT ({elapsed}s)")
        turn_application_status = _runtime_timeout_status(
            submission_phase=submission_phase,
            dry_run=dry_run,
        )
        uncertain = turn_application_status == "submission_uncertain"
        update_state(
            worker_id,
            status="submission_uncertain" if uncertain else "failed",
            last_action=f"TIMEOUT ({elapsed}s)",
        )
        turn_duration_ms = duration_ms
        turn_source = "runtime_timeout"
        turn_result = AgentTurnResult(
            run_id=run_id,
            status=turn_application_status,
            summary=f"Agent process timed out after {elapsed}s",
        )
        return turn_application_status, duration_ms
    except Exception as e:  # noqa: BLE001
        duration_ms = int((time.time() - start) * 1000)
        add_event(f"[W{worker_id}] ERROR: {str(e)[:40]}")
        uncertain = submission_phase == "submit" and not dry_run
        update_state(
            worker_id,
            status="submission_uncertain" if uncertain else "failed",
            last_action=f"ERROR: {str(e)[:25]}",
        )
        turn_application_status = (
            "submission_uncertain" if uncertain else f"failed:{str(e)[:100]}"
        )
        turn_duration_ms = duration_ms
        turn_source = "runtime_exception"
        turn_result = AgentTurnResult(
            run_id=run_id,
            status=turn_application_status,
            summary=f"Agent runtime error: {str(e)[:180]}",
        )
        return turn_application_status, duration_ms
    finally:
        if watchdog is not None:
            watchdog.cancel()
        with _claude_lock:
            _claude_procs.pop(worker_id, None)
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc.pid)
        if agent_process_started:
            final_duration_ms = turn_duration_ms or int((time.time() - start) * 1000)
            final_status = turn_application_status or (
                "submission_uncertain"
                if submission_phase == "submit" and not dry_run
                else "failed:runtime_interrupted"
            )
            final_result = turn_result or AgentTurnResult(
                run_id=run_id,
                status=final_status,
                summary="Agent turn ended without a terminal runtime result",
            )
            job["_agent_turn_result"] = contract_json(final_result)
            job["_agent_observations"] = dict(final_result.observations)
            job["_agent_turn_source"] = turn_source
            _persist_agent_turn_completed(
                agent_request,
                final_result,
                application_status=final_status,
                duration_ms=final_duration_ms,
                source=turn_source,
                metrics={
                    **setup_metrics,
                    **stats,
                    "first_output_ms": (
                        0
                        if first_output_at is None or process_spawned_at is None
                        else round((first_output_at - process_spawned_at) * 1000, 3)
                    ),
                    "first_tool_ms": (
                        0
                        if first_tool_at is None or process_spawned_at is None
                        else round((first_tool_at - process_spawned_at) * 1000, 3)
                    ),
                    "last_tool_ms": (
                        0
                        if last_tool_at is None or process_spawned_at is None
                        else round((last_tool_at - process_spawned_at) * 1000, 3)
                    ),
                    "tool_call_count": tool_call_count,
                    "unique_tool_count": len(unique_tools),
                },
            )
        if report_path.exists():
            try:
                report_path.unlink()
            except OSError as exc:
                logger.warning("Could not remove Agent turn report %s: %s", report_path, exc)
        if ats_context_path.exists():
            try:
                ats_context_path.unlink()
            except OSError as exc:
                logger.warning("Could not remove ATS context %s: %s", ats_context_path, exc)


# ---------------------------------------------------------------------------
# Permanent failure classification
# ---------------------------------------------------------------------------

PERMANENT_FAILURES: set[str] = {
    "expired",
    "already_applied",
    "not_a_job_application", "unsafe_permissions",
    "unsafe_verification",
    "assessment", "assessment_required",
}

PERMANENT_PREFIXES: tuple[str, ...] = (
    "manual_review_required:submission_validation",
    "assessment",
    "unsafe_verification",
)

def _should_retry_with_cloak(result: str, requested_backend: str) -> bool:
    """Compatibility wrapper for the structured pre-submit route policy."""
    return cloak_fallback_route(
        result,
        requested_browser_backend=requested_backend,
        phase="prepare",
        current_runtime="edge",
        fallback_already_used=False,
    ) is not None


def _is_permanent_failure(result: str) -> bool:
    """Determine whether automatic retries should remain blocked."""
    reason = result.split(":", 1)[-1] if ":" in result else result
    descriptor = classify_failure(result)
    return (
        descriptor.permanent
        or
        result in PERMANENT_FAILURES
        or reason in PERMANENT_FAILURES
        or any(reason.startswith(p) for p in PERMANENT_PREFIXES)
    )


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------


def main(limit: int = 1, target_url: str | None = None,
         min_score: int = 6, headless: bool = False, model: str = "sonnet",
         dry_run: bool = False, continuous: bool = False,
         poll_interval: int = 60, workers: int = 1,
         agent_backend: str = "codex",
         manual_captcha_relay: bool = False,
         browser_backend: str = "edge",
         interaction_mode: str = "auto",
         authorization_manifest: dict | None = None) -> None:
    """Launch the apply pipeline.

    Args:
        limit: Confirmed submissions to achieve (preview jobs for dry-run); 0 or continuous=True runs forever.
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome in headless mode.
        model: Claude model name.
        dry_run: Don't click Submit.
        continuous: Run forever, polling for new jobs.
        poll_interval: Seconds between DB polls when queue is empty.
        workers: Number of parallel workers (default 1).
    """
    global POLL_INTERVAL
    POLL_INTERVAL = poll_interval
    _stop_event.clear()

    config.ensure_dirs()
    console = Console()
    requested_browser_backend = resolve_browser_backend(browser_backend)
    requested_interaction_mode = resolve_interaction_mode(interaction_mode)

    if not dry_run and authorization_manifest is None:
        raise ValueError("Every real submission requires an authorization manifest.")

    submission_policy = config.load_profile().get("submission_policy", {})
    if (
        not dry_run
        and submission_policy.get("batch_final_authorization_required", False)
        and not authorization_manifest.get("_final_submission_authorized", False)
    ):
        raise ValueError(
            "One final batch authorization is required before browser submission."
        )

    profile_worker_cap = int(submission_policy.get("maximum_workers", 1))
    workers, reduced_for_cloak = _resolve_worker_count(
        workers,
        profile_worker_cap,
        requested_browser_backend,
        cloak_concurrency_allowed=(
            bool(submission_policy.get("cloak_concurrency_allowed", False))
            or os.environ.get("APPLYPILOT_CLOAK_ALLOW_CONCURRENCY") == "1"
        ),
    )
    if reduced_for_cloak:
        console.print(
            "[yellow]CloakBrowser defaults to one worker; set "
            "submission_policy.cloak_concurrency_allowed or "
            "APPLYPILOT_CLOAK_ALLOW_CONCURRENCY=1 only when the license permits it.[/yellow]"
        )
        workers = 1

    if continuous:
        effective_limit = 0
        mode_label = "continuous"
    else:
        run_cap = int(
            config.load_profile().get("submission_policy", {}).get(
                "maximum_verified_submissions_per_run", 12
            )
        )
        effective_limit = min(limit, run_cap) if run_cap > 0 else limit
        mode_label = f"{limit} confirmed submissions"
        if effective_limit != limit:
            mode_label = f"{effective_limit} confirmed submissions (profile cap)"

    if authorization_manifest is not None:
        manifest_cap = int(authorization_manifest.get("max_submissions", 0))
        if manifest_cap <= 0:
            raise ValueError("Authorization manifest has no positive submission allowance.")
        if effective_limit == 0:
            effective_limit = manifest_cap
        else:
            effective_limit = min(effective_limit, manifest_cap)
        mode_label = f"{effective_limit} manifest-authorized confirmed submissions"

    target_workers = _workers_for_target(workers, effective_limit)
    if target_workers != workers:
        console.print(
            f"[dim]Using {target_workers} worker(s) for a finite target of "
            f"{effective_limit}; zero-allocation workers are not started.[/dim]"
        )
        workers = target_workers

    # Initialize dashboard for all workers
    attempted_urls: set[str] = set()
    attempted_urls_lock = threading.Lock()
    for i in range(workers):
        init_worker(i)

    worker_label = f"{workers} worker{'s' if workers > 1 else ''}"
    console.print(
        f"Launching apply pipeline ({mode_label}, {worker_label}, "
        f"browser={requested_browser_backend}, interaction={requested_interaction_mode}, "
        f"poll every {POLL_INTERVAL}s)..."
    )
    console.print("[dim]Ctrl+C = skip current job(s) | Ctrl+C x2 = stop[/dim]")

    # Double Ctrl+C handler
    _ctrl_c_count = 0

    def _sigint_handler(sig, frame):
        nonlocal _ctrl_c_count
        _ctrl_c_count += 1
        if _ctrl_c_count == 1:
            console.print("\n[yellow]Skipping current job(s)... (Ctrl+C again to STOP)[/yellow]")
            # Kill all active Claude processes to skip current jobs
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
        else:
            console.print("\n[red bold]STOPPING[/red bold]")
            _stop_event.set()
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
            kill_all_chrome()
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        with Live(render_full(), console=console, refresh_per_second=2) as live:
            # Daemon thread for display refresh only (no business logic)
            _dashboard_running = True

            def _refresh():
                while _dashboard_running:
                    live.update(render_full())
                    time.sleep(0.5)

            refresh_thread = threading.Thread(target=_refresh, daemon=True)
            refresh_thread.start()

            if workers == 1:
                # Single worker — run directly in main thread
                total_applied, total_failed = worker_loop(
                    worker_id=0,
                    limit=effective_limit,
                    target_url=target_url,
                    min_score=min_score,
                    headless=headless,
                    model=model,
                    dry_run=dry_run,
                    agent_backend=agent_backend,
                    manual_captcha_relay=manual_captcha_relay,
                    browser_backend=requested_browser_backend,
                    interaction_mode=requested_interaction_mode,
                    authorization_manifest=authorization_manifest,
                    attempted_urls=attempted_urls,
                    attempted_urls_lock=attempted_urls_lock,
                )
            else:
                # Multi-worker — distribute limit across workers
                if effective_limit:
                    base = effective_limit // workers
                    extra = effective_limit % workers
                    limits = [base + (1 if i < extra else 0)
                              for i in range(workers)]
                else:
                    limits = [0] * workers  # continuous mode

                with ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="apply-worker") as executor:
                    futures = {
                        executor.submit(
                            worker_loop,
                            worker_id=i,
                            limit=limits[i],
                            target_url=target_url,
                            min_score=min_score,
                            headless=headless,
                            model=model,
                            dry_run=dry_run,
                            agent_backend=agent_backend,
                            manual_captcha_relay=manual_captcha_relay,
                            browser_backend=requested_browser_backend,
                            interaction_mode=requested_interaction_mode,
                            authorization_manifest=authorization_manifest,
                            attempted_urls=attempted_urls,
                            attempted_urls_lock=attempted_urls_lock,
                        ): i
                        for i in range(workers)
                    }

                    results: list[tuple[int, int]] = []
                    for future in as_completed(futures):
                        wid = futures[future]
                        try:
                            results.append(future.result())
                        except Exception:
                            logger.exception("Worker %d crashed", wid)
                            results.append((0, 0))

                total_applied = sum(r[0] for r in results)
                total_failed = sum(r[1] for r in results)

            _dashboard_running = False
            refresh_thread.join(timeout=2)
            live.update(render_full())

        totals = get_totals()
        console.print(
            f"\n[bold]Done: {total_applied} applied, {total_failed} failed "
            f"(${totals['cost']:.3f})[/bold]"
        )
        console.print(f"Logs: {config.LOG_DIR}")

    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        kill_all_chrome()
