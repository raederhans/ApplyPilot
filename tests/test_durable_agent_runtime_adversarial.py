"""Adversarial contracts for the durable subprocess recovery boundary.

These tests intentionally exercise failures after ``Popen``.  The contract is
fail-closed: a process that cannot be proved dead must retain its one-running-
actor reservation, and receipt observation must never be represented as an
agent subprocess with a caller-declared tool list.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import pytest

from applypilot.apply.agent_runtime import SubprocessAgentRuntime, SubprocessLaunchSpec
from applypilot.apply.contracts import AgentCheckpoint
from applypilot.apply.durable_agent_runtime import (
    DurableAgentRuntime,
    DurableLaunchIntent,
    DurableRuntimeBlocked,
)
from applypilot.storage import agent_control, runtime_control

ACTOR_ID = "application:attempt-1"


class _Pipe:
    def __init__(self, failure: Literal["write", "close"] | None = None) -> None:
        self.failure = failure

    def write(self, value: str) -> int:
        if self.failure == "write":
            raise OSError("write fault")
        return len(value)

    def close(self) -> None:
        if self.failure == "close":
            raise OSError("close fault")


class _Output:
    def read(self) -> str:
        return ""


@dataclass
class _UnkillableProcess:
    pid: int
    pipe_failure: Literal["write", "close"] | None = None
    returncode: int | None = None
    stdin: _Pipe = field(init=False)
    stdout: _Output = field(default_factory=_Output)

    def __post_init__(self) -> None:
        self.stdin = _Pipe(self.pipe_failure)

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired("agent", timeout)


def _connection_provider(path: Path):
    def connect() -> sqlite3.Connection:
        return sqlite3.connect(path, timeout=5)

    return connect


def _spec(tmp_path: Path, turn_id: str, *, parent_turn_id: str | None = None) -> SubprocessLaunchSpec:
    return SubprocessLaunchSpec(
        run_id=turn_id,
        turn_id=turn_id,
        attempt_id="attempt-1",
        actor_id=ACTOR_ID,
        command=("agent", "--json"),
        prompt=f"prompt:{turn_id}",
        cwd=tmp_path,
        env={},
        runtime_id="codex:edge:cdp:9222",
        profile_id="edge:worker:0",
        parent_run_id=parent_turn_id,
    )


def _root_intent(tmp_path: Path, turn_id: str) -> DurableLaunchIntent:
    return DurableLaunchIntent(
        spec=_spec(tmp_path, turn_id),
        runtime_backend="codex-cli",
        resume_mode="root",
        tool_surface_hash="tools-v1",
        prompt_contract_hash="prompt-v1",
        idempotency_key=f"spawn:{turn_id}",
    )


def _resume_intent(
    tmp_path: Path,
    turn_id: str,
    *,
    mode: Literal["resume", "receipt_only"] = "resume",
) -> DurableLaunchIntent:
    return DurableLaunchIntent(
        spec=_spec(tmp_path, turn_id, parent_turn_id="parent"),
        runtime_backend="codex-cli",
        resume_mode=mode,
        checkpoint_id="checkpoint-1",
        recovery_authorization_id="recovery-command-1",
        tool_surface_hash="tools-v1",
        prompt_contract_hash="prompt-v1",
        idempotency_key=f"spawn:{turn_id}",
    )


def _append_checkpoint(
    connection: sqlite3.Connection,
    checkpoint_id: str,
    *,
    sequence: int,
    expected_sequence: int,
) -> None:
    agent_control.append_checkpoint(
        connection,
        AgentCheckpoint(
            checkpoint_id=checkpoint_id,
            run_id="parent",
            turn_id="parent",
            attempt_id="attempt-1",
            actor_id=ACTOR_ID,
            phase="prepare",
            sequence=sequence,
            expected_sequence=expected_sequence,
            state={"application_status": "failed:retryable"},
            idempotency_key=f"checkpoint:{checkpoint_id}",
            schema_version="2",
        ),
    )


def _completed_parent(path: Path, tmp_path: Path) -> None:
    runtime = DurableAgentRuntime(
        SubprocessAgentRuntime(kill_process_tree=lambda _pid: None),
        _connection_provider(path),
        process_identity=lambda pid: (pid, 100_001),
        close_connections=True,
    )
    process = _UnkillableProcess(8011)
    # Make the parent successfully start; its fake child never needs to exit for
    # the explicit exact terminal transition below.
    parent = runtime.start(
        _root_intent(tmp_path, "parent"),
        popen_factory=lambda *_args, **_kwargs: process,
    )
    runtime.terminal(parent, status="completed", exit_code=0)
    connection = sqlite3.connect(path)
    try:
        _append_checkpoint(connection, "checkpoint-1", sequence=1, expected_sequence=0)
        connection.commit()
    finally:
        connection.close()


def test_resume_authorizer_has_no_database_transaction_capability(
    tmp_path: Path,
) -> None:
    """Authorizer cannot commit, rollback, or spoof the reservation transaction."""
    path = tmp_path / "atomic-resume.db"
    _completed_parent(path, tmp_path)
    popen_calls: list[str] = []
    observed_arguments: list[tuple[object, ...]] = []

    def authorizer(*arguments: object) -> bool:
        observed_arguments.append(arguments)
        assert len(arguments) == 2
        assert not any(isinstance(item, sqlite3.Connection) for item in arguments)
        return False

    runtime = DurableAgentRuntime(
        SubprocessAgentRuntime(kill_process_tree=lambda _pid: None),
        _connection_provider(path),
        process_identity=lambda pid: (pid, 100_002),
        resume_authorizer=authorizer,
        close_connections=True,
    )

    with pytest.raises(DurableRuntimeBlocked, match="authorization"):
        runtime.resume(
            _resume_intent(tmp_path, "child"),
            popen_factory=lambda *_args, **_kwargs: popen_calls.append("popen"),
        )

    assert popen_calls == []
    assert len(observed_arguments) == 1
    connection = sqlite3.connect(path)
    try:
        assert runtime_control.get_runtime_turn(connection, "child") is None
        assert runtime_control.running_runtime_turn_for_actor(connection, ACTOR_ID) is None
    finally:
        connection.close()


@pytest.mark.parametrize(
    "tools",
    (
        (),
        ("gmail:search_emails", "gmail:read_email"),
        ("mcp__playwright__browser_press",),
        ("page.goto",),
    ),
    ids=("empty-declaration", "self-reported-read-only", "browser-press", "page-goto"),
)
def test_receipt_only_intent_is_unconditionally_rejected(
    tmp_path: Path,
    tools: tuple[str, ...],
) -> None:
    """No caller-declared tool list can make an agent subprocess receipt-safe."""
    fields = {field.name for field in dataclasses.fields(DurableLaunchIntent)}
    # The public intent deliberately has no caller-controlled tool declaration.
    # Keep the conditional so a future reintroduction of that parameter is
    # covered for every known bypass spelling as well.
    extra = {"available_tools": tools} if "available_tools" in fields else {}
    with pytest.raises(ValueError, match="receipt-only"):
        DurableLaunchIntent(
            spec=_spec(tmp_path, "receipt-child", parent_turn_id="parent"),
            runtime_backend="codex-cli",
            resume_mode="receipt_only",
            checkpoint_id="checkpoint-1",
            recovery_authorization_id="recovery-command-1",
            tool_surface_hash="tools-v1",
            prompt_contract_hash="prompt-v1",
            idempotency_key="spawn:receipt-child",
            **extra,
        )
    assert "available_tools" not in fields


@pytest.mark.parametrize(
    ("fault", "pipe_failure"),
    (
        ("clock", None),
        ("write", "write"),
        ("close", "close"),
    ),
)
def test_unproven_post_popen_failure_keeps_running_reservation_and_blocks_relaunch(
    tmp_path: Path,
    fault: Literal["clock", "write", "close"],
    pipe_failure: Literal["write", "close"] | None,
) -> None:
    """A failed cleanup cannot release an actor while its child may still live."""
    path = tmp_path / f"unproven-{fault}.db"
    process = _UnkillableProcess(8100, pipe_failure=pipe_failure)
    killed: list[int] = []

    def clock() -> float:
        if fault == "clock":
            raise RuntimeError("clock fault")
        return 1.0

    runtime = DurableAgentRuntime(
        SubprocessAgentRuntime(
            kill_process_tree=lambda pid: killed.append(pid),
            clock=clock,
        ),
        _connection_provider(path),
        process_identity=lambda pid: (pid, 100_100),
        close_connections=True,
    )

    with pytest.raises((RuntimeError, OSError)):
        runtime.start(
            _root_intent(tmp_path, "first"),
            popen_factory=lambda *_args, **_kwargs: process,
        )

    assert killed == [8100]
    connection = sqlite3.connect(path)
    try:
        first = runtime_control.get_runtime_turn(connection, "first")
        assert first is not None
        assert (first.status, first.process_id, first.process_birth_time) == (
            "running",
            8100,
            100_100,
        )
    finally:
        connection.close()

    second_popen_calls: list[str] = []
    with pytest.raises(runtime_control.RuntimeTurnConflictError):
        runtime.start(
            _root_intent(tmp_path, "second"),
            popen_factory=lambda *_args, **_kwargs: second_popen_calls.append("popen"),
        )
    assert second_popen_calls == []
