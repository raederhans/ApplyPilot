"""Process-wide physical ownership for one Chromium user-data directory.

The JSON sidecar is diagnostic only.  Authority comes exclusively from the
thread-owned Windows named mutex.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import secrets
import threading
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF
STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class ProfileLockError(RuntimeError):
    """Base class for physical-profile ownership failures."""


class ProfileLockConflict(ProfileLockError):
    """Another live owner holds the profile mutex."""


class ProfileRecoveryRequired(ProfileLockError):
    """Ownership or process state is ambiguous and needs operator recovery."""


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    creation_filetime: int


def normalize_profile_path(profile_path: str | os.PathLike[str]) -> Path:
    """Return the canonical absolute profile path used as the lock identity."""
    return Path(profile_path).expanduser().resolve(strict=False)


def mutex_name_for_profile(profile_path: str | os.PathLike[str]) -> str:
    canonical = os.path.normcase(str(normalize_profile_path(profile_path))).casefold()
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return rf"Local\ApplyPilot.ProfileLock.v1.{digest}"


def sidecar_path_for_profile(profile_path: str | os.PathLike[str]) -> Path:
    profile = normalize_profile_path(profile_path)
    digest = mutex_name_for_profile(profile).rsplit(".", 1)[-1]
    return profile.parent / f".applypilot-profile-lock-{digest}.json"


def inspect_process_identity(pid: int, *, kernel=None) -> ProcessIdentity | None:
    """Return a PID-reuse-safe live-process identity on supported local hosts."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
        raise ValueError("pid must be a positive integer")
    if kernel is not None:
        return kernel.process_identity(pid)
    if os.name == "nt":
        return _Kernel32().process_identity(pid)
    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        raw = stat_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ProfileRecoveryRequired(f"Cannot inspect process {pid}") from error
    command_end = raw.rfind(")")
    fields = raw[command_end + 2 :].split() if command_end >= 0 else []
    if len(fields) <= 19:
        raise ProfileRecoveryRequired(f"Cannot parse process {pid} creation time")
    try:
        birth_token = int(fields[19])
    except ValueError as error:
        raise ProfileRecoveryRequired(
            f"Cannot parse process {pid} creation time"
        ) from error
    if birth_token < 1:
        raise ProfileRecoveryRequired(f"Invalid process {pid} creation time")
    return ProcessIdentity(pid=pid, creation_filetime=birth_token)


