from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

from applypilot.apply import chrome
from applypilot.apply.retention import (
    OWNERSHIP_MARKER,
    archive_new_evidence,
    classify_profile_path,
    inventory_profile,
    mark_owned_directory,
    prune_owned_profile,
    reclaim_terminal_artifacts,
    snapshot_files,
)


def _write(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_profile_inventory_separates_site_state_from_regenerable_cache(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "profiles" / "worker-0"
    _write(profile / "Default" / "Network" / "Cookies", b"session")
    _write(profile / "Default" / "IndexedDB" / "state", b"login-state")
    _write(profile / "Default" / "Cache" / "cache.bin", b"cache")
    _write(profile / "component_crx_cache" / "component.bin", b"component")

    inventory = inventory_profile(profile)

    assert classify_profile_path(Path("Default/Network/Cookies")) == "site_state"
    assert inventory["categories"]["site_state"] == {"files": 2, "bytes": 18}
    assert inventory["categories"]["regenerable_cache"] == {
        "files": 2,
        "bytes": 14,
    }


def test_profile_prune_is_owned_age_gated_and_preview_by_default(tmp_path: Path) -> None:
    root = tmp_path / "profiles"
    profile = root / "worker-0"
    old_cache = _write(profile / "Default" / "Cache" / "old.bin", b"old-cache")
    recent_cache = _write(profile / "Default" / "Code Cache" / "new.bin", b"new-cache")
    cookies = _write(profile / "Default" / "Network" / "Cookies", b"session")
    now = 10_000.0
    os.utime(old_cache, (now - 1_000, now - 1_000))
    os.utime(recent_cache, (now - 10, now - 10))

    with pytest.raises(PermissionError):
        prune_owned_profile(profile, profile_root=root, now=now)

    mark_owned_directory(
        profile,
        root=root,
        kind="browser_profile",
        owner_id="edge:worker-0",
    )
    preview = prune_owned_profile(
        profile,
        profile_root=root,
        minimum_age_seconds=100,
        now=now,
    )
    assert preview["mode"] == "preview"
    assert preview["eligible_files"] == 1
    assert old_cache.exists()

    with pytest.raises(RuntimeError, match="browser stop is not confirmed"):
        prune_owned_profile(
            profile,
            profile_root=root,
            minimum_age_seconds=100,
            execute=True,
            now=now,
        )

    executed = prune_owned_profile(
        profile,
        profile_root=root,
        minimum_age_seconds=100,
        execute=True,
        browser_is_stopped=True,
        now=now,
    )
    assert executed["removed_files"] == 1
    assert not old_cache.exists()
    assert recent_cache.exists()
    assert cookies.exists()


def test_profile_prune_rejects_outside_target(tmp_path: Path) -> None:
    root = tmp_path / "profiles"
    outside = tmp_path / "outside"
    mark_owned_directory(
        outside,
        root=tmp_path,
        kind="browser_profile",
        owner_id="edge:outside",
    )
    with pytest.raises(ValueError):
        prune_owned_profile(outside, profile_root=root)


def test_clone_preserves_session_state_but_skips_regenerable_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_root = tmp_path / "workers"
    source = tmp_path / "daily-profile"
    _write(source / "Default" / "Network" / "Cookies", b"session")
    _write(source / "Default" / "Local Storage" / "login", b"site")
    _write(source / "Default" / "Cache" / "cache.bin", b"cache")
    _write(source / "Default" / "Service Worker" / "worker.bin", b"worker")
    _write(source / "component_crx_cache" / "component.bin", b"component")
    _write(source / "Downloads" / "resume-copy.pdf", b"job-specific")
    monkeypatch.setattr(chrome, "resolve_browser_backend", lambda *_args, **_kwargs: "edge")
    monkeypatch.setattr(chrome.config, "CHROME_WORKER_DIR", profile_root)
    monkeypatch.setattr(chrome.config, "get_chrome_user_data", lambda: source)
    monkeypatch.setenv("APPLYPILOT_BROWSER_PROFILE_MODE", "clone")

    result = chrome.setup_worker_profile(7, "edge")

    assert (result / OWNERSHIP_MARKER).is_file()
    assert (result / "Default" / "Network" / "Cookies").read_bytes() == b"session"
    assert (result / "Default" / "Local Storage" / "login").is_file()
    assert not (result / "Default" / "Cache").exists()
    assert not (result / "Default" / "Service Worker").exists()
    assert not (result / "component_crx_cache").exists()
    assert not (result / "Downloads").exists()


def test_evidence_archive_only_copies_files_new_or_changed_in_current_run(
    tmp_path: Path,
) -> None:
    worker = tmp_path / "worker"
    destination = tmp_path / "archive"
    stale_preview = _write(worker / "final-preview.png", b"old-preview")
    baseline = snapshot_files(
        worker,
        ["final-preview.png", "submission-confirmation-observer.png"],
    )
    receipt = _write(
        worker / "submission-confirmation-observer.png",
        b"full-page-receipt",
    )

    archived = archive_new_evidence(
        worker,
        destination,
        ["final-preview.png", "submission-confirmation-observer.png"],
        baseline=baseline,
    )

    assert archived == [destination / receipt.name]
    assert not (destination / stale_preview.name).exists()
    assert (destination / receipt.name).read_bytes() == b"full-page-receipt"
    assert archive_new_evidence(
        worker,
        destination,
        ["submission-confirmation-observer.png"],
        baseline={},
    ) == []


def test_terminal_artifact_reclaim_requires_marker_terminal_state_and_age(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    old = root / "old-complete"
    active = root / "active"
    recent = root / "recent-complete"
    uncertain = root / "uncertain"
    receipt = root / "receipt"
    unmarked = root / "unmarked"
    for path in (old, active, recent, uncertain, receipt, unmarked):
        _write(path / "artifact.txt")
    mark_owned_directory(
        old,
        root=root,
        kind="job_transient",
        owner_id="attempt-old",
        state="applied",
        completed_at=1_000,
    )
    mark_owned_directory(
        active,
        root=root,
        kind="job_transient",
        owner_id="attempt-active",
        state="active",
    )
    mark_owned_directory(
        recent,
        root=root,
        kind="job_transient",
        owner_id="attempt-recent",
        state="failed",
        completed_at=9_950,
    )
    mark_owned_directory(
        uncertain,
        root=root,
        kind="job_transient",
        owner_id="attempt-uncertain",
        state="submission_uncertain",
        completed_at=1_000,
    )
    mark_owned_directory(
        receipt,
        root=root,
        kind="application_evidence",
        owner_id="attempt-receipt",
        state="applied",
        completed_at=1_000,
    )

    preview = reclaim_terminal_artifacts(
        root,
        minimum_age_seconds=100,
        now=10_000,
    )
    assert preview["eligible_directories"] == 1
    assert old.exists()

    executed = reclaim_terminal_artifacts(
        root,
        minimum_age_seconds=100,
        now=10_000,
        execute=True,
    )
    assert executed["reclaimed_directories"] == 1
    assert not old.exists()
    assert all(
        path.exists() for path in (active, recent, uncertain, receipt, unmarked)
    )


def test_worker_cleanup_reclaims_old_cache_once_and_preserves_site_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    edge_root = tmp_path / "edge"
    cloak_root = tmp_path / "cloak"
    profile = edge_root / "worker-4"
    cloak_profile = cloak_root / "worker-4"
    old_cache = _write(profile / "Default" / "Cache" / "old.bin", b"cache")
    cloak_cache = _write(cloak_profile / "Default" / "Cache" / "old.bin", b"cloak")
    cookie = _write(profile / "Default" / "Network" / "Cookies", b"session")
    mark_owned_directory(
        profile,
        root=edge_root,
        kind="browser_profile",
        owner_id="edge:worker-4",
    )
    two_days_ago = time.time() - 2 * 24 * 60 * 60
    os.utime(old_cache, (two_days_ago, two_days_ago))
    os.utime(cloak_cache, (two_days_ago, two_days_ago))
    monkeypatch.setattr(chrome.config, "CHROME_WORKER_DIR", edge_root)
    monkeypatch.setattr(chrome.config, "CLOAK_WORKER_DIR", cloak_root)
    monkeypatch.setenv("APPLYPILOT_PROFILE_CACHE_RETENTION_DAYS", "1")
    chrome._profile_maintenance_checked.clear()
    monkeypatch.setattr(chrome, "_wait_for_browser_stopped", lambda *_args: True)

    class StoppedProcess:
        @staticmethod
        def poll():
            return 0

    class OwnedProfileLock:
        held = True

        owned_by_current_thread = True

        @staticmethod
        def actual_browser_stopped():
            return True

        def release_after_stop(self, *, profile_path, browser_stopped):
            assert profile_path == profile.resolve()
            assert browser_stopped is True
            self.held = False

    chrome._profile_locks[4] = OwnedProfileLock()
    chrome._profile_paths[4] = profile.resolve()

    chrome.cleanup_worker(4, StoppedProcess())

    assert not old_cache.exists()
    assert cookie.read_bytes() == b"session"
    assert cloak_cache.read_bytes() == b"cloak"


def test_worker_cleanup_skips_prune_when_browser_endpoint_remains_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    edge_root = tmp_path / "edge"
    cloak_root = tmp_path / "cloak"
    profile = edge_root / "worker-5"
    old_cache = _write(profile / "Default" / "Cache" / "old.bin", b"cache")
    mark_owned_directory(
        profile,
        root=edge_root,
        kind="browser_profile",
        owner_id="edge:worker-5",
    )
    old_time = time.time() - 2 * 24 * 60 * 60
    os.utime(old_cache, (old_time, old_time))
    monkeypatch.setattr(chrome.config, "CHROME_WORKER_DIR", edge_root)
    monkeypatch.setattr(chrome.config, "CLOAK_WORKER_DIR", cloak_root)
    monkeypatch.setattr(
        chrome,
        "_close_browser_via_cdp",
        lambda _port: (_ for _ in ()).throw(RuntimeError("still active")),
    )
    monkeypatch.setattr(chrome, "_wait_for_browser_stopped", lambda *_args: False)

    class BootstrapExited:
        pid = 55

        @staticmethod
        def poll():
            return 0

    chrome._chrome_procs[5] = BootstrapExited()
    chrome._chrome_ports[5] = 9555
    chrome.cleanup_worker(5, chrome._chrome_procs[5])

    assert old_cache.exists()
    assert profile.resolve() not in chrome._profile_maintenance_checked


def test_cloak_version_pin_is_optional_runtime_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cloakbrowser = ModuleType("cloakbrowser")
    monkeypatch.setitem(sys.modules, "cloakbrowser", cloakbrowser)
    calls: list[str | None] = []
    monkeypatch.setattr(chrome, "resolve_browser_backend", lambda *_args, **_kwargs: "cloak")
    monkeypatch.setattr(
        cloakbrowser,
        "ensure_binary",
        lambda *, browser_version=None, **_kwargs: calls.append(browser_version)
        or "cloak.exe",
        raising=False,
    )
    for key in (
        "APPLYPILOT_CLOAK_VERSION",
        "CLOAKBROWSER_LICENSE_KEY",
        "CLOAKBROWSER_BINARY_PATH",
        "CLOAKBROWSER_DOWNLOAD_URL",
        "CLOAKBROWSER_SKIP_CHECKSUM",
    ):
        monkeypatch.delenv(key, raising=False)

    assert chrome.get_browser_executable("cloak") == "cloak.exe"
    monkeypatch.setenv("APPLYPILOT_CLOAK_VERSION", "future-version")
    assert chrome.get_browser_executable("cloak") == "cloak.exe"

    assert calls == [None, "future-version"]
