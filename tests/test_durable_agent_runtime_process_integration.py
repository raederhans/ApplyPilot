"""Real-process restart contracts for the durable agent runtime.

These checks deliberately use a short-lived Python child and a test-local
SQLite database.  They cover the OS-facing PID/birth-time handoff without
opening a browser or contacting an Agent/provider runtime.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

from applypilot.apply.agent_runtime import (
    SubprocessAgentRuntime,
    SubprocessLaunchSpec,
)
from applypilot.apply.durable_agent_runtime import (
    DurableAgentRuntime,
    DurableLaunchIntent,
)
from applypilot.apply.profile_lock import inspect_process_identity
from applypilot.storage import runtime_control

ACTOR_ID = "application:attempt-real-process"
ATTEMPT_ID = "attempt-real-process"


def _connection_provider(path: Path):
    def connect() -> sqlite3.Connection:
        return sqlite3.connect(path, timeout=5)

    return connect


def _process_identity(pid: int) -> tuple[int, int] | None:
    identity = inspect_process_identity(pid)
    if identity is None:
        return None
    return identity.pid, identity.creation_filetime


def _kill_owned_process(pid: int) -> None:
    """Terminate only the child PID created by this test runtime."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def _runtime(path: Path) -> DurableAgentRuntime:
    return DurableAgentRuntime(
        SubprocessAgentRuntime(kill_process_tree=_kill_owned_process),
        _connection_provider(path),
        process_identity=_process_identity,
        close_connections=True,
    )


def _intent(tmp_path: Path, turn_id: str, *, submit_started: bool = False) -> DurableLaunchIntent:
    spec = SubprocessLaunchSpec(
        run_id=turn_id,
        attempt_id=ATTEMPT_ID,
        actor_id=ACTOR_ID,
        turn_id=turn_id,
        command=(
            sys.executable,
            "-c",
            "import sys, time; sys.stdin.read(); time.sleep(120)",
        ),
        prompt=f"controlled-process:{turn_id}",
        cwd=tmp_path,
        env=os.environ.copy(),
        runtime_id="test:real-process",
        profile_id="test:real-process",
        submit_started=submit_started,
    )
    return DurableLaunchIntent(
        spec=spec,
        runtime_backend="test-real-process",
        resume_mode="root",
        tool_surface_hash="test-tools-v1",
        prompt_contract_hash="test-contract-v1",
        idempotency_key=f"test-real-process:{turn_id}",
    )


def _terminate(process) -> None:
    """Prove the controlled child exits, with a bounded forceful fallback."""
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    assert process.poll() is not None


def _turn(path: Path, turn_id: str) -> runtime_control.AgentRuntimeTurn:
    connection = sqlite3.connect(path)
    try:
        turn = runtime_control.get_runtime_turn(connection, turn_id)
    finally:
        connection.close()
    assert turn is not None
    return turn


def test_real_process_restart_records_pid_birth_and_persists_recovery_required(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime-control.db"
    first = _runtime(path)
    handle = first.start(_intent(tmp_path, "prepare-root"))
    try:
        assert handle.process.poll() is None
        live_identity = _process_identity(handle.process.pid)
        assert live_identity is not None

        running = _turn(path, "prepare-root")
        assert (running.status, running.process_id, running.process_birth_time) == (
            "running",
            *live_identity,
        )

        _terminate(handle.process)

        restarted = _runtime(path)
        admission = restarted.reconcile_actor(ACTOR_ID, ATTEMPT_ID)
        assert (admission.disposition, admission.parent_turn_id) == (
            "recovery_required",
            "prepare-root",
        )
        persisted = _turn(path, "prepare-root")
        assert persisted.status == "unknown"
        assert persisted.failure_code in {"PROCESS_DISAPPEARED", "PROCESS_IDENTITY_REUSED"}

        repeated = _runtime(path).reconcile_actor(ACTOR_ID, ATTEMPT_ID)
        assert (repeated.disposition, repeated.parent_turn_id) == (
            "recovery_required",
            "prepare-root",
        )
        assert _turn(path, "prepare-root").status == "unknown"
    finally:
        _terminate(handle.process)
        first.close_local("prepare-root")


def test_real_post_submit_process_restart_is_receipt_only_on_every_reconcile(
    tmp_path: Path,
) -> None:
    path = tmp_path / "submit-runtime-control.db"
    first = _runtime(path)
    handle = first.start(_intent(tmp_path, "submit-root", submit_started=True))
    try:
        assert handle.process.poll() is None
        assert _turn(path, "submit-root").submit_started == 1

        _terminate(handle.process)

        first_restart = _runtime(path).reconcile_actor(ACTOR_ID, ATTEMPT_ID)
        second_restart = _runtime(path).reconcile_actor(ACTOR_ID, ATTEMPT_ID)
        assert (first_restart.disposition, first_restart.parent_turn_id) == (
            "receipt_only",
            "submit-root",
        )
        assert second_restart == first_restart

        persisted = _turn(path, "submit-root")
        assert (persisted.status, persisted.submit_started) == ("unknown", 1)
        assert persisted.failure_code in {"PROCESS_DISAPPEARED", "PROCESS_IDENTITY_REUSED"}
    finally:
        _terminate(handle.process)
        first.close_local("submit-root")
