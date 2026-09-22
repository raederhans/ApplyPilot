"""Stable application-job API over focused acquisition and persistence owners.

Job selection and admission live in job_candidates/job_acquisition. Status
mutations live in job_results. The final-submit duplicate predicate below is
unchanged and still requires the caller's submit-claim transaction.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

from applypilot import config
from applypilot.apply import job_acquisition
from applypilot.apply.job_results import (
    mark_job,
    mark_result,
    mark_runtime_cover_not_required,
    release_lock,
    reset_failed,
    restore_preview_state,
)


__all__ = [
    "acquire_job",
    "mark_job",
    "mark_result",
    "mark_runtime_cover_not_required",
    "release_lock",
    "reset_failed",
    "restore_preview_state",
    "revalidate_duplicate_before_submit",
]


def revalidate_duplicate_before_submit(
    connection: sqlite3.Connection,
    job_url: str,
) -> dict[str, object]:
    """Recheck durable duplicate identities inside the submit claim transaction.

    Read-only duplicate analysis may run in parallel against a frozen job
    snapshot.  This final check intentionally requires an existing SQLite
    transaction so callers can keep it atomic with the submission-gate claim
    and avoid a time-of-check/time-of-use gap.
    """
    if not connection.in_transaction:
        raise RuntimeError(
            "duplicate revalidation must run inside the submission claim transaction"
        )
    job_url = str(job_url or "").strip()
    if not job_url:
        raise ValueError("job_url is required")
    current = connection.execute(
        "SELECT url, canonical_job_url, platform_job_id, apply_status "
        "FROM jobs WHERE url=?",
        (job_url,),
    ).fetchone()
    if current is None:
        return {"clear": False, "reason": "job_not_found"}
    current = dict(current)
    if str(current.get("apply_status") or "").casefold() in {
        "applied",
        "submission_uncertain",
    }:
        return {"clear": False, "reason": "current_job_already_submitted_or_uncertain"}

    receipt = connection.execute(
        "SELECT receipt_source, receipt_id FROM application_receipts "
        "WHERE job_url=? ORDER BY admitted_at DESC LIMIT 1",
        (job_url,),
    ).fetchone()
    if receipt is not None:
        return {
            "clear": False,
            "reason": "current_job_receipt_exists",
            "receipt_source": str(receipt[0]),
        }

    canonical = str(current.get("canonical_job_url") or "").strip()
    platform_id = str(current.get("platform_job_id") or "").strip()
    if not canonical and not platform_id:
        return {"clear": True, "reason": "no_duplicate_identity"}
    duplicate = connection.execute(
        """
        SELECT j.url, j.apply_status,
               EXISTS(SELECT 1 FROM application_receipts AS r WHERE r.job_url=j.url) AS has_receipt
        FROM jobs AS j
        WHERE j.url != ?
          AND ((? != '' AND j.canonical_job_url = ?)
               OR (? != '' AND j.platform_job_id = ?))
          AND (j.apply_status IN ('applied', 'submission_uncertain')
               OR EXISTS(SELECT 1 FROM application_receipts AS r WHERE r.job_url=j.url))
        ORDER BY CASE WHEN j.apply_status='applied' THEN 0 ELSE 1 END, j.url
        LIMIT 1
        """,
        (job_url, canonical, canonical, platform_id, platform_id),
    ).fetchone()
    if duplicate is None:
        return {"clear": True, "reason": "no_duplicate_submission"}
    return {
        "clear": False,
        "reason": "duplicate_submission_identity",
        "matched_job_url": str(duplicate[0]),
        "matched_status": str(duplicate[1] or "receipt"),
        "has_receipt": bool(duplicate[2]),
    }



def acquire_job(
    connection: sqlite3.Connection,
    target_url: str | None = None,
    min_score: int = 6,
    worker_id: int = 0,
    preview_only: bool = False,
    authorization_manifest: dict | None = None,
    exclude_urls: set[str] | None = None,
    *,
    performance_sink: dict[str, object] | None = None,
    load_blocked: Callable[[], tuple[list[str], list[str]]],
    application_lease_minutes: int,
    runtime_cell_claim: Callable[[sqlite3.Connection, dict, str], object] | None = None,
) -> dict | None:
    """Preserve the existing call surface; resolve mutable configuration once."""
    return job_acquisition.acquire_job(
        connection, target_url, min_score, worker_id, preview_only,
        authorization_manifest, exclude_urls,
        performance_sink=performance_sink, load_blocked=load_blocked,
        application_lease_minutes=application_lease_minutes,
        runtime_cell_claim=runtime_cell_claim,
        profile_loader=config.load_profile,
        clock=lambda: datetime.now(UTC),
        environ=dict(os.environ),
    )
