"""Bounded persistent browser/application-session ownership.

The physical browser belongs to one worker generation.  Browser/page authority
continues to belong to ``DurableBrowserBroker`` and every application receives
its own actor, attempt, runtime namespace, page binding, and optional provider
session.  This module deliberately carries no submission, ledger, receipt, or
mailbox authority.

The default production CLI still starts Playwright MCP over stdio per Agent
turn.  An explicit, default-off Codex seam can instead attach turns to one
worker-owned loopback HTTP endpoint.  Its lifecycle is fully testable with a
local child process without claiming that the pinned Playwright MCP package has
passed a real browser smoke test.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from applypilot.apply.browser_broker import BrowserLeaseBundle
from applypilot.apply.capabilities import DEFAULT_PLAYWRIGHT_MCP_PACKAGE
from applypilot.apply.contracts import application_actor_id
from applypilot.apply.runtime_namespace import RuntimeNamespace


class PersistentSessionError(RuntimeError):
    """Base failure for persistent worker/application ownership."""


class StaleBrowserGeneration(PersistentSessionError):
    """A caller attempted to use an old browser or endpoint generation."""


class EndpointUnavailable(PersistentSessionError):
    """The reusable endpoint is absent, unhealthy, or failed to stop."""


def _required(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _positive(value: object, name: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class EndpointDescriptor:
    """One immutable endpoint generation exposed to an application turn."""

    endpoint_id: str
    generation: int
    transport: str
    address: str
    reusable: bool
    process_id: int | None = None

    def __post_init__(self) -> None:
        _required(self.endpoint_id, "endpoint_id")
        _positive(self.generation, "generation")
        _required(self.transport, "transport")
        _required(self.address, "address")
        if self.process_id is not None:
            _positive(self.process_id, "process_id")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "1",
            "endpoint_id": self.endpoint_id,
            "generation": self.generation,
            "transport": self.transport,
            "address": self.address,
            "reusable": self.reusable,
            "process_id": self.process_id,
        }


@runtime_checkable
class EndpointManager(Protocol):
    """Lifecycle port for one worker-owned MCP-compatible endpoint."""

    def start(self) -> EndpointDescriptor: ...

    def health(self) -> EndpointDescriptor: ...

    def restart(self) -> EndpointDescriptor: ...

    def close(self) -> None: ...

    def shutdown(self) -> None: ...


class PerTurnStdioEndpointManager:
    """Truthful marker for the current non-reusable production MCP transport."""

    def __init__(self, worker_id: int) -> None:
        self._worker_id = _positive(worker_id, "worker_id", allow_zero=True)
        self._descriptor: EndpointDescriptor | None = None

    def start(self) -> EndpointDescriptor:
        if self._descriptor is None:
            self._descriptor = EndpointDescriptor(
                endpoint_id=f"stdio-per-turn:worker:{self._worker_id}",
                generation=1,
                transport="stdio-per-turn",
                address="agent-cli-owned",
                reusable=False,
            )
        return self._descriptor

    def health(self) -> EndpointDescriptor:
        if self._descriptor is None:
            raise EndpointUnavailable("stdio endpoint marker was not started")
        return self._descriptor

    def restart(self) -> EndpointDescriptor:
        current = self.start()
        self._descriptor = EndpointDescriptor(
            endpoint_id=current.endpoint_id,
            generation=current.generation + 1,
            transport=current.transport,
            address=current.address,
            reusable=False,
        )
        return self._descriptor

    def close(self) -> None:
        self._descriptor = None

    def shutdown(self) -> None:
        self.close()


class SubprocessEndpointManager:
    """Own a reusable local endpoint child without any network/API dependency."""

    def __init__(
        self,
        command: tuple[str, ...],
        *,
        endpoint_id: str,
        transport: str,
        address: str,
        ready_probe: Callable[[subprocess.Popen[str]], bool],
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        startup_timeout_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not command or any(not str(item).strip() for item in command):
            raise ValueError("endpoint command must contain executable arguments")
        if startup_timeout_seconds <= 0:
            raise ValueError("startup_timeout_seconds must be positive")
        self._command = command
        self._endpoint_id = _required(endpoint_id, "endpoint_id")
        self._transport = _required(transport, "transport")
        self._address = _required(address, "address")
        self._ready_probe = ready_probe
        self._popen_factory = popen_factory
        self._startup_timeout_seconds = float(startup_timeout_seconds)
        self._clock = clock
        self._sleeper = sleeper
        self._process: subprocess.Popen[str] | None = None
        self._generation = 0
        self._lock = threading.RLock()

    def _descriptor(self) -> EndpointDescriptor:
        process = self._process
        if process is None or process.poll() is not None or not self._ready_probe(process):
            raise EndpointUnavailable("endpoint child is not healthy")
        return EndpointDescriptor(
            endpoint_id=self._endpoint_id,
            generation=self._generation,
            transport=self._transport,
            address=self._address,
            reusable=True,
            process_id=process.pid,
        )

    def start(self) -> EndpointDescriptor:
        with self._lock:
            if self._process is not None:
                return self._descriptor()
            process = self._popen_factory(
                list(self._command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            self._process = process
            self._generation += 1
            deadline = self._clock() + self._startup_timeout_seconds
            while self._clock() < deadline:
                if process.poll() is not None:
                    break
                if self._ready_probe(process):
                    return self._descriptor()
                self._sleeper(0.01)
            self._stop_process(process)
            self._process = None
            raise EndpointUnavailable("endpoint child did not become ready")

    def health(self) -> EndpointDescriptor:
        with self._lock:
            return self._descriptor()

    def restart(self) -> EndpointDescriptor:
        with self._lock:
            if self._process is not None:
                self._stop_process(self._process)
                self._process = None
            return self.start()

    @staticmethod
    def _stop_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        if process.poll() is None:
            raise EndpointUnavailable("endpoint child termination is unconfirmed")

    def close(self) -> None:
        with self._lock:
            if self._process is not None:
                self._stop_process(self._process)
                self._process = None

    def shutdown(self) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class LoopbackHttpEndpointSpec:
    """Pinned Playwright MCP HTTP endpoint command for one worker generation."""

    worker_id: int
    port: int
    cdp_port: int
    launcher: tuple[str, ...] = ("npx", "-y")
    package: str = DEFAULT_PLAYWRIGHT_MCP_PACKAGE
    host: str = "127.0.0.1"

    def __post_init__(self) -> None:
        _positive(self.worker_id, "worker_id", allow_zero=True)
        _positive(self.port, "port")
        _positive(self.cdp_port, "cdp_port")
        if self.host != "127.0.0.1":
            raise ValueError("persistent Playwright MCP must bind to 127.0.0.1")
        if not self.launcher or any(not str(value).strip() for value in self.launcher):
            raise ValueError("endpoint launcher must contain executable arguments")
        if self.package != DEFAULT_PLAYWRIGHT_MCP_PACKAGE:
            raise ValueError("persistent Playwright MCP package must remain pinned")

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/mcp"

    def command(self) -> tuple[str, ...]:
        return (
            *self.launcher,
            self.package,
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--cdp-endpoint",
            f"http://127.0.0.1:{self.cdp_port}",
            "--shared-browser-context",
        )


def resolve_persistent_playwright_launcher() -> tuple[str, ...]:
    """Resolve a direct npx launcher so the listening PID remains attestable."""
    if platform.system() != "Windows":
        executable = shutil.which("npx")
        if executable is None:
            raise FileNotFoundError("npx was not found on PATH")
        return executable, "-y"
    node = shutil.which("node.exe") or shutil.which("node")
    npx_shim = shutil.which("npx.cmd")
    if node is None or npx_shim is None:
        raise FileNotFoundError("node/npx was not found on PATH")
    npx_cli = Path(npx_shim).parent / "node_modules" / "npm" / "bin" / "npx-cli.js"
    if not npx_cli.is_file():
        raise FileNotFoundError("npx-cli.js was not found beside the npx shim")
    return node, str(npx_cli), "-y"


@dataclass(slots=True)
class LoopbackPortReservation:
    """Cross-process claim for one loopback HTTP endpoint port."""

    port: int
    lock_path: Path
    _released: bool = False

    @classmethod
    def reserve(cls, root: Path, *, worker_id: int) -> LoopbackPortReservation:
        _positive(worker_id, "worker_id", allow_zero=True)
        lock_root = root.expanduser().resolve(strict=False) / "mcp-http-port-locks"
        lock_root.mkdir(parents=True, exist_ok=True)
        for _ in range(32):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", 0))
                port = int(probe.getsockname()[1])
            lock_path = lock_root / f"{port}.lock"
            try:
                descriptor = os.open(
                    lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                continue
            try:
                os.write(
                    descriptor,
                    f"pid={os.getpid()} worker={worker_id}\n".encode("ascii"),
                )
            finally:
                os.close(descriptor)
            return cls(port=port, lock_path=lock_path)
        raise EndpointUnavailable("unable to reserve a loopback MCP port")

    def release(self) -> None:
        if self._released:
            return
        self.lock_path.unlink(missing_ok=True)
        self._released = True


def _port_accepting(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.05):
            return True
    except OSError:
        return False


def _windows_exact_listener_owner(process_id: int, host: str, port: int) -> bool:
    import ctypes
    from ctypes import wintypes

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ProcessEntry32W),
    ]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ProcessEntry32W),
    ]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if snapshot == invalid_handle:
        return False
    parents: dict[int, int] = {}
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)
    owned_processes = {process_id}
    changed = True
    while changed:
        changed = False
        for child, parent in parents.items():
            if child not in owned_processes and parent in owned_processes:
                owned_processes.add(child)
                changed = True
    completed = subprocess.run(
        ["netstat", "-ano", "-p", "tcp"],
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )
    endpoint = f"{host}:{port}"
    for raw_line in completed.stdout.splitlines():
        columns = raw_line.split()
        if (
            len(columns) >= 5
            and columns[0].casefold() == "tcp"
            and columns[1] == endpoint
            and columns[3].casefold() == "listening"
            and columns[4].isdigit()
            and int(columns[4]) in owned_processes
        ):
            return True
    return False


def _linux_exact_listener_owner(process_id: int, host: str, port: int) -> bool:
    if host != "127.0.0.1":
        return False
    target = f"0100007F:{port:04X}"
    inodes: set[str] = set()
    for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        try:
            rows = table.read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            columns = row.split()
            if len(columns) > 9 and columns[1].upper() == target and columns[3] == "0A":
                inodes.add(columns[9])
    if not inodes:
        return False
    parents: dict[int, int] = {}
    for candidate in Path("/proc").iterdir():
        if not candidate.name.isdigit():
            continue
        try:
            after_name = (candidate / "stat").read_text(encoding="ascii").rsplit(")", 1)[1]
            parents[int(candidate.name)] = int(after_name.split()[1])
        except (OSError, IndexError, ValueError):
            continue
    owned_processes = {process_id}
    changed = True
    while changed:
        changed = False
        for child, parent in parents.items():
            if child not in owned_processes and parent in owned_processes:
                owned_processes.add(child)
                changed = True
    for owned_process in owned_processes:
        try:
            descriptors = (Path("/proc") / str(owned_process) / "fd").iterdir()
            if any(
                descriptor.resolve(strict=False)
                .name.removeprefix("socket:[")
                .removesuffix("]")
                in inodes
                for descriptor in descriptors
            ):
                return True
        except OSError:
            continue
    return False


def exact_listener_owner_probe(
    process: subprocess.Popen[str],
    host: str,
    port: int,
    _generation: int,
) -> bool:
    """Prove the current child process tree, not any responder, owns the listener."""
    if process.poll() is not None or not _port_accepting(host, port):
        return False
    try:
        if platform.system() == "Windows":
            return _windows_exact_listener_owner(process.pid, host, port)
        if platform.system() == "Linux":
            return _linux_exact_listener_owner(process.pid, host, port)
    except (OSError, subprocess.SubprocessError):
        return False
    return False


class LoopbackHttpEndpointManager:
    """Own one exact-PID, generation-bound streamable HTTP MCP child."""

    def __init__(
        self,
        spec: LoopbackHttpEndpointSpec,
        reservation: LoopbackPortReservation,
        *,
        owner_probe: Callable[[subprocess.Popen[str], str, int, int], bool] = (
            exact_listener_owner_probe
        ),
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        stop_process: Callable[[subprocess.Popen[str]], None] = (
            SubprocessEndpointManager._stop_process
        ),
        startup_timeout_seconds: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if reservation.port != spec.port:
            raise ValueError("endpoint spec does not match its port reservation")
        if startup_timeout_seconds <= 0:
            raise ValueError("startup_timeout_seconds must be positive")
        self._spec = spec
        self._reservation = reservation
        self._owner_probe = owner_probe
        self._popen_factory = popen_factory
        self._stop_process = stop_process
        self._startup_timeout_seconds = float(startup_timeout_seconds)
        self._clock = clock
        self._sleeper = sleeper
        self._process: subprocess.Popen[str] | None = None
        self._generation = 0
        self._shutdown = False
        self._lock = threading.RLock()

    def _descriptor(self) -> EndpointDescriptor:
        process = self._process
        if (
            process is None
            or process.poll() is not None
            or not self._owner_probe(
                process,
                self._spec.host,
                self._spec.port,
                self._generation,
            )
        ):
            raise EndpointUnavailable("HTTP endpoint child does not own its listener")
        return EndpointDescriptor(
            endpoint_id=f"playwright-http:worker:{self._spec.worker_id}",
            generation=self._generation,
            transport="streamable-http",
            address=self._spec.url,
            reusable=True,
            process_id=process.pid,
        )

    def start(self) -> EndpointDescriptor:
        with self._lock:
            if self._shutdown:
                raise EndpointUnavailable("HTTP endpoint manager is shut down")
            if self._process is not None:
                return self._descriptor()
            if _port_accepting(self._spec.host, self._spec.port):
                raise EndpointUnavailable("reserved HTTP endpoint port is already occupied")
            process = self._popen_factory(
                list(self._spec.command()),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            self._process = process
            self._generation += 1
            deadline = self._clock() + self._startup_timeout_seconds
            while self._clock() < deadline:
                if process.poll() is not None:
                    break
                if self._owner_probe(
                    process,
                    self._spec.host,
                    self._spec.port,
                    self._generation,
                ):
                    return self._descriptor()
                self._sleeper(0.01)
            self._stop_process(process)
            self._process = None
            raise EndpointUnavailable("HTTP endpoint child did not own its listener")

    def health(self) -> EndpointDescriptor:
        with self._lock:
            return self._descriptor()

    def restart(self) -> EndpointDescriptor:
        with self._lock:
            self.close()
            return self.start()

    def close(self) -> None:
        with self._lock:
            if self._process is not None:
                self._stop_process(self._process)
                self._process = None
                deadline = self._clock() + 3.0
                while self._clock() < deadline and _port_accepting(
                    self._spec.host, self._spec.port
                ):
                    self._sleeper(0.01)
                if _port_accepting(self._spec.host, self._spec.port):
                    raise EndpointUnavailable(
                        "HTTP endpoint listener remained live after child exit"
                    )

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown:
                return
            self.close()
            self._reservation.release()
            self._shutdown = True


@dataclass(frozen=True, slots=True)
class ContextBundle:
    """Compact, immutable, exact-application runtime context."""

    namespace: RuntimeNamespace
    worker_id: int
    application_session_id: str
    actor_id: str
    attempt_id: str
    phase: str
    runtime_backend: str
    browser_runtime: str
    browser_profile_id: str
    browser_generation: int
    endpoint: EndpointDescriptor
    root_target_ids: tuple[str, ...]
    page_binding: Mapping[str, object]
    provider_session_id: str | None = None

    def __post_init__(self) -> None:
        _positive(self.worker_id, "worker_id", allow_zero=True)
        for name in (
            "application_session_id",
            "actor_id",
            "attempt_id",
            "phase",
            "runtime_backend",
            "browser_runtime",
            "browser_profile_id",
        ):
            _required(getattr(self, name), name)
        if self.actor_id != application_actor_id(self.attempt_id):
            raise ValueError("ContextBundle actor/attempt identity is not canonical")
        _positive(self.browser_generation, "browser_generation")
        if self.endpoint.generation != self.browser_generation:
            raise StaleBrowserGeneration("endpoint/browser generation mismatch")
        targets = tuple(sorted({_required(item, "root_target_id") for item in self.root_target_ids}))
        if not targets:
            raise ValueError("ContextBundle requires an exact root target")
        object.__setattr__(self, "root_target_ids", targets)
        compact = json.loads(json.dumps(dict(self.page_binding), sort_keys=True))
        rendered = json.dumps(compact, sort_keys=True, separators=(",", ":"))
        if len(rendered.encode("utf-8")) > 32_768:
            raise ValueError("ContextBundle page binding is not compact")
        object.__setattr__(self, "page_binding", compact)
        if self.provider_session_id is not None:
            _required(self.provider_session_id, "provider_session_id")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "1",
            "namespace": self.namespace.as_dict(),
            "worker_id": self.worker_id,
            "application_session_id": self.application_session_id,
            "actor_id": self.actor_id,
            "attempt_id": self.attempt_id,
            "phase": self.phase,
            "runtime_backend": self.runtime_backend,
            "browser_runtime": self.browser_runtime,
            "browser_profile_id": self.browser_profile_id,
            "browser_generation": self.browser_generation,
            "endpoint": self.endpoint.as_dict(),
            "root_target_ids": list(self.root_target_ids),
            "page_binding": dict(self.page_binding),
            "provider_session_id": self.provider_session_id,
        }


class BrowserWorkerProcess:
    """Own one physical browser/endpoint generation across applications."""

    def __init__(
        self,
        *,
        worker_id: int,
        port: int,
        run_id: str,
        namespace_root: Path,
        launch_browser: Callable[..., subprocess.Popen[object]],
        cleanup_browser: Callable[[int, subprocess.Popen[object] | None], None],
        open_target: Callable[[int, str], set[str]],
        close_targets: Callable[[int, set[str]], None],
        endpoint_manager: EndpointManager,
        browser_health_probe: Callable[[], bool] | None = None,
        rss_reader: Callable[[int], int] | None = None,
        max_applications: int = 8,
        max_rss_bytes: int = 1_500_000_000,
    ) -> None:
        self.worker_id = _positive(worker_id, "worker_id", allow_zero=True)
        self.port = _positive(port, "port")
        self.run_id = _required(run_id, "run_id")
        self.namespace_root = namespace_root.expanduser().resolve(strict=False)
        self._launch_browser = launch_browser
        self._cleanup_browser = cleanup_browser
        self._open_target = open_target
        self._close_targets = close_targets
        self._endpoint_manager = endpoint_manager
        self._browser_health_probe = browser_health_probe
        self._rss_reader = rss_reader or (lambda _pid: 0)
        self._max_applications = _positive(max_applications, "max_applications")
        self._max_rss_bytes = _positive(max_rss_bytes, "max_rss_bytes")
        self._process: subprocess.Popen[object] | None = None
        self._endpoint: EndpointDescriptor | None = None
        self._endpoint_manager_generation: int | None = None
        self._browser_runtime: str | None = None
        self._generation = 0
        self._applications = 0
        self._applications_started_total = 0
        self._applications_completed_total = 0
        self._browser_starts_total = 0
        self._browser_reuses_total = 0
        self._active_session_id: str | None = None
        self._active_attempt_id: str | None = None
        self._active_targets: set[str] = set()
        self._closed = False
        self._lock = threading.RLock()

    @property
    def process(self) -> subprocess.Popen[object] | None:
        return self._process

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def browser_runtime(self) -> str:
        return _required(self._browser_runtime, "browser_runtime")

    @property
    def endpoint(self) -> EndpointDescriptor:
        if self._endpoint is None:
            raise EndpointUnavailable("browser endpoint was not started")
        return self._endpoint

    @property
    def active_targets(self) -> tuple[str, ...]:
        return tuple(sorted(self._active_targets))

    def metrics(self) -> dict[str, int]:
        """Return cumulative, process-local reuse evidence for this worker."""
        with self._lock:
            return {
                "schema_version": 1,
                "browser_generation": self._generation,
                "browser_starts": self._browser_starts_total,
                "applications_started": self._applications_started_total,
                "applications_completed": self._applications_completed_total,
                "browser_reuses": self._browser_reuses_total,
            }

    def _process_healthy(self, process: subprocess.Popen[object]) -> bool:
        poll = getattr(process, "poll", None)
        if not callable(poll) or poll() is None:
            return True
        if self._browser_health_probe is None:
            return False
        try:
            return bool(self._browser_health_probe())
        except Exception:  # noqa: BLE001 - a broken health probe must fail closed
            return False

    def _healthy(self) -> bool:
        return self._process is not None and self._process_healthy(self._process)

    def _roll_required(self) -> bool:
        if not self._healthy():
            return self._process is not None
        assert self._process is not None
        process_id = getattr(self._process, "pid", None)
        rss = (
            max(0, int(self._rss_reader(process_id)))
            if isinstance(process_id, int) and process_id > 0
            else 0
        )
        return self._applications >= self._max_applications or rss > self._max_rss_bytes

    def _start(self, browser_runtime: str, *, headless: bool) -> subprocess.Popen[object]:
        if self._closed:
            raise PersistentSessionError("browser worker process is closed")
        process = self._launch_browser(
            self.worker_id,
            port=self.port,
            headless=headless,
            start_url=None,
            browser_backend=browser_runtime,
        )
        if not self._process_healthy(process):
            self._cleanup_browser(self.worker_id, process)
            raise PersistentSessionError("browser process exited during startup")
        try:
            endpoint = self._endpoint_manager.start()
        except BaseException:
            self._cleanup_browser(self.worker_id, process)
            raise
        self._process = process
        self._browser_runtime = browser_runtime
        self._generation += 1
        self._browser_starts_total += 1
        self._applications = 0
        # A per-turn marker can report generation 1 forever. Bind the actual
        # application context to the physical browser generation instead.
        self._endpoint = EndpointDescriptor(
            endpoint_id=endpoint.endpoint_id,
            generation=self._generation,
            transport=endpoint.transport,
            address=endpoint.address,
            reusable=endpoint.reusable,
            process_id=endpoint.process_id,
        )
        self._endpoint_manager_generation = endpoint.generation
        return process

    def _stop_generation(self) -> None:
        process = self._process
        try:
            self._endpoint_manager.close()
        finally:
            if process is not None:
                self._cleanup_browser(self.worker_id, process)
            self._process = None
            self._endpoint = None
            self._endpoint_manager_generation = None
            self._browser_runtime = None
            self._active_targets.clear()

    def ensure_started(self, browser_runtime: str, *, headless: bool) -> subprocess.Popen[object]:
        runtime = _required(browser_runtime, "browser_runtime")
        with self._lock:
            if self._active_session_id is not None:
                raise PersistentSessionError("cannot change worker generation during an active application")
            if self._roll_required() or (
                self._process is not None and self._browser_runtime != runtime
            ):
                self._stop_generation()
            if self._process is None:
                return self._start(runtime, headless=headless)
            try:
                self.heartbeat(expected_generation=self._generation)
            except (EndpointUnavailable, StaleBrowserGeneration):
                self._stop_generation()
                return self._start(runtime, headless=headless)
            return self._process

    def begin_application(
        self,
        *,
        application_session_id: str,
        attempt_id: str,
        actor_id: str,
        start_url: str,
        browser_runtime: str,
        headless: bool,
    ) -> subprocess.Popen[object]:
        if actor_id != application_actor_id(attempt_id):
            raise ValueError("application actor/attempt identity is not canonical")
        with self._lock:
            if self._active_session_id is not None:
                raise PersistentSessionError("worker already owns an active application")
            starts_before = self._browser_starts_total
            process = self.ensure_started(browser_runtime, headless=headless)
            targets: set[str] | None = None
            try:
                targets = set(
                    self._open_target(self.port, _required(start_url, "start_url"))
                )
            finally:
                if targets is None:
                    self._stop_generation()
            if not targets or any(not str(item).strip() for item in targets):
                self._stop_generation()
                raise PersistentSessionError("browser did not return exact application targets")
            self._active_session_id = _required(
                application_session_id, "application_session_id"
            )
            self._active_attempt_id = _required(attempt_id, "attempt_id")
            self._active_targets = targets
            self._applications_started_total += 1
            if self._browser_starts_total == starts_before:
                self._browser_reuses_total += 1
            return process

    def restart_for_application(
        self,
        *,
        application_session_id: str,
        start_url: str,
        browser_runtime: str,
        headless: bool,
        submit_started: bool,
    ) -> subprocess.Popen[object]:
        with self._lock:
            if submit_started:
                raise PersistentSessionError(
                    "browser/runtime generation cannot change after submit_started"
                )
            if self._active_session_id != application_session_id:
                raise StaleBrowserGeneration("application session no longer owns the worker")
            self._close_targets(self.port, set(self._active_targets))
            self._active_targets.clear()
            attempt_id = _required(self._active_attempt_id, "attempt_id")
            self._active_session_id = None
            self._active_attempt_id = None
            self._stop_generation()
            process = self._start(_required(browser_runtime, "browser_runtime"), headless=headless)
            targets: set[str] | None = None
            try:
                targets = set(
                    self._open_target(self.port, _required(start_url, "start_url"))
                )
            finally:
                if targets is None:
                    self._stop_generation()
            if not targets:
                self._stop_generation()
                raise PersistentSessionError("restarted browser returned no application target")
            self._active_session_id = application_session_id
            self._active_attempt_id = attempt_id
            self._active_targets = targets
            return process

    def heartbeat(self, *, expected_generation: int) -> EndpointDescriptor:
        with self._lock:
            if expected_generation != self._generation:
                raise StaleBrowserGeneration("browser generation is stale")
            if not self._healthy():
                raise StaleBrowserGeneration("browser process is no longer live")
            observed = self._endpoint_manager.health()
            current = self.endpoint
            if (
                observed.endpoint_id != current.endpoint_id
                or observed.process_id != current.process_id
                or observed.transport != current.transport
                or observed.generation != self._endpoint_manager_generation
            ):
                raise StaleBrowserGeneration("endpoint identity changed without a restart")
            return current

    def end_application(self, application_session_id: str) -> None:
        with self._lock:
            if self._active_session_id != application_session_id:
                raise StaleBrowserGeneration("application session is stale")
            try:
                self._close_targets(self.port, set(self._active_targets))
            finally:
                self._active_targets.clear()
                self._active_session_id = None
                self._active_attempt_id = None
                self._applications += 1
                self._applications_completed_total += 1

    def recycle_idle_generation(self) -> None:
        """Discard a tainted generation after its application cleanup completed."""
        with self._lock:
            if self._active_session_id is not None:
                raise PersistentSessionError("cannot recycle an active application")
            if self._process is not None:
                self._stop_generation()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._active_session_id is not None:
                try:
                    self._close_targets(self.port, set(self._active_targets))
                finally:
                    self._active_targets.clear()
                    self._active_session_id = None
                    self._active_attempt_id = None
            try:
                if self._process is not None:
                    self._stop_generation()
            finally:
                try:
                    self._endpoint_manager.shutdown()
                finally:
                    self._closed = True


class ApplicationSupervisor:
    """Bind exactly one application to a worker generation and Broker bundle."""

    def __init__(
        self,
        browser_worker: BrowserWorkerProcess,
        *,
        attempt_id: str,
        actor_id: str,
        start_url: str,
        browser_runtime: str,
        headless: bool,
        release_browser_authority: Callable[[], None],
        session_id_factory: Callable[[], str] = lambda: f"application-{uuid.uuid4()}",
    ) -> None:
        self.browser_worker = browser_worker
        self.attempt_id = _required(attempt_id, "attempt_id")
        self.actor_id = _required(actor_id, "actor_id")
        if self.actor_id != application_actor_id(self.attempt_id):
            raise ValueError("supervisor actor/attempt identity is not canonical")
        self.start_url = _required(start_url, "start_url")
        self.application_session_id = _required(
            session_id_factory(), "application_session_id"
        )
        self._release_browser_authority = release_browser_authority
        self._browser_bundle: BrowserLeaseBundle | None = None
        self._provider_session_id: str | None = None
        self._submit_started = False
        self._closed = False
        self.browser_worker.begin_application(
            application_session_id=self.application_session_id,
            attempt_id=self.attempt_id,
            actor_id=self.actor_id,
            start_url=self.start_url,
            browser_runtime=browser_runtime,
            headless=headless,
        )

    @property
    def process(self) -> subprocess.Popen[object]:
        process = self.browser_worker.process
        if process is None:
            raise PersistentSessionError("supervisor browser process is absent")
        return process

    def mark_submit_started(self) -> None:
        self._submit_started = True

    def bind_browser_authority(self, bundle: BrowserLeaseBundle) -> None:
        if bundle.profile.attempt_id != self.attempt_id:
            raise ValueError("browser bundle attempt does not match supervisor")
        if bundle.profile.owner_id != self.actor_id:
            raise ValueError("browser bundle actor does not match supervisor")
        if bundle.page_binding.attempt_id != self.attempt_id:
            raise ValueError("page binding attempt does not match supervisor")
        self._browser_bundle = bundle

    def bind_provider_session(self, provider_session_id: str | None) -> None:
        if provider_session_id is not None:
            _required(provider_session_id, "provider_session_id")
        self._provider_session_id = provider_session_id

    def restart_browser(self, browser_runtime: str, *, headless: bool) -> subprocess.Popen[object]:
        if self._submit_started:
            raise PersistentSessionError(
                "browser/runtime generation cannot change after submit_started"
            )
        if self._browser_bundle is not None:
            self._release_browser_authority()
            self._browser_bundle = None
        process = self.browser_worker.restart_for_application(
            application_session_id=self.application_session_id,
            start_url=self.start_url,
            browser_runtime=browser_runtime,
            headless=headless,
            submit_started=self._submit_started,
        )
        return process

    def context_bundle(
        self,
        *,
        namespace: RuntimeNamespace,
        phase: str,
        runtime_backend: str,
    ) -> ContextBundle:
        bundle = self._browser_bundle
        if bundle is None:
            raise PersistentSessionError("browser authority was not bound")
        endpoint = self.browser_worker.heartbeat(
            expected_generation=self.browser_worker.generation
        )
        return ContextBundle(
            namespace=namespace,
            worker_id=self.browser_worker.worker_id,
            application_session_id=self.application_session_id,
            actor_id=self.actor_id,
            attempt_id=self.attempt_id,
            phase=phase,
            runtime_backend=runtime_backend,
            browser_runtime=self.browser_worker.browser_runtime,
            browser_profile_id=bundle.profile.resource_id,
            browser_generation=self.browser_worker.generation,
            endpoint=endpoint,
            root_target_ids=self.browser_worker.active_targets,
            page_binding=bundle.page_binding.as_dict(),
            provider_session_id=self._provider_session_id,
        )

    def close_application(self, *, recycle_worker: bool = False) -> None:
        if self._closed:
            return
        try:
            self.browser_worker.end_application(self.application_session_id)
        finally:
            try:
                if self._browser_bundle is not None:
                    self._release_browser_authority()
                    self._browser_bundle = None
            finally:
                try:
                    if recycle_worker:
                        self.browser_worker.recycle_idle_generation()
                finally:
                    self._closed = True


class DisabledResponsesRuntimeAdapter:
    """Default-off Responses seam; never reads credentials or performs I/O."""

    enabled = False

    def run(self, _context: ContextBundle) -> object:
        raise EndpointUnavailable("Responses runtime is disabled")

    def resume(self, _context: ContextBundle, _provider_session_id: str) -> object:
        raise EndpointUnavailable("Responses runtime is disabled")

    def close_application(self, _application_session_id: str) -> None:
        return None


__all__ = [
    "ApplicationSupervisor",
    "BrowserWorkerProcess",
    "ContextBundle",
    "DisabledResponsesRuntimeAdapter",
    "EndpointDescriptor",
    "EndpointManager",
    "EndpointUnavailable",
    "LoopbackHttpEndpointManager",
    "LoopbackHttpEndpointSpec",
    "LoopbackPortReservation",
    "PerTurnStdioEndpointManager",
    "PersistentSessionError",
    "StaleBrowserGeneration",
    "SubprocessEndpointManager",
    "exact_listener_owner_probe",
    "resolve_persistent_playwright_launcher",
]
