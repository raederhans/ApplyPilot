"""Safe retention helpers for ApplyPilot-owned browser and job artifacts.

Browser profiles are reusable identities, not per-application scratch space.
These helpers preserve login/site state and make only regenerable cache or
runtime files eligible for age-gated cleanup. Cleanup defaults to preview.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

OWNERSHIP_MARKER = ".applypilot-owned.json"
TERMINAL_ARTIFACT_STATES = frozenset(
    {"applied", "failed", "previewed", "skipped"}
)

# Exact path components keep this independent from browser/ATS versions.
_CACHE_COMPONENTS = frozenset(
    {
        "blob_storage",
        "browsermetrics",
        "cache",
        "cachestorage",
        "code cache",
        "component_crx_cache",
        "crashpad",
        "dawncache",
        "extensions_crx_cache",
        "gpucache",
        "graphitedawncache",
        "grshadercache",
        "optimization_guide_model_store",
        "provenancedata",
        "safe browsing",
        "safebrowsing",
        "service worker",
        "shadercache",
    }
)
_RUNTIME_COMPONENTS = frozenset(
    {"crash reports", "downloads", "recovery", "sessions", "temp"}
)
_RUNTIME_FILE_PREFIXES = (
    "browsermetrics",
    "devtoolsactiveport",
    "history",
    "last session",
    "last tabs",
    "singletoncookie",
    "singletonlock",
    "singletonsocket",
    "visited links",
)
# Browser-managed component packages. These are downloaded/generated again and
# are not the similarly named user databases nested below ``Default``.
_REGENERABLE_TOP_LEVEL_COMPONENTS = frozenset(
    {
        "crowd deny",
        "edge entity extraction",
        "edge shopping",
        "edgelanguagedetectionmodel",
        "hyphen-data",
        "meipreload",
        "provenancedatatensors",
        "speech recognition",
        "sslerrorassistant",
        "subresource filter",
        "typosquatting",
        "well known domains",
        "zxcvbndata",
    }
)
_IDENTITY_FILES = frozenset(
    {
        ".applypilot-cloak-fingerprint",
        OWNERSHIP_MARKER,
        "local state",
        "preferences",
        "secure preferences",
    }
)
_SITE_STATE_COMPONENTS = frozenset(
    {
        "cookies",
        "indexeddb",
        "local storage",
        "login data",
        "network",
        "session storage",
        "shared dictionary",
        "web data",
    }
)


def _contained_path(path: Path, root: Path) -> Path:
    resolved_root = root.resolve()
    resolved = path.resolve()
    if resolved == resolved_root or not resolved.is_relative_to(resolved_root):
        raise ValueError(f"Artifact path is outside its owned root: {resolved}")
    return resolved


def mark_owned_directory(
    path: Path,
    *,
    root: Path,
    kind: str,
    owner_id: str,
    state: str = "active",
    completed_at: float | None = None,
) -> Path:
    """Create or refresh an explicit ownership marker in a contained path."""
    resolved = _contained_path(path, root)
    resolved.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "owner": "applypilot",
        "kind": str(kind),
        "owner_id": str(owner_id),
        "state": str(state),
        "updated_at": time.time(),
    }
    if completed_at is not None:
        payload["completed_at"] = float(completed_at)
    marker = resolved / OWNERSHIP_MARKER
    temporary = marker.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temporary, marker)
    return marker


def read_ownership_marker(path: Path) -> dict | None:
    marker = path / OWNERSHIP_MARKER
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return None
    if payload.get("owner") != "applypilot" or payload.get("schema") != 1:
        return None
    return payload


def classify_profile_path(relative_path: Path) -> str:
    """Classify a browser-profile path without browser-version assumptions."""
    lowered = tuple(part.casefold() for part in relative_path.parts)
    name = relative_path.name.casefold()
    if name in _IDENTITY_FILES:
        return "identity"
    if any(part in _SITE_STATE_COMPONENTS for part in lowered):
        return "site_state"
    if any(part in _CACHE_COMPONENTS for part in lowered) or (
        lowered and lowered[0] in _REGENERABLE_TOP_LEVEL_COMPONENTS
    ):
        return "regenerable_cache"
    if any(part in _RUNTIME_COMPONENTS for part in lowered) or name.startswith(
        _RUNTIME_FILE_PREFIXES
    ):
        return "runtime_transient"
    return "other"


def inventory_profile(profile_dir: Path) -> dict:
    """Return a read-only size/count inventory grouped by retention class."""
    totals: dict[str, dict[str, int]] = defaultdict(lambda: {"files": 0, "bytes": 0})
    root = profile_dir.resolve()
    if not root.is_dir():
        return {"path": str(root), "files": 0, "bytes": 0, "categories": {}}
    total_files = 0
    total_bytes = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(current) / name).is_symlink()]
        for name in files:
            path = Path(current) / name
            if path.is_symlink():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            category = classify_profile_path(path.relative_to(root))
            totals[category]["files"] += 1
            totals[category]["bytes"] += size
            total_files += 1
            total_bytes += size
    return {
        "path": str(root),
        "files": total_files,
        "bytes": total_bytes,
        "categories": dict(sorted(totals.items())),
    }


def profile_clone_ignore(_directory: str, names: list[str]) -> set[str]:
    """Ignore clearly regenerable or job/runtime-scoped clone content."""
    ignored: set[str] = set()
    for name in names:
        lowered = name.casefold()
        if (
            lowered == OWNERSHIP_MARKER
            or lowered in _CACHE_COMPONENTS
            or lowered in _REGENERABLE_TOP_LEVEL_COMPONENTS
            or lowered in _RUNTIME_COMPONENTS
            or lowered.startswith(_RUNTIME_FILE_PREFIXES)
        ):
            ignored.add(name)
    return ignored


def prune_owned_profile(
    profile_dir: Path,
    *,
    profile_root: Path,
    minimum_age_seconds: float = 7 * 24 * 60 * 60,
    execute: bool = False,
    browser_is_stopped: bool = False,
    now: float | None = None,
) -> dict:
    """Preview or remove old regenerable files from one owned profile.

    Identity, cookies, local/session storage, IndexedDB and unknown files are
    never eligible. Execution also requires an explicit stopped-browser claim.
    """
    resolved = _contained_path(profile_dir, profile_root)
    marker = read_ownership_marker(resolved)
    if not marker or marker.get("kind") != "browser_profile":
        raise PermissionError(f"Browser profile is not explicitly ApplyPilot-owned: {resolved}")
    if execute and not browser_is_stopped:
        raise RuntimeError("Refusing profile cleanup while browser stop is not confirmed")
    cutoff = (time.time() if now is None else float(now)) - max(
        0.0, float(minimum_age_seconds)
    )
    candidates: list[Path] = []
    candidate_bytes = 0
    for current, dirs, files in os.walk(resolved, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(current) / name).is_symlink()]
        for name in files:
            path = Path(current) / name
            if path.is_symlink():
                continue
            category = classify_profile_path(path.relative_to(resolved))
            if category not in {"regenerable_cache", "runtime_transient"}:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime > cutoff:
                continue
            candidates.append(path)
            candidate_bytes += stat.st_size

    removed_files = 0
    removed_bytes = 0
    if execute:
        for path in candidates:
            try:
                size = path.stat().st_size
                path.unlink()
                removed_files += 1
                removed_bytes += size
            except FileNotFoundError:
                continue
        directories = sorted(
            (path for path in resolved.rglob("*") if path.is_dir() and not path.is_symlink()),
            key=lambda path: len(path.parts),
            reverse=True,
        )
        for directory in directories:
            try:
                directory.rmdir()
            except OSError:
                pass
    return {
        "path": str(resolved),
        "mode": "execute" if execute else "preview",
        "eligible_files": len(candidates),
        "eligible_bytes": candidate_bytes,
        "removed_files": removed_files,
        "removed_bytes": removed_bytes,
    }


def snapshot_files(root: Path, names: Iterable[str]) -> dict[str, tuple[int, int]]:
    """Capture lightweight run-start fingerprints for selected evidence files."""
    snapshot: dict[str, tuple[int, int]] = {}
    for name in names:
        path = root / name
        try:
            stat = path.stat()
        except (FileNotFoundError, OSError):
            continue
        if path.is_file() and not path.is_symlink():
            snapshot[name] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


def archive_new_evidence(
    source_root: Path,
    destination: Path,
    names: Iterable[str],
    *,
    baseline: dict[str, tuple[int, int]] | None = None,
) -> list[Path]:
    """Archive only evidence created or changed since a run-start snapshot."""
    archived: list[Path] = []
    baseline = baseline or {}
    destination.mkdir(parents=True, exist_ok=True)
    manifest_path = destination / "evidence-manifest.json"
    manifest: dict[str, str] = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            manifest = {}
    for name in names:
        source = source_root / name
        try:
            stat = source.stat()
        except (FileNotFoundError, OSError):
            continue
        if not source.is_file() or source.is_symlink():
            continue
        fingerprint = (stat.st_size, stat.st_mtime_ns)
        if baseline.get(name) == fingerprint:
            continue
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if manifest.get(name) == digest:
            continue
        target = destination / name
        shutil.copy2(source, target)
        manifest[name] = digest
        archived.append(target)
    if archived:
        manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    elif not any(destination.iterdir()):
        destination.rmdir()
    return archived


def reclaim_terminal_artifacts(
    artifact_root: Path,
    *,
    minimum_age_seconds: float = 30 * 24 * 60 * 60,
    execute: bool = False,
    now: float | None = None,
) -> dict:
    """Preview or reclaim marked terminal job-transient directories.

    Receipt and application-evidence kinds are deliberately never eligible.
    """
    root = artifact_root.resolve()
    cutoff = (time.time() if now is None else float(now)) - max(
        0.0, float(minimum_age_seconds)
    )
    candidates: list[Path] = []
    for child in root.iterdir() if root.is_dir() else ():
        if not child.is_dir() or child.is_symlink():
            continue
        resolved = _contained_path(child, root)
        marker = read_ownership_marker(resolved)
        # Evidence and receipts use distinct kinds and are never admitted here.
        if not marker or marker.get("kind") != "job_transient":
            continue
        if marker.get("state") not in TERMINAL_ARTIFACT_STATES:
            continue
        try:
            completed_at = float(marker["completed_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if completed_at <= cutoff:
            candidates.append(resolved)
    reclaimed = 0
    if execute:
        for candidate in candidates:
            shutil.rmtree(candidate)
            reclaimed += 1
    return {
        "path": str(root),
        "mode": "execute" if execute else "preview",
        "eligible_directories": len(candidates),
        "reclaimed_directories": reclaimed,
    }
