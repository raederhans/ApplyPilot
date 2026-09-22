"""Agent subprocess lifecycle, timeout containment, and best-effort telemetry.

This module knows no MCP configuration, provider command line, browser policy,
application ledger, or submission authority. Dependencies with side effects
are injected at the process boundary.
"""

from __future__ import annotations

import logging
import platform
import subprocess
import threading
import time
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)
_REAL_POPEN_TYPE = subprocess.Popen


def _current_working_set_bytes(counters: object) -> int:
    """Return current Windows working-set bytes, never the historical peak."""

    return max(0, int(getattr(counters, "WorkingSetSize", 0)))


def process_rss_bytes(pid: int) -> int:
    """Return best-effort resident bytes for one child process.

    Telemetry must never affect runtime authority, so unsupported platforms,
    exited processes, and access-denied handles return zero.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return 0
    try:
        if platform.system() == "Windows":
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            process_vm_read = 0x0010

            class ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(ProcessMemoryCounters),
                wintypes.DWORD,
            ]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(
                process_query_limited_information | process_vm_read,
                False,
                pid,
            )
            if not handle:
                return 0
            try:
                counters = ProcessMemoryCounters()
                counters.cb = ctypes.sizeof(counters)
                if not psapi.GetProcessMemoryInfo(
                    handle,
                    ctypes.byref(counters),
                    counters.cb,
                ):
                    return 0
                return _current_working_set_bytes(counters)
            finally:
                kernel32.CloseHandle(handle)

        status = Path(f"/proc/{pid}/status")
        if status.is_file():
            for line in status.read_text(encoding="ascii", errors="ignore").splitlines():
                if line.startswith("VmRSS:"):
                    fields = line.split()
                    return max(0, int(fields[1]) * 1024)
    except (OSError, TypeError, ValueError):
        return 0
    return 0


def start_timeout_watchdog(
    proc: subprocess.Popen,
    timeout_seconds: float,
    *,
    kill_process_tree: Callable[[int], None],
) -> tuple[threading.Event, threading.Timer]:
    """Kill an agent that does not reach EOF before its wall-clock deadline."""
    timed_out = threading.Event()

    def terminate_if_running() -> None:
        if proc.poll() is None:
            timed_out.set()
            kill_process_tree(proc.pid)

    timer = threading.Timer(timeout_seconds, terminate_if_running)
    timer.daemon = True
    timer.start()
    return timed_out, timer


class SubprocessRuntimeError(RuntimeError):
    """Base failure for the concrete subprocess runtime adapter."""


class RuntimeContinuityError(SubprocessRuntimeError):
    """A resumed turn violated actor, runtime, or profile continuity."""


@dataclass(frozen=True, slots=True)
class SubprocessParentIdentity:
    """Durable parent identity used when the old launcher is no longer in memory."""

    run_id: str
    actor_id: str
    attempt_id: str
    runtime_id: str
    profile_id: str
    submit_started: bool


@dataclass(frozen=True, slots=True)
class SubprocessLaunchSpec:
    """Provider-neutral subprocess turn description.

    The spec controls process lifecycle only.  It carries no browser-write,
    submit, ledger, manifest, or receipt authority.
    """

    run_id: str
    attempt_id: str
    actor_id: str
    turn_id: str
    command: tuple[str, ...]
    prompt: str
    cwd: Path
    env: MutableMapping[str, str]
    runtime_id: str
    profile_id: str
    parent_run_id: str | None = None
    submit_started: bool = False

    def __post_init__(self) -> None:
        for name in (
            "run_id",
            "attempt_id",
            "actor_id",
            "turn_id",
            "runtime_id",
            "profile_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        if self.run_id != self.turn_id:
            raise ValueError("run_id must remain the compatibility alias for turn_id")
        if not self.command or any(not str(item).strip() for item in self.command):
            raise ValueError("command must contain executable arguments")
        if self.parent_run_id is not None and not self.parent_run_id.strip():
            raise ValueError("parent_run_id must be non-empty when provided")

    def redacted_for_history(self) -> SubprocessLaunchSpec:
        """Retain continuity identity without keeping prompt or environment data."""
        return replace(
            self,
            command=(self.command[0],),
            prompt="",
            env={},
        )


@dataclass(frozen=True, slots=True)
class SubprocessRuntimeHealth:
    run_id: str
    status: str
    pid: int | None
    returncode: int | None
    started_at: float
    updated_at: float


@dataclass(slots=True)
class _ManagedSubprocess:
    spec: SubprocessLaunchSpec
    process: subprocess.Popen[str]
    status: str
    started_at: float
    updated_at: float


@runtime_checkable
class SubprocessRuntimeAdapter(Protocol):
    """Lifecycle port implemented by concrete local/provider CLI adapters."""

    def start(
        self,
        spec: SubprocessLaunchSpec,
        *,
        popen_factory: Callable[..., subprocess.Popen[str]] | None = None,
        on_spawned: Callable[[subprocess.Popen[str]], None] | None = None,
    ) -> subprocess.Popen[str]: ...

    def resume(
        self,
        parent_run_id: str,
        spec: SubprocessLaunchSpec,
        *,
        popen_factory: Callable[..., subprocess.Popen[str]] | None = None,
        on_spawned: Callable[[subprocess.Popen[str]], None] | None = None,
        persisted_parent: SubprocessParentIdentity | None = None,
    ) -> subprocess.Popen[str]: ...

    def cancel(self, run_id: str) -> None: ...

    def health(self, run_id: str) -> SubprocessRuntimeHealth: ...

    def close(self, run_id: str | None = None) -> None: ...


class SubprocessAgentRuntime:
    """Real subprocess lifecycle adapter with fresh-turn resume continuity."""

    def __init__(
        self,
        *,
        kill_process_tree: Callable[[int], None],
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._kill_process_tree = kill_process_tree
        self._popen_factory = popen_factory
        self._clock = clock
        self._lock = threading.RLock()
        self._runs: dict[str, _ManagedSubprocess] = {}
        self._closed = False

    @staticmethod
    def _fallback_clock() -> float:
        """Return a cleanup timestamp without relying on an injected clock."""
        try:
            return time.monotonic()
        except BaseException as error:
            logger.debug("fallback monotonic clock failed", exc_info=error)
            return 0.0

    @staticmethod
    def _is_terminated(process: subprocess.Popen[str]) -> bool:
        """Treat an unreadable process state as live so cleanup fails closed."""
        try:
            return process.poll() is not None
        except BaseException as error:
            logger.debug("subprocess poll failed during cleanup", exc_info=error)
            return False

    def _quarantine_spawn_failure(
        self,
        spec: SubprocessLaunchSpec,
        process: subprocess.Popen[str],
        managed: _ManagedSubprocess | None,
    ) -> bool:
        """Stop an exact spawned child, retaining ownership until death is proven."""
        try:
            if not self._is_terminated(process):
                self._kill_process_tree(process.pid)
        except BaseException as error:
            logger.debug("subprocess tree kill failed during quarantine", exc_info=error)
        try:
            process.wait(timeout=5)
        except BaseException as error:
            logger.debug("subprocess wait failed during quarantine", exc_info=error)
        terminated = self._is_terminated(process)
        now = self._fallback_clock()
        with self._lock:
            current = managed or self._runs.get(spec.run_id)
            if current is None:
                current = _ManagedSubprocess(
                    spec=spec,
                    process=process,
                    status="failed" if terminated else "quarantined",
                    started_at=now,
                    updated_at=now,
                )
                self._runs[spec.run_id] = current
            else:
                current.status = "failed" if terminated else "quarantined"
                current.updated_at = now
        return terminated

    def _spawn(
        self,
        spec: SubprocessLaunchSpec,
        *,
        popen_factory: Callable[..., subprocess.Popen[str]] | None = None,
        on_spawned: Callable[[subprocess.Popen[str]], None] | None = None,
    ) -> subprocess.Popen[str]:
        with self._lock:
            if self._closed:
                raise SubprocessRuntimeError("subprocess runtime is closed")
            if spec.run_id in self._runs:
                raise SubprocessRuntimeError(f"run_id already exists: {spec.run_id}")
            factory = popen_factory or self._popen_factory
            process = factory(
                list(spec.command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=dict(spec.env),
                cwd=str(spec.cwd),
            )
            fallback_now = self._fallback_clock()
            managed = _ManagedSubprocess(
                spec=spec,
                process=process,
                status="starting",
                started_at=fallback_now,
                updated_at=fallback_now,
            )
            self._runs[spec.run_id] = managed
        try:
            # Register the exact returned handle before any callback, injected
            # clock, or pipe operation can fail.  A failed cleanup keeps this
            # entry quarantined so close() can retry it.
            if on_spawned is not None:
                on_spawned(process)
            now = self._clock()
            with self._lock:
                managed.status = "running"
                managed.started_at = now
                managed.updated_at = now
            if process.stdin is None:
                raise SubprocessRuntimeError("subprocess stdin pipe was not created")
            process.stdin.write(spec.prompt)
            process.stdin.close()
            # The runtime owns stdin exclusively: the prompt is the complete
            # request, so closing it is how the child receives EOF.  CPython's
            # POSIX ``Popen.communicate()`` may still try to flush a closed
            # public ``stdin`` handle, however.  Detach the consumed pipe from
            # real Popen instances so callers can safely use ``communicate()``
            # to collect output after start().  Keep injected test doubles
            # intact because they model the pipe contract directly.
            if isinstance(process, _REAL_POPEN_TYPE):
                process.stdin = None
        except BaseException as error:
            terminated = self._quarantine_spawn_failure(spec, process, managed)
            if isinstance(error, BrokenPipeError) and terminated:
                try:
                    startup_output = process.stdout.read() if process.stdout else ""
                except BaseException as output_error:
                    logger.debug(
                        "startup output read failed after broken pipe",
                        exc_info=output_error,
                    )
                    startup_output = ""
                raise SubprocessRuntimeError(
                    "Agent exited before accepting the prompt: "
                    + startup_output.strip()[:500]
                ) from error
            raise
        return process

    def start(
        self,
        spec: SubprocessLaunchSpec,
        *,
        popen_factory: Callable[..., subprocess.Popen[str]] | None = None,
        on_spawned: Callable[[subprocess.Popen[str]], None] | None = None,
    ) -> subprocess.Popen[str]:
        """Start a new root turn."""
        if spec.parent_run_id is not None:
            raise RuntimeContinuityError("root start cannot declare parent_run_id")
        return self._spawn(
            spec,
            popen_factory=popen_factory,
            on_spawned=on_spawned,
        )

    def resume(
        self,
        parent_run_id: str,
        spec: SubprocessLaunchSpec,
        *,
        popen_factory: Callable[..., subprocess.Popen[str]] | None = None,
        on_spawned: Callable[[subprocess.Popen[str]], None] | None = None,
        persisted_parent: SubprocessParentIdentity | None = None,
    ) -> subprocess.Popen[str]:
        """Start a fresh child process bound to its completed parent turn."""
        with self._lock:
            parent = self._runs.get(parent_run_id)
            if parent is None and persisted_parent is None:
                raise RuntimeContinuityError("resume parent run is unknown")
            if spec.parent_run_id != parent_run_id:
                raise RuntimeContinuityError("resume parent binding does not match")
            if parent is not None:
                if parent.process.poll() is None:
                    raise RuntimeContinuityError("cannot resume while parent is still running")
                parent_identity = SubprocessParentIdentity(
                    run_id=parent.spec.run_id,
                    actor_id=parent.spec.actor_id,
                    attempt_id=parent.spec.attempt_id,
                    runtime_id=parent.spec.runtime_id,
                    profile_id=parent.spec.profile_id,
                    submit_started=parent.spec.submit_started,
                )
            else:
                assert persisted_parent is not None
                parent_identity = persisted_parent
                if persisted_parent.run_id != parent_run_id:
                    raise RuntimeContinuityError("persisted parent binding does not match")
            if (
                parent_identity.actor_id != spec.actor_id
                or parent_identity.attempt_id != spec.attempt_id
            ):
                raise RuntimeContinuityError(
                    "resume must keep the same actor and application attempt"
                )
            switched = (
                parent_identity.runtime_id != spec.runtime_id
                or parent_identity.profile_id != spec.profile_id
            )
            if switched and (parent_identity.submit_started or spec.submit_started):
                raise RuntimeContinuityError(
                    "runtime/profile switch is forbidden after submit_started"
                )
        return self._spawn(
            spec,
            popen_factory=popen_factory,
            on_spawned=on_spawned,
        )

    def health(self, run_id: str) -> SubprocessRuntimeHealth:
        """Return current process health without interpreting Agent output."""
        with self._lock:
            try:
                managed = self._runs[run_id]
            except KeyError as exc:
                raise KeyError(f"unknown subprocess run: {run_id}") from exc
            returncode = managed.process.poll()
            if managed.status == "running" and returncode is not None:
                managed.status = "completed" if returncode == 0 else "failed"
                managed.updated_at = self._clock()
            return SubprocessRuntimeHealth(
                run_id=run_id,
                status=managed.status,
                pid=managed.process.pid,
                returncode=returncode,
                started_at=managed.started_at,
                updated_at=managed.updated_at,
            )

    def cancel(self, run_id: str) -> None:
        """Cancel exactly one owned subprocess tree."""
        with self._lock:
            try:
                managed = self._runs[run_id]
            except KeyError as exc:
                raise KeyError(f"unknown subprocess run: {run_id}") from exc
            if not self._is_terminated(managed.process):
                try:
                    self._kill_process_tree(managed.process.pid)
                    managed.process.wait(timeout=5)
                except BaseException as error:
                    logger.debug("subprocess cancel cleanup failed", exc_info=error)
            if not self._is_terminated(managed.process):
                managed.status = "quarantined"
                managed.updated_at = self._fallback_clock()
                raise SubprocessRuntimeError(
                    f"cancel could not prove subprocess termination: {run_id}"
                )
            managed.status = "cancelled"
            managed.updated_at = self._fallback_clock()

    def close(self, run_id: str | None = None) -> None:
        """Close one run, or close the adapter and all live children."""
        with self._lock:
            targets = (
                [self._runs[run_id]]
                if run_id is not None and run_id in self._runs
                else list(self._runs.values()) if run_id is None else []
            )
            if run_id is not None and not targets:
                raise KeyError(f"unknown subprocess run: {run_id}")
            quarantined: list[str] = []
            for managed in targets:
                if not self._is_terminated(managed.process):
                    try:
                        self._kill_process_tree(managed.process.pid)
                        managed.process.wait(timeout=5)
                    except BaseException as error:
                        logger.debug("subprocess close cleanup failed", exc_info=error)
                if not self._is_terminated(managed.process):
                    managed.status = "quarantined"
                    managed.updated_at = self._fallback_clock()
                    quarantined.append(managed.spec.run_id)
                    continue
                managed.status = "closed"
                managed.updated_at = self._fallback_clock()
                managed.spec = managed.spec.redacted_for_history()
            if run_id is None and not quarantined:
                self._closed = True
            if quarantined:
                raise SubprocessRuntimeError(
                    "close could not prove subprocess termination: "
                    + ", ".join(quarantined)
                )

