"""Read-only application preflight orchestration.

The launcher remains the composition root.  It passes its live module namespace
so tests and runtime configuration that patch launcher dependencies continue to
resolve at call time.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any


def run_read_only_preflight(
    host: Any,
    job: Mapping[str, object],
) -> dict[str, object]:
    """Run deterministic preflight reads through composition-root dependencies."""

    BackgroundWorkerPool = host.BackgroundWorkerPool
    READ_ONLY_SPECIALIST_AUTHORITY = host.READ_ONLY_SPECIALIST_AUTHORITY
    ResourceClaim = host.ResourceClaim
    SpecialistCancelled = host.SpecialistCancelled
    SpecialistDeadlineExceeded = host.SpecialistDeadlineExceeded
    TaskResult = host.TaskResult
    TaskSpec = host.TaskSpec
    _resolve_ats_application_binding = host._resolve_ats_application_binding
    application_jobs_mod = host.application_jobs_mod
    ats_mod = host.ats_mod
    config = host.config
    get_connection = host.get_connection
    normalize_specialist_mode = host.normalize_specialist_mode
    orchestration_mod = host.orchestration_mod
    production_specialist_runners = host.production_specialist_runners
    production_specialist_spec = host.production_specialist_spec
    run_durable_material_specialist = host.run_durable_material_specialist
    run_system_specialist = host.run_system_specialist
    successfactors_binding_mod = host.successfactors_binding_mod
    task_journal = host.task_journal

    provider = ats_mod.detect_ats_site(
        str(job.get("application_url") or job.get("url") or "")
    )
    successfactors_probe = bool(
        provider == "generic"
        and successfactors_binding_mod.successfactors_probe_candidate(job)
    )
    ats_identity_provider = (
        "successfactors" if successfactors_probe else provider
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
    mode = normalize_specialist_mode(
        str(job.get("_material_specialist_mode") or configured_mode)
    )
    configured_specialist_modes = (
        orchestration.get("production_specialist_modes", {})
        if isinstance(orchestration, Mapping)
        else {}
    )
    configured_specialist_modes = (
        configured_specialist_modes
        if isinstance(configured_specialist_modes, Mapping)
        else {}
    )
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
            authority_scope=READ_ONLY_SPECIALIST_AUTHORITY,
            resource_claims=(ResourceClaim("local-read"),),
            retry_budget=0,
            retry_categories=("specialist_timeout", "specialist_transient"),
            deadline_at=datetime.now(UTC) + timedelta(seconds=5),
            partial_allowed=False,
        )
    ]
    work_authorization = profile.get("work_authorization", {})
    work_authorization = (
        work_authorization if isinstance(work_authorization, Mapping) else {}
    )
    context_snapshots: dict[str, dict[str, object]] = {
        "provider-classifier-v1": {
            "url": str(job.get("application_url") or job.get("url") or "")
        },
        "application-facts-v1": {
            key: job[key]
            for key in ("title", "company", "location", "employment_type")
            if isinstance(job.get(key), (str, bool, int, float))
        },
        "work-authorization-v1": {
            key: work_authorization[key]
            for key in (
                "legally_authorized_to_work",
                "requires_sponsorship",
                "require_sponsorship",
                "visa_status",
            )
            if isinstance(work_authorization.get(key), (str, bool))
        },
    }
    context_modes = {
        specialist_id: normalize_specialist_mode(
            str(configured_specialist_modes.get(specialist_id, "shadow"))
        )
        for specialist_id in (
            "provider-classifier-v1",
            "application-facts-v1",
            "work-authorization-v1",
            "field-semantic-v1",
            "page-failure-v1",
        )
    }
    for specialist_id, snapshot in context_snapshots.items():
        specialist_mode = context_modes[specialist_id]
        if specialist_mode == "off":
            continue
        spec = production_specialist_spec(specialist_id)
        if "preflight" not in spec.phases:
            continue
        tasks.append(
            TaskSpec(
                task_id=f"context:{specialist_id}",
                kind=specialist_id,
                objective="Produce bounded read-only preflight context.",
                inputs={"snapshot": snapshot, "mode": specialist_mode},
                effect_class="read",
                authority_scope=spec.authority_scope,
                resource_claims=(ResourceClaim("local-read"),),
                retry_budget=0,
                retry_categories=spec.retry_categories,
                deadline_at=datetime.now(UTC)
                + timedelta(seconds=spec.execution_budget_seconds),
                cancellation_mode="cooperative",
                partial_allowed=False,
            )
        )
    background_pool = BackgroundWorkerPool(
        lambda: get_connection(),
        production_specialist_runners(),
        worker_id="preflight-context-read-1",
        max_workers=1,
        coalesce_wait_seconds=2.0,
    )
    if provider == "smartrecruiters":
        tasks.append(
            TaskSpec(
                task_id="duplicate-snapshot",
                kind="duplicate-check",
                objective="Read the durable application ledger for an exact duplicate.",
                inputs={"job_url": str(job.get("url") or "")},
                effect_class="read",
                resource_claims=(ResourceClaim("database-read"),),
            )
        )
    if provider == "smartrecruiters" or successfactors_probe:
        tasks.append(
            TaskSpec(
                task_id="ats-identity",
                kind="ats-identity",
                objective="Resolve the immutable public posting identity.",
                inputs={"provider": ats_identity_provider},
                effect_class="read",
                resource_claims=(ResourceClaim("network-read"),),
            )
        )

    def runner(task: TaskSpec, context: orchestration_mod.TaskExecutionContext) -> TaskResult:
        started = time.perf_counter()
        context.heartbeat({"stage": "started"})
        if task.task_id == "material-readiness":
            if context.cancelled() or context.remaining_seconds() == 0:
                return TaskResult(
                    task_id=task.task_id,
                    status="cancelled" if context.cancelled() else "timed_out",
                    failure_category=(
                        "specialist_cancelled" if context.cancelled() else "specialist_timeout"
                    ),
                )
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
                        cancelled=context.cancelled,
                        timeout_seconds=context.remaining_seconds(),
                    )
                else:
                    # Compatibility for injected read-only test ports. Real
                    # database connections always use the durable path above.
                    material_run = run_system_specialist(
                        "material-readiness-v1",
                        material_job,
                        mode=mode,
                        timeout_seconds=context.remaining_seconds(),
                        cancelled=context.cancelled,
                    )
            except SpecialistDeadlineExceeded:
                return TaskResult(
                    task_id=task.task_id,
                    status="timed_out",
                    failure_category="specialist_timeout",
                    retryable=True,
                )
            except SpecialistCancelled:
                return TaskResult(
                    task_id=task.task_id,
                    status="cancelled",
                    failure_category="specialist_cancelled",
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
            if context.cancelled() or context.remaining_seconds() == 0:
                return TaskResult(
                    task_id=task.task_id,
                    status="timed_out",
                    failure_category="specialist_timeout",
                    retryable=True,
                )
            context.checkpoint({"stage": "material-evaluated"})
            return TaskResult(task_id=task.task_id, status="completed", output=output)
        if task.task_id.startswith("context:"):
            specialist_id = task.kind
            if context.cancelled() or context.remaining_seconds() == 0:
                return TaskResult(
                    task_id=task.task_id,
                    status="timed_out",
                    failure_category="specialist_timeout",
                    retryable=True,
                )
            snapshot = context_snapshots[specialist_id]
            specialist_mode = context_modes[specialist_id]
            durable_identity = hashlib.sha256(
                json.dumps(
                    {
                        "kind": specialist_id,
                        "mode": specialist_mode,
                        "snapshot": snapshot,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            durable_task_id = f"preflight-context:{specialist_id}:{durable_identity[:24]}"
            durable_spec = TaskSpec(
                task_id=durable_task_id,
                kind=specialist_id,
                objective="Produce bounded durable read-only preflight context.",
                inputs={"snapshot": snapshot, "mode": specialist_mode},
                effect_class="read",
                authority_scope=production_specialist_spec(specialist_id).authority_scope,
                retry_budget=0,
                retry_categories=production_specialist_spec(specialist_id).retry_categories,
                cancellation_mode="cooperative",
                partial_allowed=False,
                idempotency_key=durable_task_id,
            )
            connection = get_connection()
            try:
                task_journal.register(connection, durable_spec)
            finally:
                close = getattr(connection, "close", None)
                if callable(close):
                    close()
            worker_outcome = background_pool.run_task(durable_task_id)
            connection = get_connection()
            try:
                durable_entry = task_journal.load(connection, durable_task_id)
            finally:
                close = getattr(connection, "close", None)
                if callable(close):
                    close()
            if (
                worker_outcome is None
                or durable_entry is None
                or durable_entry.status != "completed"
                or not isinstance(durable_entry.result, Mapping)
            ):
                return TaskResult(
                    task_id=task.task_id,
                    status=(
                        "failed" if durable_entry is None else durable_entry.status
                    ),
                    failure_category="background_specialist_unavailable",
                )
            durable_output = durable_entry.result.get("output")
            if not isinstance(durable_output, Mapping):
                return TaskResult(
                    task_id=task.task_id,
                    status="failed",
                    failure_category="background_specialist_result_invalid",
                )
            return TaskResult(
                task_id=task.task_id,
                status="completed",
                output=dict(durable_output),
                authority_scope=production_specialist_spec(
                    specialist_id
                ).authority_scope,
            )
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
        status_group = (
            "specialist_task_statuses"
            if task.task_id.startswith("context:")
            else "task_statuses"
        )
        statuses = state.setdefault(status_group, {})
        if isinstance(statuses, dict):
            statuses[task.task_id] = {
                "status": task_result.status,
                "failure_category": task_result.failure_category,
                "metrics": dict(task_result.metrics),
            }

    try:
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
    finally:
        background_pool.shutdown()
    result: dict[str, object] = {
        "provider": provider,
        "ats_identity_provider": ats_identity_provider,
        "task_statuses": outcome.reduced_state.get("task_statuses", {}),
        "specialist_task_statuses": outcome.reduced_state.get(
            "specialist_task_statuses", {}
        ),
    }
    specialist_statuses = result["specialist_task_statuses"]
    if isinstance(specialist_statuses, dict):
        for specialist_id in ("field-semantic-v1", "page-failure-v1"):
            specialist_statuses[f"context:{specialist_id}"] = {
                "status": "skipped",
                "failure_category": "not_applicable_in_preflight",
                "metrics": {},
            }
    material_result = outcome.results["material-readiness"]
    material_readiness = material_result.output.get("material_readiness")
    normalized_mode = normalize_specialist_mode(mode)
    fail_closed_mode = normalized_mode == "required"
    if not material_result.succeeded:
        material_readiness = {
            "state": "blocked",
            "ready": False,
            "missing_kinds": ["material_specialist_unavailable"],
            "failure_category": material_result.failure_category,
            "error_type": material_result.output.get("error_type"),
        }
    result["material_readiness"] = (
        material_readiness if normalized_mode in {"advisory", "required"} else None
    )
    result["specialist_advisories"] = (
        [{"kind": "material-readiness-v1", "result": material_readiness}]
        if normalized_mode in {"advisory", "required"} and material_result.succeeded
        else []
    )
    required_context_failures: list[str] = []
    for specialist_id, specialist_mode in context_modes.items():
        context_result = outcome.results.get(f"context:{specialist_id}")
        if context_result is None:
            continue
        context_output = context_result.output.get("result")
        if specialist_mode in {"advisory", "required"} and isinstance(
            context_output, Mapping
        ):
            result["specialist_advisories"].append(
                {"kind": specialist_id, "result": dict(context_output)}
            )
        if specialist_mode == "required" and (
            not context_result.succeeded
            or bool(context_result.output.get("enforced"))
        ):
            required_context_failures.append(specialist_id)
    result["material_specialist_mode"] = mode
    result["material_enforced_block"] = bool(
        material_result.output.get("enforced")
        or (
            fail_closed_mode
            and (
                not material_result.succeeded
                or material_result.partial
                or bool(material_result.conflict_keys)
            )
        )
    )
    result["specialist_required_block"] = bool(required_context_failures)
    result["required_specialist_failures"] = required_context_failures
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
