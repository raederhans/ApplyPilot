from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
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
from applypilot.apply.operator_commands import (
    OperatorCommand,
    OperatorCommandError,
    OperatorCommandService,
    OperatorExecution,
)
from applypilot.apply.operator_runtime import OperatorRuntime, verified_child_execution
from applypilot.database import close_connection, init_db
from applypilot.storage import agent_control, application_ledger, runtime_control

NOW = datetime.now(UTC)
JOB = "https://example.test/jobs/1"
ATTEMPT = "attempt-1"
ACTOR = application_actor_id(ATTEMPT)
PARENT = "parent-turn"
CHECKPOINT = "checkpoint-parent"
RESPONSE_DIGEST = "a" * 64
REQUEST_INPUT_REF = "human-response:" + __import__("hashlib").sha256(b"request-1").hexdigest()


@pytest.fixture
def connection(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "operator-runtime.sqlite3"
    connection = init_db(path)
    agent_control.ensure_schema(connection)
    application_ledger.ensure_schema(connection)
    runtime_control.ensure_schema(connection)
    yield connection
    close_connection(path)


def _job(connection: sqlite3.Connection, *, status: str = "previewed") -> None:
    connection.execute(
        "INSERT INTO jobs(url,title,company_name,apply_status) VALUES(?,?,?,?)",
        (JOB, "Engineer", "Example Co", status),
    )


def _attempt(connection: sqlite3.Connection, *, submit_started: bool = False) -> None:
    instant = NOW.isoformat()
    connection.execute(
        "INSERT INTO application_attempts(attempt_id,job_url,batch_id,worker_id,started_at,"
        "lease_expires_at,phase,submit_started,status,updated_at,evidence_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
        (
            ATTEMPT,
            JOB,
            "batch-1",
            "worker-1",
            instant,
            (NOW + timedelta(hours=1)).isoformat(),
            "prepare",
            int(submit_started),
            "in_progress",
            instant,
        ),
    )


def _exception(
    connection: sqlite3.Connection,
    *,
    queue_kind: str,
    context: dict[str, object],
    exception_id: str = "exception-1",
) -> ApplicationException:
    item = ApplicationException(
        exception_id=exception_id,
        command_id=f"park:{exception_id}",
        run_id=PARENT,
        attempt_id=ATTEMPT,
        actor_id=ACTOR,
        turn_id=PARENT,
        queue_kind=queue_kind,  # type: ignore[arg-type]
        failure_category="operator_required",
        next_action="operator_review",
        context=context,
        created_at=NOW,
    )
    assert agent_control.enqueue_exception(connection, item)
    connection.commit()
    return item


def _command(
    item: ApplicationException,
    *,
    action: str,
    command_id: str = "operator-1",
    input_ref: str = "evidence:1",
    digest: str = RESPONSE_DIGEST,
) -> OperatorCommand:
    return OperatorCommand(
        command_id=command_id,
        exception_id=item.exception_id,
        action=action,  # type: ignore[arg-type]
        run_id=item.run_id,
        attempt_id=item.attempt_id,
        actor_id=item.actor_id,
        turn_id=item.turn_id,
        input_ref=None if action == "resolve" else input_ref,
        input_sha256=None if action == "resolve" else digest,
        created_at=NOW,
    )


def _resume_setup(connection: sqlite3.Connection) -> tuple[ApplicationException, OperatorCommand]:
    _job(connection)
    _attempt(connection)
    parent = runtime_control.start_runtime_turn(
        connection,
        turn_id=PARENT,
        actor_id=ACTOR,
        attempt_id=ATTEMPT,
        runtime_id="runtime-1",
        profile_id="profile-1",
        runtime_backend="codex-cli",
        resume_mode="root",
        submit_started=False,
        tool_surface_hash="tools-v1",
        prompt_contract_hash="prompt-v1",
        started_at=NOW,
    )
    runtime_control.mark_runtime_turn_terminal(
        connection,
        token=runtime_control.token_from_turn(parent),
        status="closed",
        exit_code=0,
        terminal_at=NOW + timedelta(seconds=1),
    )
    agent_control.append_checkpoint(
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
            idempotency_key="checkpoint:parent",
            schema_version="2",
            created_at=NOW,
        ),
    )
    lease = runtime_control.acquire_browser_resource_lease(
        connection,
        lease_id="lease-1",
        resource_kind="browser-profile-page",
        scope_id="scope-1",
        profile_id="profile-1",
        page_target_id="page-1",
        owner_id="owner-1",
        actor_id=ACTOR,
        attempt_id=ATTEMPT,
        runtime_id="runtime-1",
        page_epoch=3,
        lease_seconds=3600,
        now=NOW,
    )
    request = HumanRequest(
        request_id="request-1",
        run_id=PARENT,
        attempt_id=ATTEMPT,
        request_type="availability",
        prompt="Provide availability",
        context={
            "field": "availability",
            "actor_id": ACTOR,
            "turn_id": PARENT,
        },
        created_at=NOW,
    )
    agent_control.create_human_request(connection, request)
    append_human_response(
        connection,
        HumanResponseRef(
            request_id=request.request_id,
            response_ref="response:1",
            response_digest=RESPONSE_DIGEST,
            response_type=request.request_type,
            resolved_by="operator-1",
            resolved_at=NOW + timedelta(seconds=2),
        ),
    )
    item = _exception(
        connection,
        queue_kind="human_only",
        context={
            "request_id": request.request_id,
            "checkpoint_id": CHECKPOINT,
            "job_url": JOB,
            "profile_id": "profile-1",
            "browser_lease_id": lease.lease_id,
            "browser_lease_epoch": lease.lease_epoch,
            "page_target_id": str(lease.page_target_id),
            "page_epoch": lease.page_epoch,
        },
    )
    return item, _command(item, action="resume", input_ref=REQUEST_INPUT_REF)


