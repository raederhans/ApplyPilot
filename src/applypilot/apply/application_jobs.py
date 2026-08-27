"""Application-job acquisition and status persistence.

The orchestration layer supplies the connection and runtime policy inputs.
This module owns atomic job selection and durable job-status mutations.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

from applypilot import config

logger = logging.getLogger(__name__)


def acquire_job(
    connection: sqlite3.Connection,
    target_url: str | None = None,
    min_score: int = 6,
    worker_id: int = 0,
    preview_only: bool = False,
    authorization_manifest: dict | None = None,
    exclude_urls: set[str] | None = None,
    *,
    load_blocked: Callable[[], tuple[list[str], list[str]]],
    application_lease_minutes: int,
) -> dict | None:
    """Atomically acquire the next job to apply to.

    Args:
        target_url: Apply to a specific URL instead of picking from queue.
        min_score: Minimum fit_score threshold.
        worker_id: Worker claiming this job (for tracking).
        exclude_urls: Exact job URLs already attempted in this command.

    Returns:
        Job dict or None if the queue is empty.
    """
    conn = connection
    excluded = {str(url) for url in (exclude_urls or set()) if str(url)}
    from applypilot.database import (
        recover_stale_application_attempts,
        start_application_attempt,
    )
    from applypilot.eligibility import ELIGIBLE_SQL, refresh_job_eligibility
    recover_stale_application_attempts(conn)
    refresh_job_eligibility(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")

        try:
            submission_policy = config.load_profile().get("submission_policy", {})
        except FileNotFoundError:
            submission_policy = {}
        allow_runtime_cover = bool(
            submission_policy.get("allow_runtime_cover_letter_discovery", False)
        )
        allow_runtime_readiness = bool(
            submission_policy.get("allow_runtime_readiness_review", False)
        )

        if target_url:
            material_clause = """
                  AND tailored_resume_path IS NOT NULL
                  AND tailor_status = 'machine_validated'
            """
            if not preview_only and not allow_runtime_cover:
                material_clause += """
                  AND (
                    (cover_letter_path IS NOT NULL AND cover_letter_status IN ('human_approved', 'agent_validated'))
                    OR cover_letter_status = 'not_required'
                  )
                """
            target_match = "(url = ? OR application_url = ?)"
            target_params = (target_url, target_url)
            rows = conn.execute(f"""
                SELECT *
                FROM jobs
                WHERE {target_match}
                  {material_clause}
                  AND (apply_status IS NULL OR apply_status IN ('failed', 'previewed'))
                  AND COALESCE(apply_retry_blocked, 0) = 0
                  AND eligibility_status != 'ineligible'
            """, target_params).fetchall()
            if excluded:
                rows = [candidate for candidate in rows if candidate["url"] not in excluded]
        else:
            blocked_sites, blocked_patterns = load_blocked()
            # Build parameterized filters to avoid SQL injection
            params: list = [min_score]
            excluded_clause = ""
            if excluded:
                excluded_placeholders = ",".join("?" * len(excluded))
                excluded_clause = f"AND url NOT IN ({excluded_placeholders})"
                params.extend(sorted(excluded))
            site_clause = ""
            if blocked_sites:
                placeholders = ",".join("?" * len(blocked_sites))
                site_clause = f"AND site NOT IN ({placeholders})"
                params.extend(blocked_sites)
            url_clauses = ""
            if blocked_patterns:
                url_clauses = " ".join("AND url NOT LIKE ?" for _ in blocked_patterns)
                params.extend(blocked_patterns)
            rows = conn.execute(f"""
                SELECT *
                FROM jobs
                WHERE tailored_resume_path IS NOT NULL
                  AND tailor_status = 'machine_validated'
                  {"" if allow_runtime_cover else "AND ((cover_letter_path IS NOT NULL AND cover_letter_status IN ('human_approved', 'agent_validated')) OR cover_letter_status = 'not_required')"}
                  AND (apply_status IS NULL OR apply_status IN ('failed', 'previewed'))
                  AND (apply_attempts IS NULL OR apply_attempts < ?)
                  AND COALESCE(apply_retry_blocked, 0) = 0
                  AND fit_score >= ?
                  AND {ELIGIBLE_SQL}
                  {excluded_clause}
                  {site_clause}
                  {url_clauses}
                ORDER BY fit_score DESC, url
            """, [config.DEFAULTS["max_apply_attempts"]] + params).fetchall()

        row = None
        if authorization_manifest is None:
            row = rows[0] if rows else None
        else:
            from applypilot.apply.authorization import authorize_job
            from applypilot.apply.decision import evaluate

            minimum_fit_score = max(1, min(int(min_score), 10))

            try:
                expires_at = datetime.fromisoformat(
                    str(authorization_manifest.get("expires_at") or "")
                )
            except ValueError:
                expires_at = datetime.min.replace(tzinfo=UTC)
            if expires_at.tzinfo is None or datetime.now(UTC) >= expires_at:
                rows = []

            for candidate in rows:
                candidate_job = dict(candidate)
                candidate_job["application_url"] = (
                    candidate_job.get("application_url") or candidate_job.get("url")
                )
                candidate_decision = evaluate(
                    candidate_job,
                    minimum_fit_score=minimum_fit_score,
                    allow_runtime_readiness=allow_runtime_readiness,
                    allow_runtime_cover_letter=allow_runtime_cover,
                )
                if candidate_decision.get("decision") != "ready_to_apply":
                    continue
                try:
                    authorized = authorize_job(authorization_manifest, candidate_job)
                except (KeyError, PermissionError, RuntimeError, ValueError):
                    continue
                if authorized is not None:
                    row = candidate
                    break

        if not row:
            conn.rollback()
            return None

        apply_url = row["application_url"] or row["url"]
        portal_gate = config.portal_application_gate(
            apply_url,
            source_site=row["source_site"],
            site=row["site"],
            preview_only=preview_only,
        )
        if portal_gate:
            conn.execute(
                "UPDATE jobs SET apply_status = 'manual', apply_error = ? WHERE url = ?",
                (portal_gate, row["url"]),
            )
            conn.commit()
            logger.info("Portal policy paused browser application: %s", row["url"][:80])
            return None

        if (
            target_url
            and not preview_only
            and os.environ.get("APPLYPILOT_AUTO_SUBMIT") == "1"
        ):
            policy_min_score = int(
                os.environ.get("APPLYPILOT_AUTO_SUBMIT_MIN_SCORE", "8")
            )
            auto_issue = None
            if not str(row["company_name"] or "").strip():
                auto_issue = "missing verified company"
            elif not str(row["full_description"] or "").strip():
                auto_issue = "missing enriched job description"
            elif row["fit_score"] is None or int(row["fit_score"]) < policy_min_score:
                auto_issue = (
                    f"fit score {row['fit_score']} is below automatic minimum "
                    f"{policy_min_score}"
                )
            if auto_issue:
                conn.rollback()
                logger.warning("Automatic submission paused for %s: %s", row["url"], auto_issue)
                return None

        # Skip manual ATS sites (unsolvable CAPTCHAs)
        from applypilot.config import is_manual_ats
        if is_manual_ats(apply_url):
            conn.execute(
                "UPDATE jobs SET apply_status = 'manual', apply_error = 'manual ATS' WHERE url = ?",
                (row["url"],),
            )
            conn.commit()
            logger.info("Skipping manual ATS: %s", row["url"][:80])
            return None

        now = datetime.now(UTC).isoformat()
        attempt_id = start_application_attempt(
            row["url"],
            f"worker-{worker_id}",
            batch_id=(authorization_manifest or {}).get("batch_id"),
            lease_minutes=application_lease_minutes,
            conn=conn,
        )
        conn.execute("""
            UPDATE jobs SET apply_status = 'in_progress',
                           agent_id = ?,
                           last_attempted_at = ?,
                           apply_task_id = ?
            WHERE url = ?
        """, (f"worker-{worker_id}", now, attempt_id, row["url"]))
        conn.commit()

        acquired = dict(row)
        acquired["_attempt_id"] = attempt_id
        return acquired
    except Exception:
        conn.rollback()
        raise


def mark_result(
    connection: sqlite3.Connection,
    url: str,
    status: str,
    error: str | None = None,
    permanent: bool = False,
    duration_ms: int | None = None,
    task_id: str | None = None,
    evidence: dict | None = None,
) -> None:
    """Update a job's apply status in the database."""
    conn = connection
    conn.execute("BEGIN IMMEDIATE")
    now = datetime.now(UTC).isoformat()
    where = "WHERE url = ?"
    where_params: tuple[object, ...] = (url,)
    if task_id:
        where += (
            " AND apply_task_id = ? AND EXISTS ("
            "SELECT 1 FROM application_attempts a WHERE a.attempt_id=? "
            "AND a.status='in_progress' AND a.lease_expires_at > ?)"
        )
        where_params += (task_id, task_id, now)
    if status == "applied":
        cursor = conn.execute(f"""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_retry_blocked = 0, apply_retry_reason = NULL,
                           apply_duration_ms = ?, apply_task_id = ?,
                           verification_confidence = 'visible_confirmation',
                           application_evidence = ?, application_recorded_at = ?,
                           submission_observation_json = ?, submission_observed_at = ?
            {where}
        """, (
            now,
            duration_ms,
            task_id,
            json.dumps(evidence or {}, ensure_ascii=False),
            now,
            json.dumps(evidence or {}, ensure_ascii=False),
            now,
            *where_params,
        ))
    elif status == "submission_uncertain":
        observation = {
            "submit_clicked": True,
            "receipt_visible": False,
            "applied_badge_visible": False,
            "note": error or "final submission was attempted without visible confirmation",
        }
        if evidence:
            observation.update(evidence)
        cursor = conn.execute(f"""
            UPDATE jobs SET apply_status = 'submission_uncertain', applied_at = NULL,
                           apply_error = NULL, agent_id = NULL,
                           apply_retry_blocked = 1,
                           apply_retry_reason = 'submission_uncertain_requires_review',
                           apply_attempts = COALESCE(apply_attempts, 0) + 1,
                           apply_duration_ms = ?, apply_task_id = ?,
                           verification_confidence = 'browser_observation_pending',
                           application_evidence = 'submit_clicked_without_visible_confirmation',
                           application_recorded_at = ?,
                           submission_observation_json = ?, submission_observed_at = ?
            {where}
        """, (
            duration_ms,
            task_id,
            now,
            json.dumps(observation, ensure_ascii=False),
            now,
            *where_params,
        ))
    elif status == "previewed":
        cursor = conn.execute(f"""
            UPDATE jobs SET apply_status = 'previewed', applied_at = NULL,
                           apply_error = NULL, agent_id = NULL,
                           apply_retry_blocked = 0, apply_retry_reason = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            {where}
        """, (duration_ms, task_id, *where_params))
    else:
        cursor = conn.execute(f"""
            UPDATE jobs SET apply_status = ?, apply_error = ?,
                           apply_attempts = COALESCE(apply_attempts, 0) + 1,
                           apply_retry_blocked = ?, apply_retry_reason = ?,
                           agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            {where}
        """, (
            status,
            error or "unknown",
            1 if permanent else 0,
            (error or "unknown") if permanent else None,
            duration_ms,
            task_id,
            *where_params,
        ))
    if task_id and cursor.rowcount != 1:
        conn.rollback()
        raise RuntimeError("stale application attempt cannot update the current job")
    from applypilot.database import (
        finalize_application_attempt,
        record_application_risk_event,
    )

    finalize_application_attempt(
        task_id,
        status,
        evidence=evidence or ({"error": error} if error else None),
        conn=conn,
    )
    if status == "submission_uncertain":
        record_application_risk_event(
            url,
            "duplicate_submission_risk",
            "high",
            attempt_id=task_id,
            evidence={"reason": error or "confirmation_inconclusive"},
            conn=conn,
        )
    elif status == "failed" and any(
        marker in str(error or "").casefold()
        for marker in ("manual_review", "captcha", "verification", "identity")
    ):
        record_application_risk_event(
            url,
            "manual_application_gate",
            "medium",
            attempt_id=task_id,
            evidence={"reason": error},
            conn=conn,
        )
    conn.commit()


