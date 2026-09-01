from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from applypilot.apply.agent_runtime import SubprocessAgentRuntime, SubprocessLaunchSpec
from applypilot.apply.contracts import AgentCheckpoint
from applypilot.apply.durable_agent_runtime import (
    DurableAgentRuntime,
    DurableLaunchIntent,
    DurableRuntimeBlocked,
)
from applypilot.storage import agent_control, runtime_control


class _FakeStdin:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.payload = ""
        self.closed = False

    def write(self, value: str) -> int:
        self.events.append("prompt")
        self.payload += value
        return len(value)

    def close(self) -> None:
        self.closed = True


class _FakeStdout:
    def read(self) -> str:
        return ""


@dataclass
class _FakeProcess:
    pid: int
    events: list[str]
    returncode: int | None = None
    stdin: _FakeStdin = field(init=False)
    stdout: _FakeStdout = field(default_factory=_FakeStdout)

    def __post_init__(self) -> None:
        self.stdin = _FakeStdin(self.events)

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return 0 if self.returncode is None else self.returncode


def _provider(path: Path):
    def connect() -> sqlite3.Connection:
        return sqlite3.connect(path, timeout=5)

    return connect


def _spec(
    tmp_path: Path,
    turn_id: str,
    *,
    parent_run_id: str | None = None,
    submit_started: bool = False,
    runtime_id: str = "codex:edge:cdp:9222",
    profile_id: str = "edge:worker:0",
) -> SubprocessLaunchSpec:
    return SubprocessLaunchSpec(
        run_id=turn_id,
        attempt_id="attempt-1",
        actor_id="application:attempt-1",
        turn_id=turn_id,
        command=("agent", "--json"),
        prompt=f"prompt:{turn_id}",
        cwd=tmp_path,
        env={},
        runtime_id=runtime_id,
        profile_id=profile_id,
        parent_run_id=parent_run_id,
        submit_started=submit_started,
    )


def _intent(
    tmp_path: Path,
    turn_id: str,
    *,
    parent_run_id: str | None = None,
    checkpoint_id: str | None = None,
    resume_mode: str = "root",
    submit_started: bool = False,
    runtime_id: str = "codex:edge:cdp:9222",
    profile_id: str = "edge:worker:0",
) -> DurableLaunchIntent:
    return DurableLaunchIntent(
        spec=_spec(
            tmp_path,
            turn_id,
            parent_run_id=parent_run_id,
            submit_started=submit_started,
            runtime_id=runtime_id,
            profile_id=profile_id,
        ),
        runtime_backend="codex-cli",
        resume_mode=resume_mode,  # type: ignore[arg-type]
        checkpoint_id=checkpoint_id,
        recovery_authorization_id=(
            "recovery-command-1" if parent_run_id is not None else None
        ),
        tool_surface_hash="tools-v1",
        prompt_contract_hash="prompt-v1",
        idempotency_key=f"spawn:{turn_id}",
    )


def _runtime(
    path: Path,
    *,
    identity,
    killed: list[int] | None = None,
    authorize: bool = True,
) -> DurableAgentRuntime:
    killed = [] if killed is None else killed

    def kill(pid: int) -> None:
        killed.append(pid)

    return DurableAgentRuntime(
        SubprocessAgentRuntime(kill_process_tree=kill),
        _provider(path),
        process_identity=identity,
        resume_authorizer=(
            lambda intent, _parent: bool(
                authorize and intent.recovery_authorization_id == "recovery-command-1"
            )
        ),
        close_connections=True,
    )