def _owner(connection: sqlite3.Connection, calls: list[dict[str, object]]):
    def owner(command: OperatorCommand, context: dict[str, object]) -> OperatorExecution:
        calls.append(context)
        child_id = "child-turn"
        child = runtime_control.start_runtime_turn(
            connection,
            turn_id=child_id,
            actor_id=command.actor_id,
            attempt_id=command.attempt_id,
            parent_turn_id=command.run_id,
            checkpoint_id=CHECKPOINT,
            runtime_id="runtime-1",
            profile_id="profile-1",
            runtime_backend="codex-cli",
            resume_mode="resume",
            submit_started=False,
            tool_surface_hash="tools-v1",
            prompt_contract_hash="prompt-v1",
            started_at=NOW + timedelta(seconds=3),
        )
        runtime_control.mark_runtime_turn_terminal(
            connection,
            token=runtime_control.token_from_turn(child),
            status="completed",
            exit_code=0,
            terminal_at=NOW + timedelta(seconds=4),
        )
        checkpoint = AgentCheckpoint(
            checkpoint_id="checkpoint-child",
            run_id=child_id,
            attempt_id=command.attempt_id,
            actor_id=command.actor_id,
            turn_id=child_id,
            phase="prepare",
            sequence=2,
            expected_sequence=1,
            state={"job_url": JOB},
            idempotency_key="checkpoint:child",
            schema_version="2",
            created_at=NOW + timedelta(seconds=4),
        )
        agent_control.append_checkpoint(connection, checkpoint)
        digest = _digest([child_id, checkpoint.checkpoint_id, checkpoint.sequence])
        return OperatorExecution(
            True,
            "owner_completed",
            "completed",
            result_ref=f"runtime:{child_id}",
            result_sha256=digest,
        )

    return owner


def _digest(value: object) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _receipt(connection: sqlite3.Connection) -> tuple[ApplicationException, bytes]:
    _job(connection, status="submission_uncertain")
    _attempt(connection, submit_started=True)
    instant = NOW.isoformat()
    connection.execute(
        "INSERT INTO application_batch_consumptions(batch_id,job_url,reserved_at,status,updated_at) VALUES(?,?,?,?,?)",
        ("batch-1", JOB, instant, "submission_uncertain", instant),
    )
    connection.execute(
        "INSERT INTO application_submission_gates(gate_id,attempt_id,batch_id,job_url,claimed_at,"
        "claimed_at_epoch,state,updated_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "gate-1",
            ATTEMPT,
            "batch-1",
            JOB,
            instant,
            NOW.timestamp(),
            "submission_uncertain",
            instant,
            "gate-key-1",
        ),
    )
    item = _exception(connection, queue_kind="receipt_reconciliation", context={"job_url": JOB})
    raw = json.dumps(
        {
            "source": "candidate_portal",
            "receipt_id": "receipt-1",
            "job_url": JOB,
            "company_name": "Example Co",
            "job_title": "Engineer",
            "portal_status": "submitted",
            "observed_at": (NOW + timedelta(seconds=5)).isoformat(),
            "gate_id": "gate-1",
            "batch_id": "batch-1",
            "attempt_id": ATTEMPT,
        },
        sort_keys=True,
    ).encode()
    return item, raw


