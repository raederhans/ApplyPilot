from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from applypilot.apply.contracts import ApplicationException, application_actor_id
from applypilot.apply.operator_commands import (
    OperatorCircuitOpen,
    OperatorCommand,
    OperatorCommandError,
    OperatorCommandService,
    OperatorExecution,
    OperatorIdentityDrift,
    OperatorUnknownReplay,
    semantic_exception_groups,
)
from applypilot.database import execute_operator_command, list_operator_exception_groups
from applypilot.storage import agent_control

NOW = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
DIGEST = "a" * 64


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    agent_control.ensure_schema(connection)
    return connection


def _exception(
    connection: sqlite3.Connection,
    *,
    exception_id: str = "exception-1",
    run_id: str = "run-1",
    context: dict[str, object] | None = None,
    queue_kind: str = "recovery_execution",
) -> ApplicationException:
    item = ApplicationException(
        exception_id=exception_id,
        command_id=f"park:{exception_id}",
        run_id=run_id,
        attempt_id="attempt-1",
        actor_id=application_actor_id("attempt-1"),
        turn_id=run_id,
        queue_kind=queue_kind,  # type: ignore[arg-type]
        failure_category="bounded_failure",
        next_action="operator_review",
        context=context
        or {
            "application_ref": f"application:{exception_id}",
            "effect_scope": "same_application",
        },
        created_at=NOW,
    )
    assert agent_control.enqueue_exception(connection, item)
    connection.commit()
    return item


def _command(
    item: ApplicationException,
    *,
    command_id: str = "operator-command-1",
    action: str = "resolve",
    budget: int = 2,
) -> OperatorCommand:
    return OperatorCommand(
        command_id=command_id,
        exception_id=item.exception_id,
        action=action,  # type: ignore[arg-type]
        run_id=item.run_id,
        attempt_id=item.attempt_id,
        actor_id=item.actor_id,
        turn_id=item.turn_id,
        input_ref=f"evidence:{command_id}" if action != "resolve" else None,
        input_sha256=DIGEST if action != "resolve" else None,
        recovery_budget=budget,
        created_at=NOW,
    )


