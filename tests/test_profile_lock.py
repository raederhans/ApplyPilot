from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import ClassVar

import pytest

from applypilot.apply import chrome
from applypilot.apply.profile_lock import (
    WAIT_ABANDONED,
    WAIT_FAILED,
    WAIT_OBJECT_0,
    WAIT_TIMEOUT,
    ProcessIdentity,
    ProfileLock,
    ProfileLockConflict,
    ProfileLockError,
    ProfileRecoveryRequired,
    _Kernel32,
    inspect_process_identity,
    mutex_name_for_profile,
    normalize_profile_path,
    sidecar_path_for_profile,
)


class FakeKernel:
    def __init__(self, wait_result: int = WAIT_OBJECT_0) -> None:
        self.wait_result = wait_result
        self.events: list[tuple[str, object]] = []
        self.identities: dict[int, ProcessIdentity | None | Exception] = {
            os.getpid(): ProcessIdentity(os.getpid(), 100)
        }

    def create_mutex(self, name: str) -> int:
        self.events.append(("create", name))
        return 41

    def wait(self, handle: int) -> int:
        self.events.append(("wait", handle))
        return self.wait_result

    def release(self, handle: int) -> bool:
        self.events.append(("release", handle))
        return True

    def close(self, handle: int) -> bool:
        self.events.append(("close", handle))
        return True

    @staticmethod
    def last_error() -> int:
        return 123

    def process_identity(self, pid: int) -> ProcessIdentity | None:
        value = self.identities.get(pid)
        if isinstance(value, Exception):
            raise value
        return value


def test_process_identity_helper_preserves_pid_and_birth_token() -> None:
    kernel = FakeKernel()
    kernel.identities[77] = ProcessIdentity(77, 123_456)
    assert inspect_process_identity(77, kernel=kernel) == ProcessIdentity(77, 123_456)
    kernel.identities[77] = None
    assert inspect_process_identity(77, kernel=kernel) is None


def test_path_identity_is_absolute_casefolded_and_stable(tmp_path: Path) -> None:
    profile = tmp_path / "Profiles" / ".." / "Profiles" / "Worker-1"
    canonical = normalize_profile_path(profile)
    assert canonical.is_absolute()
    assert mutex_name_for_profile(profile).startswith("Local\\ApplyPilot.ProfileLock.v1.")
    assert mutex_name_for_profile(profile) == mutex_name_for_profile(
        Path(str(canonical).swapcase())
    )
    assert sidecar_path_for_profile(profile).parent == canonical.parent


@pytest.mark.parametrize(
    ("result", "error", "events"),
    [
        (WAIT_TIMEOUT, ProfileLockConflict, ["create", "wait", "close"]),
        (WAIT_FAILED, ProfileRecoveryRequired, ["create", "wait", "close"]),
        (
            WAIT_ABANDONED,
            ProfileRecoveryRequired,
            ["create", "wait", "release", "close"],
        ),
        (777, ProfileRecoveryRequired, ["create", "wait", "close"]),
    ],
)
def test_wait_failures_close_handles_in_order(
    tmp_path: Path, result: int, error: type[Exception], events: list[str]
) -> None:
    kernel = FakeKernel(result)
    with pytest.raises(error):
        ProfileLock(tmp_path / "profile", kernel=kernel).acquire()
    assert [event[0] for event in kernel.events] == events


