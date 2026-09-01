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
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlparse

from rich.console import Console
from rich.live import Live

from applypilot import config
from applypilot.apply import agent_output as agent_output_mod
from applypilot.apply import agent_runtime as agent_runtime_mod
from applypilot.apply import answer_provenance as answer_provenance_mod
from applypilot.apply import application_actor as application_actor_mod
from applypilot.apply import application_jobs as application_jobs_mod
from applypilot.apply import ats as ats_mod
from applypilot.apply import orchestration as orchestration_mod
from applypilot.apply import page_observation as page_observation_mod
from applypilot.apply import prompt as prompt_mod
from applypilot.apply import receipt_observer as receipt_observer_mod
from applypilot.apply import resume_authorization as resume_authorization_mod
from applypilot.apply import submission_surfaces as submission_surfaces_mod
from applypilot.apply import worker_orchestration as worker_orchestration_mod
from applypilot.apply.agent_report_mcp import REPORT_PATH_ENV, RUN_ID_ENV
from applypilot.apply.answer_policy import field_risk
from applypilot.apply.application_facts import (
    current_profile_facts,
    resolve_application_fact_ref,
)
from applypilot.apply.ats_tools_mcp import ATS_CONTEXT_PATH_ENV
from applypilot.apply.authentication_policy import authentication_capability
from applypilot.apply.browser_broker import (
    BrowserBrokerError,
    BrowserLeaseBundle,
    LeaseHeartbeat,
    StalePageBinding,
)
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
    kill_all_chrome,
    launch_chrome,  # noqa: F401 - injected worker port
    release_cdp_port,  # noqa: F401 - injected worker port
    reset_worker_dir,
    resolve_browser_backend,
    restore_browser_session,  # noqa: F401 - injected worker port
)
from applypilot.apply.chrome import (
    cleanup_worker as _cleanup_chrome_worker,
)
from applypilot.apply.contracts import (
    AgentCheckpoint,
    AgentRunRequest,
    AgentTurnResult,
    ApplicationEvent,
    HumanRequest,
    RecoveryCommand,
    ResourceClaim,
    TaskResult,
    TaskSpec,
    application_actor_id,
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
from applypilot.apply.durable_agent_runtime import (
    DurableAgentRuntime,
    DurableLaunchIntent,
    DurableRunHandle,
)
from applypilot.apply.durable_browser_broker import DurableBrowserBroker
from applypilot.apply.email_routing import (
    MailboxMcpSpec,
    direct_email_send_is_reserved,
    mailbox_mcp_for_phase,
    mailbox_prepare_duplicate_receipt,
    mailbox_read_input_matches_message,
    mailbox_search_message_id,
    mailbox_send_input_matches_plan,
    mailbox_sent_search_input_matches_plan,
    normalize_mailbox_read_receipt,
    normalize_sent_receipt,
    resolve_mailbox_mcp_spec,
)
from applypilot.apply.execution_scheduler import PhaseDemand, build_execution_plan
from applypilot.apply.failure_taxonomy import classify_failure
from applypilot.apply.operator_commands import OperatorCommand
from applypilot.apply.profile_lock import inspect_process_identity
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
from applypilot.apply.run_progress import RunProgress
from applypilot.apply.semantic_browser_ops import (
    SEMANTIC_WRITE_POLICY,
    SEMANTIC_WRITE_POLICY_DIGEST,
    ResumeUploadPostcondition,
    ResumeUploadRequest,
    SemanticBrowserOps,
    SemanticWriteAuthorityIssuer,
    SemanticWriteDenied,
    SemanticWriteUncertain,
    resume_postcondition_digest,
)
from applypilot.apply.semantic_resume_runtime import (
    DurableSemanticWriteLifecycle,
    PlaywrightResumeUploadDriver,
    SemanticResumeTargetError,
    application_binding_hash,
    bound_artifact,
    material_binding_hash,
    provider_for_url,
)
from applypilot.apply.semantic_resume_runtime import (
    operation_id as semantic_operation_id,
)
from applypilot.apply.semantic_resume_upload import ADAPTER_VERSION
from applypilot.apply.specialists import (
    prompt_safe_ats_fill_plan,
    run_durable_ats_fill_plan_specialist,
    run_durable_material_specialist,
    run_system_specialist,
)
from applypilot.database import get_connection
from applypilot.runtime_settings import load_runtime_settings
from applypilot.storage import semantic_browser_writes as semantic_write_journal


def _open_durable_control_connection() -> sqlite3.Connection:
    """Open a fresh control-plane connection exclusively owned by its caller."""
    from applypilot import database as database_mod

    connection = sqlite3.connect(str(database_mod.DB_PATH), timeout=30)
    connection.execute("PRAGMA busy_timeout=10000")
    connection.row_factory = sqlite3.Row
    return connection


def _process_identity_tuple(pid: int) -> tuple[int, int] | None:
    identity = inspect_process_identity(pid)
    if identity is None:
        return None
    return identity.pid, identity.creation_filetime


def _launcher_process_identity() -> tuple[int, int]:
    identity = _process_identity_tuple(os.getpid())
    if identity is None:
        raise RuntimeError("launcher process identity is unavailable")
    return identity


_runtime_recovery_local = threading.local()


@contextmanager
def _runtime_recovery_scope(command: RecoveryCommand):
    """Expose one admitted recovery command only to its current worker thread."""
    if command.command not in {"retry_same_application", "retry_new_session"}:
        raise ValueError("runtime recovery scope only accepts agent retry commands")
    if getattr(_runtime_recovery_local, "state", None) is not None:
        raise RuntimeError("nested runtime recovery scope is forbidden")
    state = {"command": command, "consumed": False}
    _runtime_recovery_local.state = state
    try:
        yield
    finally:
        if getattr(_runtime_recovery_local, "state", None) is state:
            del _runtime_recovery_local.state


@contextmanager
def _runtime_operator_resume_scope(
    command: OperatorCommand,
    *,
    checkpoint_id: str,
    resume_context: Mapping[str, object],
):
    """Authorize one exact operator-requested, prepare-only child turn.

    The command does not grant page-write or Submit authority.  It only lets
    the already-owning worker present the existing policy, browser lease and
    page binding to the durable runtime for one fresh child turn.
    """
    human_response = resume_context.get("human_response")
    if (
        command.action != "resume"
        or command.browser_authority
        or command.page_write_authority
        or command.submit_authority
        or command.ledger_write_authority
        or not str(checkpoint_id or "").strip()
        or resume_context.get("resume_mode") != "fresh_agent_turn"
        or resume_context.get("parent_run_id") != command.run_id
        or resume_context.get("checkpoint_ref") != checkpoint_id
        or not isinstance(human_response, Mapping)
        or not str(human_response.get("request_id") or "").strip()
        or not str(human_response.get("response_type") or "").strip()
    ):
        raise ValueError("operator resume scope requires a non-authoritative resume command")
    if getattr(_runtime_recovery_local, "state", None) is not None:
        raise RuntimeError("nested runtime continuation scope is forbidden")
    state = {
        "kind": "operator_resume",
        "command": command,
        "authorization_id": command.command_id,
        "actor_id": command.actor_id,
        "attempt_id": command.attempt_id,
        "parent_turn_id": command.run_id,
        "checkpoint_id": str(checkpoint_id),
        "request_id": str(human_response["request_id"]),
        "response_type": str(human_response["response_type"]),
        "consumed": False,
    }
    _runtime_recovery_local.state = state
    try:
        yield
    finally:
        if getattr(_runtime_recovery_local, "state", None) is state:
            del _runtime_recovery_local.state


@contextmanager
def _runtime_submit_scope(job: Mapping[str, object]):
    """Authorize one exact prepare-checkpoint to SubmissionGate continuation."""
    if getattr(_runtime_recovery_local, "state", None) is not None:
        raise RuntimeError("nested runtime continuation scope is forbidden")
    binding = job.get("_submission_gate_binding")
    if not isinstance(binding, Mapping):
        raise TypeError("submit continuation requires a gate binding")
    gate_id = str(binding.get("gate_id") or "").strip()
    attempt_id = str(job.get("_attempt_id") or "").strip()
    parent_turn_id = str(job.get("_parent_agent_run_id") or "").strip()
    checkpoint_id = str(job.get("_parent_agent_checkpoint_id") or "").strip()
    job_url = str(job.get("url") or "").strip()
    if (
        not gate_id
        or not attempt_id
        or not parent_turn_id
        or not checkpoint_id
        or binding.get("attempt_id") != attempt_id
        or binding.get("job_url") != job_url
    ):
        raise RuntimeError("submit continuation binding is incomplete or stale")
    state = {
        "kind": "submit",
        "authorization_id": gate_id,
        "actor_id": application_actor_id(attempt_id),
        "attempt_id": attempt_id,
        "parent_turn_id": parent_turn_id,
        "checkpoint_id": checkpoint_id,
        "consumed": False,
    }
    _runtime_recovery_local.state = state
    try:
        yield
    finally:
        if getattr(_runtime_recovery_local, "state", None) is state:
            del _runtime_recovery_local.state


def _active_runtime_recovery(
    *,
    actor_id: str,
    attempt_id: str,
    parent_turn_id: str,
) -> RecoveryCommand | None:
    command = _scoped_runtime_recovery()
    if command is None:
        return None
    if (
        command.actor_id,
        command.attempt_id,
        command.turn_id,
    ) != (actor_id, attempt_id, parent_turn_id):
        return None
    return command


def _scoped_runtime_recovery() -> RecoveryCommand | None:
    state = getattr(_runtime_recovery_local, "state", None)
    command = state.get("command") if isinstance(state, dict) else None
    if not isinstance(command, RecoveryCommand) or bool(state.get("consumed")):
        return None
    return command


def _scoped_submit_continuation() -> dict[str, object] | None:
    state = getattr(_runtime_recovery_local, "state", None)
    if (
        not isinstance(state, dict)
        or state.get("kind") != "submit"
        or bool(state.get("consumed"))
    ):
        return None
    return state


def _scoped_operator_resume() -> dict[str, object] | None:
    state = getattr(_runtime_recovery_local, "state", None)
    if (
        not isinstance(state, dict)
        or state.get("kind") != "operator_resume"
        or bool(state.get("consumed"))
        or not isinstance(state.get("command"), OperatorCommand)
    ):
        return None
    return state


def _consume_runtime_recovery_authorization(
    intent: DurableLaunchIntent,
    parent,
) -> bool:
    command = _active_runtime_recovery(
        actor_id=intent.spec.actor_id,
        attempt_id=intent.spec.attempt_id,
        parent_turn_id=parent.turn_id,
    )
    state = getattr(_runtime_recovery_local, "state", None)
    if command is not None:
        if (
            command.command_id != intent.recovery_authorization_id
            or command.submit_authority
            or command.page_write_authority
            or command.ledger_write_authority
            or not isinstance(state, dict)
            or state.get("command") is not command
        ):
            return False
    else:
        operator_resume = _scoped_operator_resume()
        if operator_resume is not None:
            if (
                intent.resume_mode != "resume"
                or intent.spec.submit_started
                or operator_resume.get("authorization_id")
                != intent.recovery_authorization_id
                or operator_resume.get("actor_id") != intent.spec.actor_id
                or operator_resume.get("attempt_id") != intent.spec.attempt_id
                or operator_resume.get("parent_turn_id") != parent.turn_id
                or operator_resume.get("checkpoint_id") != intent.checkpoint_id
            ):
                return False
            state = operator_resume
            state["consumed"] = True
            return True
        continuation = _scoped_submit_continuation()
        if continuation is None:
            return False
        if (
            intent.resume_mode != "resume"
            or not intent.spec.submit_started
            or continuation.get("authorization_id")
            != intent.recovery_authorization_id
            or continuation.get("actor_id") != intent.spec.actor_id
            or continuation.get("attempt_id") != intent.spec.attempt_id
            or continuation.get("parent_turn_id") != parent.turn_id
            or continuation.get("checkpoint_id") != intent.checkpoint_id
        ):
            return False
        state = continuation
    state["consumed"] = True
    return True


_browser_broker = DurableBrowserBroker(
    _open_durable_control_connection,
    default_ttl_seconds=45 * 60,
    process_identity_provider=lambda: _launcher_process_identity(),
    close_connections=True,
)
_semantic_write_authority_issuer = SemanticWriteAuthorityIssuer()
_agent_subprocess_runtime = agent_runtime_mod.SubprocessAgentRuntime(
    kill_process_tree=_kill_process_tree
)
_durable_agent_runtime = DurableAgentRuntime(
    _agent_subprocess_runtime,
    _open_durable_control_connection,
    process_identity=lambda pid: _process_identity_tuple(pid),
    resume_authorizer=_consume_runtime_recovery_authorization,
    close_connections=True,
)
atexit.register(_browser_broker.close)
atexit.register(_agent_subprocess_runtime.close)


def cleanup_worker(worker_id: int, process: subprocess.Popen | None) -> None:
    """Release browser resources together with the existing Chrome cleanup."""
    try:
        _cleanup_chrome_worker(worker_id, process)
    finally:
        _browser_broker.release_scope(f"worker:{worker_id}")


def _heartbeat_operator_handoff(
    job: dict,
    *,
    lease_minutes: int,
) -> bool:
    """Keep one already-owned pre-submit attempt/page alive during a bounded wait."""
    from applypilot.database import update_application_attempt

    raw_bundle = job.get("_browser_lease_binding")
    attempt_id = str(job.get("_attempt_id") or "").strip()
    if (
        not isinstance(raw_bundle, Mapping)
        or not attempt_id
        or isinstance(lease_minutes, bool)
        or not isinstance(lease_minutes, int)
        or lease_minutes < 1
    ):
        return False
    try:
        previous = BrowserLeaseBundle.from_mapping(raw_bundle)
        refreshed = _browser_broker.heartbeat(
            previous,
            ttl_seconds=min(3600.0, float(lease_minutes * 60)),
        )
    except (BrowserBrokerError, TypeError, ValueError):
        return False
    fixed_identity = (
        refreshed.profile.lease_id == previous.profile.lease_id
        and refreshed.profile.owner_id == previous.profile.owner_id
        and refreshed.profile.attempt_id == previous.profile.attempt_id == attempt_id
        and refreshed.page.resource_id == previous.page.resource_id
        and refreshed.page_binding.page_id == previous.page_binding.page_id
        and refreshed.page_binding.page_epoch == previous.page_binding.page_epoch
    )
    if not fixed_identity:
        return False
    if not update_application_attempt(
        attempt_id,
        phase="human_wait",
        submit_started=False,
        lease_minutes=lease_minutes,
        evidence={"operator_handoff": "waiting", "submit_started": False},
    ):
        return False
    job["_browser_lease_binding"] = refreshed.as_dict()
    return True

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
_observe_linkedin_external_handoff_page = (
    page_observation_mod._observe_linkedin_external_handoff_page
)
_click_linkedin_main_apply_causally = (
    page_observation_mod._click_linkedin_main_apply_causally
)
_verify_linkedin_post_login_state = (
    page_observation_mod._verify_linkedin_post_login_state
)


def _prepare_ats_fill_plan_repair(
    job: Mapping[str, object], audit_report: Mapping[str, object]
) -> dict[str, object]:
    """Execute the durable repair-only specialist and return prompt-safe state."""
    repairable = audit_report.get("repairable_issues")
    if not isinstance(repairable, list) or not any(
        str(issue).startswith("required_field_empty:") for issue in repairable
    ):
        raise ValueError("ATS fill-plan repair requires an ordinary empty field")
    snapshot = audit_report.get("ats_fill_plan_snapshot")
    if not isinstance(snapshot, Mapping):
        raise TypeError("ATS fill-plan repair has no launcher-owned snapshot")
    attempt_id = str(job.get("_attempt_id") or "").strip()
    if not attempt_id:
        raise ValueError("ATS fill-plan repair requires an attempt id")
    workflow_id = f"{attempt_id}:ats-fill-repair"
    connection = get_connection()
    try:
        run = run_durable_ats_fill_plan_specialist(
            connection,
            snapshot,
            attempt_id=attempt_id,
            workflow_id=workflow_id,
        )
    finally:
        close = getattr(connection, "close", None)
        if callable(close):
            close()
    safe_plan = prompt_safe_ats_fill_plan(run.result)
    return {
        "context": safe_plan,
        "feedback": {
            "task_id": run.task_id,
            "proposal_id": run.proposal_id,
            "attempt_id": attempt_id,
            "workflow_id": workflow_id,
            "snapshot_ref": str(run.result.get("snapshot_ref") or ""),
            "snapshot_sha256": str(run.result.get("snapshot_sha256") or ""),
            "plan_sha256": str(run.result.get("plan_sha256") or ""),
            "replay": run.replay,
            "before_disposition": str(audit_report.get("disposition") or ""),
            "before_issue_count": len(repairable),
        },
    }


def _record_ats_fill_plan_feedback(
    feedback: Mapping[str, object],
    *,
    event: str,
    audit_report: Mapping[str, object] | None = None,
) -> None:
    """Persist actual consumption/decision feedback without page-controlled text."""
    from applypilot.database import append_agent_event

    if event not in {"consumed", "changed_decision"}:
        raise ValueError("unsupported ATS fill-plan feedback event")
    task_id = str(feedback.get("task_id") or "")
    attempt_id = str(feedback.get("attempt_id") or "")
    workflow_id = str(feedback.get("workflow_id") or "")
    if not task_id or not attempt_id or not workflow_id:
        raise ValueError("ATS fill-plan feedback identity is incomplete")
    payload: dict[str, object] = {
        key: feedback[key]
        for key in (
            "task_id",
            "proposal_id",
            "snapshot_ref",
            "snapshot_sha256",
            "plan_sha256",
            "replay",
        )
        if key in feedback
    }
    event_type = f"agent.proposal.{event}"
    if event == "changed_decision":
        if not isinstance(audit_report, Mapping):
            raise ValueError("changed_decision requires the second audit")
        after_disposition = str(audit_report.get("disposition") or "")
        after_issues = audit_report.get("repairable_issues")
        after_issue_count = len(after_issues) if isinstance(after_issues, list) else 0
        before_disposition = str(feedback.get("before_disposition") or "")
        before_issue_count = int(feedback.get("before_issue_count") or 0)
        changed = (
            before_disposition == "retry_prepare"
            and after_disposition in {"clear", "proceed_with_advisories"}
            and after_issue_count < before_issue_count
        )
        payload.update(
            {
                "before_disposition": before_disposition,
                "after_disposition": after_disposition,
                "before_issue_count": before_issue_count,
                "after_issue_count": after_issue_count,
                "changed": changed,
            }
        )
    append_agent_event(
        ApplicationEvent(
            event_id=f"{task_id}:{event}",
            attempt_id=attempt_id,
            run_id=workflow_id,
            phase="prepare",
            actor="agent-orchestrator",
            event_type=event_type,
            payload=payload,
            idempotency_key=f"{task_id}:{event}",
        )
    )
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
    credential_relay_authorized: bool = False,
    identity_relay_authorized: bool = False,
) -> dict:
    return agent_runtime_mod.make_mcp_config(
        cdp_port,
        playwright_mcp=playwright_mcp,
        capability_registry=capability_registry,
        runtime_metadata=runtime_metadata,
        python_executable=sys.executable,
        mailbox_mcp=mailbox_mcp,
        direct_email_send_authorized=direct_email_send_authorized,
        credential_relay_authorized=credential_relay_authorized,
        identity_relay_authorized=identity_relay_authorized,
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


def _browser_lease_for_agent_turn(
    job: dict,
    *,
    worker_id: int,
    port: int,
    agent_backend: str,
    actor_id: str,
    attempt_id: str,
    submission_phase: str,
    dry_run: bool,
    resume_existing_page: bool,
) -> BrowserLeaseBundle:
    """Acquire or continue the non-authoritative browser continuity bundle."""
    browser_runtime = str(job.get("_browser_root_runtime") or "isolated")
    profile_id = f"{browser_runtime}:worker:{worker_id}"
    page_id = f"application:{attempt_id}"
    runtime_id = f"{agent_backend}:{browser_runtime}:cdp:{port}"
    scope_id = f"worker:{worker_id}"
    submit_started = submission_phase == "submit" and not dry_run
    ttl_seconds = max(
        60.0,
        float(load_runtime_settings().application_lease_minutes * 60),
    )
    raw_previous = job.get("_browser_lease_binding")
    if isinstance(raw_previous, Mapping):
        previous = BrowserLeaseBundle.from_mapping(raw_previous)
        bundle = _browser_broker.continue_bundle(
            previous,
            profile_id=profile_id,
            page_id=page_id,
            owner_id=actor_id,
            scope_id=scope_id,
            attempt_id=attempt_id,
            runtime_id=runtime_id,
            submit_started=submit_started,
            resume_existing_page=resume_existing_page,
            ttl_seconds=ttl_seconds,
        )
    else:
        bundle = _browser_broker.acquire_bundle(
            profile_id=profile_id,
            page_id=page_id,
            owner_id=actor_id,
            scope_id=scope_id,
            attempt_id=attempt_id,
            runtime_id=runtime_id,
            ttl_seconds=ttl_seconds,
        )
    job["_browser_lease_binding"] = bundle.as_dict()
    return bundle


def _refresh_semantic_browser_bundle(job: dict) -> BrowserLeaseBundle:
    """Reconstruct the current durable page epoch from an exact job-held bundle."""

    raw_bundle = job.get("_browser_lease_binding")
    if not isinstance(raw_bundle, Mapping):
        raise SemanticWriteDenied("semantic resume repair requires a browser lease")
    previous = BrowserLeaseBundle.from_mapping(raw_bundle)
    attempt_id = str(job.get("_attempt_id") or "").strip()
    actor_id = application_actor_id(attempt_id) if attempt_id else ""
    if (
        not attempt_id
        or previous.page_binding.attempt_id != attempt_id
        or previous.page_binding.owner_id != actor_id
    ):
        raise SemanticWriteDenied("semantic browser lease is not attempt-bound")
    current = _browser_broker.acquire_bundle(
        profile_id=previous.profile.resource_id,
        page_id=previous.page.resource_id,
        owner_id=previous.profile.owner_id,
        scope_id=previous.profile.scope_id,
        attempt_id=previous.profile.attempt_id,
        runtime_id=previous.profile.runtime_id,
        ttl_seconds=max(
            60.0,
            float(load_runtime_settings().application_lease_minutes * 60),
        ),
    )
    job["_browser_lease_binding"] = current.as_dict()
    return current


def _semantic_operation_relevant(
    record: semantic_write_journal.SemanticWriteRecord,
    *,
    provider: str,
    artifact_sha256: str,
    artifact_size: int,
    application_hash: str,
    material_hash: str,
) -> bool:
    return (
        record.provider == provider
        and record.artifact_sha256 == artifact_sha256
        and record.artifact_size == artifact_size
        and record.application_binding_hash == application_hash
        and record.material_binding_hash == material_hash
    )


def _semantic_legacy_fallback_decision(
    job: Mapping[str, object],
    *,
    reason_code: str,
) -> dict[str, object]:
    """Allow the legacy writer only when this attempt has never dispatched.

    Provider, page, container, and material bindings are deliberately ignored:
    any of them may change after a browser-side write or process interruption.
    """

    attempt_id = str(job.get("_attempt_id") or "").strip()
    if not attempt_id:
        return {
            "status": "not_applicable",
            "legacy_fallback_safe": True,
            "reason": reason_code,
        }
    try:
        connection = get_connection()
        dispatched = [
            record
            for record in semantic_write_journal.list_attempt_operations(
                connection,
                attempt_id,
            )
            if record.dispatch_count > 0
        ]
    except Exception as exc:  # noqa: BLE001 - a missing guard must fail closed
        logger.warning(
            "Semantic fallback journal guard failed for %s: %s",
            attempt_id,
            exc,
        )
        return {"status": "journal_guard_unavailable"}
    if not dispatched:
        return {
            "status": "not_applicable",
            "legacy_fallback_safe": True,
            "reason": reason_code,
        }

    previous = dispatched[-1]
    if previous.state == "started":
        try:
            previous = semantic_write_journal.park_side_effect_unknown(
                connection,
                previous.operation_id,
                reason_code=reason_code,
            )
        except semantic_write_journal.SemanticWriteTransitionError:
            refreshed = semantic_write_journal.get_operation(
                connection,
                previous.operation_id,
            )
            if refreshed is None:
                return {"status": "journal_guard_unavailable"}
            previous = refreshed
    status = (
        "verified_state_unobserved"
        if previous.state == "verified"
        else previous.state
    )
    return {
        "status": status,
        "operation_id": previous.operation_id,
        "legacy_fallback_safe": False,
    }


def _complete_observed_semantic_resume_effect(
    job: dict,
    bundle: BrowserLeaseBundle,
    record: semantic_write_journal.SemanticWriteRecord,
    connection: sqlite3.Connection,
) -> dict[str, object]:
    """Finish only the journal/CAS tail after an exact effect was observed."""

    binding = bundle.page_binding
    exact_lease = (
        binding.page_id == record.page_id
        and binding.page_lease_id == record.page_lease_id
        and binding.page_lease_epoch == record.page_lease_epoch
    )
    if not exact_lease:
        semantic_write_journal.park_stale_after_effect(
            connection,
            record.operation_id,
            reason_code="page_lease_changed_after_effect",
        )
        return {"status": "parked_stale_after_effect"}
    if binding.page_epoch == record.expected_page_epoch:
        try:
            bundle = _browser_broker.advance_page(
                bundle,
                expected_page_epoch=record.expected_page_epoch,
            )
        except BrowserBrokerError:
            semantic_write_journal.park_stale_after_effect(
                connection,
                record.operation_id,
                reason_code="page_epoch_cas_failed",
            )
            return {"status": "parked_stale_after_effect"}
    elif binding.page_epoch != record.expected_page_epoch + 1:
        semantic_write_journal.park_stale_after_effect(
            connection,
            record.operation_id,
            reason_code="page_epoch_changed_after_effect",
        )
        return {"status": "parked_stale_after_effect"}
    semantic_write_journal.mark_verified(
        connection,
        record.operation_id,
        resulting_page_epoch=record.expected_page_epoch + 1,
    )
    job["_browser_lease_binding"] = bundle.as_dict()
    return {
        "status": "replayed",
        "operation_id": record.operation_id,
        "page_epoch": bundle.page_binding.page_epoch,
    }


def _try_semantic_pre_submit_repair(
    port: int,
    worker_id: int,
    job: dict,
    authorization_manifest: Mapping[str, object] | None,
    audit_report: Mapping[str, object],
) -> dict[str, object]:
    """Repair one exact missing resume without granting Agent or Submit authority."""

    if os.getenv("APPLYPILOT_SEMANTIC_RESUME_UPLOAD", "1").strip().casefold() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return _semantic_legacy_fallback_decision(
            job,
            reason_code="feature_disabled_after_dispatch",
        )
    repairable = audit_report.get("repairable_issues")
    if not isinstance(repairable, list) or not repairable or any(
        str(issue)
        not in {
            "resume_not_uploaded",
            "resume_state_unconfirmed",
        }
        for issue in repairable
    ):
        return _semantic_legacy_fallback_decision(
            job,
            reason_code="repair_scope_changed_after_dispatch",
        )
    if job.get("_submission_gate_binding") or job.get("_submission_gate"):
        return {"status": "submit_started"}
    if not isinstance(authorization_manifest, Mapping):
        return {"status": "authorization_manifest_required"}
    try:
        manifest_expires_at = datetime.fromisoformat(
            str(authorization_manifest.get("expires_at") or "")
        )
    except ValueError:
        return {"status": "authorization_manifest_invalid"}
    if (
        manifest_expires_at.tzinfo is None
        or datetime.now(UTC) >= manifest_expires_at
    ):
        return {"status": "authorization_manifest_expired"}

    from playwright.sync_api import sync_playwright

    from applypilot.apply.authorization import (
        authorize_job,
        compute_file_binding,
        resolve_resume_attachment,
    )

    authorization_entry = authorize_job(dict(authorization_manifest), job)
    persisted_entry = job.get("_authorization_entry")
    if authorization_entry is None or (
        isinstance(persisted_entry, Mapping)
        and dict(persisted_entry) != dict(authorization_entry)
    ):
        return {"status": "authorization_mismatch"}
    try:
        resume_path = resolve_resume_attachment(job)
        resume_sha256, resume_size = compute_file_binding(resume_path)
    except (OSError, RuntimeError, ValueError):
        return {"status": "artifact_binding_invalid"}
    if (
        authorization_entry.get("resume_sha256") != resume_sha256
        or authorization_entry.get("resume_size") != resume_size
    ):
        return {"status": "artifact_binding_changed"}

    try:
        bundle = _refresh_semantic_browser_bundle(job)
        _browser_broker.validate_page(bundle.page_binding)
    except Exception as exc:  # noqa: BLE001 - fail closed before page access
        logger.warning("Semantic browser binding rejected: %s", exc)
        return {"status": "stale_precondition"}

    playwright = sync_playwright().start()
    operation: semantic_write_journal.SemanticWriteRecord | None = None
    connection: sqlite3.Connection | None = None
    try:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        pages = [page for context in browser.contexts for page in context.pages]
        pages = _bound_application_pages(browser, pages, job)
        if not pages:
            return {"status": "no_bound_application_page"}
        page, surface = page_observation_mod._select_application_page_and_frame(pages)
        page.bring_to_front()
        provider = provider_for_url(page.url)
        reported_provider = provider_for_url(audit_report.get("page_url"))
        if provider is None:
            return _semantic_legacy_fallback_decision(
                job,
                reason_code="provider_changed_after_dispatch",
            )
        if reported_provider is not None and reported_provider != provider:
            return {"status": "page_provider_changed"}

        artifact = bound_artifact(resume_path, resume_sha256, resume_size)
        application_hash = application_binding_hash(
            job,
            authorization_entry,
            page_url=page.url,
        )
        material_hash = material_binding_hash(job, authorization_entry)
        postcondition = ResumeUploadPostcondition(
            filename=artifact.filename,
            size_bytes=artifact.size_bytes,
        )
        connection = get_connection()
        semantic_write_journal.ensure_schema(connection)
        attempt_records = semantic_write_journal.list_attempt_operations(
            connection,
            bundle.page_binding.attempt_id,
        )
        prior = [
            record
            for record in attempt_records
            if _semantic_operation_relevant(
                record,
                provider=provider,
                artifact_sha256=artifact.sha256,
                artifact_size=artifact.size_bytes,
                application_hash=application_hash,
                material_hash=material_hash,
            )
        ]
        relevant_operation_ids = {record.operation_id for record in prior}
        foreign_dispatched = [
            record
            for record in attempt_records
            if record.dispatch_count > 0
            and record.operation_id not in relevant_operation_ids
        ]
        if foreign_dispatched:
            previous = foreign_dispatched[-1]
            if previous.state == "started":
                semantic_write_journal.park_side_effect_unknown(
                    connection,
                    previous.operation_id,
                    reason_code="attempt_scope_changed_after_dispatch",
                )
                return {"status": "parked_side_effect_unknown"}
            if previous.state == "verified":
                return {"status": "verified_state_conflict"}
            return {"status": "prior_operation_unresolved"}
        for previous in reversed(prior):
            if previous.state in {
                "parked_side_effect_unknown",
                "parked_stale_after_effect",
            }:
                return {"status": previous.state}
            if previous.state == "effect_observed":
                return _complete_observed_semantic_resume_effect(
                    job,
                    bundle,
                    previous,
                    connection,
                )
            if previous.state == "verified":
                if (
                    previous.resulting_page_epoch == bundle.page_binding.page_epoch
                    and previous.page_lease_id == bundle.page_binding.page_lease_id
                ):
                    job["_browser_lease_binding"] = bundle.as_dict()
                    return {
                        "status": "replayed",
                        "operation_id": previous.operation_id,
                        "page_epoch": bundle.page_binding.page_epoch,
                    }
                return {"status": "verified_state_conflict"}

        driver = PlaywrightResumeUploadDriver(surface, provider)
        discovered = driver.discover()
        unresolved_started = [
            record
            for record in prior
            if record.state == "started" and record.dispatch_count > 0
        ]
        if discovered.status != "ready" or not discovered.container_key:
            if unresolved_started:
                previous = unresolved_started[-1]
                semantic_write_journal.park_side_effect_unknown(
                    connection,
                    previous.operation_id,
                    reason_code="target_unobservable_after_dispatch",
                )
                return {"status": "parked_side_effect_unknown"}
            if any(record.dispatch_count > 0 for record in prior):
                return {"status": "failed_no_effect"}
            if not prior:
                if discovered.status == "unsupported":
                    return _semantic_legacy_fallback_decision(
                        job,
                        reason_code="target_unobservable_after_dispatch",
                    )
                return {"status": f"target_{discovered.status}"}
            return {"status": f"target_{discovered.status}_after_start"}

        request = ResumeUploadRequest(
            actor_id=bundle.page_binding.owner_id,
            attempt_id=bundle.page_binding.attempt_id,
            provider=provider,
            container_key=discovered.container_key,
            artifact=artifact,
            application_binding_hash=application_hash,
            material_binding_hash=material_hash,
            policy_digest=SEMANTIC_WRITE_POLICY_DIGEST,
            adapter_version=ADAPTER_VERSION,
            expected_postcondition=postcondition,
        )
        authority = _semantic_write_authority_issuer.issue(
            bundle=bundle,
            request=request,
            submit_started=False,
        )
        operation_key = semantic_operation_id(authority.operation_digest)
        claims = semantic_write_journal.SemanticWriteClaims(
            operation_id=operation_key,
            operation_digest=authority.operation_digest,
            actor_id=request.actor_id,
            attempt_id=request.attempt_id,
            provider=provider,
            operation_kind="upload_bound_artifact",
            adapter_version=ADAPTER_VERSION,
            application_binding_hash=application_hash,
            page_id=bundle.page_binding.page_id,
            page_lease_id=bundle.page_binding.page_lease_id,
            page_lease_epoch=bundle.page_binding.page_lease_epoch,
            expected_page_epoch=bundle.page_binding.page_epoch,
            artifact_sha256=artifact.sha256,
            artifact_size=artifact.size_bytes,
            material_binding_hash=material_hash,
            policy_contract_version=SEMANTIC_WRITE_POLICY,
            policy_digest=SEMANTIC_WRITE_POLICY_DIGEST,
            expected_postcondition_digest=resume_postcondition_digest(postcondition),
        )
        conflicting = [
            record
            for record in prior
            if record.operation_digest != authority.operation_digest
            and (
                record.dispatch_count > 0
                or record.state
                in {
                    "effect_observed",
                    "verified",
                    "parked_side_effect_unknown",
                    "parked_stale_after_effect",
                }
            )
        ]
        if conflicting:
            previous = conflicting[-1]
            if previous.state == "started":
                semantic_write_journal.park_side_effect_unknown(
                    connection,
                    previous.operation_id,
                    reason_code="page_binding_changed_after_dispatch",
                )
                return {"status": "parked_side_effect_unknown"}
            return {"status": "prior_operation_unresolved"}
        operation = semantic_write_journal.begin_operation(connection, claims)
        if operation.state in {
            "parked_side_effect_unknown",
            "parked_stale_after_effect",
        }:
            return {"status": operation.state}
        if operation.state == "verified":
            if (
                operation.resulting_page_epoch == bundle.page_binding.page_epoch
                and operation.page_lease_id == bundle.page_binding.page_lease_id
            ):
                job["_browser_lease_binding"] = bundle.as_dict()
                return {
                    "status": "replayed",
                    "operation_id": operation.operation_id,
                    "page_epoch": bundle.page_binding.page_epoch,
                }
            return {"status": "verified_state_conflict"}
        if operation.state == "effect_observed":
            return _complete_observed_semantic_resume_effect(
                job,
                bundle,
                operation,
                connection,
            )

        if operation.dispatch_count == 0:
            claimed = semantic_write_journal.claim_dispatch(
                connection,
                operation.operation_id,
                expected_dispatch_count=0,
            )
        elif operation.dispatch_count == 1:
            if operation.state != "failed_no_effect":
                if operation.state == "started":
                    semantic_write_journal.park_side_effect_unknown(
                        connection,
                        operation.operation_id,
                        reason_code="process_interrupted_after_dispatch",
                    )
                return {"status": "parked_side_effect_unknown"}
            claimed = semantic_write_journal.claim_dispatch(
                connection,
                operation.operation_id,
                expected_dispatch_count=1,
                allow_replay=True,
            )
        else:
            return {"status": "bounded_replay_exhausted"}
        if claimed is None:
            return {"status": "dispatch_conflict"}
        operation = claimed
        lifecycle = DurableSemanticWriteLifecycle(
            connection,
            operation_id=operation.operation_id,
            operation_digest=operation.operation_digest,
        )
        semantic_ops = SemanticBrowserOps(
            _browser_broker,
            authority_issuer=_semantic_write_authority_issuer,
            resume_driver=driver,
            lifecycle=lifecycle,
        )
        result = semantic_ops.upload_bound_resume(bundle, authority, request)
        job["_browser_lease_binding"] = result.bundle.as_dict()
        return {
            "status": "replayed" if result.replayed else "verified",
            "operation_id": operation.operation_id,
            "page_epoch": result.bundle.page_binding.page_epoch,
        }
    except StalePageBinding:
        if connection is not None and operation is not None:
            current = semantic_write_journal.get_operation(
                connection, operation.operation_id
            )
            if current is not None and current.state == "started":
                semantic_write_journal.mark_failed_no_effect(
                    connection,
                    operation.operation_id,
                    reason_code="stale_precondition",
                )
        return {"status": "stale_precondition"}
    except SemanticResumeTargetError as exc:
        if connection is not None and operation is not None:
            current = semantic_write_journal.get_operation(
                connection, operation.operation_id
            )
            if current is not None and current.state == "started":
                semantic_write_journal.mark_failed_no_effect(
                    connection,
                    operation.operation_id,
                    reason_code="target_changed_before_write",
                )
        return {"status": f"target_{exc.status}"}
    except SemanticWriteDenied:
        if connection is not None and operation is not None:
            current = semantic_write_journal.get_operation(
                connection, operation.operation_id
            )
            if current is not None and current.state == "started":
                semantic_write_journal.mark_failed_no_effect(
                    connection,
                    operation.operation_id,
                    reason_code="authority_denied_before_write",
                )
        return {"status": "authority_denied"}
    except SemanticWriteUncertain:
        if connection is not None and operation is not None:
            current = semantic_write_journal.get_operation(
                connection, operation.operation_id
            )
            if current is not None:
                return {"status": current.state}
        return {"status": "parked_side_effect_unknown"}
    except Exception as exc:
        logger.exception("Semantic resume repair failed before verification")
        return {"status": f"semantic_error:{type(exc).__name__}"}
    finally:
        playwright.stop()


def _runtime_timeout_status(*, submission_phase: str, dry_run: bool) -> str:
    """Keep an interrupted real submit uncertain; identify all other budget exhaustion."""
    if submission_phase == "submit" and not dry_run:
        return "submission_uncertain"
    return "failed:agent_runtime_timeout"


def _normalize_browser_runtime_failure(
    status: str,
    *,
    browser_tool_call_count: int,
    browser_tool_success_count: int,
    failure_context: dict[str, object] | None,
) -> tuple[str, dict[str, object] | None]:
    """Distinguish an absent browser MCP from a later site interaction failure."""
    if (
        status.strip().casefold() != "failed:browser_mcp_unavailable"
        or browser_tool_success_count < 1
    ):
        return status, failure_context

    context = dict(failure_context or {})
    context.update(
        {
            "category": "browser_interaction_unavailable",
            "recoverability": "requires_capability",
            "missing_capability": "site_specific_browser_interaction_or_app_handoff",
            "next_action": "inspect_page_state_or_route_to_authorized_app_browser",
            "visible_state": (
                f"{browser_tool_success_count} browser tool call(s) succeeded before "
                "the site interaction became unavailable"
            ),
            "attempts": min(max(browser_tool_call_count, 0), 10),
        }
    )
    return "failed:browser_interaction_unavailable", context


def _build_agent_command(
    backend: str,
    model: str,
    port: int,
    worker_dir: Path,
    mcp_config_path: Path,
    *,
    credential_relay_authorized: bool = False,
    identity_relay_authorized: bool = False,
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
        identity_relay_authorized=identity_relay_authorized,
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


_agent_event_clock = lambda: datetime.now(UTC)


def _ordered_agent_event_time(previous: datetime | None = None) -> datetime:
    """Keep one run's durable lifecycle ordered on coarse wall clocks."""
    current = _agent_event_clock()
    if previous is not None and current <= previous:
        return previous + timedelta(microseconds=1)
    return current


def _persist_agent_turn_started(
    request: AgentRunRequest,
    *,
    backend: str,
    model: str,
    runtime_metadata: dict,
) -> datetime:
    """Best-effort control telemetry; never becomes application authority."""
    from applypilot.database import append_agent_event

    occurred_at = _ordered_agent_event_time()
    idempotency_key = f"agent-turn:v2:{request.actor_id}:{request.turn_id}:started"
    event = ApplicationEvent(
        event_id=idempotency_key,
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
        idempotency_key=idempotency_key,
        actor_id=request.actor_id,
        turn_id=request.turn_id,
        schema_version="2",
        occurred_at=occurred_at,
    )
    try:
        append_agent_event(event)
    except Exception as exc:  # noqa: BLE001 - advisory telemetry must not alter apply outcome
        logger.warning("Could not persist Agent turn start %s: %s", request.run_id, exc)
    return occurred_at


def _persist_agent_turn_completed(
    request: AgentRunRequest,
    result: AgentTurnResult,
    *,
    application_status: str,
    duration_ms: int,
    source: str,
    metrics: Mapping[str, object] | None = None,
    occurred_after: datetime | None = None,
    expected_checkpoint_sequence: int | None = None,
) -> datetime:
    """Atomically save the terminal control event and resumable checkpoint."""
    from applypilot.database import (
        current_agent_checkpoint_sequence,
        record_agent_turn_control,
    )

    actor_decision = application_actor_mod.decision_for_turn(
        request,
        result,
        application_status=application_status,
    )
    actor_decision_json = contract_json(actor_decision)
    legacy_actor_decision_json = {
        key: actor_decision_json[key]
        for key in (
            "run_id",
            "attempt_id",
            "phase",
            "disposition",
            "next_phase",
            "recovery_action",
            "human_interruption",
            "shadow_only",
        )
    }
    legacy_actor_decision_json["schema_version"] = "1"
    raw_evidence_refs = result.observations.get("evidence_refs", ())
    evidence_ref_count = (
        len(raw_evidence_refs) if isinstance(raw_evidence_refs, (list, tuple)) else 0
    )
    bounded_metrics: dict[str, int | float] = {}
    for key, value in (metrics or {}).items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        bounded_metrics[_bounded_control_text(key, maximum=80)] = max(0, value)
    occurred_at = _ordered_agent_event_time(occurred_after)
    completed_idempotency_key = (
        f"agent-turn:v2:{request.actor_id}:{request.turn_id}:completed"
    )
    event = ApplicationEvent(
        event_id=completed_idempotency_key,
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
            # Preserve the established v1 key for existing readers while the
            # native v2 envelope is published alongside it for durable actors.
            "actor_decision": legacy_actor_decision_json,
            "actor_decision_v2": actor_decision_json,
        },
        evidence_refs=(),
        idempotency_key=completed_idempotency_key,
        actor_id=request.actor_id,
        turn_id=request.turn_id,
        schema_version="2",
        occurred_at=occurred_at,
    )
    if expected_checkpoint_sequence is None:
        try:
            expected_checkpoint_sequence = current_agent_checkpoint_sequence(request.actor_id)
        except Exception as exc:  # noqa: BLE001 - control state remains advisory
            logger.warning(
                "Could not read Agent actor sequence %s: %s",
                request.actor_id,
                exc,
            )
            return occurred_at
    checkpoint = AgentCheckpoint(
        checkpoint_id=f"{completed_idempotency_key}:checkpoint",
        run_id=request.run_id,
        attempt_id=request.attempt_id,
        phase=request.phase,
        sequence=expected_checkpoint_sequence + 1,
        state={
            "application_status": application_status,
            "result": _durable_agent_result(result),
            "source": source,
            "actor_decision": legacy_actor_decision_json,
            "actor_decision_v2": actor_decision_json,
        },
        actor_id=request.actor_id,
        turn_id=request.turn_id,
        idempotency_key=completed_idempotency_key,
        expected_sequence=expected_checkpoint_sequence,
        fresh_turn_resume_authorized=False,
        schema_version="2",
        created_at=occurred_at,
    )
    human_request = application_actor_mod.human_request_for_decision(actor_decision)
    if (
        human_request is None
        and result.requested_human_input
        and (
            actor_decision.recovery_action is None
            or (
                actor_decision.recovery_action.action == "park"
                and actor_decision.recovery_action.missing_material is not None
            )
        )
    ):
        human_request = HumanRequest(
            request_id=f"{request.run_id}:human:1",
            run_id=request.run_id,
            attempt_id=request.attempt_id,
            request_type="agent_clarification",
            prompt="Agent requested human review; inspect the run evidence before continuing.",
            context={
                "actor_id": request.actor_id,
                "turn_id": request.turn_id,
                "phase": request.phase,
                "application_status": application_status,
                "requested_input_length": len(result.requested_human_input),
            },
        )
    try:
        record_agent_turn_control(event, checkpoint, human_request)
    except Exception as exc:  # noqa: BLE001 - advisory telemetry must not alter apply outcome
        logger.warning("Could not persist Agent turn completion %s: %s", request.run_id, exc)
    return occurred_at


def _control_contract_digest(payload: Mapping[str, object]) -> str:
    """Hash bounded control metadata without retaining prompt or environment text."""
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _confirmed_agent_checkpoint_id(request: AgentRunRequest) -> str | None:
    """Return the exact latest checkpoint only after its durable write is visible."""
    from applypilot.storage import agent_control

    expected = (
        f"agent-turn:v2:{request.actor_id}:{request.turn_id}:completed:checkpoint"
    )
    connection = _open_durable_control_connection()
    try:
        checkpoint = agent_control.latest_actor_checkpoint(
            connection,
            request.actor_id,
        )
    finally:
        connection.close()
    if checkpoint is None:
        return None
    identity = (
        checkpoint.checkpoint_id,
        checkpoint.run_id,
        checkpoint.attempt_id,
        checkpoint.actor_id,
        checkpoint.turn_id,
        checkpoint.schema_version,
    )
    if identity != (
        expected,
        request.run_id,
        request.attempt_id,
        request.actor_id,
        request.turn_id,
        "2",
    ):
        return None
    return expected


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
    occurred_after: datetime | None = None,
) -> datetime:
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
    occurred_at = _ordered_agent_event_time(occurred_after)
    idempotency_key = (
        f"agent-turn:v2:{request.actor_id}:{request.turn_id}:proposals:1"
    )
    event = ApplicationEvent(
        event_id=idempotency_key,
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
        idempotency_key=idempotency_key,
        actor_id=request.actor_id,
        turn_id=request.turn_id,
        schema_version="2",
        occurred_at=occurred_at,
    )
    try:
        append_agent_event(event)
    except Exception as exc:  # noqa: BLE001 - advisory telemetry must not alter apply outcome
        logger.warning("Could not persist Agent proposal outcomes %s: %s", request.run_id, exc)
    return occurred_at


