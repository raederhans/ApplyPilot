"""Chrome lifecycle management for apply workers.

Handles launching an isolated Chrome instance with remote debugging,
worker profile setup/cloning, and cross-platform process cleanup.
"""

import json
import logging
import os
import platform
import secrets
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

from applypilot import config
from applypilot.apply.profile_lock import ProfileLock, ProfileLockError
from applypilot.apply.retention import (
    mark_owned_directory,
    profile_clone_ignore,
    prune_owned_profile,
    read_ownership_marker,
)
from applypilot.runtime_settings import load_runtime_settings

logger = logging.getLogger(__name__)

# CDP port base — each worker uses BASE_CDP_PORT + worker_id
BASE_CDP_PORT = int(os.environ.get("APPLYPILOT_CDP_PORT", "9222"))

# Track Chrome processes per worker for cleanup
_chrome_procs: dict[int, subprocess.Popen] = {}
_chrome_ports: dict[int, int] = {}
_profile_locks: dict[int, ProfileLock] = {}
_profile_paths: dict[int, Path] = {}
_launching_workers: set[int] = set()
_allocating_cdp_workers: set[int] = set()
_releasing_cdp_workers: set[int] = set()
_profile_maintenance_checked: set[Path] = set()
_chrome_lock = threading.Lock()
_cdp_port_claims: dict[int, tuple[int, Path]] = {}

SUPPORTED_BROWSER_BACKENDS = {"edge", "cloak", "auto"}


def _profile_lock_owned_by_current_thread(profile_lock: object | None) -> bool:
    """Return confirmed lock ownership without trusting shutdown-time state.

    Process shutdown can observe partially initialized test doubles or objects
    left by a failed launch.  Treat an absent or unreadable ownership marker as
    unowned so cleanup never prunes or releases a profile without proof.
    """
    try:
        return bool(getattr(profile_lock, "owned_by_current_thread", False))
    except AttributeError:
        return False


def resolve_worker_profile_path(worker_id: int, browser_backend: str) -> Path:
    """Purely resolve the concrete profile identity before any mutation."""
    backend = resolve_browser_backend(browser_backend, allow_auto=False)
    worker_root = config.CLOAK_WORKER_DIR if backend == "cloak" else config.CHROME_WORKER_DIR
    return (worker_root / f"worker-{worker_id}").expanduser().resolve(strict=False)


def _lock_owner_is_running(lock_path: Path) -> bool | None:
    """Return process liveness, or None when ownership cannot be proven."""
    try:
        content = lock_path.read_text(encoding="ascii")
        token = next(part for part in content.split() if part.startswith("pid="))
        pid = int(token.removeprefix("pid="))
        if pid <= 0:
            return None
        if platform.system() == "Windows":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False if ctypes.get_last_error() == 87 else None
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return None
                return exit_code.value == 259
            finally:
                kernel32.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, StopIteration, ValueError):
        return None


def _remove_stale_cdp_locks(lock_dir: Path) -> None:
    """Remove only lock files whose recorded process is confirmed dead."""
    for lock_path in lock_dir.glob("*.lock"):
        if _lock_owner_is_running(lock_path) is False:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                logger.debug("Unable to remove stale CDP lock %s", lock_path, exc_info=True)


def allocate_cdp_port(worker_id: int) -> int:
    """Claim a run-owned ephemeral CDP port without touching other processes.

    A small lock file coordinates concurrent ApplyPilot processes. The socket
    is released immediately before Chromium binds it; the lock prevents other
    ApplyPilot runs from selecting the same port during that short window.
    """
    with _chrome_lock:
        if (
            worker_id in _cdp_port_claims
            or worker_id in _allocating_cdp_workers
            or worker_id in _releasing_cdp_workers
            or worker_id in _launching_workers
            or worker_id in _profile_locks
            or worker_id in _profile_paths
            or worker_id in _chrome_procs
            or worker_id in _chrome_ports
        ):
            raise RuntimeError(f"Worker {worker_id} already has an unresolved CDP generation")
        _allocating_cdp_workers.add(worker_id)
    lock_dir = config.APPLY_WORKER_DIR / "cdp-port-locks"
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
        _remove_stale_cdp_locks(lock_dir)

        for _ in range(32):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", 0))
                port = int(probe.getsockname()[1])
            lock_path = lock_dir / f"{port}.lock"
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                continue
            try:
                os.write(fd, f"pid={os.getpid()} worker={worker_id}\n".encode("ascii"))
            finally:
                os.close(fd)
            with _chrome_lock:
                _cdp_port_claims[worker_id] = (port, lock_path)
            return port
        raise RuntimeError("Unable to reserve an isolated CDP port")
    finally:
        with _chrome_lock:
            _allocating_cdp_workers.discard(worker_id)