def test_sidecar_is_atomic_and_malformed_state_fails_closed(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    sidecar = sidecar_path_for_profile(profile)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("not-json", encoding="utf-8")
    kernel = FakeKernel()

    with pytest.raises(ProfileRecoveryRequired, match="Malformed"):
        ProfileLock(profile, kernel=kernel).acquire()

    assert [event[0] for event in kernel.events][-2:] == ["release", "close"]
    assert not list(sidecar.parent.glob(f"{sidecar.name}.*.tmp"))


def test_pid_reuse_still_requires_explicit_recovery(tmp_path: Path) -> None:
    profile = normalize_profile_path(tmp_path / "profile")
    sidecar = sidecar_path_for_profile(profile)
    sidecar.write_text(
        json.dumps(
            {
                "version": 1,
                "profile_path": str(profile),
                "mutex_name": mutex_name_for_profile(profile),
                "nonce": "old-generation",
                "state": "recovery_required",
                "launcher": {"pid": 90, "creation_filetime": 1000},
                "browser": None,
            }
        ),
        encoding="utf-8",
    )
    kernel = FakeKernel()
    kernel.identities[90] = ProcessIdentity(90, 2000)
    with pytest.raises(ProfileRecoveryRequired, match="explicit recovery"):
        ProfileLock(profile, kernel=kernel).acquire()
    assert sidecar.exists()


def test_unknown_process_identity_fails_closed(tmp_path: Path) -> None:
    profile = normalize_profile_path(tmp_path / "profile")
    sidecar = sidecar_path_for_profile(profile)
    sidecar.write_text(
        json.dumps(
            {
                "version": 1,
                "profile_path": str(profile),
                "mutex_name": mutex_name_for_profile(profile),
                "nonce": "old-generation",
                "state": "running",
                "launcher": {"pid": 90, "creation_filetime": 1000},
                "browser": None,
            }
        ),
        encoding="utf-8",
    )
    kernel = FakeKernel()
    kernel.identities[90] = ProfileRecoveryRequired("access denied")
    with pytest.raises(ProfileRecoveryRequired, match="explicit recovery"):
        ProfileLock(profile, kernel=kernel).acquire()


def test_mutex_release_is_thread_affine(tmp_path: Path) -> None:
    lock = ProfileLock(tmp_path / "profile", kernel=FakeKernel()).acquire()
    errors: list[Exception] = []

    def release_elsewhere() -> None:
        try:
            lock.release_before_spawn()
        except Exception as exc:  # noqa: BLE001 - assertion captures exact contract
            errors.append(exc)

    thread = threading.Thread(target=release_elsewhere)
    thread.start()
    thread.join()
    assert isinstance(errors[0], ProfileLockError)
    assert lock.held
    lock.release_before_spawn()


@pytest.mark.skipif(os.name != "nt", reason="requires the Windows kernel mutex")
def test_real_windows_mutex_conflict_release_and_abandoned(tmp_path: Path) -> None:
    profile = str(tmp_path / "profile")
    helper = (
        "import sys; from applypilot.apply.profile_lock import ProfileLock; "
        "lock=ProfileLock(sys.argv[1]).acquire(); print('READY', flush=True); "
        "sys.stdin.readline(); lock.release_before_spawn()"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", helper, profile],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "READY"
    with pytest.raises(ProfileLockConflict):
        ProfileLock(profile).acquire()
    assert holder.stdin is not None
    holder.stdin.write("release\n")
    holder.stdin.flush()
    assert holder.wait(timeout=10) == 0
    ProfileLock(profile).acquire().release_before_spawn()

    abandoned = subprocess.Popen(
        [sys.executable, "-c", helper, profile],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert abandoned.stdout is not None
    assert abandoned.stdout.readline().strip() == "READY"
    kernel = _Kernel32()
    waiting_handle = kernel.create_mutex(mutex_name_for_profile(profile))

    class ExistingHandleKernel(_Kernel32):
        def create_mutex(self, _name: str) -> int:
            return waiting_handle

    abandoned.kill()
    abandoned.wait(timeout=10)
    with pytest.raises(ProfileRecoveryRequired, match="Abandoned"):
        ProfileLock(profile, kernel=ExistingHandleKernel()).acquire()
    with pytest.raises(ProfileRecoveryRequired, match="explicit recovery"):
        ProfileLock(profile).acquire()


class FakeProfileLock:
    events: ClassVar[list[str]] = []

    def __init__(self, profile: Path) -> None:
        self.profile_path = Path(profile)
        self.sidecar_path = self.profile_path.parent / ".fake-sidecar"
        self.held = False

    def acquire(self):
        self.held = True
        self.events.append("lock")
        return self

    @property
    def owned_by_current_thread(self) -> bool:
        return self.held

    @property
    def has_native_resource(self) -> bool:
        return self.held

    @property
    def spawn_attempted(self) -> bool:
        return "spawn-attempted" in self.events

    @property
    def requires_recovery(self) -> bool:
        return False

    def record_browser(self, _pid: int) -> None:
        self.events.append("spawned")

    def record_spawn_attempt(self) -> None:
        self.events.append("spawn-attempted")

    def actual_browser_stopped(self) -> bool:
        return True

    def release_before_spawn(self) -> None:
        self.events.append("release-before-spawn")
        self.held = False

    def release_after_stop(self, *, profile_path: Path, browser_stopped: bool) -> None:
        assert Path(profile_path) == self.profile_path
        assert browser_stopped
        self.events.append("release-after-stop")
        self.held = False

    def mark_recovery_required(self) -> None:
        self.events.append("recovery-required")


def test_chrome_locks_before_profile_mutation(monkeypatch, tmp_path: Path) -> None:
    FakeProfileLock.events = []
    profile = tmp_path / "edge" / "worker-1"
    monkeypatch.setattr(chrome, "ProfileLock", FakeProfileLock)
    monkeypatch.setattr(chrome, "resolve_worker_profile_path", lambda *_args: profile)
    monkeypatch.setattr(
        chrome,
        "setup_worker_profile",
        lambda *_args: FakeProfileLock.events.append("mutate") or profile,
    )
    monkeypatch.setattr(chrome, "_suppress_restore_nag", lambda _p: None)
    monkeypatch.setattr(chrome, "get_browser_executable", lambda _b: (_ for _ in ()).throw(ValueError("bad exe")))

    with pytest.raises(ValueError, match="bad exe"):
        chrome.launch_chrome(1, port=9551)
    assert FakeProfileLock.events == ["lock", "mutate", "release-before-spawn"]


def test_failed_spawn_with_unknown_child_retains_lock(monkeypatch, tmp_path: Path) -> None:
    FakeProfileLock.events = []
    profile = tmp_path / "edge" / "worker-2"

    class Process:
        pid = 222

        @staticmethod
        def poll():
            return 0

    monkeypatch.setattr(chrome, "ProfileLock", FakeProfileLock)
    monkeypatch.setattr(chrome, "resolve_worker_profile_path", lambda *_args: profile)
    monkeypatch.setattr(chrome, "setup_worker_profile", lambda *_args: profile)
    monkeypatch.setattr(chrome, "_suppress_restore_nag", lambda _p: None)
    monkeypatch.setattr(chrome, "get_browser_executable", lambda _b: "edge.exe")
    monkeypatch.setattr(chrome.platform, "system", lambda: "Windows")
    monkeypatch.setattr(chrome.subprocess, "Popen", lambda *_a, **_k: Process())
    monkeypatch.setattr(chrome, "_wait_for_cdp_ready", lambda *_a: (_ for _ in ()).throw(RuntimeError("failed")))
    monkeypatch.setattr(chrome, "_close_browser_via_cdp", lambda _p: None)
    monkeypatch.setattr(chrome, "_wait_for_browser_stopped", lambda *_a: False)

    with pytest.raises(RuntimeError, match="failed"):
        chrome.launch_chrome(2, port=9552)
    assert FakeProfileLock.events[-1] == "recovery-required"
    assert 2 in chrome._profile_locks
    chrome._profile_locks.pop(2)
    chrome._profile_paths.pop(2)
    chrome._chrome_procs.pop(2)
    chrome._chrome_ports.pop(2)


def test_actual_holder_must_disappear_before_release(tmp_path: Path) -> None:
    kernel = FakeKernel()
    kernel.identities[222] = ProcessIdentity(222, 500)
    lock = ProfileLock(tmp_path / "profile", kernel=kernel).acquire()
    lock.record_spawn_attempt()
    lock.record_browser(222)
    with pytest.raises(ProfileRecoveryRequired, match="not confirmed"):
        lock.release_after_stop(profile_path=lock.profile_path, browser_stopped=True)
    assert lock.held
    kernel.identities[222] = None
    lock.release_after_stop(profile_path=lock.profile_path, browser_stopped=True)
    assert not lock.held


def test_actual_holder_unknown_state_fails_closed(tmp_path: Path) -> None:
    kernel = FakeKernel()
    kernel.identities[223] = ProcessIdentity(223, 501)
    lock = ProfileLock(tmp_path / "profile", kernel=kernel).acquire()
    lock.record_spawn_attempt()
    lock.record_browser(223)
    kernel.identities[223] = ProfileRecoveryRequired("process inspection denied")
    with pytest.raises(ProfileRecoveryRequired, match="inspection denied"):
        lock.actual_browser_stopped()
    assert lock.held


@pytest.mark.parametrize("failure", ["release", "close"])
def test_release_or_close_failure_keeps_diagnostic_state(
    tmp_path: Path, failure: str
) -> None:
    class FailingKernel(FakeKernel):
        def release(self, handle: int) -> bool:
            super().release(handle)
            return failure != "release"

        def close(self, handle: int) -> bool:
            super().close(handle)
            return failure != "close"

    lock = ProfileLock(tmp_path / "profile", kernel=FailingKernel()).acquire()
    sidecar = lock.sidecar_path
    with pytest.raises(ProfileRecoveryRequired):
        lock.release_before_spawn()
    assert lock.held is (failure == "release")
    assert lock.has_native_resource
    assert sidecar.exists()


def test_close_retry_never_releases_mutex_twice(tmp_path: Path) -> None:
    class FlakyCloseKernel(FakeKernel):
        close_attempts = 0

        def close(self, handle: int) -> bool:
            self.close_attempts += 1
            super().close(handle)
            return self.close_attempts > 1

    kernel = FlakyCloseKernel()
    lock = ProfileLock(tmp_path / "profile", kernel=kernel).acquire()
    with pytest.raises(ProfileRecoveryRequired, match="CloseHandle"):
        lock.release_before_spawn()
    assert not lock.held
    assert lock.has_native_resource
    lock.release_before_spawn()
    assert [event[0] for event in kernel.events].count("release") == 1
    assert [event[0] for event in kernel.events].count("close") == 2
    assert not lock.has_native_resource


def test_unlink_failure_does_not_claim_mutex_is_held(
    monkeypatch, tmp_path: Path
) -> None:
    lock = ProfileLock(tmp_path / "profile", kernel=FakeKernel()).acquire()
    sidecar = lock.sidecar_path
    original_unlink = Path.unlink

    def fail_sidecar_unlink(path: Path, *args, **kwargs):
        if path == sidecar:
            raise OSError("sidecar busy")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_sidecar_unlink)
    with pytest.raises(ProfileRecoveryRequired, match="sidecar cleanup"):
        lock.release_before_spawn()
    assert not lock.held
    assert not lock.has_native_resource
    assert sidecar.exists()


def test_worker_generation_blocks_backend_switch_before_mutation(
    monkeypatch, tmp_path: Path
) -> None:
    worker_id = 71
    chrome._profile_locks[worker_id] = object()
    mutations: list[str] = []
    monkeypatch.setattr(
        chrome,
        "setup_worker_profile",
        lambda *_args: mutations.append("mutated"),
    )
    monkeypatch.setattr(
        chrome,
        "resolve_worker_profile_path",
        lambda _worker, backend: tmp_path / backend / f"worker-{worker_id}",
    )
    with pytest.raises(RuntimeError, match="unresolved browser generation"):
        chrome.launch_chrome(worker_id, port=9771, browser_backend="cloak")
    assert mutations == []
    chrome._profile_locks.pop(worker_id)


def test_worker_generation_blocks_concurrent_launch_reservation(
    monkeypatch, tmp_path: Path
) -> None:
    worker_id = 72
    chrome._launching_workers.add(worker_id)
    monkeypatch.setattr(
        chrome,
        "resolve_worker_profile_path",
        lambda *_args: tmp_path / "edge" / f"worker-{worker_id}",
    )
    with pytest.raises(RuntimeError, match="unresolved browser generation"):
        chrome.launch_chrome(worker_id, port=9772)
    chrome._launching_workers.remove(worker_id)


def test_cleanup_does_not_prune_when_actual_child_is_still_alive(
    monkeypatch, tmp_path: Path
) -> None:
    worker_id = 73
    profile = tmp_path / "edge" / f"worker-{worker_id}"

    class Process:
        pid = 730

        @staticmethod
        def poll():
            return 0

    class Lock:
        held = True
        owned_by_current_thread = True

        @staticmethod
        def actual_browser_stopped():
            return False

    process = Process()
    chrome._chrome_procs[worker_id] = process
    chrome._chrome_ports[worker_id] = 9773
    chrome._profile_locks[worker_id] = Lock()
    chrome._profile_paths[worker_id] = profile
    pruned: list[Path] = []
    monkeypatch.setattr(chrome, "_close_browser_via_cdp", lambda _port: None)
    monkeypatch.setattr(chrome, "_wait_for_browser_stopped", lambda *_args: True)
    monkeypatch.setattr(chrome, "prune_owned_profile", lambda path, **_kwargs: pruned.append(path))
    chrome.cleanup_worker(worker_id, process)
    assert pruned == []
    assert worker_id in chrome._profile_locks
    chrome._chrome_procs.pop(worker_id)
    chrome._chrome_ports.pop(worker_id)
    chrome._profile_locks.pop(worker_id)
    chrome._profile_paths.pop(worker_id)


def test_unresolved_generation_retains_cdp_claim_and_rejects_reallocate(
    monkeypatch, tmp_path: Path
) -> None:
    worker_id = 74
    port = 9774
    lock_file = tmp_path / "cdp-port-locks" / f"{port}.lock"
    lock_file.parent.mkdir(parents=True)
    lock_file.write_text("pid=1 worker=74\n", encoding="ascii")

    class Process:
        pid = 740

        @staticmethod
        def poll():
            return 0

    class Lock:
        held = True
        has_native_resource = True
        owned_by_current_thread = True
        spawn_attempted = True

        @staticmethod
        def actual_browser_stopped():
            return False

    process = Process()
    chrome._cdp_port_claims[worker_id] = (port, lock_file)
    chrome._chrome_procs[worker_id] = process
    chrome._chrome_ports[worker_id] = port
    chrome._profile_locks[worker_id] = Lock()
    chrome._profile_paths[worker_id] = tmp_path / "edge" / f"worker-{worker_id}"
    closed: list[int] = []
    monkeypatch.setattr(chrome.config, "APPLY_WORKER_DIR", tmp_path)
    monkeypatch.setattr(chrome, "_close_browser_via_cdp", closed.append)
    monkeypatch.setattr(chrome, "_wait_for_browser_stopped", lambda *_args: True)

    chrome.cleanup_worker(worker_id, process)
    assert chrome.release_cdp_port(worker_id) is False
    assert lock_file.exists()
    assert chrome._chrome_ports[worker_id] == port
    with pytest.raises(RuntimeError, match="unresolved CDP generation"):
        chrome.allocate_cdp_port(worker_id)
    assert closed == [port]

    chrome._chrome_procs.pop(worker_id)
    chrome._chrome_ports.pop(worker_id)
    chrome._profile_locks.pop(worker_id)
    chrome._profile_paths.pop(worker_id)
    chrome._cdp_port_claims.pop(worker_id)
    lock_file.unlink()


def test_pre_spawn_release_failure_stays_registered(monkeypatch, tmp_path: Path) -> None:
    worker_id = 75
    profile = tmp_path / "edge" / f"worker-{worker_id}"

    class Lock:
        def __init__(self, path):
            self.profile_path = Path(path)
            self.sidecar_path = tmp_path / ".release-failed"
            self.has_native_resource = False
            self.requires_recovery = False

        def acquire(self):
            self.has_native_resource = True
            return self

        def release_before_spawn(self):
            raise ProfileRecoveryRequired("release failed")

    monkeypatch.setattr(chrome, "ProfileLock", Lock)
    monkeypatch.setattr(chrome, "resolve_worker_profile_path", lambda *_args: profile)
    monkeypatch.setattr(
        chrome,
        "setup_worker_profile",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("setup failed")),
    )
    with pytest.raises(RuntimeError, match="setup failed"):
        chrome.launch_chrome(worker_id, port=9775)
    assert worker_id not in chrome._launching_workers
    assert worker_id in chrome._profile_locks
    chrome._profile_locks.pop(worker_id)
    chrome._profile_paths.pop(worker_id)


def test_acquire_cleanup_failure_stays_registered(monkeypatch, tmp_path: Path) -> None:
    worker_id = 76
    profile = tmp_path / "edge" / f"worker-{worker_id}"

    class Lock:
        def __init__(self, path):
            self.profile_path = Path(path)
            self.sidecar_path = tmp_path / ".acquire-failed"
            self.has_native_resource = False
            self.requires_recovery = False

        def acquire(self):
            self.has_native_resource = True
            raise ProfileRecoveryRequired("acquire cleanup failed")

    monkeypatch.setattr(chrome, "ProfileLock", Lock)
    monkeypatch.setattr(chrome, "resolve_worker_profile_path", lambda *_args: profile)
    with pytest.raises(ProfileRecoveryRequired, match="cleanup failed"):
        chrome.launch_chrome(worker_id, port=9776)
    assert worker_id not in chrome._launching_workers
    assert worker_id in chrome._profile_locks
    chrome._profile_locks.pop(worker_id)
    chrome._profile_paths.pop(worker_id)


def test_recovery_sidecar_write_failure_keeps_full_generation_registered(
    monkeypatch, tmp_path: Path
) -> None:
    worker_id = 77
    profile = tmp_path / "edge" / f"worker-{worker_id}"

    class Lock(FakeProfileLock):
        def mark_recovery_required(self) -> None:
            raise OSError("sidecar write failed")

    class Process:
        pid = 770

        @staticmethod
        def poll():
            return 0

    monkeypatch.setattr(chrome, "ProfileLock", Lock)
    monkeypatch.setattr(chrome, "resolve_worker_profile_path", lambda *_args: profile)
    monkeypatch.setattr(chrome, "setup_worker_profile", lambda *_args: profile)
    monkeypatch.setattr(chrome, "_suppress_restore_nag", lambda _path: None)
    monkeypatch.setattr(chrome, "get_browser_executable", lambda _backend: "edge.exe")
    monkeypatch.setattr(chrome.platform, "system", lambda: "Windows")
    monkeypatch.setattr(chrome.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(
        chrome,
        "_wait_for_cdp_ready",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("readiness failed")),
    )
    monkeypatch.setattr(chrome, "_close_browser_via_cdp", lambda _port: None)
    monkeypatch.setattr(chrome, "_wait_for_browser_stopped", lambda *_args: False)
    with pytest.raises(OSError, match="sidecar write failed"):
        chrome.launch_chrome(worker_id, port=9777)
    assert worker_id not in chrome._launching_workers
    assert worker_id in chrome._profile_locks
    assert worker_id in chrome._profile_paths
    assert worker_id in chrome._chrome_procs
    assert chrome._chrome_ports[worker_id] == 9777
    chrome._profile_locks.pop(worker_id)
    chrome._profile_paths.pop(worker_id)
    chrome._chrome_procs.pop(worker_id)
    chrome._chrome_ports.pop(worker_id)


def test_cdp_release_is_cas_reserved_during_blocked_unlink(
    monkeypatch, tmp_path: Path
) -> None:
    worker_id = 78
    port = 9778
    lock_file = tmp_path / "cdp-port-locks" / f"{port}.lock"
    lock_file.parent.mkdir(parents=True)
    lock_file.write_text("pid=1 worker=78\n", encoding="ascii")
    claim = (port, lock_file)
    chrome._cdp_port_claims[worker_id] = claim
    monkeypatch.setattr(chrome.config, "APPLY_WORKER_DIR", tmp_path)
    entered = threading.Event()
    allow_failure = threading.Event()
    original_unlink = Path.unlink

    def blocked_unlink(path: Path, *args, **kwargs):
        if path == lock_file:
            entered.set()
            assert allow_failure.wait(timeout=5)
            raise OSError("deterministic unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", blocked_unlink)
    results: list[bool] = []
    releaser = threading.Thread(
        target=lambda: results.append(chrome.release_cdp_port(worker_id))
    )
    releaser.start()
    assert entered.wait(timeout=5)
    assert chrome._cdp_port_claims[worker_id] == claim
    assert worker_id in chrome._releasing_cdp_workers
    with pytest.raises(RuntimeError, match="unresolved CDP generation"):
        chrome.allocate_cdp_port(worker_id)
    allow_failure.set()
    releaser.join(timeout=5)
    assert not releaser.is_alive()
    assert results == [False]
    assert chrome._cdp_port_claims[worker_id] == claim
    assert worker_id not in chrome._releasing_cdp_workers
    assert list(lock_file.parent.glob("*.lock")) == [lock_file]

    chrome._cdp_port_claims.pop(worker_id)
    original_unlink(lock_file)


def test_cdp_release_interrupt_clears_reservation_and_can_retry(
    monkeypatch, tmp_path: Path
) -> None:
    worker_id = 79
    port = 9779
    lock_file = tmp_path / "cdp-port-locks" / f"{port}.lock"
    lock_file.parent.mkdir(parents=True)
    lock_file.write_text("pid=1 worker=79\n", encoding="ascii")
    claim = (port, lock_file)
    chrome._cdp_port_claims[worker_id] = claim
    original_unlink = Path.unlink
    interrupted = False

    def interrupt_once(path: Path, *args, **kwargs):
        nonlocal interrupted
        if path == lock_file and not interrupted:
            interrupted = True
            raise KeyboardInterrupt("deterministic interruption")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", interrupt_once)
    with pytest.raises(KeyboardInterrupt, match="deterministic interruption"):
        chrome.release_cdp_port(worker_id)
    assert chrome._cdp_port_claims[worker_id] == claim
    assert lock_file.exists()
    assert worker_id not in chrome._releasing_cdp_workers

    assert chrome.release_cdp_port(worker_id) is True
    assert worker_id not in chrome._cdp_port_claims
    assert worker_id not in chrome._releasing_cdp_workers
    assert not lock_file.exists()
