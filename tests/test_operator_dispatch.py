from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from applypilot.apply.contracts import (
    AgentCheckpoint,
    ApplicationException,
    HumanRequest,
    application_actor_id,
)
from applypilot.apply.human_handoff import HumanResponseRef, append_human_response
from applypilot.apply.operator_commands import OperatorCommand, OperatorExecution
from applypilot.apply.operator_dispatch import wait_for_requested_resume
from applypilot.apply.operator_runtime import OperatorRuntime, verified_child_execution
from applypilot.database import close_connection, init_db
from applypilot.storage import agent_control, runtime_control

NOW = datetime.now(UTC)
JOB = "https://example.test/operator-dispatch"
ATTEMPT = "attempt-dispatch"
ACTOR = application_actor_id(ATTEMPT)
PARENT = "parent-dispatch"
REQUEST = "request-dispatch"
CHECKPOINT = "checkpoint-dispatch"
DIGEST = "a" * 64
INPUT_REF = "human-response:" + hashlib.sha256(REQUEST.encode()).hexdigest()


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "operator-dispatch.sqlite3"
    connection = init_db(path)
    try:
        yield connection
    finally:
        close_connection(path)


def _pair(connection: sqlite3.Connection) -> ApplicationException:
    request = HumanRequest(
        request_id=REQUEST,
        run_id=PARENT,
        attempt_id=ATTEMPT,
        request_type="availability",
        prompt="Provide availability",
        context={"actor_id": ACTOR, "turn_id": PARENT},
        created_at=NOW,
    )
    assert agent_control.create_human_request(connection, request)
    assert append_human_response(
        connection,
        HumanResponseRef(
            request_id=REQUEST,
            response_ref="response-store://dispatch",
            response_digest=DIGEST,
            response_type=request.request_type,
            resolved_by="human:user",
            resolved_at=NOW + timedelta(seconds=1),
        ),
    )
    item = ApplicationException(
        exception_id="exception-dispatch",
        command_id="park:exception-dispatch",
        run_id=PARENT,
        attempt_id=ATTEMPT,
        actor_id=ACTOR,
        turn_id=PARENT,
        queue_kind="human_only",
        failure_category="operator_required",
        next_action="operator_review",
        context={"request_id": REQUEST},
        created_at=NOW,
    )
    assert agent_control.enqueue_exception(connection, item)
    connection.commit()
    return item


def _resume_setup(
    connection: sqlite3.Connection,
) -> tuple[ApplicationException, OperatorCommand]:
    connection.execute(
        "INSERT INTO jobs(url,title,company_name,apply_status) VALUES(?,?,?,'previewed')",
        (JOB, "Engineer", "Example Co"),
    )
    connection.execute(
        "INSERT INTO application_attempts(attempt_id,job_url,batch_id,worker_id,started_at,"
        "lease_expires_at,phase,submit_started,status,updated_at,evidence_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
        (
            ATTEMPT,
            JOB,
            "batch-dispatch",
            "worker-dispatch",
            NOW.isoformat(),
            (NOW + timedelta(hours=1)).isoformat(),
            "prepare",
            0,
            "in_progress",
            NOW.isoformat(),
        ),
    )
    parent = runtime_control.start_runtime_turn(
        connection,
        turn_id=PARENT,
        actor_id=ACTOR,
        attempt_id=ATTEMPT,
        runtime_id="runtime-dispatch",
        profile_id="profile-dispatch",
        runtime_backend="test-runtime",
        resume_mode="root",
        submit_started=False,
        tool_surface_hash="tools",
        prompt_contract_hash="prompt",
        started_at=NOW,
    )
    runtime_control.mark_runtime_turn_terminal(
        connection,
        token=runtime_control.token_from_turn(parent),
        status="closed",
        exit_code=0,
        terminal_at=NOW + timedelta(seconds=1),
    )
    assert agent_control.append_checkpoint(
        connection,
        AgentCheckpoint(
            checkpoint_id=CHECKPOINT,
            run_id=PARENT,
            attempt_id=ATTEMPT,
            actor_id=ACTOR,
            turn_id=PARENT,
            phase="prepare",
            sequence=1,
            expected_sequence=0,
            state={"job_url": JOB},
            idempotency_key="checkpoint:dispatch-parent",
            schema_version="2",
            created_at=NOW,
        ),
    )
    lease = runtime_control.acquire_browser_resource_lease(
        connection,
        lease_id="lease-dispatch",
        resource_kind="browser-profile-page",
        scope_id="scope-dispatch",
        profile_id="profile-dispatch",
        page_target_id="page-dispatch",
        owner_id="owner-dispatch",
        actor_id=ACTOR,
        attempt_id=ATTEMPT,
        runtime_id="runtime-dispatch",
        page_epoch=1,
        lease_seconds=3600,
        now=NOW,
    )
    item = _pair(connection)
    connection.execute(
        "UPDATE agent_exception_queue SET context_json=? WHERE exception_id=?",
        (
            __import__("json").dumps(
                {
                    "request_id": REQUEST,
                    "checkpoint_id": CHECKPOINT,
                    "job_url": JOB,
                    "profile_id": "profile-dispatch",
                    "browser_lease_id": lease.lease_id,
                    "browser_lease_epoch": lease.lease_epoch,
                    "page_target_id": str(lease.page_target_id),
                    "page_epoch": lease.page_epoch,
                },
                sort_keys=True,
            ),
            item.exception_id,
        ),
    )
    connection.commit()
    item = agent_control.get_exception(connection, item.exception_id)
    assert item is not None
    command = OperatorCommand(
        command_id="operator-dispatch",
        exception_id=item.exception_id,
        action="resume",
        run_id=item.run_id,
        attempt_id=item.attempt_id,
        actor_id=item.actor_id,
        turn_id=item.turn_id,
        input_ref=INPUT_REF,
        input_sha256=DIGEST,
        recovery_budget=1,
        created_at=NOW,
    )
    assert OperatorRuntime(connection).resume(command, request_id=REQUEST).status == "requested"
    return item, command


