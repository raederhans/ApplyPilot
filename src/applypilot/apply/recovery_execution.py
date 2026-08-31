"""Policy admission and durable at-most-once execution for recovery commands."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from applypilot.apply.contracts import (
    DecisionEnvelope,
    RecoveryCommand,
    RecoveryCommandName,
    RecoveryEffectScope,
    RecoveryExecutionResult,
    RecoveryTerminalStatus,
)
from applypilot.apply.exception_queue import exception_for_command
from applypilot.storage import agent_control

RecoveryHandler = Callable[[RecoveryCommand], Mapping[str, object]]
RecoveryVerifier = Callable[[RecoveryCommand, Mapping[str, object]], bool]

_COMMAND_BY_ACTION: dict[str, tuple[RecoveryCommandName, RecoveryEffectScope]] = {
    "retry_same_application": ("retry_same_application", "same_application"),
    "retry_new_session": ("retry_new_session", "new_session"),
    "requires_capability": ("park_exception", "exception_queue"),
    "reconcile_receipt": (
        "enqueue_receipt_reconciliation",
        "receipt_reconciliation",
    ),
    "park": ("park_exception", "exception_queue"),
    "human_only": ("enqueue_human_handoff", "human_handoff"),
    "no_retry": ("record_no_retry", "none"),
}
_HUMAN_ONLY_CATEGORIES = frozenset(
    {
        "human_verification_required",
        "security_challenge_required",
        "assessment_required",
        "sensitive_identity_boundary",
        "unsupported_legal_declaration",
        "truth_or_security_boundary",
    }
)
_RECEIPT_ONLY_CATEGORIES = frozenset(
    {
        "submission_confirmation_missing",
        "provider_submission_failure",
        "post_submit_observation_failure",
    }
)
_QUEUE_COMMANDS = frozenset(
    {"enqueue_receipt_reconciliation", "park_exception", "enqueue_human_handoff"}
)


@dataclass(frozen=True, slots=True)
class RecoveryPolicyAdmission:
    admitted: bool
    reason: str
    command: RecoveryCommand | None = None

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("recovery policy admission reason is required")
        if self.admitted != (self.command is not None):
            raise ValueError("admitted recovery policy requires exactly one command")


def _command_id(decision: DecisionEnvelope, command: RecoveryCommandName) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"applypilot:recovery-command-v1:{decision.actor_id}:"
            f"{decision.turn_id}:{command}",
        )
    )


def admit_recovery_decision(
    decision: DecisionEnvelope,
    *,
    submit_started: bool,
) -> RecoveryPolicyAdmission:
    """Convert one v2 proposal to an allowlisted command or reject it closed."""
    if decision.upcast_from_schema_version is not None:
        return RecoveryPolicyAdmission(False, "legacy_decision_is_read_only")
    recovery = decision.recovery_action
    if recovery is None:
        return RecoveryPolicyAdmission(False, "decision_has_no_recovery_action")
    command_projection = _COMMAND_BY_ACTION.get(recovery.action)
    if command_projection is None:
        return RecoveryPolicyAdmission(False, "recovery_action_not_allowlisted")
    if (
        recovery.failure_category in _HUMAN_ONLY_CATEGORIES
        and recovery.action != "human_only"
    ):
        return RecoveryPolicyAdmission(False, "human_only_boundary_mismatch")
    if (
        recovery.failure_category in _RECEIPT_ONLY_CATEGORIES
        and recovery.action != "reconcile_receipt"
    ):
        return RecoveryPolicyAdmission(False, "receipt_reconciliation_boundary_mismatch")
    if submit_started and recovery.action != "reconcile_receipt":
        return RecoveryPolicyAdmission(False, "post_submit_recovery_must_reconcile_receipt")
    if recovery.action == "human_only" and decision.human_interruption is None:
        return RecoveryPolicyAdmission(False, "human_only_command_requires_interruption")

    command_name, effect_scope = command_projection
    payload: dict[str, object] = {}
    if recovery.missing_capability is not None:
        payload["missing_capability"] = recovery.missing_capability
    if recovery.missing_material is not None:
        payload["missing_material"] = recovery.missing_material
    if decision.human_interruption is not None:
        payload["interruption_type"] = decision.human_interruption.interruption_type
    command = RecoveryCommand(
        command_id=_command_id(decision, command_name),
        run_id=decision.run_id,
        attempt_id=decision.attempt_id,
        actor_id=decision.actor_id,
        turn_id=decision.turn_id,
        command=command_name,
        effect_scope=effect_scope,
        recovery_action=recovery.action,
        failure_category=recovery.failure_category,
        next_action=recovery.next_action,
        retry_budget_remaining=recovery.retry_budget_remaining,
        payload=payload,
    )
    return RecoveryPolicyAdmission(True, "recovery_policy_v1_admitted", command)


def _result(
    command: RecoveryCommand,
    stage: str,
    outcome: str,
    details: Mapping[str, object] | None = None,
    *,
    terminal_status: RecoveryTerminalStatus | None = None,
) -> RecoveryExecutionResult:
    return RecoveryExecutionResult(
        result_id=f"{command.command_id}:{stage}",
        command_id=command.command_id,
        run_id=command.run_id,
        attempt_id=command.attempt_id,
        actor_id=command.actor_id,
        turn_id=command.turn_id,
        stage=stage,  # type: ignore[arg-type]
        outcome=outcome,
        terminal_status=terminal_status,
        details=dict(details or {}),
    )


def _append_durable(
    connection: sqlite3.Connection,
    result: RecoveryExecutionResult,
) -> bool:
    try:
        inserted = agent_control.append_recovery_result(connection, result)
        connection.commit()
        return inserted
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        raise


def _enqueue_durable(connection: sqlite3.Connection, command: RecoveryCommand) -> bool:
    try:
        inserted = agent_control.enqueue_exception(
            connection,
            exception_for_command(command),
        )
        connection.commit()
        return inserted
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        raise


def _terminal_result(
    history: list[RecoveryExecutionResult],
) -> RecoveryExecutionResult | None:
    return next(
        (result for result in reversed(history) if result.stage in {"verified", "failed"}),
        None,
    )


def _terminal_status_for_exception(exc: Exception) -> RecoveryTerminalStatus:
    if isinstance(exc, TimeoutError):
        return "timed_out"
    if isinstance(exc, InterruptedError):
        return "canceled"
    if isinstance(exc, (BrokenPipeError, ChildProcessError, ProcessLookupError)):
        return "terminated"
    return "failed"


def _verify_builtin_effect(
    connection: sqlite3.Connection,
    command: RecoveryCommand,
) -> bool:
    if command.command == "record_no_retry":
        return True
    if command.command in _QUEUE_COMMANDS:
        return bool(
            agent_control.list_exceptions(
                connection,
                status=None,
                command_id=command.command_id,
            )
        )
    return False


def _fail_and_park(
    connection: sqlite3.Connection,
    command: RecoveryCommand,
    outcome: str,
    details: Mapping[str, object],
    *,
    terminal_status: RecoveryTerminalStatus = "failed",
) -> RecoveryExecutionResult:
    failure_details = {**details, "terminal_status": terminal_status}
    failed = _result(
        command,
        "failed",
        outcome,
        failure_details,
        terminal_status=terminal_status,
    )
    _append_durable(connection, failed)
    try:
        item = exception_for_command(
            command,
            queue_kind="recovery_execution",
            failure_reason=outcome,
            terminal_status=terminal_status,
        )
        agent_control.enqueue_exception(connection, item)
        connection.commit()
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
    return failed


def execute_recovery_command(
    connection: sqlite3.Connection,
    command: RecoveryCommand,
    *,
    handler: RecoveryHandler | None = None,
    verifier: RecoveryVerifier | None = None,
    incomplete_started_timeout_seconds: float = 300.0,
) -> RecoveryExecutionResult:
    """Execute once, persist every lifecycle boundary, and never replay an effect.

    A started-only command is treated as still owned until its timeout expires.
    Once stale, it is failed and parked instead of being executed again because
    the previous process may have applied the effect before crashing.
    """
    if incomplete_started_timeout_seconds < 0:
        raise ValueError("incomplete_started_timeout_seconds must be non-negative")
    if connection.in_transaction:
        raise ValueError("recovery execution requires an independent durable transaction")
    history = agent_control.list_recovery_results(
        connection,
        command_id=command.command_id,
    )
    terminal = _terminal_result(history)
    if terminal is not None:
        return terminal

    executed = next((item for item in history if item.stage == "executed"), None)
    if executed is not None:
        try:
            verified = (
                verifier(command, executed.details)
                if verifier is not None
                else _verify_builtin_effect(connection, command)
            )
        except Exception as exc:  # noqa: BLE001 - verifier is an injected boundary
            terminal_status = _terminal_status_for_exception(exc)
            return _fail_and_park(
                connection,
                command,
                f"recovery_verification_{terminal_status}",
                {"error_type": type(exc).__name__, "replayed_from_executed": True},
                terminal_status=terminal_status,
            )
        if verified:
            result = _result(
                command,
                "verified",
                "recovery_effect_verified",
                {"replayed_from_executed": True},
                terminal_status="completed",
            )
            _append_durable(connection, result)
            return result
        return _fail_and_park(
            connection,
            command,
            "recovery_effect_verification_failed",
            {"replayed_from_executed": True},
        )

    started = next((item for item in history if item.stage == "started"), None)
    if started is not None:
        age = max(
            0.0,
            (datetime.now(UTC) - started.occurred_at).total_seconds(),
        )
        if age < incomplete_started_timeout_seconds:
            return started
        return _fail_and_park(
            connection,
            command,
            "indeterminate_effect_after_started_replay",
            {"effect_replayed": False},
            terminal_status="terminated",
        )

    started = _result(
        command,
        "started",
        "recovery_command_started",
        {
            "command": command.command,
            "effect_scope": command.effect_scope,
            "policy_reason": command.policy_reason,
        },
    )
    inserted = _append_durable(connection, started)
    if not inserted:
        return started

    try:
        if command.command in _QUEUE_COMMANDS:
            effect_details: Mapping[str, object] = {
                "exception_inserted": _enqueue_durable(connection, command),
            }
        elif command.command == "record_no_retry":
            effect_details = {"effect": "none"}
        elif handler is not None:
            effect_details = dict(handler(command))
        else:
            raise RuntimeError("recovery_handler_unavailable")
        executed = _result(
            command,
            "executed",
            "recovery_effect_executed",
            effect_details,
        )
        _append_durable(connection, executed)
        verified = (
            verifier(command, effect_details)
            if verifier is not None
            else _verify_builtin_effect(connection, command)
        )
        if not verified:
            return _fail_and_park(
                connection,
                command,
                "recovery_effect_verification_failed",
                {"effect_replayed": False},
            )
        result = _result(
            command,
            "verified",
            "recovery_effect_verified",
            {"effect_replayed": False},
            terminal_status="completed",
        )
        _append_durable(connection, result)
        return result
    except Exception as exc:  # noqa: BLE001 - handler is an injected effect boundary
        terminal_status = _terminal_status_for_exception(exc)
        return _fail_and_park(
            connection,
            command,
            f"recovery_effect_{terminal_status}",
            {"error_type": type(exc).__name__, "effect_replayed": False},
            terminal_status=terminal_status,
        )