def release_cdp_port(worker_id: int) -> bool:
    """Release only the CDP-port claim owned by this process and worker."""
    with _chrome_lock:
        if (
            worker_id in _launching_workers
            or worker_id in _allocating_cdp_workers
            or worker_id in _releasing_cdp_workers
            or worker_id in _profile_locks
            or worker_id in _profile_paths
            or worker_id in _chrome_procs
            or worker_id in _chrome_ports
        ):
            return False
        claim = _cdp_port_claims.get(worker_id)
        if claim is not None:
            _releasing_cdp_workers.add(worker_id)
    if claim is None:
        return True
    _port, lock_path = claim
    unlink_succeeded = False
    try:
        try:
            lock_path.unlink(missing_ok=True)
            unlink_succeeded = True
        except OSError:
            logger.debug("Unable to remove owned CDP lock %s", lock_path, exc_info=True)
            return False
    finally:
        with _chrome_lock:
            if unlink_succeeded and _cdp_port_claims.get(worker_id) == claim:
                _cdp_port_claims.pop(worker_id, None)
            _releasing_cdp_workers.discard(worker_id)
    return True


def resolve_browser_backend(value: str | None = None, *, allow_auto: bool = True) -> str:
    """Resolve and validate the requested browser runtime."""
    return load_runtime_settings().resolve_browser_backend(value, allow_auto=allow_auto)


def get_browser_executable(browser_backend: str) -> str:
    """Return the executable for an Edge/Chrome or CloakBrowser worker."""
    backend = resolve_browser_backend(browser_backend, allow_auto=False)
    if backend == "edge":
        return config.get_chrome_path()

    unsafe_overrides = (
        "CLOAKBROWSER_BINARY_PATH",
        "CLOAKBROWSER_DOWNLOAD_URL",
        "CLOAKBROWSER_SKIP_CHECKSUM",
    )
    configured_overrides = [name for name in unsafe_overrides if os.environ.get(name)]
    if configured_overrides:
        raise RuntimeError(
            "CloakBrowser security policy rejects binary/checksum overrides: "
            + ", ".join(configured_overrides)
        )

    try:
        from cloakbrowser import ensure_binary
    except ImportError as exc:
        raise RuntimeError(
            "CloakBrowser backend is not installed; install applypilot-local[stealth]"
        ) from exc

    # A version pin is an optional deployment choice, not an ApplyPilot code
    # dependency. Without one, the installed CloakBrowser package owns its
    # stable-channel resolution and cache policy.
    requested_version = os.environ.get("APPLYPILOT_CLOAK_VERSION") or None
    if requested_version:
        os.environ["CLOAKBROWSER_AUTO_UPDATE"] = "false"
    if os.environ.get("CLOAKBROWSER_LICENSE_KEY"):
        from cloakbrowser.license import resolve_license_key, validate_license

        license_info = validate_license(resolve_license_key(None))
        if not license_info or not license_info.valid:
            raise RuntimeError("CloakBrowser license could not be validated for pinned execution")
        if requested_version and str(license_info.plan).casefold() == "free":
            raise RuntimeError(
                "CloakBrowser free licenses force an unpinned latest binary; "
                "unset the key or use a pin-capable license"
            )
    return ensure_binary(browser_version=requested_version)


# ---------------------------------------------------------------------------
# Cross-platform process helpers
# ---------------------------------------------------------------------------