def _owner(connection: sqlite3.Connection, events: list[str]):
    def owner(command: OperatorCommand, _context: object) -> OperatorExecution:
        events.append("owner")
        child = runtime_control.start_runtime_turn(
            connection,
            turn_id="child-dispatch",
            actor_id=command.actor_id,
            attempt_id=command.attempt_id,
            parent_turn_id=command.run_id,
            checkpoint_id=CHECKPOINT,
            runtime_id="runtime-dispatch",
            profile_id="profile-dispatch",
            runtime_backend="test-runtime",
            resume_mode="resume",
            submit_started=False,
            tool_surface_hash="tools",
            prompt_contract_hash="prompt",
            started_at=NOW + timedelta(seconds=2),
        )
        runtime_control.mark_runtime_turn_terminal(
            connection,
            token=runtime_control.token_from_turn(child),
            status="completed",
            exit_code=0,
            terminal_at=NOW + timedelta(seconds=3),
        )
        assert agent_control.append_checkpoint(
            connection,
            AgentCheckpoint(
                checkpoint_id="checkpoint-dispatch-child",
                run_id=child.turn_id,
                attempt_id=command.attempt_id,
                actor_id=command.actor_id,
                turn_id=child.turn_id,
                phase="prepare",
                sequence=2,
                expected_sequence=1,
                state={"job_url": JOB},
                idempotency_key="checkpoint:dispatch-child",
                schema_version="2",
                created_at=NOW + timedelta(seconds=3),
            ),
        )
        return verified_child_execution(
            connection,
            actor_id=command.actor_id,
            attempt_id=command.attempt_id,
            parent_turn_id=command.run_id,
            child_turn_id=child.turn_id,
        )

    return owner


def _response_row(connection: sqlite3.Connection) -> tuple[object, ...]:
    row = connection.execute(
        "SELECT * FROM agent_human_responses WHERE request_id=?",
        (REQUEST,),
    ).fetchone()
    assert row is not None
    return tuple(row)


def _assert_expired(connection: sqlite3.Connection, before_response: tuple[object, ...]) -> None:
    assert agent_control.get_exception(connection, "exception-dispatch").status == "expired"  # type: ignore[union-attr]
    assert connection.execute(
        "SELECT status FROM agent_human_requests WHERE request_id=?",
        (REQUEST,),
    ).fetchone()[0] == "expired"
    assert _response_row(connection) == before_response