def _checkpoint(path: Path, parent_turn_id: str, checkpoint_id: str) -> None:
    connection = sqlite3.connect(path)
    try:
        agent_control.append_checkpoint(
            connection,
            AgentCheckpoint(
                checkpoint_id=checkpoint_id,
                run_id=parent_turn_id,
                attempt_id="attempt-1",
                actor_id="application:attempt-1",
                turn_id=parent_turn_id,
                phase="prepare",
                sequence=1,
                expected_sequence=0,
                state={"application_status": "failed:retryable"},
                idempotency_key=f"checkpoint:{parent_turn_id}",
                fresh_turn_resume_authorized=False,
                schema_version="2",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def test_reserve_attach_happens_before_prompt_and_terminal_is_exact(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    events: list[str] = []
    process = _FakeProcess(4242, events)

    def popen(*_args, **_kwargs):
        events.append("popen")
        return process

    def identity(pid: int):
        events.append("identity")
        return pid, 100_000

    durable = _runtime(path, identity=identity)
    handle = durable.start(_intent(tmp_path, "turn-1"), popen_factory=popen)

    assert events == ["popen", "identity", "prompt"]
    assert process.stdin.payload == "prompt:turn-1"
    connection = sqlite3.connect(path)
    try:
        running = runtime_control.get_runtime_turn(connection, "turn-1")
    finally:
        connection.close()
    assert running is not None
    assert (running.process_id, running.process_birth_time) == (4242, 100_000)

    terminal = durable.terminal(handle, status="completed", exit_code=0)
    assert durable.terminal(handle, status="completed", exit_code=0) == terminal
    with pytest.raises(RuntimeError, match="already terminal"):
        durable.terminal(handle, status="failed", failure_code="LATE", exit_code=9)


def test_popen_failure_terminalizes_exact_unbound_reservation(tmp_path: Path) -> None:
    path = tmp_path / "popen-failure.db"
    durable = _runtime(path, identity=lambda pid: (pid, 1))

    def fail(*_args, **_kwargs):
        raise OSError("Popen failed")

    with pytest.raises(OSError, match="Popen failed"):
        durable.start(_intent(tmp_path, "turn-popen-failed"), popen_factory=fail)

    connection = sqlite3.connect(path)
    try:
        turn = runtime_control.get_runtime_turn(connection, "turn-popen-failed")
    finally:
        connection.close()
    assert turn is not None
    assert (turn.status, turn.failure_code, turn.process_id) == (
        "failed",
        "SPAWN_FAILED",
        None,
    )


def test_attach_failure_kills_exact_child_before_prompt_and_parks_unknown(
    tmp_path: Path,
) -> None:
    path = tmp_path / "attach-failure.db"
    events: list[str] = []
    process = _FakeProcess(4343, events)
    killed: list[int] = []

    def kill(pid: int) -> None:
        killed.append(pid)
        process.returncode = -15

    durable = DurableAgentRuntime(
        SubprocessAgentRuntime(kill_process_tree=kill),
        _provider(path),
        process_identity=lambda _pid: None,
        close_connections=True,
    )

    with pytest.raises(DurableRuntimeBlocked, match="identity"):
        durable.start(
            _intent(tmp_path, "turn-attach-failed"),
            popen_factory=lambda *_args, **_kwargs: process,
        )

    assert killed == [4343]
    assert "prompt" not in events
    connection = sqlite3.connect(path)
    try:
        turn = runtime_control.get_runtime_turn(connection, "turn-attach-failed")
    finally:
        connection.close()
    assert turn is not None
    assert (turn.status, turn.failure_code, turn.process_id) == (
        "unknown",
        "SPAWN_IDENTITY_UNBOUND",
        None,
    )


def test_new_launcher_resumes_from_durable_parent_and_latest_checkpoint(
    tmp_path: Path,
) -> None:
    path = tmp_path / "restart.db"
    parent_process = _FakeProcess(5001, [])
    first = _runtime(path, identity=lambda pid: (pid, 200_001))
    parent = first.start(
        _intent(tmp_path, "parent"),
        popen_factory=lambda *_args, **_kwargs: parent_process,
    )
    first.terminal(parent, status="completed", exit_code=0)
    _checkpoint(path, "parent", "checkpoint-parent")

    child_process = _FakeProcess(5002, [])
    second = _runtime(path, identity=lambda pid: (pid, 200_002))
    child = second.resume(
        _intent(
            tmp_path,
            "child",
            parent_run_id="parent",
            checkpoint_id="checkpoint-parent",
            resume_mode="resume",
        ),
        popen_factory=lambda *_args, **_kwargs: child_process,
    )

    connection = sqlite3.connect(path)
    try:
        persisted = runtime_control.get_runtime_turn(connection, "child")
    finally:
        connection.close()
    assert persisted is not None
    assert (persisted.parent_turn_id, persisted.checkpoint_id) == (
        "parent",
        "checkpoint-parent",
    )
    assert child.process is child_process


def test_checkpoint_without_durable_recovery_authorization_never_spawns(
    tmp_path: Path,
) -> None:
    path = tmp_path / "authorization.db"
    parent_process = _FakeProcess(5101, [])
    first = _runtime(path, identity=lambda pid: (pid, 210_001))
    parent = first.start(
        _intent(tmp_path, "parent"),
        popen_factory=lambda *_args, **_kwargs: parent_process,
    )
    first.terminal(parent, status="completed", exit_code=0)
    _checkpoint(path, "parent", "checkpoint-parent")
    calls: list[str] = []
    second = _runtime(
        path,
        identity=lambda pid: (pid, 210_002),
        authorize=False,
    )

    with pytest.raises(DurableRuntimeBlocked, match="authorization"):
        second.resume(
            _intent(
                tmp_path,
                "child",
                parent_run_id="parent",
                checkpoint_id="checkpoint-parent",
                resume_mode="resume",
            ),
            popen_factory=lambda *_args, **_kwargs: calls.append("popen"),
        )

    assert calls == []


@pytest.mark.parametrize(
    ("identity", "expected_disposition", "expected_reason"),
    (
        (lambda pid: (pid, 300_001), "live_owner", "PROCESS_STILL_RUNNING"),
        (lambda _pid: None, "recovery_required", "PROCESS_DISAPPEARED"),
        (lambda pid: (pid, 999_999), "recovery_required", "PROCESS_IDENTITY_REUSED"),
    ),
)
def test_restart_reconciliation_uses_pid_and_birth_not_memory(
    tmp_path: Path,
    identity,
    expected_disposition: str,
    expected_reason: str,
) -> None:
    path = tmp_path / f"reconcile-{expected_reason}.db"
    first = _runtime(path, identity=lambda pid: (pid, 300_001))
    first.start(
        _intent(tmp_path, "parent"),
        popen_factory=lambda *_args, **_kwargs: _FakeProcess(6001, []),
    )

    second = _runtime(path, identity=identity)
    admission = second.reconcile_actor("application:attempt-1", "attempt-1")
    assert admission.disposition == expected_disposition
    assert admission.reason_code == expected_reason

    if expected_disposition == "recovery_required":
        repeated = second.reconcile_actor("application:attempt-1", "attempt-1")
        assert repeated == admission


def test_unbound_crash_reconciles_unknown_and_never_reuses_reservation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unbound.db"
    connection = sqlite3.connect(path)
    try:
        runtime_control.start_runtime_turn(
            connection,
            turn_id="unbound",
            actor_id="application:attempt-1",
            attempt_id="attempt-1",
            runtime_id="codex:edge:cdp:9222",
            profile_id="edge:worker:0",
            runtime_backend="codex-cli",
            resume_mode="root",
            submit_started=False,
            tool_surface_hash="tools-v1",
            prompt_contract_hash="prompt-v1",
            idempotency_key="spawn:unbound",
        )
    finally:
        connection.close()

    durable = _runtime(path, identity=lambda _pid: None)
    admission = durable.reconcile_actor("application:attempt-1", "attempt-1")
    assert (admission.disposition, admission.reason_code) == (
        "recovery_required",
        "SPAWN_IDENTITY_UNBOUND",
    )
    with pytest.raises(runtime_control.RuntimeTurnConflictError):
        durable.start(_intent(tmp_path, "unbound"), popen_factory=lambda: None)


def test_post_submit_restart_routes_to_observer_and_never_spawns_receipt_agent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "receipt.db"
    first = _runtime(path, identity=lambda pid: (pid, 400_001))
    parent = first.start(
        _intent(tmp_path, "submitted", submit_started=True),
        popen_factory=lambda *_args, **_kwargs: _FakeProcess(7001, []),
    )
    first.terminal(
        parent,
        status="unknown",
        failure_code="SUBMIT_RESULT_UNKNOWN",
        exit_code=None,
    )
    _checkpoint(path, "submitted", "checkpoint-submitted")
    second = _runtime(path, identity=lambda pid: (pid, 400_002))

    first_admission = second.reconcile_actor("application:attempt-1", "attempt-1")
    repeated_admission = second.reconcile_actor("application:attempt-1", "attempt-1")
    assert repeated_admission == first_admission
    assert (first_admission.disposition, first_admission.reason_code) == (
        "receipt_only",
        "SUBMIT_RESULT_UNKNOWN",
    )

    with pytest.raises(DurableRuntimeBlocked, match="deterministic receipt"):
        second.resume(
            _intent(
                tmp_path,
                "ordinary-child",
                parent_run_id="submitted",
                checkpoint_id="checkpoint-submitted",
                resume_mode="resume",
                submit_started=True,
            ),
            popen_factory=lambda *_args, **_kwargs: _FakeProcess(7002, []),
        )
    with pytest.raises(ValueError, match="deterministic receipt observer"):
        _intent(
            tmp_path,
            "receipt-child",
            parent_run_id="submitted",
            checkpoint_id="checkpoint-submitted",
            resume_mode="receipt_only",
            submit_started=True,
        )

    connection = sqlite3.connect(path)
    try:
        assert runtime_control.running_runtime_turn_for_actor(
            connection, "application:attempt-1"
        ) is None
    finally:
        connection.close()


def test_post_submit_vanished_owner_repeatedly_routes_to_receipt_only(
    tmp_path: Path,
) -> None:
    path = tmp_path / "receipt-vanished.db"
    first = _runtime(path, identity=lambda pid: (pid, 400_101))
    first.start(
        _intent(tmp_path, "submitted-vanished", submit_started=True),
        popen_factory=lambda *_args, **_kwargs: _FakeProcess(7101, []),
    )

    restarted = _runtime(path, identity=lambda _pid: None)
    first_admission = restarted.reconcile_actor(
        "application:attempt-1", "attempt-1"
    )
    repeated_admission = restarted.reconcile_actor(
        "application:attempt-1", "attempt-1"
    )

    assert repeated_admission == first_admission
    assert (first_admission.disposition, first_admission.reason_code) == (
        "receipt_only",
        "PROCESS_DISAPPEARED",
    )
