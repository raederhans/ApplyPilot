"""Application-launch configuration and readiness queries."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path

from applypilot import config
from applypilot.apply.submission_admission import evaluate_submission_admission
from applypilot.runtime_settings import load_runtime_settings


def load_profile(path: Path) -> dict:
    """Load a profile for command policy checks, preserving the legacy fallback."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def resolve_apply_backend(override: str | None, environ: Mapping[str, str]) -> str:
    """Resolve the selected agent backend without validating CLI presentation."""
    settings = load_runtime_settings(environ)
    try:
        return settings.resolve_apply_backend(override)
    except ValueError:
        return settings.raw_apply_backend(override)


def resolve_apply_model(
    backend: str,
    override: str | None,
    environ: Mapping[str, str],
) -> str:
    """Resolve the backend-specific model using the established environment defaults."""
    return load_runtime_settings(environ).resolve_model(backend, override)


def count_submission_ready_jobs(
    conn: sqlite3.Connection,
    *,
    dry_run: bool,
    profile: dict,
    minimum_fit_score: int | None = None,
) -> int:
    """Count jobs through the shared admission predicate without writing state."""
    policy = profile.get("submission_policy", {})
    configured_score = (
        policy.get("minimum_fit_score", config.DEFAULTS.get("min_score", 4))
        if isinstance(policy, Mapping)
        else config.DEFAULTS.get("min_score", 4)
    )
    score = minimum_fit_score if minimum_fit_score is not None else configured_score
    if isinstance(score, bool) or not isinstance(score, int):
        raise TypeError("minimum_fit_score must be an integer")
    score = max(1, min(score, 10))
    ready = 0
    for row in conn.execute("SELECT * FROM jobs").fetchall():
        job = dict(row)
        job["application_url"] = job.get("application_url") or job.get("url")
        if evaluate_submission_admission(
            job,
            profile,
            minimum_fit_score=score,
            preview_only=dry_run,
        ).get("admitted"):
            ready += 1
    return ready