def acquire_job(
    target_url: str | None = None,
    min_score: int = 6,
    worker_id: int = 0,
    preview_only: bool = False,
    authorization_manifest: dict | None = None,
    exclude_urls: set[str] | None = None,
    application_lease_minutes: int | None = None,
    performance_sink: dict[str, object] | None = None,
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
        performance_sink=performance_sink,
        load_blocked=_load_blocked,
        application_lease_minutes=application_lease_minutes,
    )


def record_application_attempt_performance(
    attempt_id: str | None,
    performance: object,
) -> bool:
    """Compatibility port for final attempt-bound orchestration telemetry."""
    from applypilot.database import (
        record_application_attempt_performance as record_performance,
    )

    return record_performance(attempt_id, performance)


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


def mark_job(url: str, status: str, reason: str | None = None) -> str:
    return application_jobs_mod.mark_job(get_connection(), url, status, reason)


def reset_failed(url: str | None = None) -> int:
    return application_jobs_mod.reset_failed(get_connection(), url)


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
    run_progress: RunProgress | None = None,
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
        run_progress=run_progress,
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
    """Run system-seeded deterministic reads before browser/Agent work."""
    provider = ats_mod.detect_ats_site(
        str(job.get("application_url") or job.get("url") or "")
    )
    try:
        profile = config.load_profile()
    except FileNotFoundError:
        # Library-level/static preflight remains usable before local profile
        # initialization; production runs already require a profile upstream.
        profile = {}
    runtime = profile.get("agent_runtime", {}) if isinstance(profile, Mapping) else {}
    orchestration = (
        runtime.get("orchestration", {}) if isinstance(runtime, Mapping) else {}
    )
    configured_mode = (
        orchestration.get("material_specialist_mode", "shadow")
        if isinstance(orchestration, Mapping)
        else "shadow"
    )
    mode = str(job.get("_material_specialist_mode") or configured_mode)
    material_job = dict(job)
    submission_policy = (
        profile.get("submission_policy", {}) if isinstance(profile, Mapping) else {}
    )
    if isinstance(submission_policy, Mapping):
        material_job.setdefault(
            "_allow_runtime_cover_letter",
            bool(
                submission_policy.get(
                    "allow_runtime_cover_letter_discovery",
                    False,
                )
            ),
        )

    tasks = [
        TaskSpec(
            task_id="material-readiness",
            kind="material-readiness",
            objective="Consume the deterministic system-seeded material result.",
            inputs={"specialist": "material-readiness-v1", "mode": mode},
            effect_class="read",
            resource_claims=(ResourceClaim("local-read"),),
        )
    ]
    if provider == "smartrecruiters":
        tasks.extend(
            (
                TaskSpec(
                    task_id="duplicate-snapshot",
                    kind="duplicate-check",
                    objective="Read the durable application ledger for an exact duplicate.",
                    inputs={"job_url": str(job.get("url") or "")},
                    effect_class="read",
                    resource_claims=(ResourceClaim("database-read"),),
                ),
                TaskSpec(
                    task_id="ats-identity",
                    kind="ats-identity",
                    objective="Resolve the immutable public posting identity.",
                    inputs={"provider": provider},
                    effect_class="read",
                    resource_claims=(ResourceClaim("network-read"),),
                ),
            )
        )

    def runner(task: TaskSpec, _context: object) -> TaskResult:
        started = time.perf_counter()
        if task.task_id == "material-readiness":
            attempt_id = str(
                job.get("_attempt_id")
                or f"preflight-{hashlib.sha256(str(job.get('url') or '').encode()).hexdigest()[:16]}"
            )
            workflow_id = f"{attempt_id}:material-preflight"
            connection = get_connection()
            try:
                if isinstance(connection, sqlite3.Connection):
                    material_run = run_durable_material_specialist(
                        connection,
                        material_job,
                        mode=mode,
                        attempt_id=attempt_id,
                        workflow_id=workflow_id,
                    )
                else:
                    # Compatibility for injected read-only test ports. Real
                    # database connections always use the durable path above.
                    material_run = run_system_specialist(
                        "material-readiness-v1",
                        material_job,
                        mode=mode,
                    )
            finally:
                close = getattr(connection, "close", None)
                if callable(close):
                    close()
            output = {
                "material_readiness": None if material_run is None else material_run.result,
                "mode": mode,
                "enforced": False if material_run is None else material_run.enforced,
                "proposal_feedback": (
                    [] if material_run is None else list(material_run.telemetry)
                ),
                "replay": False if material_run is None else material_run.replay,
                "task_id": None if material_run is None else material_run.task_id,
                "proposal_id": None if material_run is None else material_run.proposal_id,
            }
            return TaskResult(task_id=task.task_id, status="completed", output=output)
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
        if task.task_id == "material-readiness":
            return
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
        resource_capacities={
            "local-read": 1,
            "database-read": 1,
            "network-read": 1,
        },
    )
    result: dict[str, object] = {
        "provider": provider,
        "task_statuses": outcome.reduced_state.get("task_statuses", {}),
    }
    material_result = outcome.results["material-readiness"]
    material_readiness = material_result.output.get("material_readiness")
    normalized_mode = mode.casefold().strip()
    fail_closed_mode = normalized_mode not in {"off", "shadow"}
    if not material_result.succeeded:
        material_readiness = {
            "state": "blocked",
            "ready": False,
            "missing_kinds": ["material_specialist_unavailable"],
            "failure_category": material_result.failure_category,
            "error_type": material_result.output.get("error_type"),
        }
    result["material_readiness"] = material_readiness
    result["material_specialist_mode"] = mode
    result["material_enforced_block"] = bool(
        material_result.output.get("enforced")
        or (fail_closed_mode and not material_result.succeeded)
    )
    result["proposal_feedback"] = material_result.output.get("proposal_feedback", [])
    result["material_specialist_replay"] = bool(material_result.output.get("replay"))
    result["material_task_id"] = material_result.output.get("task_id")
    result["material_proposal_id"] = material_result.output.get("proposal_id")
    duplicate_result = outcome.results.get("duplicate-snapshot")
    if duplicate_result is not None and duplicate_result.succeeded:
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
_submit_writer_lane = threading.Semaphore(1)

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


