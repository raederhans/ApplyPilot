from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from applypilot.apply import browser_worker
from applypilot.apply.visual_bridge import HostBinding, VisualBridgeError


def _binding(tmp_path: Path, *, phase: str = "prepare", **target) -> HostBinding:
    return HostBinding(
        root=tmp_path,
        session_id="session-1",
        token_epoch="epoch-1",
        surface="browser",
        phase=phase,
        target={
            "runtime": "iab",
            "tab_id": "tab-7",
            "application_url": "https://jobs.example.test/opening/42",
            **target,
        },
    )


def test_build_command_binds_only_visual_tool_and_requested_phase(monkeypatch, tmp_path):
    monkeypatch.setattr(browser_worker, "read_active_host", lambda _root: _binding(tmp_path, phase="discovery"))
    monkeypatch.setattr(browser_worker, "resolve_codex_command", lambda: ["codex-native"])

    command = browser_worker.build_browser_worker_command(
        bridge_dir=tmp_path,
        phase="discovery",
        model="gpt-test",
        python_executable="python-test",
    )

    joined = " ".join(command)
    assert command[:2] == ["codex-native", "exec"]
    assert "--ignore-user-config" in command
    assert "--sandbox read-only" in joined
    assert 'enabled_tools=["visual_operation"]' in joined
    assert "applypilot.apply.visual_bridge_mcp" in joined
    assert "skills.max_context_tokens=1" in command
    assert "features.plugins=false" in command
    assert "features.apps=false" in command
    assert "features.multi_agent=false" in command
    assert "agents.enabled=false" in command
    assert "playwright" not in joined.casefold()
    assert "computer_use" not in joined.casefold()


@pytest.mark.parametrize(
    ("binding", "phase", "message"),
    [
        (lambda root: _binding(root, phase="prepare"), "discovery", "not requested phase"),
        (
            lambda root: HostBinding(root, "s", "e", "computer_use", "prepare", _binding(root).target),
            "prepare",
            "browser visual host",
        ),
        (lambda root: _binding(root, runtime="edge"), "prepare", "in-app-browser"),
        (lambda root: _binding(root, tab_id=""), "prepare", "tab_id"),
        (lambda root: _binding(root, application_url="file:///private"), "prepare", "application_url"),
    ],
)
def test_build_command_rejects_wrong_or_unbound_host(monkeypatch, tmp_path, binding, phase, message):
    monkeypatch.setattr(browser_worker, "read_active_host", lambda _root: binding(tmp_path))

    with pytest.raises(VisualBridgeError, match=message):
        browser_worker.build_browser_worker_command(bridge_dir=tmp_path, phase=phase, model="gpt-test")


def test_run_uses_goal_directed_bounded_prompt_and_returns_real_exit(monkeypatch, tmp_path):
    task_file = tmp_path / "goal.txt"
    task_file.write_text("Find roles posted in the last 8 hours and inspect the best match.", encoding="utf-8")
    monkeypatch.setattr(browser_worker, "read_active_host", lambda _root: _binding(tmp_path, phase="discovery"))
    monkeypatch.setattr(browser_worker, "resolve_codex_command", lambda: ["codex-native"])
    observed = {}

    class Process:
        returncode = 7

        def __init__(self, command, **kwargs):
            observed["command"] = command
            observed["kwargs"] = kwargs

        def communicate(self, *, input, timeout):
            observed["prompt"] = input
            observed["timeout"] = timeout

    monkeypatch.setattr(browser_worker.subprocess, "Popen", Process)

    result = browser_worker.run_browser_worker(
        bridge_dir=tmp_path,
        task_file=task_file,
        phase="discovery",
        timeout_seconds=33,
        model="gpt-test",
        environment={"PYTHONPATH": "existing"},
    )

    assert result == 7
    assert observed["timeout"] == 33
    prompt = observed["prompt"]
    assert 'Goal JSON string: "Find roles posted in the last 8 hours' in prompt
    assert "auth_required" in prompt
    assert "Prefer a guest path" in prompt
    assert "Do not make a final submission" in prompt
    assert "Never infer an 8-hour result from a 24-hour filter" in prompt
    assert "Only one actor may write" in prompt
    assert "exact web link present in the current DOM observation" in prompt
    assert "Do not retry after a click or navigation" in prompt
    assert len(prompt) < 4_500
    env = observed["kwargs"]["env"]
    assert env["APPLYPILOT_VISUAL_BRIDGE_TIMEOUT_SECONDS"] == "33"
    assert env["PYTHONPATH"].endswith("existing")