def test_resume_exact_owner_child_and_replay_once(connection: sqlite3.Connection) -> None:
    item, command = _resume_setup(connection)
    calls: list[dict[str, object]] = []
    runtime = OperatorRuntime(connection, resume_owner=_owner(connection, calls))

    result = runtime.resume(command, request_id="request-1")
    replay = runtime.resume(command, request_id="request-1")

    assert result.resolved is True and replay.replayed is True
    assert len(calls) == 1
    assert "answer" not in json.dumps(calls)
    assert agent_control.get_exception(connection, item.exception_id).status == "resolved"  # type: ignore[union-attr]
    assert connection.execute(
        "SELECT status FROM agent_human_requests WHERE request_id='request-1'"
    ).fetchone()[0] == "consumed"
    evidence = verified_child_execution(
        connection,
        actor_id=ACTOR,
        attempt_id=ATTEMPT,
        parent_turn_id=PARENT,
        child_turn_id="child-turn",
    )
    assert evidence.result_ref == "runtime:child-turn"
    assert evidence.result_sha256 == _digest(["child-turn", "checkpoint-child", 2])
    with pytest.raises(OperatorCommandError, match="exact and completed"):
        verified_child_execution(
            connection,
            actor_id=ACTOR,
            attempt_id="wrong-attempt",
            parent_turn_id=PARENT,
            child_turn_id="child-turn",
        )


def test_no_owner_persists_requested_then_owner_executes_exact_request(
    connection: sqlite3.Connection,
) -> None:
    item, command = _resume_setup(connection)
    command = replace(command, recovery_budget=1)
    requested = OperatorRuntime(connection).resume(command, request_id="request-1")
    replay = OperatorRuntime(connection).resume(
        replace(command, created_at=NOW + timedelta(minutes=1)),
        request_id="request-1",
    )
    assert requested.status == "requested" and requested.resolved is False
    assert replay.status == "requested" and replay.replayed is True
    with pytest.raises(ValueError, match="collision"):
        OperatorRuntime(connection).resume(
            replace(command, input_sha256="b" * 64),
            request_id="request-1",
        )
    persisted = OperatorRuntime(connection).load_requested_resume(item.exception_id)
    assert persisted is not None and persisted.created_at == command.created_at
    calls: list[dict[str, object]] = []
    completed = OperatorRuntime(connection, resume_owner=_owner(connection, calls)).resume(
        persisted,
        request_id="request-1",
    )
    assert completed.resolved is True
    assert len(calls) == 1
    assert OperatorRuntime(connection).load_requested_resume(item.exception_id) is None
    assert [row[2] for row in agent_control.list_operator_result_rows(connection)] == [
        "requested",
        "started",
        "verified",
    ]
    assert agent_control.get_exception(connection, item.exception_id).status == "resolved"  # type: ignore[union-attr]


def test_requested_resume_timeout_is_atomic_and_does_not_consume_response(
    connection: sqlite3.Connection,
) -> None:
    item, command = _resume_setup(connection)
    runtime = OperatorRuntime(connection)
    assert runtime.resume(command, request_id="request-1").status == "requested"
    before_response = tuple(
        connection.execute(
            "SELECT * FROM agent_human_responses WHERE request_id='request-1'"
        ).fetchone()
    )

    expired = runtime.expire_resume_request(
        item.exception_id,
        request_id="request-1",
        expired_at=NOW + timedelta(minutes=5),
    )
    replay = runtime.expire_resume_request(item.exception_id, request_id="request-1")

    assert expired.command_id == command.command_id and expired.replayed is False
    assert replay.replayed is True
    assert agent_control.get_exception(connection, item.exception_id).status == "expired"  # type: ignore[union-attr]
    assert runtime.list_exceptions() == ()
    assert connection.execute(
        "SELECT status FROM agent_human_requests WHERE request_id='request-1'"
    ).fetchone()[0] == "expired"
    assert tuple(
        connection.execute(
            "SELECT * FROM agent_human_responses WHERE request_id='request-1'"
        ).fetchone()
    ) == before_response
    assert [row[2] for row in agent_control.list_operator_result_rows(connection)] == [
        "requested",
        "failed",
    ]