def release_lock(
    connection: sqlite3.Connection,
    url: str,
    task_id: str | None = None,
) -> None:
    """Release the in_progress lock without changing status."""
    conn = connection
    cursor = conn.execute(
        "UPDATE jobs SET apply_status = NULL, agent_id = NULL "
        "WHERE url = ? AND apply_status = 'in_progress' "
        "AND (? IS NULL OR apply_task_id = ?)",
        (url, task_id, task_id),
    )
    if cursor.rowcount:
        from applypilot.database import finalize_application_attempt

        finalize_application_attempt(task_id, "released", conn=conn)
    conn.commit()


def restore_preview_state(connection: sqlite3.Connection, job: dict) -> None:
    """Restore the exact application state captured before a dry-run lock."""
    conn = connection
    cursor = conn.execute(
        """
        UPDATE jobs SET apply_status = ?, apply_error = ?, apply_attempts = ?,
                        agent_id = ?, last_attempted_at = ?,
                        apply_duration_ms = ?, apply_task_id = ?,
                        apply_retry_blocked = ?, apply_retry_reason = ?
        WHERE url = ? AND (? IS NULL OR apply_task_id = ?)
        """,
        (
            job.get("apply_status"),
            job.get("apply_error"),
            job.get("apply_attempts"),
            job.get("agent_id"),
            job.get("last_attempted_at"),
            job.get("apply_duration_ms"),
            job.get("apply_task_id"),
            job.get("apply_retry_blocked"),
            job.get("apply_retry_reason"),
            job["url"],
            job.get("_attempt_id"),
            job.get("_attempt_id"),
        ),
    )
    from applypilot.database import finalize_application_attempt

    if cursor.rowcount:
        finalize_application_attempt(job.get("_attempt_id"), "previewed", conn=conn)
    conn.commit()


