"""Exercise the scheduler with real child processes, without models or browsers."""
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from applypilot.apply import browser_batch as module
from applypilot.apply.visual_bridge import write_host_metadata


def test_interruption_after_spawn_keeps_child_tracked(tmp_path, monkeypatch):
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    write_host_metadata(bridge_dir, session_id="s", token_epoch="t", surface="browser", phase="prepare",
                        target={"runtime": "iab", "tab_id": "t", "application_url": "https://example.test/job"})
    (tmp_path / "goal.txt").write_text("Observe only.")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [{"job_id": "j", "bridge_dir": "bridge", "task_file": "goal.txt"}]}))
    real_popen = subprocess.Popen
    children = []

    def launch(_command, **kwargs):
        if _command[:3] != [sys.executable, "-m", "applypilot.apply.browser_worker"]:
            return real_popen(_command, **kwargs)
        child = real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(child)
        return child

    @contextmanager
    def interrupted_spawn():
        yield
        raise KeyboardInterrupt

    monkeypatch.setattr(module.subprocess, "Popen", launch)
    monkeypatch.setattr(module, "_defer_spawn_signals", interrupted_spawn)
    monkeypatch.setattr(module, "get_available_memory_mb", lambda: 4096)
    batch = module.BrowserBatch(manifest, tmp_path / "output")
    try:
        with pytest.raises(KeyboardInterrupt):
            batch.run()
        assert len(children) == 1 and children[0].poll() is not None
        assert batch.states["j"].status == "cancelled"
        assert not (bridge_dir / ".batch_lease").exists()
    finally:
        for child in children:
            module.stop_batch_worker(child)


def test_real_process_queue_cap_failure_isolation_and_cleanup(tmp_path, monkeypatch):
    jobs = []
    for index in range(6):
        root = tmp_path / str(index)
        root.mkdir()
        write_host_metadata(root, session_id=f"s{index}", token_epoch=f"t{index}",
                            surface="browser", phase="prepare",
                            target={"runtime": "iab", "tab_id": str(index),
                                    "application_url": f"https://company{index % 3}.test/job/{index}"})
        (root / "goal.txt").write_text("synthetic preparation only")
        jobs.append({"job_id": f"j{index}", "bridge_dir": str(root), "task_file": str(root / "goal.txt")})
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": jobs, "max_workers": 4, "per_origin_limit": 1}))
    real_popen = subprocess.Popen
    children = []

    def launch(command, **kwargs):
        assert command[:3] == [sys.executable, "-m", "applypilot.apply.browser_worker"]
        index = len(children)
        child = real_popen([sys.executable, "-c",
                           f"import time,sys; time.sleep(0.3); sys.exit({7 if index == 1 else 0})"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(module.subprocess, "Popen", launch)
    monkeypatch.setattr(module, "get_available_memory_mb", lambda: 4096)
    batch = module.BrowserBatch(manifest, tmp_path / "output")
    assert batch.run() is False
    assert all(child.poll() is not None for child in children)
    assert len(children) == 6
    assert batch.highwater == 3  # Three distinct origins, each capped at one.
    assert sum(s.status == "completed" for s in batch.states.values()) == 5
    assert sum(s.status == "failed" for s in batch.states.values()) == 1
    for first, second in [("j0", "j3"), ("j1", "j4"), ("j2", "j5")]:
        assert batch.states[first].ended_at <= batch.states[second].started_at
    assert not list(tmp_path.glob("*/.batch_lease"))
    report = json.loads(batch.status_path.read_text())
    assert report["counts"]["running"] == report["counts"]["queued"] == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group and SIGTERM contract")
@pytest.mark.parametrize("stop", ["cancel", "timeout", "during_spawn"])
def test_worker_reaps_isolated_cli_and_grandchild(tmp_path, stop):
    """Exercise the real worker, its isolated CLI tree and batch cancellation."""
    (tmp_path / "goal.txt").write_text("Observe only.")
    child_script = tmp_path / "dummy_cli.py"
    child_script.write_text(
        "import os,sys,subprocess,time,json\n"
        "from pathlib import Path\n"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        "Path(sys.argv[1]).write_text(json.dumps([os.getpid(),p.pid]))\n"
        "time.sleep(60)\n"
    )
    pid_file = tmp_path / "pids.json"
    spawn_hook = (
        "real_popen=w.subprocess.Popen\n"
        "def launch(*a,**kw):\n"
        " p=real_popen(*a,**kw)\n"
        " deadline=time.monotonic()+5\n"
        f" while not Path({str(pid_file)!r}).exists() and time.monotonic()<deadline: time.sleep(0.01)\n"
        " os.kill(os.getpid(),signal.SIGTERM)\n"
        " return p\n"
        "w.subprocess.Popen=launch\n"
    ) if stop == "during_spawn" else ""
    code = (
        "import sys,os,signal,time\nfrom pathlib import Path\n"
        "from applypilot.apply import browser_worker as w\n"
        + spawn_hook +
        f"w.build_browser_worker_command=lambda **kw: [sys.executable,{str(child_script)!r},{str(pid_file)!r}]\n"
        f"raise SystemExit(w.run_browser_worker(bridge_dir=Path({str(tmp_path)!r}),"
        f"task_file=Path({str(tmp_path / 'goal.txt')!r}),phase='prepare',"
        f"timeout_seconds={2 if stop == 'timeout' else 30}))\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(module.__file__).resolve().parents[2])
    wrapper = subprocess.Popen([sys.executable, "-c", code], env=env, start_new_session=True)
    pids = []

    def alive(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        stat = Path(f"/proc/{pid}/stat")
        # A killed grandchild may await PID 1 reaping in a CI container.
        return not (stat.exists() and stat.read_text().split(") ", 1)[1].startswith("Z"))

    try:
        if stop == "during_spawn":
            wrapper.wait(timeout=10)
            assert wrapper.returncode == 143
            assert not (tmp_path / ".worker-owner").exists()
            assert pid_file.exists()
            pids = json.loads(pid_file.read_text())
            deadline = time.monotonic() + 3
            while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
                time.sleep(0.02)
            assert not any(alive(pid) for pid in pids)
            return
        deadline = time.monotonic() + 10
        while not pid_file.exists() and wrapper.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_file.exists(), "Dummy CLI never started"
        pids = json.loads(pid_file.read_text())
        assert all(alive(pid) for pid in pids)
        if stop == "cancel":
            module.stop_batch_worker(wrapper)
        wrapper.wait(timeout=10)
        assert wrapper.returncode == (143 if stop == "cancel" else 124)
        deadline = time.monotonic() + 3
        while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not any(alive(pid) for pid in pids)
        assert not (tmp_path / ".worker-owner").exists()
    finally:
        module.stop_batch_worker(wrapper)
        for pid in pids:
            if alive(pid):
                os.kill(pid, signal.SIGKILL)