def _kill_process_tree(pid: int) -> None:
    """Kill a process and all its children.

    On Windows, Chrome spawns 10+ child processes (GPU, renderer, etc.),
    so taskkill /T is needed to kill the entire tree. On Unix, os.killpg
    handles the process group.
    """
    import signal as _signal

    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        else:
            # Unix: kill entire process group
            import os
            try:
                os.killpg(os.getpgid(pid), _signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                # Process already gone or owned by another user
                try:
                    os.kill(pid, _signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
    except Exception:
        logger.debug("Failed to kill process tree for PID %d", pid, exc_info=True)


# ---------------------------------------------------------------------------
# Worker profile management
# ---------------------------------------------------------------------------

def setup_worker_profile(
    worker_id: int,
    browser_backend: str = "edge",
    profile_lock: ProfileLock | None = None,
) -> Path:
    """Create an isolated Chrome profile for a worker.

    On first run, clones from an existing worker profile (preferred, since
    it already has session cookies) or from the user's real Chrome profile.
    Subsequent runs reuse the existing worker profile.

    Args:
        worker_id: Numeric worker identifier.

    Returns:
        Path to the worker's Chrome user-data directory.
    """
    backend = resolve_browser_backend(browser_backend, allow_auto=False)
    worker_root = (
        config.CLOAK_WORKER_DIR if backend == "cloak" else config.CHROME_WORKER_DIR
    ).expanduser().resolve(strict=False)
    profile_dir = resolve_worker_profile_path(worker_id, backend)
    owned_lock = profile_lock is None
    lock = profile_lock or ProfileLock(profile_dir).acquire()
    if lock.profile_path != profile_dir:
        raise ProfileLockError("Profile lock does not match the requested browser profile")

    def mark_owned() -> None:
        mark_owned_directory(
            profile_dir,
            root=worker_root,
            kind="browser_profile",
            owner_id=f"{backend}:worker-{worker_id}",
        )

    mode = os.environ.get("APPLYPILOT_BROWSER_PROFILE_MODE", "clone").lower()

    # A patched Chromium profile is a separate browser identity. Copying the
    # user's daily Edge profile across browser products is both fragile and a
    # privacy leak, so the legacy clone default becomes an isolated persistent
    # profile for CloakBrowser.
    if backend == "cloak" and mode == "clone":
        mode = "persistent"
        logger.info(
            "[worker-%d] CloakBrowser never clones the daily browser profile; "
            "using an isolated persistent profile",
            worker_id,
        )

    # Preview runs must not inherit extensions, autofill data, or stale session
    # state from either the user's daily profile or a previous experiment. Only
    # the ApplyPilot-owned worker directory is ever removed here.
    try:
        if mode == "fresh":
            resolved_profile = profile_dir.resolve()
            if resolved_profile.parent != worker_root or resolved_profile == worker_root:
                raise ValueError(f"Unsafe browser worker profile path: {resolved_profile}")
            if resolved_profile.exists():
                shutil.rmtree(resolved_profile)
            profile_dir.mkdir(parents=True, exist_ok=True)
            mark_owned()
            logger.info("[worker-%d] Using a fresh isolated browser profile", worker_id)
            return profile_dir

    # Persistent mode is an ApplyPilot-owned browser identity. It is created
    # empty and then reused, so a one-time interactive login can survive later
    # application runs without copying the user's daily Edge/Chrome profile.
        if mode == "persistent":
            profile_dir.mkdir(parents=True, exist_ok=True)
            mark_owned()
            logger.info("[worker-%d] Using the persistent CapyPilot browser profile", worker_id)
            return profile_dir

        if mode != "clone":
            raise ValueError(
                "APPLYPILOT_BROWSER_PROFILE_MODE must be fresh, persistent, or clone"
            )

        if (profile_dir / "Default").exists():
            mark_owned()
            return profile_dir  # Already initialized

    # Find a source: prefer existing worker (has session cookies), else user profile
        source: Path | None = None
        for wid in range(10):
            if wid == worker_id:
                continue
            candidate = config.CHROME_WORKER_DIR / f"worker-{wid}"
            if (candidate / "Default").exists():
                source = candidate
                break
        if source is None:
            source = config.get_chrome_user_data()
        if not source.exists():
            profile_dir.mkdir(parents=True, exist_ok=True)
            mark_owned()
            logger.warning(
                "[worker-%d] Browser profile source not found at %s; using a fresh profile",
                worker_id,
                source,
            )
            return profile_dir

        logger.info("[worker-%d] Copying Chrome profile from %s (first time setup)...",
                    worker_id, source.name)
        profile_dir.mkdir(parents=True, exist_ok=True)
        mark_owned()

        for item in source.iterdir():
            if item.name in profile_clone_ignore(str(source), [item.name]):
                continue
            dst = profile_dir / item.name
            try:
                if item.is_dir():
                    shutil.copytree(
                        str(item), str(dst), dirs_exist_ok=True,
                        ignore=profile_clone_ignore,
                    )
                else:
                    shutil.copy2(str(item), str(dst))
            except (PermissionError, OSError):
                pass  # skip locked files

        return profile_dir
    finally:
        if owned_lock and lock.has_native_resource:
            lock.release_before_spawn()


def _cloak_fingerprint_seed(profile_dir: Path) -> int:
    """Return a stable fingerprint seed for one isolated CloakBrowser profile."""
    override = os.environ.get("APPLYPILOT_CLOAK_FINGERPRINT_SEED")
    if override:
        try:
            value = int(override)
        except ValueError as exc:
            raise ValueError("APPLYPILOT_CLOAK_FINGERPRINT_SEED must be an integer") from exc
        if value <= 0:
            raise ValueError("APPLYPILOT_CLOAK_FINGERPRINT_SEED must be positive")
        return value

    marker = profile_dir / ".applypilot-cloak-fingerprint"
    if marker.is_file():
        try:
            value = int(marker.read_text(encoding="ascii").strip())
            if value > 0:
                return value
        except (OSError, ValueError):
            logger.warning("Ignoring invalid CloakBrowser fingerprint marker at %s", marker)

    value = secrets.randbelow(900_000) + 100_000
    marker.write_text(str(value), encoding="ascii")
    return value


def _suppress_restore_nag(profile_dir: Path) -> None:
    """Clear Chrome's 'restore pages' nag by fixing Preferences.

    Chrome writes exit_type=Crashed when killed, which triggers a
    'Restore pages?' prompt on next launch. This patches it out.
    """
    prefs_file = profile_dir / "Default" / "Preferences"
    if not prefs_file.exists():
        return

    try:
        prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
        prefs.setdefault("profile", {})["exit_type"] = "Normal"
        prefs.setdefault("session", {})["restore_on_startup"] = 4  # 4 = open blank
        prefs.setdefault("session", {}).pop("startup_urls", None)
        prefs["credentials_enable_service"] = False
        prefs.setdefault("password_manager", {})["saving_enabled"] = False
        prefs.setdefault("autofill", {})["profile_enabled"] = False
        prefs_file.write_text(json.dumps(prefs), encoding="utf-8")
    except Exception:
        logger.debug("Could not patch Chrome preferences", exc_info=True)


# ---------------------------------------------------------------------------
# Chrome launch / kill
# ---------------------------------------------------------------------------

def _wait_for_cdp_ready(
    process: subprocess.Popen, port: int, timeout_seconds: float = 20.0
) -> None:
    """Wait until the browser exposes its local DevTools endpoint."""
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    clean_bootstrap_exit = False
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code not in (None, 0):
            raise RuntimeError(
                f"Browser exited before CDP became ready (exit={exit_code})"
            )
        # Current Edge builds can relaunch through a compatibility bootstrap:
        # the Popen handle exits successfully while the child browser continues
        # starting on the requested CDP port. A zero exit is therefore not a
        # failure until the bounded readiness window also expires.
        clean_bootstrap_exit = clean_bootstrap_exit or exit_code == 0
        try:
            with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.5) as response:
                if response.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001 - readiness preserves the last transport error
            last_error = exc
        time.sleep(0.2)
    if clean_bootstrap_exit:
        raise RuntimeError(
            "Browser bootstrap exited successfully, but the relaunched browser "
            f"did not expose CDP port {port}: {last_error}"
        )
    raise TimeoutError(f"Browser CDP port {port} was not ready: {last_error}")


def _close_browser_via_cdp(port: int) -> None:
    """Close the real browser even when Edge's bootstrap Popen has exited."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        browser.new_browser_cdp_session().send("Browser.close")


def _resolve_actual_browser_pid(port: int) -> int:
    """Resolve the OS PID of the browser process that actually holds the profile."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        payload = browser.new_browser_cdp_session().send("SystemInfo.getProcessInfo")
    matches = [item for item in payload.get("processInfo", []) if item.get("type") == "browser"]
    if len(matches) != 1:
        raise RuntimeError("CDP did not identify exactly one browser process")
    value = matches[0].get("osProcessId", matches[0].get("id"))
    try:
        pid = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("CDP browser process did not expose an OS PID") from exc
    if pid <= 0:
        raise RuntimeError("CDP browser process exposed an invalid OS PID")
    return pid


def _cdp_endpoint_reachable(port: int) -> bool:
    """Return whether the worker's browser endpoint is still serving CDP."""
    try:
        with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.25):
            return True
    except OSError:
        return False


