"""Tests for browser_batch."""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from applypilot.apply.browser_batch import BrowserBatch, BrowserBatchError, claim_worker_bridges
from applypilot.apply.visual_bridge import write_host_metadata


@pytest.fixture
def workspace(tmp_path: Path):
    manifest_path = tmp_path / "manifest.json"
    output_dir = tmp_path / "output"

    bridge_dir1 = tmp_path / "bridge1"
    bridge_dir2 = tmp_path / "bridge2"
    bridge_dir3 = tmp_path / "bridge3"
    bridge_dir4 = tmp_path / "bridge4"
    task_file = tmp_path / "task.txt"
    task_file.write_text("dummy task")

    # Write active host metadata
    for bd, url in [(bridge_dir1, "https://example.com"),
                    (bridge_dir2, "https://example.com"),
                    (bridge_dir3, "https://other.com"),
                    (bridge_dir4, "https://other.com")]:
        bd.mkdir(parents=True, exist_ok=True)
        write_host_metadata(
            bd,
            session_id=f"session_{bd.name}",
            token_epoch=f"epoch_{bd.name}",
            surface="browser",
            phase="prepare",
            target={"runtime": "iab", "tab_id": f"tab_{bd.name}", "application_url": url},
        )

    return tmp_path, manifest_path, output_dir, task_file

@patch("applypilot.apply.browser_batch._read_task")
@patch("applypilot.apply.browser_batch.read_active_host")
def test_invalid_manifest_validation(mock_read_active, mock_read_task, workspace):
    _tmp_path, manifest_path, output_dir, _ = workspace

    # test max_workers type validation
    manifest_path.write_text(json.dumps({"jobs": [{"job_id": "j1"}], "max_workers": True}))
    with pytest.raises(BrowserBatchError, match="max_workers must be an int within 1..4"):
        BrowserBatch(manifest_path, output_dir)

    # test output dir existence validation
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "foo.txt").write_text("bar")
    manifest_path.write_text(json.dumps({
        "jobs": [{"job_id": "j1", "bridge_dir": "bridge1", "task_file": "task.txt"}]
    }))
    with pytest.raises(BrowserBatchError, match="Output directory must be empty or not exist."):
        BrowserBatch(manifest_path, output_dir)
    (output_dir / "foo.txt").unlink()

    # test invalid job_id
    manifest_path.write_text(json.dumps({
        "jobs": [{"job_id": "CON", "bridge_dir": "bridge1", "task_file": "task.txt"}]
    }))
    with pytest.raises(BrowserBatchError, match="Invalid job_id: CON"):
        BrowserBatch(manifest_path, output_dir)

    # test duplicate identity
    manifest_path.write_text(json.dumps({
        "jobs": [
            {"job_id": "j1", "bridge_dir": "bridge1", "task_file": "task.txt"},
            {"job_id": "j2", "bridge_dir": "bridge2", "task_file": "task.txt"}
        ]
    }))
    mock_read_active.return_value = MagicMock(
        phase="prepare", surface="browser", session_id="s1", token_epoch="t1", target={"runtime": "iab", "tab_id": "tab1", "application_url": "https://example.com"}
    )
    with pytest.raises(BrowserBatchError, match="Duplicate host identity"):
        BrowserBatch(manifest_path, output_dir)

    # test duplicate bridge
    manifest_path.write_text(json.dumps({
        "jobs": [
            {"job_id": "j1", "bridge_dir": "bridge1", "task_file": "task.txt"},
            {"job_id": "j2", "bridge_dir": "bridge1", "task_file": "task.txt"}
        ]
    }))
    with pytest.raises(BrowserBatchError, match="Duplicate bridge dir"):
        BrowserBatch(manifest_path, output_dir)