def test_timeout_without_requested_command_expires_exact_pair(
    connection: sqlite3.Connection,
) -> None:
    item, _ = _resume_setup(connection)
    result = OperatorRuntime(connection).expire_resume_request(
        item.exception_id,
        request_id="request-1",
    )
    assert result.command_id is None
    assert agent_control.get_exception(connection, item.exception_id).status == "expired"  # type: ignore[union-attr]
    assert connection.execute(
        "SELECT status FROM agent_human_requests WHERE request_id='request-1'"
    ).fetchone()[0] == "expired"


def test_multiple_requested_resume_commands_fail_closed(
    connection: sqlite3.Connection,
) -> None:
    item, command = _resume_setup(connection)
    service = OperatorCommandService(connection)
    assert service.request_resume(command).status == "requested"
    second = _command(
        item,
        action="resume",
        command_id="operator-2",
        input_ref="human-response:distinct",
        digest="b" * 64,
    )
    assert service.request_resume(second).status == "requested"
    runtime = OperatorRuntime(connection)
    with pytest.raises(OperatorCommandError, match="multiple"):
        runtime.load_requested_resume(item.exception_id)
    with pytest.raises(OperatorCommandError, match="multiple"):
        runtime.expire_resume_request(item.exception_id, request_id="request-1")
    assert agent_control.get_exception(connection, item.exception_id).status == "open"  # type: ignore[union-attr]


def test_requested_resume_reader_rejects_exception_identity_drift(
    connection: sqlite3.Connection,
) -> None:
    item, command = _resume_setup(connection)
    assert OperatorRuntime(connection).resume(command, request_id="request-1").status == "requested"
    connection.execute(
        "UPDATE agent_exception_queue SET run_id='drifted',turn_id='drifted' WHERE exception_id=?",
        (item.exception_id,),
    )
    connection.commit()
    with pytest.raises(OperatorCommandError, match="drift"):
        OperatorRuntime(connection).load_requested_resume(item.exception_id)


def test_existing_child_blocks_second_resume_before_owner(connection: sqlite3.Connection) -> None:
    item, command = _resume_setup(connection)
    runtime_control.start_runtime_turn(
        connection,
        turn_id="existing-child",
        actor_id=ACTOR,
        attempt_id=ATTEMPT,
        parent_turn_id=PARENT,
        checkpoint_id=CHECKPOINT,
        runtime_id="runtime-1",
        profile_id="profile-1",
        runtime_backend="codex-cli",
        resume_mode="resume",
        submit_started=False,
        tool_surface_hash="tools-v1",
        prompt_contract_hash="prompt-v1",
        started_at=NOW + timedelta(seconds=3),
    )
    calls: list[dict[str, object]] = []
    with pytest.raises(OperatorCommandError, match="already has"):
        OperatorRuntime(connection, resume_owner=_owner(connection, calls)).resume(command, request_id="request-1")
    assert calls == []
    assert agent_control.get_exception(connection, item.exception_id).status == "open"  # type: ignore[union-attr]


def test_legacy_exception_without_resume_bindings_fails_before_owner(
    connection: sqlite3.Connection,
) -> None:
    _job(connection)
    _attempt(connection)
    item = _exception(
        connection,
        queue_kind="human_only",
        context={"application_ref": "application:legacy"},
    )
    calls: list[dict[str, object]] = []
    with pytest.raises(OperatorCommandError, match="lacks exact"):
        OperatorRuntime(connection, resume_owner=_owner(connection, calls)).resume(
            _command(item, action="resume", input_ref=REQUEST_INPUT_REF),
            request_id="request-1",
        )
    assert calls == []