def cdp_endpoint_reachable(port: int) -> bool:
    """Return whether the owned browser still exposes its local CDP endpoint."""
    return _cdp_endpoint_reachable(port)


def _wait_for_browser_stopped(
    port: int | None,
    process: subprocess.Popen | None,
    actual_browser_stopped: Callable[[], bool] | None = None,
    *,
    timeout_seconds: float = 2.0,
) -> bool:
    """Confirm the launcher, CDP endpoint, and actual profile holder stopped.

    Edge can return a bootstrap ``Popen`` that exits while a child browser keeps
    serving the profile.  Profile maintenance is therefore allowed only when a
    process handle exists, that handle is stopped, and the known CDP endpoint is
    no longer reachable.  The CDP endpoint can disappear shortly before the
    real browser process exits, so a supplied holder probe is polled under the
    same bounded shutdown deadline instead of being sampled only once.
    """
    if process is None:
        return False
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        process_stopped = process.poll() is not None
        endpoint_stopped = port is None or not _cdp_endpoint_reachable(port)
        actual_stopped = actual_browser_stopped is None
        if actual_browser_stopped is not None:
            try:
                actual_stopped = bool(actual_browser_stopped())
            except ProfileLockError:
                return False
        if process_stopped and endpoint_stopped and actual_stopped:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _scoped_cookie_urls(allowed_urls: list[str] | tuple[str, ...]) -> list[str]:
    scoped_urls = []
    for value in allowed_urls:
        parsed = urlparse(str(value))
        if parsed.scheme == "https" and parsed.hostname:
            scoped_urls.append(str(value))
    return scoped_urls


