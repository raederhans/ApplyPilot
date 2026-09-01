"""Pure construction helpers for durable per-application exception parking."""

from __future__ import annotations

import uuid

from applypilot.apply.contracts import (
    ApplicationException,
    ExceptionQueueKind,
    RecoveryCommand,
)
from applypilot.apply.operator_binding import OPERATOR_RESUME_BINDING_KEYS


def exception_id_for_command(command_id: str) -> str:
    """Return the stable queue identity for one recovery command."""
    if not str(command_id or "").strip():
        raise ValueError("command_id is required")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"applypilot:exception:{command_id}"))


def queue_kind_for_command(command: RecoveryCommand) -> ExceptionQueueKind:
    """Project an allowlisted command onto one operator queue lane."""
    if command.command == "enqueue_receipt_reconciliation":
        return "receipt_reconciliation"
    if command.command == "enqueue_human_handoff":
        return "human_only"
    if command.command == "park_exception" and command.recovery_action == "requires_capability":
        return "capability"
    if command.command == "park_exception":
        return "parked"
    return "recovery_execution"


def exception_for_command(
    command: RecoveryCommand,
    *,
    queue_kind: ExceptionQueueKind | None = None,
    failure_reason: str | None = None,
    terminal_status: str | None = None,
) -> ApplicationException:
    """Build a secret-free, idempotent queue item for a blocked attempt."""
    context: dict[str, object] = {
        "recovery_action": command.recovery_action,
        "recovery_command": command.command,
        "effect_scope": command.effect_scope,
        "policy_reason": command.policy_reason,
    }
    if failure_reason is not None:
        context["failure_reason"] = str(failure_reason)[:200]
    if terminal_status is not None:
        context["terminal_status"] = str(terminal_status)[:40]
    if command.command == "enqueue_human_handoff":
        resume_binding = {
            key: command.payload[key]
            for key in OPERATOR_RESUME_BINDING_KEYS
            if key in command.payload
        }
        if set(resume_binding) == OPERATOR_RESUME_BINDING_KEYS:
            context.update(resume_binding)
    return ApplicationException(
        exception_id=exception_id_for_command(command.command_id),
        command_id=command.command_id,
        run_id=command.run_id,
        attempt_id=command.attempt_id,
        actor_id=command.actor_id,
        turn_id=command.turn_id,
        queue_kind=queue_kind or queue_kind_for_command(command),
        failure_category=command.failure_category,
        next_action=command.next_action,
        context=context,
    )
