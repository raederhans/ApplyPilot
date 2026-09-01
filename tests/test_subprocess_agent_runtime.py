from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from applypilot.apply.agent_runtime import (
    RuntimeContinuityError,
    SubprocessAgentRuntime,
    SubprocessLaunchSpec,
    SubprocessRuntimeAdapter,
)


def _spec(
    tmp_path: Path,
    run_id: str,
    *,
    parent_run_id: str | None = None,
    runtime_id: str = "codex:edge:cdp:9222",
    profile_id: str = "edge:worker:0",
    submit_started: bool = False,
) -> SubprocessLaunchSpec:
    return SubprocessLaunchSpec(
        run_id=run_id,
        attempt_id="attempt-1",
        actor_id="application:attempt-1",
        turn_id=run_id,
        command=(
            sys.executable,
            "-c",
            "import json,sys; print(json.dumps({'prompt': sys.stdin.read()}))",
        ),
        prompt=f"prompt:{run_id}",
        cwd=tmp_path,
        env=os.environ.copy(),
        runtime_id=runtime_id,
        profile_id=profile_id,
        parent_run_id=parent_run_id,
        submit_started=submit_started,
    )


def test_real_subprocess_adapter_start_resume_health_and_close(tmp_path: Path) -> None:
    runtime = SubprocessAgentRuntime(
        kill_process_tree=lambda pid: os.kill(pid, 15)
    )
    assert isinstance(runtime, SubprocessRuntimeAdapter)
    root = runtime.start(_spec(tmp_path, "turn-1"))
    assert root.stdout is not None
    assert root.stdout.read().strip() == '{"prompt": "prompt:turn-1"}'
    root.wait(timeout=5)
    assert runtime.health("turn-1").status == "completed"

    child = runtime.resume(
        "turn-1",
        _spec(tmp_path, "turn-2", parent_run_id="turn-1"),
    )
    assert child.stdout is not None
    assert child.stdout.read().strip() == '{"prompt": "prompt:turn-2"}'
    child.wait(timeout=5)
    assert runtime.health("turn-2").returncode == 0

    runtime.close("turn-1")
    runtime.close("turn-2")
    assert runtime.health("turn-1").status == "closed"
    assert runtime.health("turn-2").status == "closed"


def test_subprocess_resume_rejects_runtime_or_profile_switch_after_submit_started(
    tmp_path: Path,
) -> None:
    runtime = SubprocessAgentRuntime(
        kill_process_tree=lambda pid: os.kill(pid, 15)
    )
    root = runtime.start(_spec(tmp_path, "turn-1"))
    assert root.stdin is None
    output, stderr = root.communicate(timeout=5)
    assert stderr is None
    assert output.strip() == '{"prompt": "prompt:turn-1"}'

    with pytest.raises(RuntimeContinuityError, match="submit_started"):
        runtime.resume(
            "turn-1",
            _spec(
                tmp_path,
                "turn-2",
                parent_run_id="turn-1",
                runtime_id="claude:cloak:cdp:9333",
                profile_id="cloak:worker:0",
                submit_started=True,
            ),
        )
    runtime.close()


def test_subprocess_start_survives_shared_popen_monkeypatch_and_detaches_real_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_popen = subprocess.Popen
    prompt = _spec(tmp_path, "turn-monkeypatch")

    class FakeStdin:
        closed = False

        def write(self, value: str) -> int:
            return len(value)

        def close(self) -> None:
            self.closed = True

    class FakeProcess:
        pid = 42
        stdin = FakeStdin()
        stdout = None

        def poll(self) -> int:
            return 0

    fake_process = FakeProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: fake_process)

    fake_runtime = SubprocessAgentRuntime(
        kill_process_tree=lambda _pid: None,
        popen_factory=lambda *_args, **_kwargs: fake_process,  # type: ignore[arg-type]
    )
    assert fake_runtime.start(prompt) is fake_process  # type: ignore[comparison-overlap]
    assert fake_process.stdin.closed

    real_runtime = SubprocessAgentRuntime(
        kill_process_tree=lambda pid: os.kill(pid, 15),
        popen_factory=real_popen,
    )
    process = real_runtime.start(_spec(tmp_path, "turn-real-popen"))
    assert process.stdin is None
    output, stderr = process.communicate(timeout=5)
    assert stderr is None
    assert output.strip() == '{"prompt": "prompt:turn-real-popen"}'


def test_subprocess_cancel_targets_only_owned_run(tmp_path: Path) -> None:
    processes: dict[int, subprocess.Popen[str]] = {}

    def kill(pid: int) -> None:
        processes[pid].terminate()

    runtime = SubprocessAgentRuntime(kill_process_tree=kill)
    spec = SubprocessLaunchSpec(
        run_id="turn-cancel",
        attempt_id="attempt-1",
        actor_id="application:attempt-1",
        turn_id="turn-cancel",
        command=(sys.executable, "-c", "import time; time.sleep(30)"),
        prompt="cancel-me",
        cwd=tmp_path,
        env=os.environ.copy(),
        runtime_id="codex:edge:cdp:9222",
        profile_id="edge:worker:0",
    )
    process = runtime.start(spec)
    processes[process.pid] = process

    runtime.cancel("turn-cancel")
    process.wait(timeout=5)

    assert runtime.health("turn-cancel").status == "cancelled"
    runtime.close("turn-cancel")