def capture_browser_session(port: int, allowed_urls: list[str] | tuple[str, ...]) -> dict:
    """Capture only cookies applicable to explicitly bound application URLs."""
    scoped_urls = _scoped_cookie_urls(allowed_urls)
    from playwright.sync_api import sync_playwright

    endpoint = f"http://127.0.0.1:{port}"
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(endpoint)
        if not browser.contexts or not scoped_urls:
            return {"cookies": []}
        return {"cookies": browser.contexts[0].cookies(scoped_urls)}


def restore_browser_session(port: int, session: dict, start_url: str) -> int:
    """Import an in-memory session into a running ApplyPilot browser."""
    from playwright.sync_api import sync_playwright

    cookies = list(session.get("cookies") or [])
    endpoint = f"http://127.0.0.1:{port}"
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(endpoint)
        if not browser.contexts:
            raise RuntimeError("CloakBrowser exposed no default browser context")
        context = browser.contexts[0]
        if cookies:
            context.add_cookies(cookies)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(start_url, wait_until="domcontentloaded", timeout=45_000)
    return len(cookies)


def launch_chrome(worker_id: int, port: int | None = None,
                  headless: bool = False,
                  start_url: str | None = None,
                  browser_backend: str = "edge") -> subprocess.Popen:
    """Launch an isolated Chromium runtime with remote debugging for a worker.

    Args:
        worker_id: Numeric worker identifier.
        port: CDP port. Defaults to BASE_CDP_PORT + worker_id.
        headless: Run Chrome in headless mode (no visible window).
        start_url: Optional initial page to open.

    Returns:
        subprocess.Popen handle for the Chrome process.
    """
    if port is None:
        port = BASE_CDP_PORT + worker_id

    backend = resolve_browser_backend(browser_backend, allow_auto=False)
    profile_dir = resolve_worker_profile_path(worker_id, backend)
    with _chrome_lock:
        if (
            worker_id in _launching_workers
            or worker_id in _profile_locks
            or worker_id in _profile_paths
            or worker_id in _chrome_procs
            or worker_id in _chrome_ports
        ):
            raise RuntimeError(f"Worker {worker_id} already has an unresolved browser generation")
        _launching_workers.add(worker_id)
    profile_lock: ProfileLock | None = None
    try:
        profile_lock = ProfileLock(profile_dir)
        with _chrome_lock:
            _profile_locks[worker_id] = profile_lock
            _profile_paths[worker_id] = profile_dir
        profile_lock.acquire()
    finally:
        with _chrome_lock:
            _launching_workers.discard(worker_id)
            if (
                profile_lock is not None
                and not profile_lock.has_native_resource
                and not profile_lock.requires_recovery
                and not profile_lock.sidecar_path.exists()
            ):
                _profile_locks.pop(worker_id, None)
                _profile_paths.pop(worker_id, None)
    proc: subprocess.Popen | None = None
    try:
        profile_dir = setup_worker_profile(worker_id, backend, profile_lock)

        # Patch preferences only while physical profile ownership is held.
        _suppress_restore_nag(profile_dir)

        browser_exe = get_browser_executable(backend)

        cmd = [
        browser_exe,
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1024,768",
        "--disable-session-crashed-bubble",
        "--disable-features=InfiniteSessionRestore,PasswordManagerOnboarding",
        "--hide-crash-restore-bubble",
        "--noerrdialogs",
        "--password-store=basic",
        "--disable-save-password-bubble",
        "--disable-popup-blocking",
        # System-level external extension registrations can repopulate even a
        # brand-new Edge profile. Disable extensions at launch so no third
        # party can observe or rewrite application fields.
        "--disable-extensions",
        "--disable-component-extensions-with-background-pages",
        # Block dangerous permissions at browser level. Do not use Chromium's
        # fake-media auto-accept flags: they grant a synthetic camera/mic and
        # contradict the application's permission boundary.
        "--deny-permission-prompts",
        "--disable-notifications",
        ]
        if backend == "cloak":
            cmd.append(f"--fingerprint={_cloak_fingerprint_seed(profile_dir)}")
        if headless:
            cmd.append("--headless=new")
        if start_url:
            cmd.append(start_url)

    # On Unix, start in a new process group so we can kill the whole tree
        kwargs: dict = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if platform.system() != "Windows":
            import os
            kwargs["preexec_fn"] = os.setsid

        proc = subprocess.Popen(cmd, **kwargs)
        profile_lock.record_spawn_attempt()
        _wait_for_cdp_ready(proc, port)
        profile_lock.record_browser(_resolve_actual_browser_pid(port))
    except Exception:
        if proc is None:
            try:
                profile_lock.release_before_spawn()
            except ProfileLockError:
                logger.warning("Pre-spawn profile cleanup failed", exc_info=True)
        else:
            with _chrome_lock:
                _chrome_procs[worker_id] = proc
                _chrome_ports[worker_id] = port
            try:
                _close_browser_via_cdp(port)
            except Exception:
                logger.debug("Unable to close failed browser launch via CDP", exc_info=True)
            if proc.poll() is None:
                _kill_process_tree(proc.pid)
            stopped = _wait_for_browser_stopped(
                port,
                proc,
                profile_lock.actual_browser_stopped,
            )
            released = False
            if stopped:
                try:
                    profile_lock.release_after_stop(
                        profile_path=profile_dir,
                        browser_stopped=True,
                    )
                    released = True
                except ProfileLockError:
                    logger.warning(
                        "Failed launch stopped, but profile ownership is ambiguous",
                        exc_info=True,
                    )
            if released:
                with _chrome_lock:
                    _chrome_procs.pop(worker_id, None)
                    _chrome_ports.pop(worker_id, None)
                    _profile_locks.pop(worker_id, None)
                    _profile_paths.pop(worker_id, None)
            else:
                profile_lock.mark_recovery_required()
        with _chrome_lock:
            _launching_workers.discard(worker_id)
            if (
                proc is None
                and not profile_lock.has_native_resource
                and not profile_lock.requires_recovery
                and not profile_lock.sidecar_path.exists()
            ):
                _profile_locks.pop(worker_id, None)
                _profile_paths.pop(worker_id, None)
        raise
    assert proc is not None
    with _chrome_lock:
        _chrome_procs[worker_id] = proc
        _chrome_ports[worker_id] = port
        _launching_workers.discard(worker_id)

    logger.info("[worker-%d] %s browser started on port %d (pid %d)",
                worker_id, backend, port, proc.pid)
    return proc