def test_first_poll_heartbeats_then_same_process_owner_resumes(
    connection: sqlite3.Connection,
) -> None:
    item, _ = _resume_setup(connection)
    events: list[str] = []

    result = wait_for_requested_resume(
        connection,
        exception_id=item.exception_id,
        request_id=REQUEST,
        resume_owner=_owner(connection, events),
        heartbeat=lambda: events.append("heartbeat") is None,
        stop_wait=lambda _seconds: False,
        timeout_seconds=10,
    )

    assert result.status == "resumed"
    assert result.command_result is not None and result.command_result.resolved is True
    assert events == ["heartbeat", "owner"]


def test_no_command_timeout_atomically_expires_without_consuming_response(
    connection: sqlite3.Connection,
) -> None:
    item = _pair(connection)
    response = _response_row(connection)
    ticks = iter((0.0, 2.0))
    result = wait_for_requested_resume(
        connection,
        exception_id=item.exception_id,
        request_id=REQUEST,
        resume_owner=lambda _command, _context: pytest.fail("owner must not run"),
        heartbeat=lambda: True,
        stop_wait=lambda _seconds: pytest.fail("expired wait must not sleep"),
        timeout_seconds=1,
        monotonic=lambda: next(ticks),
    )
    assert result.status == "expired"
    _assert_expired(connection, response)


def test_requested_command_arriving_after_deadline_expires_before_owner(
    connection: sqlite3.Connection,
) -> None:
    item, _ = _resume_setup(connection)
    response = _response_row(connection)
    owner_calls: list[str] = []
    ticks = iter((0.0, 2.0))

    result = wait_for_requested_resume(
        connection,
        exception_id=item.exception_id,
        request_id=REQUEST,
        resume_owner=lambda _command, _context: (
            owner_calls.append("owner")
            or pytest.fail("a requested command past the deadline must not execute")
        ),
        heartbeat=lambda: True,
        stop_wait=lambda _seconds: pytest.fail("expired wait must not sleep"),
        timeout_seconds=1,
        monotonic=lambda: next(ticks),
    )

    assert result.status == "expired"
    assert owner_calls == []
    _assert_expired(connection, response)


def test_lease_loss_expires_immediately(connection: sqlite3.Connection) -> None:
    item = _pair(connection)
    response = _response_row(connection)
    result = wait_for_requested_resume(
        connection,
        exception_id=item.exception_id,
        request_id=REQUEST,
        resume_owner=lambda _command, _context: pytest.fail("owner must not run"),
        heartbeat=lambda: False,
        stop_wait=lambda _seconds: pytest.fail("lease loss must not sleep"),
        timeout_seconds=10,
    )
    assert result.status == "lease_lost"
    _assert_expired(connection, response)


def test_stop_event_expires_immediately_after_heartbeat(
    connection: sqlite3.Connection,
) -> None:
    item = _pair(connection)
    response = _response_row(connection)
    events: list[str] = []
    result = wait_for_requested_resume(
        connection,
        exception_id=item.exception_id,
        request_id=REQUEST,
        resume_owner=lambda _command, _context: pytest.fail("owner must not run"),
        heartbeat=lambda: events.append("heartbeat") is None,
        stop_wait=lambda _seconds: events.append("stop") is None,
        timeout_seconds=10,
        monotonic=lambda: 0.0,
    )
    assert result.status == "stopped"
    assert events == ["heartbeat", "stop"]
    _assert_expired(connection, response)


def test_requested_command_does_not_run_when_stop_is_already_set(
    connection: sqlite3.Connection,
) -> None:
    item, _ = _resume_setup(connection)
    response = _response_row(connection)
    events: list[str] = []
    result = wait_for_requested_resume(
        connection,
        exception_id=item.exception_id,
        request_id=REQUEST,
        resume_owner=lambda _command, _context: (
            events.append("owner")
            or pytest.fail("a requested command must not run after stop")
        ),
        heartbeat=lambda: events.append("heartbeat") is None,
        stop_wait=lambda seconds: events.append(f"stop:{seconds}") is None,
        timeout_seconds=10,
        monotonic=lambda: 0.0,
    )

    assert result.status == "stopped"
    assert events == ["heartbeat", "stop:0.0"]
    _assert_expired(connection, response)