def test_schema_upgrade_is_append_only_and_exact_resolve_replay_is_idempotent() -> None:
    connection = sqlite3.connect(":memory:")
    agent_control.ensure_schema(connection)
    connection.execute("DROP TABLE operator_command_results")
    connection.execute("DROP TABLE operator_command_envelopes")
    agent_control.ensure_schema(connection)
    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"operator_command_envelopes", "operator_command_results"} <= tables

    connection = _connection()
    item = _exception(connection)
    command = _command(item)
    executor_calls: list[str] = []
    service = OperatorCommandService(
        connection,
        executor=lambda supplied: executor_calls.append(supplied.action),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    first = service.execute(command)
    reconstructed = replace(command, created_at=datetime(2026, 9, 1, 9, 5, tzinfo=UTC))
    replay = service.execute(reconstructed)

    assert first.resolved is True and replay.replayed is True
    assert first.status == "dismissed"
    assert first.continuation_authorized is False
    assert executor_calls == []
    assert agent_control.list_exceptions(connection, status="open") == []
    assert len(agent_control.list_operator_result_rows(connection, command_id=command.command_id)) == 1
    with pytest.raises(ValueError, match="collision"):
        service.execute(replace(reconstructed, recovery_budget=1))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(
            "UPDATE operator_command_envelopes SET action='resume' WHERE command_id=?",
            (command.command_id,),
        )


def test_schema_upgrade_refuses_preexisting_duplicate_resume_refs() -> None:
    connection = _connection()
    item = _exception(connection)
    connection.execute("DROP INDEX idx_operator_resume_input_ref")
    first = _command(item, command_id="duplicate-one", action="resume")
    second = replace(
        _command(item, command_id="duplicate-two", action="resume"),
        input_ref=first.input_ref,
        input_sha256=first.input_sha256,
    )
    columns = (
        "command_id, exception_id, action, run_id, attempt_id, actor_id, turn_id, "
        "expected_status, input_ref, input_sha256, recovery_budget, browser_authority, "
        "page_write_authority, submit_authority, ledger_write_authority, schema_version, "
        "created_at"
    )
    connection.execute(
        "INSERT INTO operator_command_envelopes (" + columns + ") "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        first.storage_values(),
    )
    connection.execute(
        "INSERT INTO operator_command_envelopes (" + columns + ") "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        second.storage_values(),
    )

    with pytest.raises(ValueError, match="duplicate durable resume"):
        agent_control.ensure_schema(connection)


def test_command_rejects_identity_drift_authority_and_raw_payload_fields() -> None:
    connection = _connection()
    item = _exception(connection)
    service = OperatorCommandService(connection, clock=lambda: NOW)

    with pytest.raises(ValueError, match="authority"):
        replace(_command(item), submit_authority=True)
    with pytest.raises(ValueError, match="cannot carry"):
        replace(_command(item), input_ref="human-response:one", input_sha256=DIGEST)
    with pytest.raises(ValueError, match="require"):
        replace(_command(item, action="resume"), input_ref=None, input_sha256=None)
    with pytest.raises(TypeError):
        OperatorCommand(  # type: ignore[call-arg]
            command_id="raw-command",
            exception_id=item.exception_id,
            action="resolve",
            run_id=item.run_id,
            attempt_id=item.attempt_id,
            actor_id=item.actor_id,
            turn_id=item.turn_id,
            created_at=NOW,
            human_answer="raw secret",
        )
    drifted = replace(_command(item), run_id="other-run", turn_id="other-run")
    with pytest.raises(OperatorIdentityDrift):
        service.execute(drifted)
    assert agent_control.list_exceptions(connection, status="open") == [item]


def test_action_must_match_queue_lane_before_any_executor_call() -> None:
    connection = _connection()
    item = _exception(connection, queue_kind="recovery_execution")
    calls: list[str] = []
    service = OperatorCommandService(
        connection,
        executor=lambda command: calls.append(command.action),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    with pytest.raises(OperatorIdentityDrift, match="lane"):
        service.execute(_command(item, action="reconcile"))
    post_submit = _exception(
        connection,
        exception_id="post-submit-recovery",
        run_id="run-post-submit",
        queue_kind="recovery_execution",
        context={"effect_scope": "receipt_reconciliation"},
    )
    with pytest.raises(OperatorIdentityDrift, match="pre-submit"):
        service.execute(_command(post_submit, command_id="post-submit-resume", action="resume"))
    assert calls == []


@pytest.mark.parametrize("action", ["resume", "reconcile"])
def test_verified_executor_resolves_once_and_terminal_replay_never_reexecutes(action: str) -> None:
    connection = _connection()
    item = _exception(
        connection,
        queue_kind="receipt_reconciliation" if action == "reconcile" else "recovery_execution",
    )
    calls: list[str] = []

    def executor(command: OperatorCommand) -> OperatorExecution:
        calls.append(command.action)
        return OperatorExecution(
            verified=True,
            outcome="verified_result",
            terminal_status="completed",
            result_ref="receipt:verified" if action == "reconcile" else "checkpoint:verified",
            result_sha256=DIGEST,
        )

    service = OperatorCommandService(
        connection, executor=executor, verifier=lambda _command, _result: True, clock=lambda: NOW
    )
    command = _command(item, action=action)

    assert service.execute(command).resolved is True
    assert service.execute(command).replayed is True
    assert calls == [action]
    assert [row[2] for row in agent_control.list_operator_result_rows(connection)] == [
        "started",
        "verified",
    ]


def test_executor_unknown_is_durable_and_replay_fails_closed_without_second_call() -> None:
    connection = _connection()
    item = _exception(connection)
    calls = 0

    def unknown(_command: OperatorCommand) -> OperatorExecution:
        nonlocal calls
        calls += 1
        raise TimeoutError("executor outcome unknown")

    command = _command(item, action="resume")
    service = OperatorCommandService(connection, executor=unknown, clock=lambda: NOW)
    with pytest.raises(OperatorCommandError, match="unknown"):
        service.execute(command)
    with pytest.raises(OperatorUnknownReplay):
        service.execute(command)
    restarted_calls = 0

    def must_not_run(_command: OperatorCommand) -> OperatorExecution:
        nonlocal restarted_calls
        restarted_calls += 1
        return OperatorExecution(False, "unexpected_call", "failed")

    restarted = OperatorCommandService(connection, executor=must_not_run, clock=lambda: NOW)
    with pytest.raises(OperatorCircuitOpen, match="unknown"):
        restarted.execute(_command(item, command_id="new-command", action="resume"))

    assert calls == 1
    assert restarted_calls == 0
    assert agent_control.list_exceptions(connection, status="open") == [item]
    assert [row[2] for row in agent_control.list_operator_result_rows(connection)] == ["started"]


def test_verified_executor_cas_drift_remains_open_and_replay_never_reexecutes() -> None:
    connection = _connection()
    item = _exception(connection)
    calls = 0

    def drift_then_verify(_command: OperatorCommand) -> OperatorExecution:
        nonlocal calls
        calls += 1
        connection.execute(
            "UPDATE agent_exception_queue SET turn_id='drifted-turn' WHERE exception_id=?",
            (item.exception_id,),
        )
        return OperatorExecution(
            True,
            "verified_result",
            "completed",
            result_ref="checkpoint:verified",
            result_sha256=DIGEST,
        )

    command = _command(item, action="resume")
    service = OperatorCommandService(
        connection,
        executor=drift_then_verify,
        verifier=lambda _command, _result: True,
        clock=lambda: NOW,
    )
    with pytest.raises(OperatorIdentityDrift, match="CAS"):
        service.execute(command)
    with pytest.raises(OperatorUnknownReplay):
        service.execute(command)

    assert calls == 1
    assert connection.execute(
        "SELECT status FROM agent_exception_queue WHERE exception_id=?", (item.exception_id,)
    ).fetchone()[0] == "open"
    assert [row[2] for row in agent_control.list_operator_result_rows(connection)] == ["started"]


def test_conflicting_command_rejected_and_unverified_result_does_not_resolve() -> None:
    connection = _connection()
    item = _exception(connection)
    service = OperatorCommandService(
        connection,
        executor=lambda _command: OperatorExecution(
            verified=False,
            outcome="verification_failed",
            terminal_status="failed",
        ),
        clock=lambda: NOW,
    )
    command = _command(item, action="resume")

    assert service.execute(command).resolved is False
    assert service.execute(command).replayed is True
    with pytest.raises(ValueError, match="collision"):
        service.execute(replace(command, input_sha256="b" * 64))
    assert agent_control.list_exceptions(connection, status="open") == [item]


def test_durable_budget_and_repeated_failure_breaker_prevent_new_executor_calls() -> None:
    connection = _connection()
    item = _exception(connection)
    calls: list[str] = []

    def failed(command: OperatorCommand) -> OperatorExecution:
        calls.append(command.command_id)
        return OperatorExecution(False, "failed_verification", "failed")

    service = OperatorCommandService(connection, executor=failed, clock=lambda: NOW)
    assert service.execute(_command(item, command_id="cmd-1", action="resume", budget=2)).resolved is False
    assert service.execute(_command(item, command_id="cmd-2", action="resume", budget=2)).resolved is False
    with pytest.raises(OperatorCircuitOpen, match="budget|circuit"):
        service.execute(_command(item, command_id="cmd-3", action="resume", budget=2))
    assert calls == ["cmd-1", "cmd-2"]

    resolve = _command(item, command_id="cmd-resolve", action="resolve", budget=0)
    assert service.execute(resolve).resolved is True
    assert len(agent_control.list_operator_result_rows(connection, exception_id=item.exception_id)) == 5


def test_budget_exhaustion_blocks_second_command_even_before_failure_threshold() -> None:
    connection = _connection()
    item = _exception(connection)
    calls = 0

    def failed(_command: OperatorCommand) -> OperatorExecution:
        nonlocal calls
        calls += 1
        return OperatorExecution(False, "failed_verification", "failed")

    service = OperatorCommandService(connection, executor=failed, clock=lambda: NOW)
    service.execute(_command(item, command_id="budget-1", action="resume", budget=1))
    with pytest.raises(OperatorCircuitOpen, match="budget"):
        service.execute(_command(item, command_id="budget-2", action="resume", budget=2))
    assert calls == 1


def test_semantic_grouping_never_promotes_sensitive_or_employer_specific_context() -> None:
    connection = _connection()
    _exception(
        connection,
        exception_id="global-safe",
        run_id="run-global",
        context={"group_scope": "global", "global_ref": "global:safe"},
    )
    _exception(
        connection,
        exception_id="employer-specific",
        run_id="run-employer",
        context={
            "group_scope": "global",
            "global_ref": "global:unsafe",
            "employer_ref": "employer:one",
            "employer_specific": True,
        },
    )
    _exception(
        connection,
        exception_id="human-sensitive",
        run_id="run-human",
        context={
            "group_scope": "global",
            "global_ref": "global:unsafe",
            "application_ref": "application:human",
            "human_specific": True,
        },
    )
    _exception(
        connection,
        exception_id="job-family",
        run_id="run-family",
        context={"group_scope": "job_family", "job_family_ref": "job-family:data"},
    )
    _exception(
        connection,
        exception_id="jurisdiction",
        run_id="run-jurisdiction",
        context={"group_scope": "jurisdiction", "jurisdiction_ref": "jurisdiction:sg"},
    )
    _exception(
        connection,
        exception_id="employer-safe",
        run_id="run-employer-safe",
        context={"group_scope": "global", "global_ref": "global:caller-controlled"},
    )

    untrusted_groups = semantic_exception_groups(connection)
    assert set(untrusted_groups) == {
        "application:global-safe",
        "application:employer-specific",
        "application:human-sensitive",
        "application:job-family",
        "application:jurisdiction",
        "application:employer-safe",
    }

    trusted = {
        "global-safe": ("global", "global:safe"),
        "employer-specific": ("global", "global:unsafe"),
        "human-sensitive": ("global", "global:unsafe"),
        "job-family": ("job_family", "job-family:data"),
        "jurisdiction": ("jurisdiction", "jurisdiction:sg"),
        "employer-safe": ("employer", "employer:trusted"),
    }
    groups = semantic_exception_groups(
        connection, scope_resolver=lambda item: trusted[item.exception_id]
    )

    assert groups["global:global:safe"] == ("global-safe",)
    assert groups["application:employer-specific"] == ("employer-specific",)
    assert groups["application:human-sensitive"] == ("human-sensitive",)
    assert groups["job_family:job-family:data"] == ("job-family",)
    assert groups["jurisdiction:jurisdiction:sg"] == ("jurisdiction",)
    assert groups["employer:employer:trusted"] == ("employer-safe",)
    assert all("global:unsafe" not in key for key in groups)


def test_operator_service_uses_savepoints_without_committing_caller_transaction() -> None:
    connection = _connection()
    connection.execute("CREATE TABLE caller_state(value TEXT)")
    connection.commit()
    item = _exception(connection)
    connection.commit()
    connection.execute("BEGIN")
    connection.execute("INSERT INTO caller_state VALUES('preserved')")

    result = OperatorCommandService(connection, clock=lambda: NOW).execute(_command(item))

    assert result.resolved is True
    assert connection.in_transaction is True
    assert connection.execute("SELECT value FROM caller_state").fetchone()[0] == "preserved"
    connection.rollback()
    assert agent_control.list_exceptions(connection, status="open") == [item]


def test_database_facade_exposes_command_service_and_read_only_groups() -> None:
    connection = _connection()
    item = _exception(connection)

    assert list_operator_exception_groups(conn=connection) == {
        "application:exception-1": ("exception-1",)
    }
    result = execute_operator_command(_command(item), conn=connection)

    assert result.resolved is True
    assert list_operator_exception_groups(conn=connection) == {}


def test_self_reported_verified_result_needs_trusted_verifier() -> None:
    connection = _connection()
    item = _exception(connection)
    service = OperatorCommandService(
        connection,
        executor=lambda _command: OperatorExecution(
            True,
            "executor_claimed_verified",
            "completed",
            result_ref="checkpoint:claimed",
            result_sha256=DIGEST,
        ),
        clock=lambda: NOW,
    )
    command = _command(item, action="resume")

    result = service.execute(command)

    assert result.status == "blocked"
    assert result.resolved is False
    assert result.continuation_authorized is False
    assert agent_control.list_exceptions(connection, status="open") == [item]
    assert [row[2] for row in agent_control.list_operator_result_rows(connection)] == [
        "started",
        "failed",
    ]


def test_effectful_command_refuses_caller_outer_transaction_before_executor() -> None:
    connection = _connection()
    item = _exception(connection)
    calls = 0

    def executor(_command: OperatorCommand) -> OperatorExecution:
        nonlocal calls
        calls += 1
        return OperatorExecution(False, "must_not_run", "failed")

    connection.execute("BEGIN")
    service = OperatorCommandService(connection, executor=executor, clock=lambda: NOW)
    with pytest.raises(OperatorCommandError, match="outer transaction"):
        service.execute(_command(item, action="resume"))

    assert calls == 0
    assert connection.in_transaction is True
    assert agent_control.list_operator_result_rows(connection) == []


def test_executor_observes_durable_started_record_from_second_connection(tmp_path) -> None:
    database_path = tmp_path / "operator-command.db"
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    agent_control.ensure_schema(connection)
    item = _exception(connection)
    command = _command(item, action="resume")

    def executor(supplied: OperatorCommand) -> OperatorExecution:
        observer = sqlite3.connect(database_path)
        observer.row_factory = sqlite3.Row
        try:
            rows = agent_control.list_operator_result_rows(
                observer, command_id=supplied.command_id
            )
            assert [row[2] for row in rows] == ["started"]
        finally:
            observer.close()
        return OperatorExecution(
            True,
            "durable_evidence_verified",
            "completed",
            result_ref="checkpoint:durable",
            result_sha256=DIGEST,
        )

    service = OperatorCommandService(
        connection,
        executor=executor,
        verifier=lambda _command, _execution: True,
        clock=lambda: NOW,
    )
    assert service.execute(command).resolved is True


def test_resume_input_ref_cannot_authorize_two_distinct_commands() -> None:
    connection = _connection()
    item = _exception(connection)
    calls: list[str] = []

    def failed(command: OperatorCommand) -> OperatorExecution:
        calls.append(command.command_id)
        return OperatorExecution(False, "verification_failed", "failed")

    service = OperatorCommandService(connection, executor=failed, clock=lambda: NOW)
    first = _command(item, command_id="first-command", action="resume")
    assert service.execute(first).resolved is False
    second = replace(
        _command(item, command_id="second-command", action="resume"),
        input_ref=first.input_ref,
        input_sha256=first.input_sha256,
    )

    with pytest.raises(ValueError, match="already bound"):
        service.execute(second)
    assert calls == ["first-command"]