def cleanup_worker(worker_id: int, process: subprocess.Popen | None) -> None:
    """Kill a worker's Chrome instance and remove it from tracking.

    Args:
        worker_id: Numeric worker identifier.
        process: The Popen handle returned by launch_chrome.
    """
    with _chrome_lock:
        tracked_process = _chrome_procs.get(worker_id)
        port = _chrome_ports.get(worker_id)
        profile_lock = _profile_locks.get(worker_id)
        locked_profile = _profile_paths.get(worker_id)
    if port is not None:
        try:
            _close_browser_via_cdp(port)
        except Exception:
            logger.debug(
                "[worker-%d] Unable to close browser via CDP port %d",
                worker_id,
                port,
                exc_info=True,
            )
    owned_process = tracked_process or process
    if owned_process and owned_process.poll() is None:
        _kill_process_tree(owned_process.pid)
    lock_is_owned = _profile_lock_owned_by_current_thread(profile_lock)
    browser_is_stopped = _wait_for_browser_stopped(
        port,
        owned_process,
        profile_lock.actual_browser_stopped if lock_is_owned else None,
    )
    if not browser_is_stopped:
        logger.warning(
            "[worker-%d] Browser stop could not be confirmed; skipping profile pruning",
            worker_id,
        )
    actual_browser_stopped = False
    if browser_is_stopped and lock_is_owned:
        try:
            actual_browser_stopped = profile_lock.actual_browser_stopped()
        except ProfileLockError:
            logger.warning(
                "[worker-%d] Actual browser process stop could not be confirmed",
                worker_id,
                exc_info=True,
            )
    if (
        browser_is_stopped
        and actual_browser_stopped
        and profile_lock is not None
        and locked_profile is not None
        and _profile_lock_owned_by_current_thread(profile_lock)
    ):
        profile_dir = locked_profile
        profile_root = profile_dir.parent
        resolved_profile = profile_dir.resolve()
        with _chrome_lock:
            already_maintained = resolved_profile in _profile_maintenance_checked
            if not already_maintained:
                _profile_maintenance_checked.add(resolved_profile)
        if not already_maintained:
            marker = read_ownership_marker(profile_dir)
            if marker and marker.get("kind") == "browser_profile":
                try:
                    retention_days = max(
                        1.0,
                        float(os.environ.get("APPLYPILOT_PROFILE_CACHE_RETENTION_DAYS", "7")),
                    )
                except ValueError:
                    retention_days = 7.0
                try:
                    maintenance = prune_owned_profile(
                        profile_dir,
                        profile_root=profile_root,
                        minimum_age_seconds=retention_days * 24 * 60 * 60,
                        execute=True,
                        browser_is_stopped=True,
                    )
                    if maintenance["removed_files"]:
                        logger.info(
                            "[worker-%d] Reclaimed %d old regenerable profile files (%d bytes)",
                            worker_id,
                            maintenance["removed_files"],
                            maintenance["removed_bytes"],
                        )
                except (OSError, PermissionError, RuntimeError, ValueError):
                    logger.debug(
                        "[worker-%d] Browser profile maintenance skipped for %s",
                        worker_id,
                        profile_dir,
                        exc_info=True,
                    )
        try:
            profile_lock.release_after_stop(profile_path=profile_dir, browser_stopped=True)
        except ProfileLockError:
            logger.warning(
                "[worker-%d] Profile mutex retained; owner-thread release is required",
                worker_id,
                exc_info=True,
            )
        else:
            with _chrome_lock:
                _chrome_procs.pop(worker_id, None)
                _chrome_ports.pop(worker_id, None)
                _profile_locks.pop(worker_id, None)
                _profile_paths.pop(worker_id, None)
    elif profile_lock is None:
        with _chrome_lock:
            _chrome_procs.pop(worker_id, None)
            _chrome_ports.pop(worker_id, None)
    logger.info("[worker-%d] Chrome cleaned up", worker_id)


