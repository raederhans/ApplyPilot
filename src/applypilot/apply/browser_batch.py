"""Bounded concurrent attended IAB prepare workers."""

from __future__ import annotations

import contextlib
import ctypes
import json
import math
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from applypilot.apply.browser_worker import (
    _defer_spawn_signals,
    _read_task,
    _stop_process,
    _termination_guard,
    _validate_browser_host,
)
from applypilot.apply.visual_bridge import VisualBridgeError, read_active_host

JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
RESERVED_NAMES = frozenset({"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)})

@dataclass
class JobConfig:
    job_id: str
    bridge_dir: Path
    task_file: Path
    origin: str = ""
    session_id: str = ""
    token_epoch: str = ""
    target: dict = field(default_factory=dict)


@dataclass
class JobState:
    job_id: str
    status: str = "queued"
    queued_at: float = field(default_factory=time.time)
    started_at: float | None = None
    ended_at: float | None = None
    exit_code: int | None = None
    outcome: str | None = None


class BrowserBatchError(Exception):
    pass


def get_available_memory_mb() -> float | None:
    system = platform.system()
    if system == "Windows":
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return stat.ullAvailPhys / (1024 * 1024)
        except (OSError, AttributeError):
            return None
        return None
    elif system == "Linux":
        try:
            with open("/proc/meminfo", "r") as f:
                meminfo = {}
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        meminfo[parts[0].strip()] = parts[1].strip()
            if "MemAvailable" in meminfo:
                return float(meminfo["MemAvailable"].split()[0]) / 1024
            if "MemFree" in meminfo:
                return float(meminfo["MemFree"].split()[0]) / 1024
        except (OSError, ValueError, IndexError):
            return None
    return None


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except Exception:
        temp.unlink(missing_ok=True)
        raise

@contextlib.contextmanager
def claim_worker_bridges(paths: list[Path]):
    """Claim exclusive bridge leases for batch scheduling.

    Protocol:
    - Creates a '.batch_lease' file in each bridge root using O_EXCL.
    - Writes a unique UUID token to each lease file.
    - Yields a dict mapping bridge Path to its lease token string.
    - The caller passes this token to the worker via APPLYPILOT_BROWSER_BATCH_LEASE.
    - The worker will validate the file token matches its environment.
    - On exit, all lease files created by this context are removed.
    """
    leases = {}
    cleanup = []
    try:
        for p in paths:
            token = str(uuid.uuid4())
            lease_path = p / ".batch_lease"
            try:
                fd = os.open(lease_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                raise BrowserBatchError(f"Bridge {p} is already leased.")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(token)
            cleanup.append(lease_path)
            if (p / ".worker-owner").exists():
                raise BrowserBatchError(f"Bridge {p} already has a worker.")
            leases[p] = token
        yield leases
    finally:
        for lease_path in cleanup:
            lease_path.unlink(missing_ok=True)

class BrowserBatch:
    def __init__(self, manifest_path: Path, output_dir: Path, min_ram_mb: float = 1024.0):
        self.manifest_path = manifest_path.resolve()
        self.output_dir = output_dir.resolve()

        if type(min_ram_mb) not in (int, float) or min_ram_mb < 0 or not math.isfinite(min_ram_mb):
            raise BrowserBatchError("min_ram_mb must be a finite non-negative number")
        self.min_ram_mb = float(min_ram_mb)

        if self.output_dir.exists() and any(self.output_dir.iterdir()):
            raise BrowserBatchError("Output directory must be empty or not exist.")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.status_path = self.output_dir / "status.json"

        self.start_time = time.time()
        self.highwater = 0
        self.last_mem_mb: float | None = None
        self.min_mem_mb: float | None = None
        self.unknown_mem_seen = False

        self.leases: dict[Path, str] = {}

        self.max_workers = 2
        self.per_origin_limit = 1
        self.timeout_seconds = 600.0
        self.model: str | None = None
        self.jobs: list[JobConfig] = []
        self.states: dict[str, JobState] = {}

        self._load_manifest()

    def set_leases(self, leases: dict[Path, str]) -> None:
        self.leases = leases

    def _load_manifest(self) -> None:
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise BrowserBatchError(f"Failed to read manifest: {e}")
        if not isinstance(data, dict):
            raise BrowserBatchError("manifest must be an object")

        max_workers = data.get("max_workers", 2)
        if type(max_workers) is not int or not (1 <= max_workers <= 4):
            raise BrowserBatchError("max_workers must be an int within 1..4")
        self.max_workers = max_workers

        per_origin_limit = data.get("per_origin_limit", 1)
        if type(per_origin_limit) is not int or per_origin_limit < 1 or per_origin_limit > self.max_workers:
            raise BrowserBatchError("per_origin_limit must be an int >= 1 and <= max_workers")
        self.per_origin_limit = per_origin_limit

        timeout_seconds = data.get("timeout_seconds", 600.0)
        if type(timeout_seconds) not in (int, float) or timeout_seconds <= 0 or not math.isfinite(timeout_seconds):
            raise BrowserBatchError("timeout_seconds must be a positive finite number")
        self.timeout_seconds = float(timeout_seconds)

        model = data.get("model")
        if model is not None and (type(model) is not str or not model.strip()):
            raise BrowserBatchError("model must be a non-empty string")
        self.model = model

        if data.get("phase", "prepare") != "prepare":
            raise BrowserBatchError("Phase submit is rejected.")

        raw_jobs = data.get("jobs")
        if not isinstance(raw_jobs, list) or not raw_jobs:
            raise BrowserBatchError("jobs must be a non-empty list")

        seen_job_ids = set()
        seen_bridge_dirs = set()
        seen_identities = set()
        seen_tabs = set()
        seen_sessions = set()

        for rj in raw_jobs:
            if not isinstance(rj, dict):
                raise BrowserBatchError("job must be an object")

            if rj.get("phase", "prepare") != "prepare":
                raise BrowserBatchError("Explicit phase submit keys are rejected.")

            job_id = rj.get("job_id")
            if type(job_id) is not str or len(job_id) > 80 or not JOB_ID_RE.fullmatch(job_id) or job_id.upper() in RESERVED_NAMES:
                raise BrowserBatchError(f"Invalid job_id: {job_id}")
            if job_id.casefold() in seen_job_ids:
                raise BrowserBatchError(f"Duplicate job_id: {job_id}")
            seen_job_ids.add(job_id.casefold())

            bdir = rj.get("bridge_dir")
            tfile = rj.get("task_file")
            if not bdir or type(bdir) is not str or not tfile or type(tfile) is not str:
                raise BrowserBatchError(f"bridge_dir and task_file are required for job {job_id}")

            bridge_dir = (self.manifest_path.parent / bdir).resolve()
            task_file = (self.manifest_path.parent / tfile).resolve()

            if bridge_dir in seen_bridge_dirs:
                raise BrowserBatchError(f"Duplicate bridge dir in batch: {bridge_dir}")
            seen_bridge_dirs.add(bridge_dir)

            try:
                _read_task(task_file)
            except (OSError, ValueError) as e:
                raise BrowserBatchError(f"Invalid task file for job {job_id}: {e}")

            try:
                host = read_active_host(bridge_dir)
            except (OSError, ValueError, VisualBridgeError) as e:
                raise BrowserBatchError(f"Invalid host for job {job_id}: {e}")

            try:
                _validate_browser_host(host, phase="prepare")
            except (ValueError, VisualBridgeError) as e:
                raise BrowserBatchError(f"Host validation failed for job {job_id}: {e}")

            tab_id = host.target.get("tab_id")
            identity = (host.session_id, host.token_epoch, tab_id)
            if identity in seen_identities or tab_id in seen_tabs or host.session_id in seen_sessions:
                raise BrowserBatchError(f"Duplicate host identity in batch: {identity}")
            seen_identities.add(identity)
            seen_tabs.add(tab_id)
            seen_sessions.add(host.session_id)

            app_url = host.target.get("application_url")
            parsed = urlsplit(str(app_url or ""))
            origin = parsed.hostname or ""

            job_config = JobConfig(
                job_id=job_id,
                bridge_dir=bridge_dir,
                task_file=task_file,
                origin=origin,
                session_id=host.session_id,
                token_epoch=host.token_epoch,
                target=host.target
            )
            self.jobs.append(job_config)

        self.states = {j.job_id: JobState(job_id=j.job_id, queued_at=self.start_time) for j in self.jobs}
        self._write_status()

    def _write_status(self) -> None:
        now = time.time()
        wall = now - self.start_time

        counts = {"queued": 0, "running": 0, "completed": 0, "failed": 0, "timed_out": 0, "cancelled": 0}
        jobs_dict = {}
        for state in self.states.values():
            counts[state.status] = counts.get(state.status, 0) + 1
            s: dict[str, Any] = {
                "status": state.status,
                "queued_at": state.queued_at,
            }
            if state.started_at is not None:
                s["started_at"] = state.started_at
            if state.ended_at is not None:
                s["ended_at"] = state.ended_at
            if state.exit_code is not None:
                s["exit_code"] = state.exit_code
            if state.outcome is not None:
                s["outcome"] = state.outcome

            q_time = 0.0
            j_time = 0.0

            if state.started_at is not None:
                q_time = state.started_at - state.queued_at
                j_time = (state.ended_at or now) - state.started_at
            else:
                q_time = (state.ended_at or now) - state.queued_at

            s["durations"] = {
                "queue_seconds": round(q_time, 3),
                "job_seconds": round(j_time, 3)
            }

            jobs_dict[state.job_id] = s

        data = {
            "schema_version": 1,
            "owner_pid": os.getpid(),
            "updated_at": now,
            "status_semantics": "last_observed; interrupted owner requires reconciliation, never replay",
            "limits": {
                "max_workers": self.max_workers,
                "per_origin_limit": self.per_origin_limit,
                "timeout_seconds": self.timeout_seconds,
                "min_ram_mb": self.min_ram_mb
            },
            "memory_seen": {
                "last_mb": self.last_mem_mb,
                "min_mb": self.min_mem_mb,
                "unknown_seen": self.unknown_mem_seen
            },
            "durations": {
                "wall_seconds": round(wall, 3)
            },
            "counts": counts,
            "highwater": self.highwater,
            "jobs": jobs_dict
        }
        atomic_write_json(self.status_path, data)

    def run(self) -> bool:
        with _termination_guard():
            return self._run()

    def _run(self) -> bool:
        if not self.leases:
            with claim_worker_bridges([j.bridge_dir for j in self.jobs]) as leases:
                self.set_leases(leases)
                try:
                    return self.run()
                finally:
                    self.leases = {}
        if any(s.status != "queued" for s in self.states.values()):
            raise BrowserBatchError("A batch cannot be replayed; reconcile outcomes and use a fresh manifest.")
        running_procs: dict[str, subprocess.Popen[Any]] = {}
        pending_jobs = list(self.jobs)
        last_telemetry = 0.0

        package_root = Path(__file__).resolve().parents[2]

        try:
            while pending_jobs or running_procs:
                changed = False
                now = time.time()
                if now - last_telemetry >= 1.0:
                    memory = get_available_memory_mb()
                    self.last_mem_mb = memory
                    self.unknown_mem_seen |= memory is None
                    if memory is not None:
                        self.min_mem_mb = memory if self.min_mem_mb is None else min(self.min_mem_mb, memory)
                    last_telemetry = now
                    changed = True

                done_jobs = []
                for jid, proc in running_procs.items():
                    ret = proc.poll()
                    if ret is not None:
                        done_jobs.append((jid, ret))
                    elif now - self.states[jid].started_at > self.timeout_seconds + 5.0:
                        stop_batch_worker(proc)
                        # We wait for next loop iteration to harvest the exit code,
                        # but in case it's really hung, we can just kill it.

                for jid, ret in done_jobs:
                    del running_procs[jid]
                    state = self.states[jid]
                    state.ended_at = now
                    state.exit_code = ret
                    if ret == 0:
                        state.status = "completed"
                        state.outcome = "worker_completed_needs_review"
                    elif ret == 124 or now - state.started_at > self.timeout_seconds:
                        state.status = "timed_out"
                        state.outcome = "timed_out"
                    else:
                        state.status = "failed"
                        state.outcome = "failed"
                    changed = True

                job_cfg_by_id = {j.job_id: j for j in self.jobs}
                active_origins = [job_cfg_by_id[jid].origin for jid in running_procs]
                origin_counts = {orig: active_origins.count(orig) for orig in set(active_origins)}

                newly_started = []
                for j in pending_jobs:
                    if len(running_procs) >= self.max_workers:
                        break

                    if origin_counts.get(j.origin, 0) >= self.per_origin_limit:
                        continue

                    mem_mb = get_available_memory_mb()
                    if mem_mb is None:
                        self.unknown_mem_seen = True
                    else:
                        self.last_mem_mb = mem_mb
                        if self.min_mem_mb is None or mem_mb < self.min_mem_mb:
                            self.min_mem_mb = mem_mb

                    if self.min_ram_mb > 0 and mem_mb is None:
                        state = self.states[j.job_id]
                        state.status = "cancelled"
                        state.outcome = "cancelled_memory_unknown"
                        state.ended_at = now
                        newly_started.append(j)
                        changed = True
                        continue

                    if mem_mb is not None and mem_mb < self.min_ram_mb:
                        if now - self.states[j.job_id].queued_at > self.timeout_seconds:
                            state = self.states[j.job_id]
                            state.status = "cancelled"
                            state.outcome = "cancelled_memory_pause_timeout"
                            state.ended_at = now
                            newly_started.append(j)
                            changed = True
                        continue

                    try:
                        host = read_active_host(j.bridge_dir)
                        _validate_browser_host(host, phase="prepare")
                        if host.session_id != j.session_id or host.token_epoch != j.token_epoch or host.target != j.target:
                            raise BrowserBatchError("Binding drift detected")
                    except (OSError, ValueError, VisualBridgeError, BrowserBatchError) as e:
                        state = self.states[j.job_id]
                        state.status = "cancelled"
                        state.outcome = f"cancelled_before_launch: {e}"
                        state.ended_at = now
                        newly_started.append(j)
                        changed = True
                        continue

                    log_path = self.output_dir / f"{j.job_id}.log"

                    cmd = [
                        sys.executable,
                        "-m", "applypilot.apply.browser_worker",
                        "--bridge-dir", str(j.bridge_dir),
                        "--task-file", str(j.task_file),
                        "--phase", "prepare",
                        "--timeout-seconds", str(self.timeout_seconds),
                    ]
                    if self.model:
                        cmd.extend(["--model", self.model])

                    env = os.environ.copy()
                    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(package_root), env.get("PYTHONPATH")]))
                    if j.bridge_dir in self.leases:
                        env["APPLYPILOT_BROWSER_BATCH_LEASE"] = self.leases[j.bridge_dir]

                    process_options = {}
                    if platform.system() == "Windows":
                        process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                    else:
                        process_options["start_new_session"] = True

                    try:
                        with log_path.open("w", encoding="utf-8") as log_fd, _defer_spawn_signals():
                            proc = subprocess.Popen(cmd, stdout=log_fd, stderr=subprocess.STDOUT,
                                                    env=env, **process_options)
                            running_procs[j.job_id] = proc
                    except (OSError, ValueError) as e:
                        state = self.states[j.job_id]
                        state.status = "failed"
                        state.outcome = f"failed_launch: {e}"
                        state.ended_at = now
                        newly_started.append(j)
                        changed = True
                        continue


                    state = self.states[j.job_id]
                    state.status = "running"
                    state.started_at = now
                    origin_counts[j.origin] = origin_counts.get(j.origin, 0) + 1
                    newly_started.append(j)
                    changed = True

                    self.highwater = max(self.highwater, len(running_procs))

                for j in newly_started:
                    pending_jobs.remove(j)

                if changed:
                    self._write_status()

                time.sleep(0.1)

        except Exception:
            for proc in running_procs.values():
                stop_batch_worker(proc)
            raise
        finally:
            changed = False
            now = time.time()
            for jid, proc in running_procs.items():
                stop_batch_worker(proc)
                state = self.states[jid]
                state.status = "cancelled"
                state.outcome = "cancelled_due_to_interruption"
                state.ended_at = now
                changed = True
            for j in pending_jobs:
                state = self.states[j.job_id]
                state.status = "cancelled"
                state.outcome = "cancelled_queued_interruption"
                state.ended_at = now
                changed = True
            if changed:
                self._write_status()

        return all(s.status == "completed" for s in self.states.values())


def stop_batch_worker(process: subprocess.Popen[Any]) -> None:
    """Allow the POSIX wrapper to reap its separately isolated Codex/MCP tree."""
    if platform.system() != "Windows" and process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=5)
            return
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            pass
    _stop_process(process)