class _Kernel32:
    def __init__(self) -> None:
        if os.name != "nt":
            raise ProfileRecoveryRequired("Windows profile mutex is unavailable")
        self._dll = ctypes.WinDLL("kernel32", use_last_error=True)
        self._dll.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        self._dll.CreateMutexW.restype = wintypes.HANDLE
        self._dll.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._dll.WaitForSingleObject.restype = wintypes.DWORD
        self._dll.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self._dll.ReleaseMutex.restype = wintypes.BOOL
        self._dll.CloseHandle.argtypes = [wintypes.HANDLE]
        self._dll.CloseHandle.restype = wintypes.BOOL
        self._dll.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._dll.OpenProcess.restype = wintypes.HANDLE
        self._dll.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
        self._dll.GetExitCodeProcess.restype = wintypes.BOOL
        self._dll.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            wintypes.LPFILETIME,
            wintypes.LPFILETIME,
            wintypes.LPFILETIME,
            wintypes.LPFILETIME,
        ]
        self._dll.GetProcessTimes.restype = wintypes.BOOL

    def create_mutex(self, name: str) -> int:
        return int(self._dll.CreateMutexW(None, False, name) or 0)

    def wait(self, handle: int) -> int:
        return int(self._dll.WaitForSingleObject(handle, 0))

    def release(self, handle: int) -> bool:
        return bool(self._dll.ReleaseMutex(handle))

    def close(self, handle: int) -> bool:
        return bool(self._dll.CloseHandle(handle))

    def last_error(self) -> int:
        return int(ctypes.get_last_error())

    def process_identity(self, pid: int) -> ProcessIdentity | None:
        handle = self._dll.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: no such PID
                return None
            raise ProfileRecoveryRequired(f"Cannot inspect process {pid}")
        try:
            exit_code = wintypes.DWORD()
            if not self._dll.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                raise ProfileRecoveryRequired(f"Cannot read process {pid} exit state")
            if exit_code.value != STILL_ACTIVE:
                return None
            created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if not self._dll.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                raise ProfileRecoveryRequired(f"Cannot read process {pid} creation time")
            value = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
            return ProcessIdentity(pid=pid, creation_filetime=value)
        finally:
            self._dll.CloseHandle(handle)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ProfileLock:
    """A thread-owned named mutex held for a browser profile's full lifetime."""

    def __init__(self, profile_path: str | os.PathLike[str], *, kernel=None) -> None:
        self.profile_path = normalize_profile_path(profile_path)
        self.name = mutex_name_for_profile(self.profile_path)
        self.sidecar_path = sidecar_path_for_profile(self.profile_path)
        self._kernel = kernel or _Kernel32()
        self._handle: int | None = None
        self._mutex_owned = False
        self._handle_open = False
        self._owner_thread: int | None = None
        self._nonce = secrets.token_hex(16)
        self._launcher: ProcessIdentity | None = None
        self._browser: ProcessIdentity | None = None
        self._spawn_attempted = False
        self._requires_recovery = False

    @property
    def held(self) -> bool:
        return self._mutex_owned

    @property
    def has_native_resource(self) -> bool:
        return self._mutex_owned or self._handle_open

    @property
    def spawn_attempted(self) -> bool:
        return self._spawn_attempted

    @property
    def requires_recovery(self) -> bool:
        return self._requires_recovery

    @property
    def owned_by_current_thread(self) -> bool:
        return self.has_native_resource and self._owner_thread == threading.get_ident()

    def _read_existing_sidecar(self) -> dict[str, Any] | None:
        if not self.sidecar_path.exists():
            return None
        try:
            payload = json.loads(self.sidecar_path.read_text(encoding="utf-8"))
            required = {"version", "profile_path", "mutex_name", "nonce", "state"}
            if (
                not isinstance(payload, dict)
                or not required.issubset(payload)
                or payload.get("version") != 1
                or payload.get("profile_path") != str(self.profile_path)
                or payload.get("mutex_name") != self.name
                or not isinstance(payload.get("nonce"), str)
                or not payload["nonce"]
                or payload.get("state")
                not in {"acquired", "spawn_attempted", "running", "recovery_required"}
            ):
                raise ValueError("invalid profile sidecar")
            return payload
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ProfileRecoveryRequired(f"Malformed profile lock sidecar: {exc}") from exc

    def _identity_from_payload(self, value: Any) -> ProcessIdentity | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ProfileRecoveryRequired("Malformed process identity in profile sidecar")
        try:
            identity = ProcessIdentity(int(value["pid"]), int(value["creation_filetime"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ProfileRecoveryRequired("Malformed process identity in profile sidecar") from exc
        if identity.pid <= 0 or identity.creation_filetime <= 0:
            raise ProfileRecoveryRequired("Invalid process identity in profile sidecar")
        return identity

    def _check_existing_sidecar(self) -> None:
        payload = self._read_existing_sidecar()
        if payload is None:
            return
        raise ProfileRecoveryRequired(
            f"Existing {payload['state']} profile sidecar requires explicit recovery"
        )

    @staticmethod
    def _identity_payload(identity: ProcessIdentity | None) -> dict[str, int] | None:
        if identity is None:
            return None
        return {"pid": identity.pid, "creation_filetime": identity.creation_filetime}

    def _write_sidecar(self, state: str, *, reason: str | None = None) -> None:
        _atomic_write_json(
            self.sidecar_path,
            {
                "version": 1,
                "profile_path": str(self.profile_path),
                "mutex_name": self.name,
                "nonce": self._nonce,
                "state": state,
                "launcher": self._identity_payload(self._launcher),
                "browser": self._identity_payload(self._browser),
                "reason": reason,
            },
        )

    def acquire(self) -> ProfileLock:
        if self.has_native_resource:
            raise ProfileLockError("Profile lock is already held")
        handle = self._kernel.create_mutex(self.name)
        if not handle:
            raise ProfileRecoveryRequired(
                f"CreateMutexW failed with error {self._kernel.last_error()}"
            )
        self._handle = handle
        self._handle_open = True
        self._owner_thread = threading.get_ident()
        result = self._kernel.wait(handle)
        if result == WAIT_TIMEOUT:
            if self._kernel.close(handle):
                self._handle_open = False
                self._handle = None
                self._owner_thread = None
            raise ProfileLockConflict(f"Profile is already owned: {self.profile_path}")
        if result == WAIT_FAILED:
            error = self._kernel.last_error()
            if self._kernel.close(handle):
                self._handle_open = False
                self._handle = None
                self._owner_thread = None
            raise ProfileRecoveryRequired(f"WaitForSingleObject failed with error {error}")
        if result == WAIT_ABANDONED:
            self._owner_thread = threading.get_ident()
            self._mutex_owned = True
            self._requires_recovery = True
            try:
                self._write_sidecar("recovery_required", reason="abandoned_mutex")
            finally:
                self._release_mutex(remove_sidecar=False)
            raise ProfileRecoveryRequired(f"Abandoned profile mutex: {self.profile_path}")
        if result != WAIT_OBJECT_0:
            if self._kernel.close(handle):
                self._handle_open = False
                self._handle = None
                self._owner_thread = None
            raise ProfileRecoveryRequired(f"Unknown mutex wait result: {result}")
        self._mutex_owned = True
        self._owner_thread = threading.get_ident()
        try:
            self._check_existing_sidecar()
            launcher = self._kernel.process_identity(os.getpid())
            if launcher is None:
                raise ProfileRecoveryRequired("Launcher process identity disappeared")
            self._launcher = launcher
            self._write_sidecar("acquired")
        except Exception:
            self._requires_recovery = True
            self._release_mutex(remove_sidecar=False)
            raise
        return self

    def record_spawn_attempt(self) -> None:
        self._require_owner()
        self._spawn_attempted = True
        self._write_sidecar("spawn_attempted")

    def record_browser(self, pid: int) -> None:
        self._require_owner()
        if not self._spawn_attempted:
            raise ProfileLockError("Browser holder cannot be recorded before spawn")
        identity = self._kernel.process_identity(pid)
        if identity is None:
            raise ProfileRecoveryRequired(f"Spawned browser process {pid} is not alive")
        self._browser = identity
        self._write_sidecar("running")

    def actual_browser_stopped(self) -> bool:
        self._require_owner()
        if self._browser is None:
            raise ProfileRecoveryRequired("Actual browser process identity is unknown")
        current = self._kernel.process_identity(self._browser.pid)
        return current != self._browser

    def mark_recovery_required(self) -> None:
        self._require_owner()
        self._requires_recovery = True
        self._write_sidecar("recovery_required")

    def release_before_spawn(self) -> None:
        self._require_owner()
        if self._spawn_attempted:
            raise ProfileLockError("Browser spawn was attempted; confirmed stop is required")
        self._release_mutex(remove_sidecar=True)

    def release_after_stop(self, *, profile_path: Path, browser_stopped: bool) -> None:
        self._require_owner()
        if normalize_profile_path(profile_path) != self.profile_path:
            raise ProfileLockError("Refusing to release for a different profile")
        if self._browser is None or not browser_stopped or not self.actual_browser_stopped():
            raise ProfileRecoveryRequired("Browser stop was not confirmed")
        self._release_mutex(remove_sidecar=True)

    def _require_owner(self) -> None:
        if not self.has_native_resource:
            raise ProfileLockError("Profile lock has no native resource")
        if self._owner_thread != threading.get_ident():
            raise ProfileLockError("Profile mutex may only be released by its owner thread")

    def _release_mutex(self, *, remove_sidecar: bool) -> None:
        handle = self._handle
        if handle is None or not self.has_native_resource:
            return
        if self._mutex_owned:
            if not self._kernel.release(handle):
                self._requires_recovery = True
                raise ProfileRecoveryRequired("ReleaseMutex failed")
            self._mutex_owned = False
        if self._handle_open:
            if not self._kernel.close(handle):
                self._requires_recovery = True
                raise ProfileRecoveryRequired("CloseHandle failed")
            self._handle_open = False
            self._handle = None
        if remove_sidecar:
            try:
                self.sidecar_path.unlink(missing_ok=True)
            except OSError as exc:
                self._requires_recovery = True
                raise ProfileRecoveryRequired("Profile sidecar cleanup failed") from exc
            self._requires_recovery = False
        if not self.has_native_resource:
            self._owner_thread = None