def test_run_stops_timed_out_process_and_reports_exit_124(monkeypatch, tmp_path, capsys):
    task_file = tmp_path / "goal.txt"
    task_file.write_text("Inspect the current application page.", encoding="utf-8")
    monkeypatch.setattr(browser_worker, "read_active_host", lambda _root: _binding(tmp_path))
    monkeypatch.setattr(browser_worker, "resolve_codex_command", lambda: ["codex-native"])

    class Process:
        returncode = None
        terminated = False
        pid = 1234

        def __init__(self, _command, **_kwargs):
            pass

        def communicate(self, *, input, timeout):
            raise subprocess.TimeoutExpired("codex", timeout)

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    process = Process([], stdin=None)
    monkeypatch.setattr(browser_worker.subprocess, "Popen", lambda *_args, **_kwargs: process)
    killed = []
    monkeypatch.setattr(browser_worker, "_kill_process_tree", lambda pid: killed.append(pid) or True)

    result = browser_worker.run_browser_worker(
        bridge_dir=tmp_path,
        task_file=task_file,
        phase="prepare",
        timeout_seconds=2,
        model="gpt-test",
    )

    assert result == browser_worker.TIMEOUT_EXIT_CODE
    assert killed == [1234]
    assert process.terminated is False
    assert "timed out after 2 seconds" in capsys.readouterr().err


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), 0, -1])
def test_run_rejects_non_finite_or_non_positive_timeout(tmp_path, timeout):
    with pytest.raises(ValueError, match="positive"):
        browser_worker.run_browser_worker(
            bridge_dir=tmp_path,
            task_file=tmp_path / "unused.txt",
            phase="prepare",
            timeout_seconds=timeout,
        )


def test_task_file_is_bounded(tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text(" ", encoding="utf-8")
    with pytest.raises(ValueError, match="non-empty"):
        browser_worker._read_task(empty)

    large = tmp_path / "large.txt"
    large.write_text("x" * (browser_worker.MAX_TASK_CHARACTERS + 1), encoding="utf-8")
    with pytest.raises(ValueError, match="exceeds"):
        browser_worker._read_task(large)


@pytest.mark.parametrize("authorization", [None, False, "true", 1])
def test_submit_requires_explicit_boolean_host_authorization(monkeypatch, tmp_path, authorization):
    fields = vars(_binding(tmp_path, phase="submit")) | {"submission_authorized": authorization}
    monkeypatch.setattr(browser_worker, "read_active_host", lambda _root: SimpleNamespace(**fields))
    with pytest.raises(VisualBridgeError, match="submission_authorized=true"):
        browser_worker.build_browser_worker_command(bridge_dir=tmp_path, phase="submit", model="gpt-test")


def test_submit_accepts_authorized_binding_but_prepare_cannot_escalate(monkeypatch, tmp_path):
    fields = vars(_binding(tmp_path, phase="submit")) | {"submission_authorized": True}
    monkeypatch.setattr(browser_worker, "read_active_host", lambda _root: SimpleNamespace(**fields))
    monkeypatch.setattr(browser_worker, "resolve_codex_command", lambda: ["codex-native"])
    assert browser_worker.build_browser_worker_command(bridge_dir=tmp_path, phase="submit", model="gpt-test")
    fields["phase"] = "prepare"
    with pytest.raises(VisualBridgeError, match="not requested phase"):
        browser_worker.build_browser_worker_command(bridge_dir=tmp_path, phase="submit", model="gpt-test")


@pytest.mark.parametrize("phase", ["discovery", "prepare", "submit"])
def test_prompt_keeps_auth_handoff_and_missing_facts_local_to_one_job(phase):
    prompt = browser_worker._worker_prompt(task="Apply to this exact job.", phase=phase)
    assert "identified account" in prompt
    assert "Never put passwords or OTPs in type_text" in prompt
    assert "matching current employer's email OTP" in prompt
    assert "coordinator to continue other jobs" in prompt
    assert "only when exposed by the host" in prompt
    if phase == "submit":
        assert "Submit once" in prompt
        assert "submission_uncertain" in prompt
    else:
        assert "Do not make a final submission" in prompt
def test_worker_lease_excludes_duplicate_and_foreign_batch(tmp_path):
    from applypilot.apply.browser_worker import _worker_lease
    from applypilot.apply.visual_bridge import VisualBridgeError
    with _worker_lease(tmp_path, {}):
        with pytest.raises(VisualBridgeError, match="already has a worker"):
            with _worker_lease(tmp_path, {}):
                pass
    assert not (tmp_path / ".worker-owner").exists()
    (tmp_path / ".batch_lease").write_text("parent-token", encoding="utf-8")
    with pytest.raises(VisualBridgeError, match="reserved"):
        with _worker_lease(tmp_path, {}):
            pass
    with _worker_lease(tmp_path, {"APPLYPILOT_BROWSER_BATCH_LEASE": "parent-token"}):
        assert (tmp_path / ".worker-owner").exists()