def test_forged_verified_result_without_child_keeps_exception_open(
    connection: sqlite3.Connection,
) -> None:
    item, _ = _resume_setup(connection)
    # A callback can claim success, but without a unique child and higher checkpoint it is rejected.
    forged = lambda _command, _context: OperatorExecution(True, "claimed", "completed", "runtime:forged", "b" * 64)
    result = OperatorRuntime(connection, resume_owner=forged).resume(
        _command(item, action="resume", command_id="operator-2", input_ref=REQUEST_INPUT_REF),
        request_id="request-1",
    )
    assert result.status == "blocked" and result.resolved is False
    assert agent_control.get_exception(connection, item.exception_id).status == "open"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("checkpoint", "checkpoint"),
        ("lease", "lease"),
        ("job", "job"),
        ("submit", "parent"),
    ],
)
def test_resume_identity_drift_never_calls_owner(
    connection: sqlite3.Connection,
    mutation: str,
    match: str,
) -> None:
    item, command = _resume_setup(connection)
    if mutation == "checkpoint":
        connection.execute(
            "UPDATE agent_exception_queue SET context_json=json_set(context_json,'$.checkpoint_id','stale')"
        )
    elif mutation == "lease":
        connection.execute("UPDATE agent_exception_queue SET context_json=json_set(context_json,'$.page_epoch',99)")
    elif mutation == "job":
        connection.execute("UPDATE application_attempts SET job_url='https://other.test/job'")
    else:
        connection.execute("UPDATE agent_runtime_turns SET submit_started=1 WHERE turn_id=?", (PARENT,))
    connection.commit()
    calls: list[dict[str, object]] = []
    with pytest.raises(OperatorCommandError, match=match):
        OperatorRuntime(connection, resume_owner=_owner(connection, calls)).resume(command, request_id="request-1")
    assert calls == []
    assert agent_control.get_exception(connection, item.exception_id).status == "open"  # type: ignore[union-attr]


def test_receipt_exact_binding_reconciles_and_replays(connection: sqlite3.Connection) -> None:
    item, raw = _receipt(connection)
    digest = _digest_bytes(raw)
    command = _command(item, action="reconcile", input_ref="receipt-file:1", digest=digest)
    runtime = OperatorRuntime(connection)

    result = runtime.reconcile(command, evidence_ref="receipt-file:1", evidence_bytes=raw)
    replay = runtime.reconcile(command, evidence_ref="receipt-file:1", evidence_bytes=raw)

    assert result.resolved is True and replay.replayed is True
    assert connection.execute("SELECT apply_status FROM jobs WHERE url=?", (JOB,)).fetchone()[0] == "applied"


@pytest.mark.parametrize("mutation", ["hash", "attempt", "gate", "job"])
def test_receipt_substitution_or_identity_drift_fails_closed(
    connection: sqlite3.Connection,
    mutation: str,
) -> None:
    item, raw = _receipt(connection)
    value = json.loads(raw)
    digest = _digest_bytes(raw)
    if mutation == "hash":
        supplied = raw + b" "
    else:
        value[mutation if mutation != "job" else "job_url"] = f"wrong-{mutation}"
        supplied = json.dumps(value, sort_keys=True).encode()
        digest = _digest_bytes(supplied)
    command = _command(item, action="reconcile", input_ref="receipt-file:1", digest=digest)
    with pytest.raises(OperatorCommandError):
        OperatorRuntime(connection).reconcile(
            command,
            evidence_ref="receipt-file:1",
            evidence_bytes=supplied,
        )
    assert agent_control.get_exception(connection, item.exception_id).status == "open"  # type: ignore[union-attr]
    assert (
        connection.execute("SELECT apply_status FROM jobs WHERE url=?", (JOB,)).fetchone()[0] == "submission_uncertain"
    )
    assert connection.execute("SELECT COUNT(*) FROM application_receipts").fetchone()[0] == 0


def test_read_models_are_reference_only(connection: sqlite3.Connection) -> None:
    item, _ = _resume_setup(connection)
    runtime = OperatorRuntime(connection)
    assert runtime.show_exception(item.exception_id) == item
    assert runtime.list_exceptions() == (item,)
    assert runtime.group_exceptions() == {f"application:{item.exception_id}": (item.exception_id,)}
    inspection = runtime.inspect_run(ACTOR)
    assert inspection.latest_turn_id == PARENT
    assert inspection.open_exception_ids == (item.exception_id,)


def _digest_bytes(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()