def _acquire_submit_writer_lane(worker_id: int) -> bool:
    """Wait interruptibly for the sole final-submit/receipt ownership lane."""
    update_state(
        worker_id,
        status="waiting",
        last_action="waiting for final submit lane",
    )
    while not _stop_event.is_set():
        if _submit_writer_lane.acquire(timeout=0.5):
            return True
    return False


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
    "post-submit-observer.png",
    "post-submit-observer-attempt-2.png",
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
    *,
    disposition: str | None = None,
    receipt_admitted: bool | None = None,
) -> None:
    """Classify evidence from an explicit outcome, never from a filename."""
    global _evidence_retention_checked
    try:
        normalized_disposition = str(
            disposition or job.get("_post_submit_disposition") or ""
        ).casefold()
        durable_receipt = bool(
            job.get("_durable_receipt_admitted")
            if receipt_admitted is None
            else receipt_admitted
        )
        if durable_receipt:
            artifact_kind, artifact_state = "application_evidence", "applied"
        elif normalized_disposition == "historical_duplicate":
            artifact_kind, artifact_state = (
                "application_evidence",
                "historical_duplicate",
            )
        elif normalized_disposition in {
            "uncertain",
            "submission_uncertain",
            "conflicting_post_submit_status",
        }:
            artifact_kind, artifact_state = "job_transient", "submission_uncertain"
        else:
            artifact_kind, artifact_state = "job_transient", "previewed"
        mark_owned_directory(
            destination,
            root=config.LOG_DIR / "application-evidence",
            kind=artifact_kind,
            owner_id=str(job.get("_attempt_id") or job.get("url") or "preview"),
            state=artifact_state,
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
    *,
    disposition: str | None = None,
    receipt_admitted: bool | None = None,
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
        _record_evidence_retention(
            destination,
            archived,
            job,
            disposition=disposition,
            receipt_admitted=receipt_admitted,
        )
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


_LINKEDIN_TRACKING_QUERY_KEYS = frozenset(
    {
        "gh_src",
        "li_fat_id",
        "referrer",
        "trackingid",
        "trk",
    }
)
_LINKEDIN_TRACKING_SOURCE_VALUES = frozenset(
    {"linkedin", "linkedin.com", "linkedin_jobs", "linkedin-jobs"}
)
_COMPANY_SUFFIX_TOKENS = frozenset(
    {"co", "company", "corp", "corporation", "group", "holdings", "inc", "limited", "ltd", "plc", "pte"}
)


def _sanitize_linkedin_external_target(observed) -> str:
    """Drop known campaign parameters without erasing posting identity."""
    retained_query: list[tuple[str, str]] = []
    for key, value in parse_qsl(observed.query, keep_blank_values=True):
        normalized_key = key.casefold().strip()
        normalized_value = value.casefold().strip()
        if normalized_key.startswith("utm_"):
            continue
        if normalized_key in _LINKEDIN_TRACKING_QUERY_KEYS:
            continue
        if (
            normalized_key in {"source", "src"}
            and normalized_value in _LINKEDIN_TRACKING_SOURCE_VALUES
        ):
            continue
        retained_query.append((key, value))

    host = (observed.hostname or "").casefold().rstrip(".")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{observed.port}" if observed.port else ""
    return observed._replace(
        netloc=f"{host}{port}",
        query=urlencode(retained_query, doseq=True),
    ).geturl()


def _identity_tokens(value: object) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKD", str(value or "")).casefold()
    tokens = re.findall(r"[a-z0-9]+", normalized)
    aliases = {
        "engineering": "engineer",
        "engineers": "engineer",
        "internship": "intern",
        "internships": "intern",
        "interns": "intern",
    }
    return tuple(aliases.get(token, token) for token in tokens)


def _normalize_linkedin_page_identity(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping) or value.get("version") != 1:
        return None
    page_title = " ".join(str(value.get("page_title") or "").split())[:300]
    raw_headings = value.get("primary_headings")
    headings = (
        [" ".join(str(item).split())[:300] for item in raw_headings[:6]]
        if isinstance(raw_headings, list)
        else []
    )
    headings = [item for item in headings if item]
    if not page_title and not headings:
        return None
    return {
        "version": 1,
        "page_title": page_title,
        "primary_headings": headings,
    }


def _linkedin_external_identity_matches(
    job: Mapping[str, object], identity: Mapping[str, object]
) -> bool:
    """Require both the authorized title and company on the external job page."""
    title_tokens = _identity_tokens(job.get("title"))
    company_tokens = tuple(
        token
        for token in _identity_tokens(
            job.get("company_name") or job.get("company")
        )
        if token not in _COMPANY_SUFFIX_TOKENS
    )
    if not title_tokens or not company_tokens:
        return False
    evidence_tokens = set(
        _identity_tokens(
            " ".join(
                [
                    str(identity.get("page_title") or ""),
                    *(
                        str(item)
                        for item in identity.get("primary_headings", [])
                        if item
                    ),
                ]
            )
        )
    )
    return set(title_tokens).issubset(evidence_tokens) and set(company_tokens).issubset(
        evidence_tokens
    )


def _linkedin_identity_evidence_digest(identity: Mapping[str, object]) -> str:
    encoded = json.dumps(
        identity,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _linkedin_causal_attestation_matches(
    job: Mapping[str, object], observed: object
) -> dict[str, object] | None:
    private = job.get("_linkedin_causal_apply_attestation")
    if not isinstance(private, Mapping) or not isinstance(observed, Mapping):
        return None
    if private.get("version") != 1 or observed.get("version") != 1:
        return None
    source_job_id = page_observation_mod._linkedin_job_id(
        job.get("url") or job.get("application_url")
    )
    if not source_job_id or private.get("source_job_id") != source_job_id:
        return None
    if (
        private.get("lineage_complete") is not True
        or observed.get("lineage_complete") is not True
    ):
        return None
    mode = str(observed.get("mode") or "")
    if mode not in {"same_target_navigation", "new_popup_from_source"}:
        return None
    expected_attestation_digest = hashlib.sha256(
        str(private.get("attestation_id") or "").encode("utf-8")
    ).hexdigest()
    if observed.get("attestation_id_digest") != expected_attestation_digest:
        return None
    if observed.get("source_target_id_digest") != hashlib.sha256(
        str(private.get("source_target_id") or "").encode("utf-8")
    ).hexdigest():
        return None
    if observed.get("target_id_digest") != private.get("target_id_digest"):
        return None
    if mode != private.get("mode"):
        return None
    if str(observed.get("final_url") or "") != str(private.get("final_url") or ""):
        return None
    observed_lineage = observed.get("redirect_lineage")
    private_lineage = private.get("redirect_lineage")
    if (
        not isinstance(observed_lineage, list)
        or not isinstance(private_lineage, list)
        or observed_lineage != private_lineage
        or not observed_lineage
        or str(observed_lineage[-1]) != str(observed.get("final_url") or "")
    ):
        return None
    return {
        "version": 1,
        "verified": True,
        "attestation_id_digest": expected_attestation_digest,
        "source_target_id_digest": str(observed["source_target_id_digest"]),
        "target_id_digest": str(observed["target_id_digest"]),
        "mode": mode,
        "initial_url": str(observed.get("initial_url") or "")[:2000],
        "final_url": str(observed.get("final_url") or "")[:2000],
        "redirect_lineage": [str(value)[:2000] for value in observed_lineage[:12]],
        "lineage_complete": True,
    }


def _runtime_linkedin_route_gate(
    job: dict,
    audit_report: Mapping[str, object] | None,
    profile: Mapping[str, object],
    *,
    persist_external_handoff: bool = True,
) -> tuple[bool, str]:
    """Resolve a LinkedIn landing page to native apply or an exact safe handoff.

    A newly observed external target is persisted for the next authorization
    manifest, so the current manifest never silently expands to a different
    host. Native LinkedIn forms may continue only after a live form audit.
    """
    landing_url = str(job.get("application_url") or job.get("url") or "").strip()
    landing = urlparse(landing_url)
    landing_host = (landing.hostname or "").casefold().rstrip(".")
    if not (
        landing_host == "linkedin.com" or landing_host.endswith(".linkedin.com")
    ):
        return True, "runtime_route_already_bound"

    audit = audit_report if isinstance(audit_report, Mapping) else {}
    observed_url = str(audit.get("page_url") or "").strip()
    observed = urlparse(observed_url)
    observed_host = (observed.hostname or "").casefold().rstrip(".")
    if (
        observed.scheme != "https"
        or not observed_host
        or observed.username
        or observed.password
    ):
        return False, "linkedin_apply_target_not_observed"

    if observed_host == "linkedin.com" or observed_host.endswith(".linkedin.com"):
        if int(audit.get("submit_control_count") or 0) < 1:
            return False, "linkedin_native_application_form_not_observed"
        allowed, reason = submission_surfaces_mod.surface_allowed(
            "linkedin_native_easy_apply",
            profile,
        )
        if not allowed:
            return False, reason
        job["_linkedin_runtime_surface"] = "linkedin_native_easy_apply"
        return True, "linkedin_native_application_observed"

    try:
        observed_port = observed.port
    except ValueError:
        return False, "linkedin_apply_target_port_invalid"
    del observed_port  # validated above; the sanitizer reads the parsed port again
    sanitized_url = _sanitize_linkedin_external_target(observed)
    runtime_job = dict(job)
    runtime_job["application_url"] = sanitized_url
    verified, verification = submission_surfaces_mod.linkedin_target_verification(
        runtime_job,
        profile,
    )
    if not verified:
        return False, verification
    allowed, reason = submission_surfaces_mod.surface_allowed(
        "linkedin_to_official_ats",
        profile,
    )
    if not allowed:
        return False, reason

    causal_attestation = _linkedin_causal_attestation_matches(
        job, audit.get("causal_apply_attestation")
    )
    if causal_attestation is None:
        return False, "linkedin_external_causal_apply_attestation_required"

    identity_evidence = _normalize_linkedin_page_identity(audit.get("page_identity"))
    if (
        audit.get("disposition") != "linkedin_external_handoff"
        or identity_evidence is None
        or not _linkedin_external_identity_matches(job, identity_evidence)
    ):
        return False, "linkedin_external_job_identity_unverified"

    from applypilot.apply.authorization import compute_job_fingerprint

    source_job = dict(job)
    source_job["application_url"] = landing_url
    runtime_binding = {
        "version": 4,
        "attempt_id": str(job.get("_attempt_id") or ""),
        "job_url": str(job.get("url") or ""),
        "source_application_url": landing_url,
        "target_application_url": sanitized_url,
        "source_job_fingerprint": compute_job_fingerprint(source_job),
        "target_host": observed_host,
        "verification": verification,
        "lineage_verified": True,
        "identity_evidence": identity_evidence,
        "identity_evidence_sha256": _linkedin_identity_evidence_digest(
            identity_evidence
        ),
        "causal_apply_attestation": causal_attestation,
    }
    job["_discovered_application_url"] = sanitized_url
    job["_linkedin_target_verification"] = verification
    job["_linkedin_runtime_route_binding"] = runtime_binding
    if not persist_external_handoff:
        return False, "linkedin_external_handoff_preview_verified"

    connection = get_connection()
    if connection.in_transaction:
        return False, "linkedin_handoff_persistence_transaction_busy"
    connection.execute("BEGIN IMMEDIATE")
    try:
        cursor = connection.execute(
            "UPDATE jobs SET application_url=?, apply_error=NULL WHERE url=? "
            "AND (COALESCE(application_url, '')='' OR application_url=?)",
            (sanitized_url, str(job.get("url") or ""), landing_url),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return False, "linkedin_handoff_persistence_conflict"
        attempt_id = str(job.get("_attempt_id") or "")
        if runtime_binding["lineage_verified"] and attempt_id:
            from applypilot.database import update_application_attempt

            if not update_application_attempt(
                attempt_id,
                phase="route_handoff",
                submit_started=False,
                evidence={"linkedin_runtime_route_binding": runtime_binding},
                conn=connection,
            ):
                connection.rollback()
                return False, "linkedin_handoff_attempt_inactive"
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    return False, "linkedin_external_handoff_reauthorized"


def _authorize_linkedin_runtime_route(
    manifest: dict,
    job: dict,
    profile: Mapping[str, object],
) -> tuple[bool, str]:
    """Validate one attempt-bound LinkedIn-to-ATS route without mutating a manifest."""
    binding = job.get("_linkedin_runtime_route_binding")
    if (
        not isinstance(binding, Mapping)
        or binding.get("version") != 4
        or binding.get("lineage_verified") is not True
    ):
        return False, "linkedin_runtime_route_binding_required"
    if binding.get("attempt_id") != str(job.get("_attempt_id") or ""):
        return False, "linkedin_runtime_route_attempt_mismatch"
    if binding.get("job_url") != str(job.get("url") or ""):
        return False, "linkedin_runtime_route_job_mismatch"
    if binding.get("target_application_url") != str(job.get("application_url") or ""):
        return False, "linkedin_runtime_route_target_mismatch"
    target = urlparse(str(job.get("application_url") or ""))
    target_host = (target.hostname or "").casefold().rstrip(".")
    if not target_host or binding.get("target_host") != target_host:
        return False, "linkedin_runtime_route_target_host_mismatch"

    source_application_url = str(binding.get("source_application_url") or "")
    source = urlparse(source_application_url)
    source_host = (source.hostname or "").casefold().rstrip(".")
    if source_host != "linkedin.com" and not source_host.endswith(".linkedin.com"):
        return False, "linkedin_runtime_route_source_invalid"

    from applypilot.apply.authorization import authorize_job, compute_job_fingerprint

    source_job = dict(job)
    source_job["application_url"] = source_application_url
    if binding.get("source_job_fingerprint") != compute_job_fingerprint(source_job):
        return False, "linkedin_runtime_route_source_fingerprint_mismatch"
    if authorize_job(manifest, source_job) is None:
        return False, "authorization_manifest_source_job_mismatch"

    identity_evidence = _normalize_linkedin_page_identity(
        binding.get("identity_evidence")
    )
    if (
        identity_evidence is None
        or binding.get("identity_evidence_sha256")
        != _linkedin_identity_evidence_digest(identity_evidence)
    ):
        return False, "linkedin_runtime_route_identity_evidence_mismatch"
    if not _linkedin_external_identity_matches(source_job, identity_evidence):
        return False, "linkedin_runtime_route_job_identity_unverified"
    causal_attestation = _linkedin_causal_attestation_matches(
        source_job, binding.get("causal_apply_attestation")
    )
    if causal_attestation is None:
        return False, "linkedin_runtime_route_causal_attestation_mismatch"

    verified, verification = submission_surfaces_mod.linkedin_target_verification(
        job,
        profile,
    )
    if not verified or verification != binding.get("verification"):
        return False, "linkedin_runtime_route_target_verification_failed"
    allowed, reason = submission_surfaces_mod.surface_allowed(
        "linkedin_to_official_ats",
        profile,
    )
    if not allowed:
        return False, reason
    return True, "linkedin_runtime_route_authorized"


def _reserve_manifest_submission(
    manifest: dict | None,
    job: dict,
    audit_report: Mapping[str, object] | None = None,
    *,
    success_target: int | None = None,
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

        profile = config.load_profile()
        if authorize_job(manifest, job) is None:
            if not isinstance(job.get("_linkedin_runtime_route_binding"), Mapping):
                return False, "authorization_manifest_job_mismatch"
            route_authorized, route_authorization_reason = (
                _authorize_linkedin_runtime_route(manifest, job, profile)
            )
            if not route_authorized:
                return False, route_authorization_reason
        runtime_route_allowed, runtime_route_reason = _runtime_linkedin_route_gate(
            job,
            audit_report,
            profile,
        )
        if not runtime_route_allowed:
            return False, runtime_route_reason
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
                    success_target=success_target,
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
                job["_submission_gate_binding"] = {
                    "gate_id": str(claim.get("gate_id") or ""),
                    "batch_id": str(manifest.get("batch_id") or ""),
                    "job_url": str(job.get("url") or ""),
                    "attempt_id": attempt_id,
                }
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


def _has_admitted_submission_receipt(
    manifest: Mapping[str, object] | None,
    job: Mapping[str, object],
) -> bool:
    """Check durable receipt admission for this exact authorized attempt."""
    if not isinstance(manifest, Mapping):
        return False
    from applypilot.database import has_admitted_submission_receipt

    return has_admitted_submission_receipt(
        str(manifest.get("batch_id") or ""),
        str(job.get("url") or ""),
        str(job.get("_attempt_id") or ""),
        conn=get_connection(),
    )


def _admit_direct_email_receipt(job: dict, receipt: object) -> dict[str, object]:
    """Admit one provider message id before a direct-email success is recorded."""
    if not isinstance(receipt, dict):
        return {"status": "rejected", "reason": "sent_receipt_required"}
    from applypilot.database import admit_direct_email_sent_receipt

    return admit_direct_email_sent_receipt(
        str(job.get("url") or ""),
        receipt,
        gate_binding=(
            job.get("_submission_gate_binding")
            if isinstance(job.get("_submission_gate_binding"), Mapping)
            else None
        ),
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


def _issue_manual_resume_authorization(
    job: dict,
    *,
    submit_started: bool,
    timeout_seconds: int | None = None,
) -> dict[str, object] | None:
    """Persist an exact page-bound resume token before waiting for a human action."""
    raw_bundle = job.get("_browser_lease_binding")
    attempt_id = str(job.get("_attempt_id") or "").strip()
    application_id = str(job.get("url") or "").strip()
    if not isinstance(raw_bundle, Mapping) or not attempt_id or not application_id:
        return None
    bundle = BrowserLeaseBundle.from_mapping(raw_bundle)
    policy = config.load_profile().get("submission_policy", {})
    configured_timeout = (
        int(timeout_seconds)
        if timeout_seconds is not None
        else int(
            policy.get("manual_intervention_timeout_seconds", 1800)
            if isinstance(policy, Mapping)
            else 1800
        )
    )
    authorization = resume_authorization_mod.issue_resume_authorization(
        attempt_id=attempt_id,
        application_id=application_id,
        page_binding=bundle.page_binding,
        trigger="captcha_cleared",
        submit_started=submit_started,
        ttl_seconds=max(60, min(configured_timeout, 3600)),
    )
    resume_authorization_mod.store_resume_authorization(
        get_connection(),
        authorization,
    )
    encoded = authorization.as_dict()
    job["_resume_authorization"] = encoded
    return encoded


def _configured_receipt_observers(
    profile: Mapping[str, object],
) -> list[tuple[str, MailboxMcpSpec]]:
    """Return configured Gmail/Outlook observers without exposing mailbox values."""
    authentication = profile.get("authentication", {})
    if not isinstance(authentication, Mapping):
        return []
    if not bool(
        authentication.get(
            "mailbox_read_authorized",
            authentication.get("gmail_verification_authorized", False),
        )
    ):
        return []
    configured = authentication.get("receipt_mailboxes")
    observers: list[tuple[str, MailboxMcpSpec]] = []
    if isinstance(configured, list):
        for item in configured:
            if not isinstance(item, Mapping):
                continue
            provider = str(item.get("provider") or "").strip().casefold()
            if provider not in receipt_observer_mod.SUPPORTED_PROVIDERS:
                continue
            raw_spec = item.get("mailbox_mcp")
            if provider == "outlook" and not isinstance(raw_spec, Mapping):
                # The built-in default is Gmail-specific.  Outlook must name
                # its own read-only MCP surface so we never query one provider
                # through another provider's credentials or tools.
                continue
            spec = resolve_mailbox_mcp_spec(
                raw_spec if isinstance(raw_spec, Mapping) else None,
            )
            if spec.enabled and provider not in {name for name, _ in observers}:
                observers.append((provider, spec))
        return observers

    mailbox = str(
        authentication.get("mailbox")
        or authentication.get("gmail_verification_mailbox")
        or ""
    ).casefold()
    if not mailbox:
        return []
    provider = (
        "outlook"
        if mailbox.endswith(("@outlook.com", "@hotmail.com", "@live.com"))
        else "gmail"
    )
    if provider == "outlook":
        return []
    return [(provider, resolve_mailbox_mcp_spec())]


def _build_receipt_observer_context(
    job: Mapping[str, object],
    *,
    provider: str,
    submitted_at: datetime,
) -> dict[str, object]:
    return receipt_observer_mod.receipt_observer_context(
        get_connection(),
        job,
        provider=provider,
        submitted_at=submitted_at,
    )


def _process_receipt_observer_result(
    job: Mapping[str, object],
    *,
    provider: str,
    submitted_at: datetime,
    observation: Mapping[str, object],
) -> dict[str, object]:
    return receipt_observer_mod.process_receipt_observation(
        get_connection(),
        job,
        provider=provider,
        submitted_at=submitted_at,
        observation=observation,
        gate_binding=(
            job.get("_submission_gate_binding")
            if isinstance(job.get("_submission_gate_binding"), Mapping)
            else None
        ),
    )


def _consume_manual_resume_authorization(
    job: dict,
    authorization: Mapping[str, object],
    *,
    submit_started: bool,
) -> bool:
    """Consume one resume token only while its exact page epoch remains bound."""
    raw_bundle = job.get("_browser_lease_binding")
    if not isinstance(raw_bundle, Mapping):
        return False
    try:
        bundle = BrowserLeaseBundle.from_mapping(raw_bundle)
        parsed = resume_authorization_mod.ResumeAuthorization.from_mapping(
            authorization
        )
        resume_authorization_mod.validate_resume_authorization(
            parsed,
            attempt_id=str(job.get("_attempt_id") or ""),
            application_id=str(job.get("url") or ""),
            page_binding=bundle.page_binding,
            trigger="captcha_cleared",
            submit_started=submit_started,
        )
        return resume_authorization_mod.consume_resume_authorization(
            get_connection(),
            parsed,
        )
    except (KeyError, TypeError, ValueError, sqlite3.Error):
        logger.warning(
            "Resume authorization rejected for attempt %s",
            job.get("_attempt_id"),
            exc_info=True,
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
    return bool(
        authentication_capability(profile, "credential_relay_authorized")
        and job.get("_browser_backend") != "cloak"
    )


def _runtime_application_route(
    job: Mapping[str, object],
    *,
    submission_phase: str,
    direct_email_send_authorized: bool,
) -> str:
    """Keep email-only preparation off the browser tool surface."""
    if submission_phase == "receipt":
        return "receipt_mailbox"
    if direct_email_send_authorized:
        return "direct_email"
    if (
        submission_phase == "prepare"
        and submission_surfaces_mod.classify_submission_surface(job)
        == "official_direct_email"
    ):
        return "direct_email"
    return "browser"


def _identity_relay_allowed(profile: dict, job: dict) -> bool:
    identity = profile.get("identity_materials", {})
    fin_policy = identity.get("fin", {}) if isinstance(identity, Mapping) else {}
    return bool(
        isinstance(fin_policy, Mapping)
        and fin_policy.get("secure_relay_authorized") is True
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


def _credential_application_id(job: Mapping[str, object]) -> str:
    """Derive an opaque, stable identity for this exact selected posting."""
    payload = {
        "url": str(job.get("url") or ""),
        "application_url": str(job.get("application_url") or ""),
        "canonical_job_url": str(job.get("canonical_job_url") or ""),
        "platform_job_id": str(job.get("platform_job_id") or ""),
        "provider_application_id": str(job.get("provider_application_id") or ""),
        "requisition_id": str(job.get("requisition_id") or ""),
        "company": str(job.get("company_name") or ""),
        "title": str(job.get("title") or ""),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _credential_target_urls(
    job: Mapping[str, object], *, attempt_id: str
) -> list[str]:
    """Return only launcher-selected exact application routes for this attempt."""
    primary_target = job.get("application_url") or job.get("url")
    candidates: list[object] = [primary_target, job.get("_discovered_application_url")]
    runtime_binding = job.get("_linkedin_runtime_route_binding")
    if (
        isinstance(runtime_binding, Mapping)
        and str(runtime_binding.get("attempt_id") or "") == attempt_id
        and runtime_binding.get("lineage_verified") is True
    ):
        candidates.append(runtime_binding.get("target_application_url"))
    routes: set[str] = set()
    identity_query_keys = {
        "career_job_req_id",
        "gh_jid",
        "job",
        "job_id",
        "jobid",
        "posting_id",
        "postingid",
        "reqid",
        "requisition_id",
        "requisitionid",
    }
    for candidate in candidates:
        raw = str(candidate or "").strip()
        if not raw:
            continue
        safe = _safe_ats_target_url({"application_url": raw})
        if safe:
            parsed = urlparse(raw)
            identity_query = sorted(
                (key, value)
                for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                if key.casefold() in identity_query_keys
            )
            routes.add(
                f"{safe}?{urlencode(identity_query)}" if identity_query else safe
            )
    return sorted(routes)


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


def _install_answer_provenance_context(job: dict) -> dict[str, object]:
    """Install one host-owned, non-authorizing provenance binding."""

    binding = answer_provenance_mod.build_host_provenance_binding(job)
    job["_answer_provenance_binding"] = binding
    return answer_provenance_mod.public_provenance_context(binding)


def _host_staged_field_risk(field: Mapping[str, object]) -> str:
    """Derive risk from host-observed field state, never caller metadata."""

    label = str(field.get("label") or field.get("text") or "")
    semantic = str(field.get("semantic") or "unknown")
    control = str(field.get("control") or "").casefold()
    declaration = control == "checkbox" or bool(
        re.search(
            r"\b(?:declare|declaration|attest|attestation|certify|certification|"
            r"acknowledge|consent|terms and conditions)\b",
            label,
            re.IGNORECASE,
        )
    )
    staged_adapter_risk = field.get("risk")
    adapter_risk = (
        staged_adapter_risk
        if staged_adapter_risk in {"low", "medium", "high"}
        else None
    )
    return field_risk(
        f"{label} {semantic}",
        adapter_risk=adapter_risk,
        direct_impact=field.get("direct_impact") is True,
        declaration=declaration,
    )


def _build_ats_application_context(
    job: Mapping[str, object],
    profile: Mapping[str, object],
    *,
    attempt_id: str = "",
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
    provenance_binding = job.get("_answer_provenance_binding")
    if isinstance(provenance_binding, Mapping):
        recomputed = answer_provenance_mod.build_host_provenance_binding(job)
        if dict(provenance_binding) != recomputed:
            raise ValueError("answer provenance binding drifted before context staging")
        context["answer_provenance"] = answer_provenance_mod.public_provenance_context(
            recomputed
        )
        context["_trusted_fact_scopes"] = list(recomputed["fact_scopes"])
        trusted_scopes = set(recomputed["fact_scopes"])
        available_fact_refs: list[dict[str, str]] = []
        for fact in current_profile_facts(profile):
            if fact.scope not in trusted_scopes:
                continue
            resolution = resolve_application_fact_ref(
                current_profile_facts(profile),
                fact_ref=fact.fact_ref,
                scope=str(fact.scope),
                minimum_sensitivity=fact.sensitivity,
            )
            if not resolution.production_ready:
                continue
            available_fact_refs.append(
                {
                    "key": fact.key[:120],
                    "fact_ref": fact.fact_ref[:160],
                    "sensitivity": fact.sensitivity,
                }
            )
            if len(available_fact_refs) >= 200:
                break
        context["available_fact_refs"] = available_fact_refs
    if attempt_id:
        provider_binding = job.get("_ats_application_binding")
        context["credential_binding"] = {
            "schema_version": "1",
            "attempt_id": attempt_id,
            "application_id": _credential_application_id(job),
            "target_urls": _credential_target_urls(job, attempt_id=attempt_id),
            "provider_binding": (
                dict(provider_binding)
                if isinstance(provider_binding, Mapping)
                else {}
            ),
        }
    observation = job.get("_browser_observation")
    if isinstance(observation, Mapping):
        form_context = observation.get("ats_adapter_context")
        if isinstance(form_context, Mapping):
            safe_form = dict(form_context)
            raw_fields = safe_form.get("fields")
            if isinstance(raw_fields, list):
                safe_fields: list[dict[str, object]] = []
                for raw_field in raw_fields:
                    if not isinstance(raw_field, Mapping):
                        continue
                    safe_field = {
                        key: value
                        for key, value in raw_field.items()
                        if key not in {"options", "options_truncated"}
                    }
                    safe_field["risk"] = _host_staged_field_risk(raw_field)
                    full_digest = raw_field.get("options_full_sha256")
                    source_count = raw_field.get("options_source_count")
                    source_truncated = raw_field.get("options_source_truncated")
                    if not isinstance(full_digest, str) or not re.fullmatch(
                        r"[0-9a-f]{64}", full_digest
                    ):
                        legacy_options = raw_field.get("options")
                        declared_count = raw_field.get("option_count")
                        count_matches = declared_count is None or (
                            isinstance(declared_count, int)
                            and not isinstance(declared_count, bool)
                            and isinstance(legacy_options, list)
                            and declared_count == len(legacy_options)
                        )
                        if (
                            isinstance(legacy_options, list)
                            and raw_field.get("options_truncated") is False
                            and count_matches
                        ):
                            full_digest = hashlib.sha256(
                                json.dumps(
                                    legacy_options,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ).encode("utf-8")
                            ).hexdigest()
                            source_count = len(legacy_options)
                            source_truncated = False
                        else:
                            full_digest = None
                            source_count = declared_count
                            source_truncated = True
                    safe_field["options_sha256"] = full_digest
                    safe_field["options_source_count"] = source_count
                    safe_field["options_source_truncated"] = source_truncated
                    safe_fields.append(safe_field)
                safe_form["fields"] = safe_fields
            context["observed_form"] = safe_form
            context["adapter"] = str(form_context.get("adapter") or adapter)
        provenance_observation = observation.get("answer_provenance")
        provenance_context = context.get("answer_provenance")
        if isinstance(provenance_observation, Mapping) and isinstance(
            provenance_context, dict
        ):
            snapshot_digest = str(
                provenance_observation.get("snapshot_digest") or ""
            )
            if re.fullmatch(r"[0-9a-f]{64}", snapshot_digest):
                provenance_context["expected_snapshot_digest"] = snapshot_digest
        workday_context = observation.get("workday_state")
        if isinstance(workday_context, Mapping):
            context["workday_state"] = dict(workday_context)
    fill_plan = job.get("_ats_fill_plan_context")
    if isinstance(fill_plan, Mapping):
        context["fill_plan"] = dict(fill_plan)
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
    if submission_phase == "receipt":
        job["_runtime_recovery_admission"] = {
            "disposition": "receipt_only",
            "reason_code": "DETERMINISTIC_RECEIPT_OBSERVER_REQUIRED",
            "requires_fresh_observation": True,
        }
        return "submission_uncertain", 0
    turn_id = f"agent-{uuid.uuid4()}"
    run_id = turn_id  # Backward-compatible runtime/report identity.
    attempt_id = str(job.get("_attempt_id") or f"preview-{turn_id}")
    actor_id = application_actor_id(attempt_id)
    stored_parent_turn_id = str(job.get("_parent_agent_run_id") or "").strip()
    stored_parent_checkpoint_id = str(
        job.get("_parent_agent_checkpoint_id") or ""
    ).strip()
    recovery_command = _scoped_runtime_recovery()
    operator_resume = _scoped_operator_resume()
    submit_continuation = _scoped_submit_continuation()
    if recovery_command is not None:
        if (
            recovery_command.actor_id != actor_id
            or recovery_command.attempt_id != attempt_id
            or recovery_command.turn_id != stored_parent_turn_id
            or not stored_parent_checkpoint_id
        ):
            raise RuntimeError("runtime recovery parent/checkpoint binding is stale")
        runtime_parent_turn_id: str | None = recovery_command.turn_id
        runtime_parent_checkpoint_id: str | None = stored_parent_checkpoint_id
        runtime_authorization_id: str | None = recovery_command.command_id
    elif operator_resume is not None:
        if (
            submission_phase != "prepare"
            or operator_resume.get("actor_id") != actor_id
            or operator_resume.get("attempt_id") != attempt_id
            or operator_resume.get("parent_turn_id") != stored_parent_turn_id
            or operator_resume.get("checkpoint_id") != stored_parent_checkpoint_id
        ):
            raise RuntimeError("operator resume parent/checkpoint binding is stale")
        runtime_parent_turn_id = stored_parent_turn_id
        runtime_parent_checkpoint_id = stored_parent_checkpoint_id
        runtime_authorization_id = str(
            operator_resume.get("authorization_id") or ""
        )
    elif submit_continuation is not None:
        if (
            submission_phase != "submit"
            or submit_continuation.get("actor_id") != actor_id
            or submit_continuation.get("attempt_id") != attempt_id
            or submit_continuation.get("parent_turn_id") != stored_parent_turn_id
            or submit_continuation.get("checkpoint_id")
            != stored_parent_checkpoint_id
        ):
            raise RuntimeError("submit continuation parent/checkpoint binding is stale")
        runtime_parent_turn_id = stored_parent_turn_id
        runtime_parent_checkpoint_id = stored_parent_checkpoint_id
        runtime_authorization_id = str(
            submit_continuation.get("authorization_id") or ""
        )
    else:
        # A job-carried parent is diagnostic only.  It cannot authorize a child
        # process without the currently executing recovery command capability.
        runtime_parent_turn_id = None
        runtime_parent_checkpoint_id = None
        runtime_authorization_id = None
    restart_admission = _durable_agent_runtime.reconcile_actor(actor_id, attempt_id)
    job["_runtime_recovery_admission"] = {
        "disposition": restart_admission.disposition,
        "parent_turn_id": restart_admission.parent_turn_id,
        "reason_code": restart_admission.reason_code,
        "requires_fresh_observation": restart_admission.requires_fresh_observation,
    }
    if restart_admission.disposition == "live_owner":
        return "failed:runtime_owner_active", 0
    if restart_admission.disposition == "blocked":
        return "failed:runtime_recovery_blocked", 0
    if restart_admission.disposition == "receipt_only":
        return "submission_uncertain", 0
    if restart_admission.disposition == "recovery_required":
        scoped_recovery_matches = bool(
            (recovery_command is not None or operator_resume is not None)
            and runtime_parent_turn_id == restart_admission.parent_turn_id
        )
        if not scoped_recovery_matches:
            return "failed:runtime_recovery_required", 0
    setup_started = time.perf_counter()
    setup_metrics: dict[str, int | float] = {}
    runtime_settings = load_runtime_settings()
    agent_timeout_seconds = runtime_settings.agent_timeout_seconds
    timeout_multiplier = job.get("_agent_timeout_multiplier", 1)
    if (
        isinstance(timeout_multiplier, (int, float))
        and not isinstance(timeout_multiplier, bool)
        and timeout_multiplier > 1
    ):
        agent_timeout_seconds = min(
            3600,
            max(
                agent_timeout_seconds,
                int(agent_timeout_seconds * min(float(timeout_multiplier), 2.0)),
            ),
        )
    profile = config.load_profile()
    authentication = profile.get("authentication", {})
    credential_relay_authorized = _credential_relay_allowed(profile, job)
    identity_relay_authorized = _identity_relay_allowed(profile, job)
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
    mailbox_mcp = mailbox_mcp_for_phase(
        mailbox_mcp,
        submission_phase=submission_phase,
        direct_email_send_authorized=direct_email_send_authorized,
        verification_resume="verification_resumed" in runtime_state,
    )
    runtime_ats_adapter = ats_mod.detect_ats_site(_safe_ats_target_url(job))
    runtime_state.add(
        "ats_unknown"
        if runtime_ats_adapter in {"", "generic"}
        else f"ats_{runtime_ats_adapter.casefold()}"
    )
    runtime_route = _runtime_application_route(
        job,
        submission_phase=submission_phase,
        direct_email_send_authorized=direct_email_send_authorized,
    )
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

    broker_session_persistent = bool(job.get("_browser_root_runtime"))
    browser_lease_bundle = _browser_lease_for_agent_turn(
        job,
        worker_id=worker_id,
        port=port,
        agent_backend=agent_backend,
        actor_id=actor_id,
        attempt_id=attempt_id,
        submission_phase=submission_phase,
        dry_run=dry_run,
        resume_existing_page=resume_existing_page,
    )
    _install_answer_provenance_context(job)
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
    ats_context = _build_ats_application_context(job, profile, attempt_id=attempt_id)
    credential_binding = ats_context.get("credential_binding")
    if not isinstance(credential_binding, Mapping):
        raise TypeError("Credential relay application binding was not generated")
    ats_context_json = json.dumps(ats_context, ensure_ascii=False, sort_keys=True)
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
        credential_relay_authorized=credential_relay_authorized,
        identity_relay_authorized=identity_relay_authorized,
    )
    if operator_resume is not None:
        agent_prompt += (
            "\n\nOPERATOR RESUME BOUNDARY:\n"
            "An exact reference-only human response has been admitted for this "
            "same page and checkpoint. Re-observe the current page and use only "
            "current host-provided facts or visible state. Never infer answer "
            "content from an opaque response reference. This is a prepare-only "
            "turn and does not authorize Submit.\n"
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
    runtime_metadata["browser_broker"] = {
        "schema_version": "1",
        "profile_id": browser_lease_bundle.profile.resource_id,
        "profile_epoch": browser_lease_bundle.profile.epoch,
        "page_id": browser_lease_bundle.page.resource_id,
        "page_lease_epoch": browser_lease_bundle.page.epoch,
        "page_epoch": browser_lease_bundle.page_binding.page_epoch,
        "capabilities": list(browser_lease_bundle.page.capabilities),
        "authority": "observation_only",
    }
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
                credential_relay_authorized=credential_relay_authorized,
                identity_relay_authorized=identity_relay_authorized,
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
        identity_relay_authorized=identity_relay_authorized,
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
        "APPLYPILOT_IDENTITY_CREDENTIAL_FILE",
        "APPLYPILOT_CREDENTIAL_ALLOWED_HOSTS",
        "APPLYPILOT_CREDENTIAL_PASSWORD_ALLOWED_HOSTS",
        "APPLYPILOT_CREDENTIAL_ALLOW_KNOWN_ATS_REDIRECT",
        "APPLYPILOT_CREDENTIAL_ROOT_TARGET_IDS",
        "APPLYPILOT_CREDENTIAL_APPLICATION_CONTEXT_SHA256",
        "APPLYPILOT_CREDENTIAL_ATTEMPT_ID",
        "APPLYPILOT_CREDENTIAL_APPLICATION_ID",
        "APPLYPILOT_CREDENTIAL_RELAY_AUTHORIZED",
        "APPLYPILOT_IDENTITY_RELAY_AUTHORIZED",
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
        env["APPLYPILOT_CREDENTIAL_PASSWORD_ALLOWED_HOSTS"] = ",".join(
            sorted(
                str(host).strip().casefold()
                for host in configured_password_hosts
                if str(host).strip()
            )
        )
    if identity_relay_authorized:
        env["APPLYPILOT_IDENTITY_RELAY_AUTHORIZED"] = "1"
        env["APPLYPILOT_IDENTITY_CREDENTIAL_FILE"] = str(
            config.APP_DIR / "credentials" / "identity-protected.json"
        )
    if credential_relay_authorized or identity_relay_authorized:
        env["APPLYPILOT_CREDENTIAL_ALLOWED_HOSTS"] = ",".join(
            sorted(host for host in allowed_hosts if host)
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
        env["APPLYPILOT_CREDENTIAL_APPLICATION_CONTEXT_SHA256"] = hashlib.sha256(
            ats_context_json.encode("utf-8")
        ).hexdigest()
        env["APPLYPILOT_CREDENTIAL_ATTEMPT_ID"] = attempt_id
        env["APPLYPILOT_CREDENTIAL_APPLICATION_ID"] = str(
            credential_binding["application_id"]
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
            "actor_same_application_retries_remaining": int(
                job.get("_application_actor_same_application_retries_remaining") or 0
            ),
            "actor_new_session_retries_remaining": int(
                job.get("_application_actor_new_session_retries_remaining") or 0
            ),
            "ats_adapter": str(ats_context.get("adapter") or "generic"),
            "ats_context_schema_version": str(
                ats_context.get("schema_version") or ats_mod.ATS_SCHEMA_VERSION
            ),
            **(
                {
                    "ats_fill_plan_ref": str(
                        ats_context["fill_plan"].get("snapshot_ref") or ""
                    ),
                    "ats_fill_plan_snapshot_sha256": str(
                        ats_context["fill_plan"].get("snapshot_sha256") or ""
                    ),
                    "ats_fill_plan_sha256": str(
                        ats_context["fill_plan"].get("plan_sha256") or ""
                    ),
                }
                if isinstance(ats_context.get("fill_plan"), Mapping)
                else {}
            ),
            **(
                {"workday_state": ats_context["workday_state"]}
                if isinstance(ats_context.get("workday_state"), Mapping)
                else {}
            ),
            **(
                {
                    "operator_resume": {
                        "request_id": str(operator_resume["request_id"]),
                        "response_type": _bounded_control_text(
                            operator_resume["response_type"], maximum=80
                        ),
                        "reference_only": True,
                        "submit_authority": False,
                    }
                }
                if operator_resume is not None
                else {}
            ),
        },
        available_tools=tuple(job["_available_tools"]),
        actor_id=actor_id,
        turn_id=turn_id,
        parent_run_id=runtime_parent_turn_id,
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
        ats_context_json,
        encoding="utf-8",
    )
    stats: dict = {}
    proc = None
    durable_handle: DurableRunHandle | None = None
    watchdog: threading.Timer | None = None
    lease_heartbeat: LeaseHeartbeat | None = None
    timed_out = threading.Event()
    agent_process_started = False
    last_agent_event_at: datetime | None = None
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
    browser_tool_call_count = 0
    browser_tool_success_count = 0
    pending_browser_tools: set[str] = set()
    prepare_search_events: list[tuple[object, object]] = []
    if submission_phase == "prepare":
        job.pop("_mailbox_prepare_duplicate_receipt", None)

    def cancel_runtime_on_lease_failure(_exc: Exception) -> None:
        try:
            _agent_subprocess_runtime.cancel(run_id)
        except KeyError:
            return

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
        launch_spec = agent_runtime_mod.SubprocessLaunchSpec(
            run_id=run_id,
            attempt_id=attempt_id,
            actor_id=actor_id,
            turn_id=turn_id,
            command=tuple(cmd),
            prompt=agent_prompt,
            cwd=worker_dir,
            env=env,
            runtime_id=browser_lease_bundle.profile.runtime_id,
            profile_id=browser_lease_bundle.profile.resource_id,
            parent_run_id=runtime_parent_turn_id,
            submit_started=submission_phase == "submit" and not dry_run,
        )
        raw_control_contract = job.get("_control_contract")
        prompt_contract = (
            dict(raw_control_contract)
            if isinstance(raw_control_contract, Mapping)
            else {}
        )
        durable_intent = DurableLaunchIntent(
            spec=launch_spec,
            runtime_backend=f"{agent_backend}-cli",
            resume_mode="resume" if runtime_parent_turn_id else "root",
            checkpoint_id=runtime_parent_checkpoint_id,
            model=model,
            recovery_authorization_id=(
                runtime_authorization_id
            ),
            tool_surface_hash=_control_contract_digest(
                {
                    "schema_version": "1",
                    "available_tools": sorted(agent_request.available_tools),
                }
            ),
            prompt_contract_hash=_control_contract_digest(
                {
                    "schema_version": "1",
                    "phase": submission_phase,
                    "dry_run": dry_run,
                    "control_contract": prompt_contract,
                }
            ),
            idempotency_key=f"agent-turn:v2:{actor_id}:{turn_id}:spawn",
        )
        if runtime_parent_turn_id:
            durable_handle = _durable_agent_runtime.resume(
                durable_intent,
                popen_factory=subprocess.Popen,
            )
        else:
            durable_handle = _durable_agent_runtime.start(
                durable_intent,
                popen_factory=subprocess.Popen,
            )
        proc = durable_handle.process
        process_spawned_at = time.perf_counter()
        setup_metrics["process_spawn_ms"] = round(
            (process_spawned_at - process_spawn_started) * 1000,
            3,
        )
        with _claude_lock:
            _claude_procs[worker_id] = proc
        agent_process_started = True
        lease_heartbeat = LeaseHeartbeat(
            _browser_broker,
            browser_lease_bundle,
            interval_seconds=30.0,
            on_failure=cancel_runtime_on_lease_failure,
        ).start()
        timed_out, watchdog = _start_timeout_watchdog(proc, agent_timeout_seconds)
        last_agent_event_at = _persist_agent_turn_started(
            agent_request,
            backend=agent_backend,
            model=model,
            runtime_metadata=runtime_metadata,
        )

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
                                if str(raw_name).startswith("mcp__playwright__"):
                                    browser_tool_call_count += 1
                                    browser_tool_id = str(block.get("id") or "")
                                    if browser_tool_id:
                                        pending_browser_tools.add(browser_tool_id)
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
                            tool_use_id = str(block.get("tool_use_id") or "")
                            if tool_use_id in pending_browser_tools:
                                pending_browser_tools.discard(tool_use_id)
                                if block.get("is_error") is not True:
                                    browser_tool_success_count += 1
                            pending = pending_mailbox_tools.pop(
                                tool_use_id,
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
                            browser_tool = (
                                "playwright" in str(server).casefold()
                                and str(tool).casefold().startswith("browser_")
                            )
                            if browser_tool:
                                browser_tool_call_count += 1
                                if (
                                    item.get("error") in (None, "")
                                    and str(item.get("status") or "completed").casefold()
                                    in {"completed", "success", "succeeded"}
                                ):
                                    browser_tool_success_count += 1
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
        if lease_heartbeat is not None:
            lease_heartbeat.raise_if_failed()
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
        normalized_status, failure_context = _normalize_browser_runtime_failure(
            status,
            browser_tool_call_count=browser_tool_call_count,
            browser_tool_success_count=browser_tool_success_count,
            failure_context=failure_context,
        )
        if normalized_status != status:
            status = normalized_status
            result_source = f"{result_source}:runtime_browser_evidence"
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
        if turn_result.status.strip().casefold() != status.strip().casefold():
            normalized_observations = dict(turn_result.observations)
            if failure_context is not None:
                normalized_observations["failure_context"] = failure_context
            turn_result = AgentTurnResult(
                run_id=turn_result.run_id,
                status=status,
                summary=turn_result.summary,
                proposals=turn_result.proposals,
                observations=normalized_observations,
                requested_human_input=turn_result.requested_human_input,
                completed_at=turn_result.completed_at,
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
                last_agent_event_at = _persist_agent_proposal_outcomes(
                    agent_request,
                    proposal_outcomes,
                    max_workers=proposal_workers,
                    occurred_after=last_agent_event_at,
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
        fill_plan_context = job.get("_ats_fill_plan_context")
        if agent_process_started and isinstance(fill_plan_context, Mapping):
            job["_ats_fill_plan_consumed"] = {
                "accepted": True,
                "run_id": run_id,
                "snapshot_ref": str(fill_plan_context.get("snapshot_ref") or ""),
                "snapshot_sha256": str(
                    fill_plan_context.get("snapshot_sha256") or ""
                ),
                "plan_sha256": str(fill_plan_context.get("plan_sha256") or ""),
            }
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
        if lease_heartbeat is not None:
            lease_heartbeat.stop()
            browser_lease_bundle = lease_heartbeat.bundle
            job["_browser_lease_binding"] = browser_lease_bundle.as_dict()
        with _claude_lock:
            _claude_procs.pop(worker_id, None)
        if proc is not None and proc.poll() is None:
            try:
                _kill_process_tree(proc.pid)
                proc.wait(timeout=5)
            except (OSError, subprocess.SubprocessError) as exc:
                logger.warning(
                    "Could not prove Agent subprocess termination %s: %s",
                    run_id,
                    type(exc).__name__,
                )
        if agent_process_started and durable_handle is not None:
            job.pop("_parent_agent_run_id", None)
            job.pop("_parent_agent_checkpoint_id", None)
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
            process_returncode = durable_handle.process.poll()
            if process_returncode is not None:
                _persist_agent_turn_completed(
                    agent_request,
                    final_result,
                    application_status=final_status,
                    duration_ms=final_duration_ms,
                    source=turn_source,
                    occurred_after=last_agent_event_at,
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
                        "browser_tool_call_count": browser_tool_call_count,
                        "browser_tool_success_count": browser_tool_success_count,
                    },
                )
                checkpoint_id = _confirmed_agent_checkpoint_id(agent_request)
                if checkpoint_id is None:
                    durable_status = "unknown"
                    durable_failure = "CONTROL_CHECKPOINT_UNCONFIRMED"
                elif final_status == "submission_uncertain":
                    durable_status = "unknown"
                    durable_failure = "SUBMISSION_RESULT_UNKNOWN"
                elif timed_out.is_set():
                    durable_status = "timed_out"
                    durable_failure = "AGENT_RUNTIME_TIMEOUT"
                elif process_returncode == 0:
                    durable_status = "completed"
                    durable_failure = None
                else:
                    durable_status = "failed"
                    durable_failure = f"PROCESS_EXIT_{process_returncode}"
                try:
                    terminal_turn = _durable_agent_runtime.terminal(
                        durable_handle,
                        status=durable_status,
                        failure_code=durable_failure,
                        exit_code=process_returncode,
                    )
                except (KeyError, RuntimeError, ValueError, sqlite3.Error) as exc:
                    logger.warning(
                        "Could not terminalize durable Agent turn %s: %s",
                        run_id,
                        type(exc).__name__,
                    )
                else:
                    if checkpoint_id is not None and terminal_turn.status != "unknown":
                        job["_parent_agent_run_id"] = run_id
                        job["_parent_agent_checkpoint_id"] = checkpoint_id
            else:
                logger.error(
                    "Agent subprocess remains live after cleanup; durable turn %s stays running",
                    run_id,
                )
            try:
                _durable_agent_runtime.close_local(run_id)
            except (KeyError, agent_runtime_mod.SubprocessRuntimeError) as exc:
                logger.warning(
                    "Could not close local Agent runtime handle %s: %s",
                    run_id,
                    type(exc).__name__,
                )
        if not broker_session_persistent:
            _browser_broker.release_scope(f"worker:{worker_id}")
            job.pop("_browser_lease_binding", None)
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

    if dry_run and continuous:
        raise ValueError("Continuous dry-run is not supported; use a finite limit.")
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
        mode_label = (
            f"{effective_limit} confirmed submissions with "
            f"{manifest_cap} manifest-authorized slot(s)"
        )

    authorization_slot_cap = (
        int(authorization_manifest.get("max_submissions", 0))
        if authorization_manifest is not None
        else effective_limit
    )
    run_progress = RunProgress(
        dry_run=dry_run,
        success_target=effective_limit,
        preview_target=effective_limit,
        authorization_slot_cap=authorization_slot_cap,
    )

    target_workers = _workers_for_target(workers, effective_limit)
    if target_workers != workers:
        console.print(
            f"[dim]Using {target_workers} worker(s) for a finite target of "
            f"{effective_limit}; zero-allocation workers are not started.[/dim]"
        )
        workers = target_workers

    execution_plan = build_execution_plan(
        [
            *(
                PhaseDemand(
                    task_id=f"worker-{worker_id}:prepare",
                    phase="prepare",
                    browser_profile=f"{requested_browser_backend}:worker-{worker_id}",
                )
                for worker_id in range(workers)
            ),
            *(
                PhaseDemand(
                    task_id=f"worker-{worker_id}:submit",
                    phase="submit",
                    browser_profile=f"{requested_browser_backend}:worker-{worker_id}",
                    submit_writer=True,
                )
                for worker_id in range(workers)
            ),
        ],
        requested_workers=workers,
        browser_capacity=workers,
        mailbox_capacity=1,
        submit_writer_capacity=1,
    )
    workers = min(workers, execution_plan.effective_workers)

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
    console.print(
        "[dim]Phase capacity: "
        f"prepare={execution_plan.phase_concurrency.get('prepare', 0)}, "
        f"submit={execution_plan.phase_concurrency.get('submit', 0)} "
        "(single durable submit writer)[/dim]"
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
                    run_progress=run_progress,
                )
            else:
                with ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="apply-worker") as executor:
                    futures = {
                        executor.submit(
                            worker_loop,
                            worker_id=i,
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
                            run_progress=run_progress,
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
        progress_snapshot = run_progress.snapshot()
        console.print(
            f"\n[bold]Done: {total_applied} applied, {total_failed} failed "
            f"(${totals['cost']:.3f})[/bold]"
        )
        if progress_snapshot["partial"]:
            console.print(
                "[yellow]Run ended partial: manifest or authorization capacity "
                "was exhausted before the target was reached.[/yellow]"
            )
        performance = progress_snapshot.get("performance", {})
        performance_totals = (
            performance.get("totals", {})
            if isinstance(performance, dict)
            else {}
        )
        performance_samples = int(
            performance.get("job_sample_count", 0)
            if isinstance(performance, dict)
            else 0
        )
        acquisition_performance = (
            performance.get("acquisition", {})
            if isinstance(performance, dict)
            else {}
        )
        acquisition_totals = (
            acquisition_performance.get("totals", {})
            if isinstance(acquisition_performance, dict)
            else {}
        )
        acquisition_attempts = int(
            acquisition_performance.get("attempt_count", 0)
            if isinstance(acquisition_performance, dict)
            else 0
        )
        if (
            acquisition_attempts > 0
            and isinstance(performance_totals, dict)
            and isinstance(acquisition_totals, dict)
        ):
            acquire_average = (
                float(acquisition_totals.get("worker_call_ms", 0.0))
                / acquisition_attempts
            )
            acquisition_outcomes = acquisition_performance.get("outcomes", {})
            acquisition_outcomes = (
                acquisition_outcomes
                if isinstance(acquisition_outcomes, dict)
                else {}
            )
            console.print(
                "Performance: "
                f"{acquisition_attempts} acquire attempts "
                f"({int(acquisition_outcomes.get('acquired', 0))} acquired/"
                f"{int(acquisition_outcomes.get('empty', 0))} empty), "
                f"{performance_samples} job samples; "
                f"acquire avg {acquire_average:.1f} ms; "
                "submit lane wait/hold "
                f"{float(performance_totals.get('submit_lane_wait_ms', 0.0)):.1f}/"
                f"{float(performance_totals.get('submit_lane_hold_ms', 0.0)):.1f} ms; "
                "submit Agent/observer "
                f"{float(performance_totals.get('submit_agent_ms', 0.0)):.1f}/"
                f"{float(performance_totals.get('post_submit_observer_ms', 0.0)):.1f} ms"
            )
        console.print(f"Logs: {config.LOG_DIR}")

    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        kill_all_chrome()