def kill_all_chrome() -> None:
    """Kill all Chrome instances and any port zombies.

    Called during graceful shutdown to ensure no orphan Chrome processes.
    """
    with _chrome_lock:
        procs = dict(_chrome_procs)
        ports = dict(_chrome_ports)
        worker_ids = set(procs) | set(ports) | set(_profile_locks) | set(_profile_paths)

    for wid in worker_ids:
        proc = procs.get(wid)
        port = ports.get(wid)
        with _chrome_lock:
            profile_lock = _profile_locks.get(wid)
            profile_path = _profile_paths.get(wid)
        if (
            proc is None
            and profile_lock is not None
            and _profile_lock_owned_by_current_thread(profile_lock)
            and not profile_lock.spawn_attempted
        ):
            try:
                profile_lock.release_before_spawn()
            except ProfileLockError:
                logger.warning("[worker-%d] Pre-spawn profile mutex retained", wid)
            else:
                with _chrome_lock:
                    _profile_locks.pop(wid, None)
                    _profile_paths.pop(wid, None)
            release_cdp_port(wid)
            continue
        if port is not None:
            try:
                _close_browser_via_cdp(port)
            except Exception:
                logger.debug(
                    "[worker-%d] Unable to close browser via CDP port %d",
                    wid,
                    port,
                    exc_info=True,
                )
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc.pid)
        stopped = _wait_for_browser_stopped(
            port,
            proc,
            (
                profile_lock.actual_browser_stopped
                if _profile_lock_owned_by_current_thread(profile_lock)
                else None
            ),
        )
        if (
            stopped
            and profile_lock is not None
            and profile_path is not None
            and _profile_lock_owned_by_current_thread(profile_lock)
        ):
            try:
                profile_lock.release_after_stop(
                    profile_path=profile_path,
                    browser_stopped=True,
                )
            except ProfileLockError:
                logger.warning("[worker-%d] Profile mutex retained", wid, exc_info=True)
            else:
                with _chrome_lock:
                    _profile_locks.pop(wid, None)
                    _profile_paths.pop(wid, None)
                    _chrome_procs.pop(wid, None)
                    _chrome_ports.pop(wid, None)
        release_cdp_port(wid)

    with _chrome_lock:
        claimed_workers = list(_cdp_port_claims)
    for wid in claimed_workers:
        release_cdp_port(wid)


