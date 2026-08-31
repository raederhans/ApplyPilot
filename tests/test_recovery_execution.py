"""Executable recovery and exception-queue contracts."""

from __future__ import annotations

import sqlite3

import pytest

from applypilot.apply.application_actor import ApplicationActorState, decide_recovery
from applypilot.apply.recovery_execution import (
    admit_recovery_decision,
    execute_recovery_command,
)
from applypilot.storage import agent_control


def actor(**changes: object) -> ApplicationActorState:
    values: dict[str, object] = {
        "run_id": "turn-1",
        "attempt_id": "attempt-1",
        "application_id": "application-1",
        "page_id": "page-1",
        "write_owner": "playwright",
        "phase": "verify",
        "same_application_retries_remaining": 1,
        "new_session_retries_remaining": 1,
    }
    values.update(changes)
    return ApplicationActorState(**values)  # type: ignore[arg-type]


def command_for(
    result: str,
    *,
    submit_started: bool = False,
    run_id: str = "turn-1",
):
    decision = decide_recovery(actor(run_id=run_id), result)
    admission = admit_recovery_decision(decision, submit_started=submit_started)
    assert admission.command is not None
    return admission.command


def test_allowlisted_command_executes_once_and_replay_has_no_second_effect() -> None:
    connection = sqlite3.connect(":memory:")
    command = command_for("failed:site_blocked")
    effect_calls: list[str] = []

    def handler(_command):
        effect_calls.append("called")
        return {
            "browser_runtime": "cloak",
            "fallback_applied": True,
            "recovery_turn_completed": True,
        }

    def verifier(_command, details):
        return details.get("fallback_applied") is True

    first = execute_recovery_command(
        connection,
        command,
        handler=handler,
        verifier=verifier,
    )
    replay = execute_recovery_command(
        connection,
        command,
        handler=handler,
        verifier=verifier,
    )

    assert first.stage == replay.stage == "verified"
    assert first.terminal_status == replay.terminal_status == "completed"
    assert effect_calls == ["called"]
    assert [
        result.stage
        for result in agent_control.list_recovery_results(
            connection,
            command_id=command.command_id,
        )
    ] == ["started", "executed", "verified"]


def test_crash_after_started_fails_closed_without_replaying_effect() -> None:
    connection = sqlite3.connect(":memory:")
    command = command_for("failed:site_blocked")

    def crash(_command):
        raise SystemExit("synthetic process crash")

    with pytest.raises(SystemExit, match="synthetic process crash"):
        execute_recovery_command(
            connection,
            command,
            handler=crash,
            verifier=lambda _command, _details: True,
        )

    replay_calls: list[str] = []
    replay = execute_recovery_command(
        connection,
        command,
        handler=lambda _command: replay_calls.append("called") or {},
        verifier=lambda _command, _details: True,
        incomplete_started_timeout_seconds=0,
    )

    assert replay.stage == "failed"
    assert replay.terminal_status == "terminated"
    assert replay.outcome == "indeterminate_effect_after_started_replay"
    assert replay_calls == []
    queued = agent_control.list_exceptions(
        connection,
        command_id=command.command_id,
    )
    assert len(queued) == 1
    assert queued[0].queue_kind == "recovery_execution"
    assert queued[0].context["terminal_status"] == "terminated"


@pytest.mark.parametrize(
    ("error", "terminal_status"),
    [
        (RuntimeError("failed"), "failed"),
        (BrokenPipeError("terminated"), "terminated"),
        (TimeoutError("timed out"), "timed_out"),
        (InterruptedError("canceled"), "canceled"),
    ],
)
def test_execution_failures_have_queryable_terminal_classification(
    error: Exception,
    terminal_status: str,
) -> None:
    connection = sqlite3.connect(":memory:")
    command = command_for(
        "failed:site_blocked",
        run_id=f"turn-{terminal_status}",
    )

    def fail(_command):
        raise error

    result = execute_recovery_command(
        connection,
        command,
        handler=fail,
        verifier=lambda _command, _details: True,
    )
    queued = agent_control.list_exceptions(
        connection,
        command_id=command.command_id,
    )

    assert result.stage == "failed"
    assert result.terminal_status == terminal_status
    assert result.outcome == f"recovery_effect_{terminal_status}"
    assert queued[0].context["terminal_status"] == terminal_status


def test_submission_uncertain_can_only_enqueue_receipt_reconciliation() -> None:
    connection = sqlite3.connect(":memory:")
    command = command_for("submission_uncertain", submit_started=True)

    assert command.command == "enqueue_receipt_reconciliation"
    result = execute_recovery_command(connection, command)
    queued = agent_control.list_exceptions(
        connection,
        command_id=command.command_id,
    )

    assert result.stage == "verified"
    assert len(queued) == 1
    assert queued[0].queue_kind == "receipt_reconciliation"
    assert queued[0].next_action == "reconcile_receipt_without_resubmitting"


@pytest.mark.parametrize(
    "result",
    [
        "captcha",
        "failed:mfa",
        "failed:assessment_required",
        "failed:identity_material_missing:passport",
        "failed:financial_document_required",
    ],
)
def test_sensitive_and_unsupported_fact_boundaries_are_human_only(result: str) -> None:
    decision = decide_recovery(actor(), result)
    admission = admit_recovery_decision(decision, submit_started=False)

    assert decision.recovery_action is not None
    assert decision.recovery_action.action == "human_only"
    assert admission.command is not None
    assert admission.command.command == "enqueue_human_handoff"
    assert admission.command.submit_authority is False
    assert admission.command.page_write_authority is False
    assert admission.command.ledger_write_authority is False


@pytest.mark.parametrize(
    "result",
    [
        "failed:unsupported_skill:required_answer",
        "failed:unknown_required_fact",
        "failed:resume_upload",
        "failed:required_field_empty:country",
    ],
)
def test_ordinary_resolution_failures_get_one_same_application_retry(result: str) -> None:
    decision = decide_recovery(actor(), result)
    admission = admit_recovery_decision(decision, submit_started=False)

    assert decision.human_interruption is None
    assert admission.command is not None
    assert admission.command.command == "retry_same_application"
    assert admission.command.retry_budget_remaining == 0


def test_policy_rejects_any_automatic_retry_after_submit_started() -> None:
    decision = decide_recovery(actor(), "failed:site_blocked")

    admission = admit_recovery_decision(decision, submit_started=True)

    assert admission.admitted is False
    assert admission.command is None
    assert admission.reason == "post_submit_recovery_must_reconcile_receipt"
