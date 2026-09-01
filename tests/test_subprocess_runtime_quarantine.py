"""Regression tests for unsafe subprocess-start cleanup boundaries.

These tests use only fake processes.  A launcher must never lose ownership of
a child after a BaseException while binding it, recording time, or delivering
the prompt: it either proves the child stopped or retains a quarantined local
handle that cannot be reported as safely closed.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from applypilot.apply.agent_runtime import (
    SubprocessAgentRuntime,
    SubprocessLaunchSpec,
    SubprocessRuntimeError,
)


class _InjectedBaseException(BaseException):
    """Models process-abort paths that do not inherit from ``Exception``."""


@dataclass
class _FakeStdin:
    failure_point: str | None
    events: list[str]
    closed: bool = False

    def write(self, value: str) -> int:
        self.events.append("stdin.write")
        if self.failure_point == "stdin.write":
            raise _InjectedBaseException("stdin write interrupted")
        return len(value)

    def close(self) -> None:
        self.events.append("stdin.close")
        if self.failure_point == "stdin.close":
            raise _InjectedBaseException("stdin close interrupted")
        self.closed = True


class _FakeStdout:
    def read(self) -> str:
        return ""


@dataclass
class _FakeProcess:
    pid: int
    failure_point: str | None
    cleanup_mode: str
    events: list[str]
    returncode: int | None = None
    stdin: _FakeStdin = field(init=False)
    stdout: _FakeStdout = field(default_factory=_FakeStdout)
    wait_calls: list[float | None] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.stdin = _FakeStdin(self.failure_point, self.events)

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        self.events.append("wait")
        if self.cleanup_mode == "wait_timeout":
            raise subprocess.TimeoutExpired("fake-agent", timeout)
        if self.returncode is None:
            raise AssertionError("wait called before a successful fake termination")
        return self.returncode


def _spec(tmp_path: Path, turn_id: str) -> SubprocessLaunchSpec:
    return SubprocessLaunchSpec(
        run_id=turn_id,
        attempt_id="attempt-1",
        actor_id="application:attempt-1",
        turn_id=turn_id,
        command=("fake-agent", "--json"),
        prompt="prompt",
        cwd=tmp_path,
        env={},
        runtime_id="codex:edge:cdp:9222",
        profile_id="edge:worker:0",
    )


def _runtime_for(
    process: _FakeProcess,
    *,
    failure_point: str,
) -> SubprocessAgentRuntime:
    def kill(pid: int) -> None:
        assert pid == process.pid
        process.events.append("kill")
        if process.cleanup_mode == "kill_failure":
            raise OSError("synthetic kill failure")
        if process.cleanup_mode == "terminated":
            process.returncode = -15

    def clock() -> float:
        if failure_point == "clock":
            raise _InjectedBaseException("clock interrupted")
        return 1.0

    return SubprocessAgentRuntime(kill_process_tree=kill, clock=clock)


@pytest.mark.parametrize(
    "failure_point",
    ("on_spawned", "clock", "stdin.write", "stdin.close"),
)
def test_base_exception_at_each_spawn_boundary_stops_the_exact_child_when_cleanup_works(
    tmp_path: Path,
    failure_point: str,
) -> None:
    """Every BaseException boundary must attempt to stop the exact Popen child."""
    events: list[str] = []
    process = _FakeProcess(
        pid=8100,
        failure_point=failure_point,
        cleanup_mode="terminated",
        events=events,
    )
    runtime = _runtime_for(process, failure_point=failure_point)

    def popen(*_args: object, **_kwargs: object) -> _FakeProcess:
        events.append("popen")
        return process

    def on_spawned(_process: object) -> None:
        events.append("on_spawned")
        if failure_point == "on_spawned":
            raise _InjectedBaseException("spawn callback interrupted")

    with pytest.raises(_InjectedBaseException):
        runtime.start(
            _spec(tmp_path, f"turn-{failure_point}"),
            popen_factory=popen,  # type: ignore[arg-type]
            on_spawned=on_spawned,
        )

    assert process.poll() == -15
    assert "kill" in events
    assert process.wait_calls == [5]


@pytest.mark.parametrize(
    "failure_point",
    ("on_spawned", "clock", "stdin.write", "stdin.close"),
)
@pytest.mark.parametrize("cleanup_mode", ("kill_failure", "wait_timeout"))
def test_unconfirmed_child_is_quarantined_not_reported_closed(
    tmp_path: Path,
    failure_point: str,
    cleanup_mode: str,
) -> None:
    """Failed cleanup must retain a truthful local quarantine for the live child."""
    events: list[str] = []
    process = _FakeProcess(
        pid=8200,
        failure_point=failure_point,
        cleanup_mode=cleanup_mode,
        events=events,
    )
    runtime = _runtime_for(process, failure_point=failure_point)
    turn_id = f"turn-{failure_point}-{cleanup_mode}"

    def popen(*_args: object, **_kwargs: object) -> _FakeProcess:
        events.append("popen")
        return process

    def on_spawned(_process: object) -> None:
        events.append("on_spawned")
        if failure_point == "on_spawned":
            raise _InjectedBaseException("spawn callback interrupted")

    with pytest.raises(_InjectedBaseException):
        runtime.start(
            _spec(tmp_path, turn_id),
            popen_factory=popen,  # type: ignore[arg-type]
            on_spawned=on_spawned,
        )

    assert process.poll() is None
    assert "kill" in events
    if cleanup_mode == "wait_timeout":
        assert process.wait_calls == [5]

    # Retrying close may itself surface the cleanup failure, but it must never
    # change an unconfirmed live child to the terminal-looking ``closed`` state.
    with pytest.raises(SubprocessRuntimeError, match="could not prove subprocess termination"):
        runtime.close(turn_id)
    health = runtime.health(turn_id)
    assert health.pid == process.pid
    assert health.returncode is None
    assert health.status == "quarantined"