def mark_runtime_cover_not_required(
    connection: sqlite3.Connection,
    job: dict,
) -> dict:
    """Persist an ATS observation that this exact form has no required cover letter."""
    conn = connection
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "UPDATE jobs SET cover_letter_status='not_required', cover_letter_error=NULL, "
        "cover_letter_approved_at=?, cover_letter_approved_by='runtime_form_observation' "
        "WHERE url=?",
        (now, job["url"]),
    )
    conn.commit()
    refreshed = conn.execute("SELECT * FROM jobs WHERE url=?", (job["url"],)).fetchone()
    if refreshed is None:
        raise ValueError("Exact job disappeared while recording cover-letter discovery")
    refreshed_job = dict(refreshed)
    refreshed_job.update({key: value for key, value in job.items() if key.startswith("_")})
    return refreshed_job


def mark_job(
    connection: sqlite3.Connection,
    url: str,
    status: str,
    reason: str | None = None,
) -> None:
    """Manually mark a job's apply status in the database.

    Args:
        url: Job URL to mark.
        status: Either 'applied' or 'failed'.
        reason: Failure reason (only for status='failed').
    """
    conn = connection
    now = datetime.now(UTC).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_retry_blocked = 0, apply_retry_reason = NULL,
                           verification_confidence = 'manual_visual_confirmation',
                           application_evidence = 'manually_marked_applied',
                           application_recorded_at = ?
            WHERE url = ?
        """, (now, now, url))
    else:
        conn.execute("""
            UPDATE jobs SET apply_status = 'failed', apply_error = ?,
                           apply_retry_blocked = 1, apply_retry_reason = ?,
                           agent_id = NULL
            WHERE url = ?
        """, (reason or "manual", reason or "manual", url))
    conn.commit()


def reset_failed(connection: sqlite3.Connection) -> int:
    """Reset all failed jobs so they can be retried.

    Returns:
        Number of jobs reset.
    """
    conn = connection
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_attempts = 0, apply_retry_blocked = 0,
                       apply_retry_reason = NULL, agent_id = NULL
        WHERE apply_status = 'failed'
          OR (apply_status IS NOT NULL AND apply_status NOT IN (
              'applied', 'in_progress', 'submission_uncertain'
          ))
    """)
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------
