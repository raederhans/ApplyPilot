"""Read-only CLI body for durable application-batch progress."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class JsonConsole(Protocol):
    def print_json(self, *, data: object) -> None: ...

    def print(self, *objects: object, **kwargs: object) -> None: ...


def run_next(
    console: JsonConsole,
    *,
    authorization_file: Path,
    offset: int = 0,
    limit: int = 5,
) -> None:
    """Print one bounded, restart-safe projection for an existing manifest."""
    from applypilot import config
    from applypilot.apply.authorization import load_manifest
    from applypilot.apply.batch_progress import batch_progress, open_read_only_database

    manifest = load_manifest(authorization_file)
    profile = config.load_profile()
    policy = profile.get("submission_policy", {})
    minimum_fit_score = int(
        policy.get("minimum_fit_score", config.DEFAULTS["min_score"])
        if isinstance(policy, dict)
        else config.DEFAULTS["min_score"]
    )
    connection = open_read_only_database(config.DB_PATH)
    try:
        result = batch_progress(
            connection,
            manifest,
            profile,
            minimum_fit_score=minimum_fit_score,
            offset=offset,
            limit=limit,
        )
    finally:
        if connection is not None:
            connection.close()
    result["continuation"] = (
        f'applypilot --workspace "{config.APP_DIR}" apply '
        f'--authorization-file "{authorization_file.resolve()}" '
        f"--limit {min(limit, int(result['remaining_capacity']))}"
        if result["next"]
        else None
    )
    console.print_json(data=result)