def reset_worker_dir(worker_id: int) -> Path:
    """Wipe and recreate a worker's isolated working directory.

    Each job gets a fresh working directory so that file conflicts
    (resume PDFs, MCP configs) don't bleed between jobs.

    Args:
        worker_id: Numeric worker identifier.

    Returns:
        Path to the clean worker directory.
    """
    worker_dir = config.APPLY_WORKER_DIR / f"worker-{worker_id}"
    if worker_dir.exists():
        shutil.rmtree(str(worker_dir), ignore_errors=True)
    worker_dir.mkdir(parents=True, exist_ok=True)
    return worker_dir


def cleanup_on_exit() -> None:
    """Atexit handler: kill all Chrome processes and sweep CDP ports.

    Register this with atexit.register() at application startup.
    """
    with _chrome_lock:
        procs = dict(_chrome_procs)
        ports = dict(_chrome_ports)
        worker_ids = set(procs) | set(ports) | set(_profile_locks) | set(_profile_paths)

    for wid in worker_ids:
        proc = procs.get(wid)
        port = ports.get(wid)
        with _chrome_lock:
            profile_lock = _profile_locks.get(wid)
            profile_path = _profile_paths.get(wid)
        if (
            proc is None
            and profile_lock is not None
            and _profile_lock_owned_by_current_thread(profile_lock)
            and not profile_lock.spawn_attempted
        ):
            try:
                profile_lock.release_before_spawn()
            except ProfileLockError:
                logger.warning("[worker-%d] Pre-spawn profile mutex retained", wid)
            else:
                with _chrome_lock:
                    _profile_locks.pop(wid, None)
                    _profile_paths.pop(wid, None)
            release_cdp_port(wid)
            continue
        if port is not None:
            try:
                _close_browser_via_cdp(port)
            except Exception:
                logger.debug(
                    "[worker-%d] Unable to close browser via CDP port %d",
                    wid,
                    port,
                    exc_info=True,
                )
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc.pid)
        stopped = _wait_for_browser_stopped(
            port,
            proc,
            (
                profile_lock.actual_browser_stopped
                if _profile_lock_owned_by_current_thread(profile_lock)
                else None
            ),
        )
        if (
            stopped
            and profile_lock is not None
            and profile_path is not None
            and _profile_lock_owned_by_current_thread(profile_lock)
        ):
            try:
                profile_lock.release_after_stop(
                    profile_path=profile_path,
                    browser_stopped=True,
                )
            except ProfileLockError:
                logger.warning("[worker-%d] Profile mutex retained", wid, exc_info=True)
            else:
                with _chrome_lock:
                    _profile_locks.pop(wid, None)
                    _profile_paths.pop(wid, None)
                    _chrome_procs.pop(wid, None)
                    _chrome_ports.pop(wid, None)
        release_cdp_port(wid)

    with _chrome_lock:
        claimed_workers = list(_cdp_port_claims)
    for wid in claimed_workers:
        release_cdp_port(wid)