@patch("applypilot.apply.browser_batch.subprocess.Popen")
@patch("applypilot.apply.browser_worker._kill_process_tree", return_value=True)
@patch("applypilot.apply.browser_batch.get_available_memory_mb")
def test_concurrency_and_origin_cap(mock_mem, mock_kill, mock_popen, workspace):
    mock_mem.return_value = 4096.0
    _tmp_path, manifest_path, output_dir, _ = workspace

    manifest_path.write_text(json.dumps({
        "max_workers": 3,
        "per_origin_limit": 1,
        "timeout_seconds": 60,
        "jobs": [
            {"job_id": "j1", "bridge_dir": "bridge1", "task_file": "task.txt"},
            {"job_id": "j2", "bridge_dir": "bridge2", "task_file": "task.txt"},
            {"job_id": "j3", "bridge_dir": "bridge3", "task_file": "task.txt"},
            {"job_id": "j4", "bridge_dir": "bridge4", "task_file": "task.txt"},
        ]
    }))

    batch = BrowserBatch(manifest_path, output_dir)

    running_procs = []
    def popen_side_effect(*args, **kwargs):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = len(running_procs) + 1000
        running_procs.append(mock_proc)
        return mock_proc

    mock_popen.side_effect = popen_side_effect

    with patch("applypilot.apply.browser_batch.time.sleep", side_effect=InterruptedError):
        try:
            batch.run()
        except InterruptedError:
            pass

    assert len(running_procs) == 2
    started_jobs = {jid for jid, state in batch.states.items() if state.status == "cancelled"}
    assert "j1" in started_jobs
    assert "j3" in started_jobs


def test_claim_worker_bridges(workspace):
    tmp_path, _, _, _ = workspace
    bridge1 = tmp_path / "bridge1"

    with claim_worker_bridges([bridge1]) as leases:
        assert bridge1 in leases
        assert (bridge1 / ".batch_lease").exists()
        token = (bridge1 / ".batch_lease").read_text()
        assert token == leases[bridge1]

        # Test exclusivity
        with pytest.raises(BrowserBatchError, match="is already leased"), claim_worker_bridges([bridge1]):
            pass

    assert not (bridge1 / ".batch_lease").exists()


@patch("applypilot.apply.browser_batch.subprocess.Popen")
@patch("applypilot.apply.browser_worker._kill_process_tree", return_value=True)
@patch("applypilot.apply.browser_batch.get_available_memory_mb")
def test_failures_independent_and_cleanup(mock_mem, mock_kill, mock_popen, workspace):
    mock_mem.return_value = 4096.0
    _tmp_path, manifest_path, output_dir, _ = workspace
    manifest_path.write_text(json.dumps({
        "jobs": [
            {"job_id": "j1", "bridge_dir": "bridge1", "task_file": "task.txt"},
            {"job_id": "j3", "bridge_dir": "bridge3", "task_file": "task.txt"},
        ]
    }))
    batch = BrowserBatch(manifest_path, output_dir)

    mock_proc1 = MagicMock()
    mock_proc1.poll.side_effect = [124, 124, 124]
    mock_proc1.pid = 1001

    mock_proc2 = MagicMock()
    mock_proc2.poll.side_effect = [0, 0, 0]
    mock_proc2.pid = 1002

    mock_popen.side_effect = [mock_proc1, mock_proc2]

    with patch("applypilot.apply.browser_batch.time.sleep", side_effect=[None, InterruptedError]):
        try:
            batch.run()
        except InterruptedError:
            pass

    assert batch.states["j1"].status == "timed_out"
    assert batch.states["j3"].status == "completed"

@patch("applypilot.apply.browser_batch.get_available_memory_mb")
def test_memory_pause(mock_mem, workspace):
    mock_mem.return_value = 500.0 # Less than 1024
    _tmp_path, manifest_path, output_dir, _ = workspace
    manifest_path.write_text(json.dumps({
        "timeout_seconds": 1.0,
        "jobs": [
            {"job_id": "j1", "bridge_dir": "bridge1", "task_file": "task.txt"},
        ]
    }))
    batch = BrowserBatch(manifest_path, output_dir, min_ram_mb=1024.0)

    batch.states["j1"].queued_at = time.time() - 2.0

    with patch("applypilot.apply.browser_batch.time.sleep", side_effect=[InterruptedError]):
        try:
            batch.run()
        except InterruptedError:
            pass

    assert batch.states["j1"].status == "cancelled"
    assert "memory_pause" in batch.states["j1"].outcome
