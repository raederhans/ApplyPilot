"""Fail-closed filesystem namespaces for one application runtime turn.

The public package, CLI, and environment names intentionally remain
``applypilot``.  These paths are an internal isolation boundary so concurrent
pipeline runs and Agent turns never reuse transient MCP, report, or log files.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")


def _component(value: object, *, name: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{name} is required")
    # Keep a strict Windows path budget even under long pytest/temp roots.
    # Full identities remain available in ``as_dict``; disk components need
    # only a short human hint plus a collision-resistant digest.
    readable = _SAFE_COMPONENT.sub("-", raw).strip("-._")[:12] or name
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{readable}-{digest}"


def _contained(path: Path, root: Path) -> Path:
    resolved_root = root.expanduser().resolve(strict=False)
    resolved = path.expanduser().resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("runtime output escaped its namespace root") from exc
    return resolved


@dataclass(frozen=True, slots=True)
class RuntimeNamespace:
    """Bind a run, Agent session, browser profile, and output directory."""

    root: Path
    run_id: str
    session_id: str
    profile_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.expanduser().resolve(strict=False))
        for name in ("run_id", "session_id", "profile_id"):
            value = str(getattr(self, name) or "").strip()
            if not value:
                raise ValueError(f"{name} is required")
            object.__setattr__(self, name, value)

    @property
    def run_root(self) -> Path:
        return _contained(
            self.root / "runs" / _component(self.run_id, name="run"),
            self.root,
        )

    @property
    def session_root(self) -> Path:
        return _contained(
            self.run_root
            / "sessions"
            / _component(self.session_id, name="session"),
            self.run_root,
        )

    @property
    def output_root(self) -> Path:
        return _contained(
            self.session_root
            / "profiles"
            / _component(self.profile_id, name="profile"),
            self.session_root,
        )

    def path(self, name: str) -> Path:
        """Return one direct child path, rejecting traversal and aliases."""
        if not name or Path(name).name != name or name in {".", ".."}:
            raise ValueError("runtime output name must be one direct child")
        return _contained(self.output_root / name, self.output_root)

    def as_dict(self) -> dict[str, str]:
        return {
            "schema_version": "1",
            "run_id": self.run_id,
            "session_id": self.session_id,
            "profile_id": self.profile_id,
            "output_root": str(self.output_root),
        }
