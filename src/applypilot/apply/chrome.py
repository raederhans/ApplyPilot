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
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

from applypilot import config
from applypilot.runtime_settings import load_runtime_settings

logger = logging.getLogger(__name__)

# CDP port base — each worker uses BASE_CDP_PORT + worker_id
BASE_CDP_PORT = int(os.environ.get("APPLYPILOT_CDP_PORT", "9222"))

# Track Chrome processes per worker for cleanup
_chrome_procs: dict[int, subprocess.Popen] = {}
_chrome_lock = threading.Lock()
_cdp_port_claims: dict[int, tuple[int, Path]] = {}

SUPPORTED_BROWSER_BACKENDS = {"edge", "cloak", "auto"}
DEFAULT_CLOAK_BROWSER_VERSION = "146.0.7680.177.5"


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
    lock_dir = config.APPLY_WORKER_DIR / "cdp-port-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    _remove_stale_cdp_locks(lock_dir)
    release_cdp_port(worker_id)

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


def release_cdp_port(worker_id: int) -> None:
    """Release only the CDP-port claim owned by this process and worker."""
    with _chrome_lock:
        claim = _cdp_port_claims.pop(worker_id, None)
    if claim is None:
        return
    _port, lock_path = claim
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        logger.debug("Unable to remove owned CDP lock %s", lock_path, exc_info=True)


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

    # ApplyPilot owns update admission. CloakBrowser must not silently replace
    # the tested browser binary between application runs.
    os.environ["CLOAKBROWSER_AUTO_UPDATE"] = "false"
    requested_version = (
        os.environ.get("APPLYPILOT_CLOAK_VERSION")
        or DEFAULT_CLOAK_BROWSER_VERSION
    )
    if os.environ.get("CLOAKBROWSER_LICENSE_KEY"):
        from cloakbrowser.license import resolve_license_key, validate_license

        license_info = validate_license(resolve_license_key(None))
        if not license_info or not license_info.valid:
            raise RuntimeError("CloakBrowser license could not be validated for pinned execution")
        if str(license_info.plan).casefold() == "free":
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

def setup_worker_profile(worker_id: int, browser_backend: str = "edge") -> Path:
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
    )
    profile_dir = worker_root / f"worker-{worker_id}"

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
    if mode == "fresh":
        worker_root = worker_root.resolve()
        resolved_profile = profile_dir.resolve()
        if resolved_profile.parent != worker_root or resolved_profile == worker_root:
            raise ValueError(f"Unsafe browser worker profile path: {resolved_profile}")
        if resolved_profile.exists():
            shutil.rmtree(resolved_profile)
        profile_dir.mkdir(parents=True, exist_ok=True)
        logger.info("[worker-%d] Using a fresh isolated browser profile", worker_id)
        return profile_dir

    # Persistent mode is an ApplyPilot-owned browser identity. It is created
    # empty and then reused, so a one-time interactive login can survive later
    # application runs without copying the user's daily Edge/Chrome profile.
    if mode == "persistent":
        profile_dir.mkdir(parents=True, exist_ok=True)
        logger.info("[worker-%d] Using the persistent ApplyPilot browser profile", worker_id)
        return profile_dir

    if mode != "clone":
        raise ValueError(
            "APPLYPILOT_BROWSER_PROFILE_MODE must be fresh, persistent, or clone"
        )

    if (profile_dir / "Default").exists():
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
        logger.warning(
            "[worker-%d] Browser profile source not found at %s; using a fresh profile",
            worker_id,
            source,
        )
        return profile_dir

    logger.info("[worker-%d] Copying Chrome profile from %s (first time setup)...",
                worker_id, source.name)
    profile_dir.mkdir(parents=True, exist_ok=True)

    # Copy essential profile dirs -- skip caches and heavy transient data
    skip = {
        "ShaderCache", "GrShaderCache", "Service Worker", "Cache",
        "Code Cache", "GPUCache", "CacheStorage", "Crashpad",
        "BrowserMetrics", "SafeBrowsing", "Crowd Deny",
        "MEIPreload", "SSLErrorAssistant", "recovery", "Temp",
        "SingletonLock", "SingletonSocket", "SingletonCookie",
    }

    for item in source.iterdir():
        if item.name in skip:
            continue
        dst = profile_dir / item.name
        try:
            if item.is_dir():
                shutil.copytree(
                    str(item), str(dst), dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(
                        "Cache", "Code Cache", "GPUCache", "Service Worker",
                    ),
                )
            else:
                shutil.copy2(str(item), str(dst))
        except (PermissionError, OSError):
            pass  # skip locked files

    return profile_dir


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
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"Browser exited before CDP became ready (exit={process.returncode})"
            )
        try:
            with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=0.5) as response:
                if response.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001 - readiness preserves the last transport error
            last_error = exc
        time.sleep(0.2)
    raise TimeoutError(f"Browser CDP port {port} was not ready: {last_error}")


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
    profile_dir = setup_worker_profile(worker_id, backend)

    # Patch preferences to suppress restore nag
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
    try:
        _wait_for_cdp_ready(proc, port)
    except Exception:
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
        raise
    with _chrome_lock:
        _chrome_procs[worker_id] = proc

    logger.info("[worker-%d] %s browser started on port %d (pid %d)",
                worker_id, backend, port, proc.pid)
    return proc


def cleanup_worker(worker_id: int, process: subprocess.Popen | None) -> None:
    """Kill a worker's Chrome instance and remove it from tracking.

    Args:
        worker_id: Numeric worker identifier.
        process: The Popen handle returned by launch_chrome.
    """
    if process and process.poll() is None:
        _kill_process_tree(process.pid)
    with _chrome_lock:
        _chrome_procs.pop(worker_id, None)
    logger.info("[worker-%d] Chrome cleaned up", worker_id)


def kill_all_chrome() -> None:
    """Kill all Chrome instances and any port zombies.

    Called during graceful shutdown to ensure no orphan Chrome processes.
    """
    with _chrome_lock:
        procs = dict(_chrome_procs)
        _chrome_procs.clear()

    for wid, proc in procs.items():
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
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
        _chrome_procs.clear()

    for wid, proc in procs.items():
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
        release_cdp_port(wid)

    with _chrome_lock:
        claimed_workers = list(_cdp_port_claims)
    for wid in claimed_workers:
        release_cdp_port(wid)
