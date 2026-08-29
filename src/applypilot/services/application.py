"""Application-launch configuration and readiness queries."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path

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
) -> int:
    """Count jobs satisfying the existing preview or submission material contract."""
    if dry_run:
        return int(conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL "
            "AND tailor_status = 'machine_validated' AND applied_at IS NULL "
            "AND COALESCE(eligibility_status, 'eligible') != 'ineligible'"
        ).fetchone()[0])

    allow_runtime_cover = bool(
        profile.get("submission_policy", {}).get(
            "allow_runtime_cover_letter_discovery", False
        )
    )
    return int(conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL "
        "AND tailor_status = 'machine_validated' "
        "AND applied_at IS NULL "
        + (
            ""
            if allow_runtime_cover
            else "AND ((cover_letter_path IS NOT NULL AND cover_letter_status IN "
            "('human_approved', 'agent_validated')) OR cover_letter_status = 'not_required')"
        )
    ).fetchone()[0])
