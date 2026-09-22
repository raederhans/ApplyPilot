"""Application status writes, independent of queue ranking and agent execution.

Each operation is atomic. When composed inside an existing transaction, it
uses a savepoint and leaves the caller responsible for the final commit.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from applypilot.storage.transactions import write_transaction


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
    with write_transaction(conn):
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
            agent_evidence = (
                evidence.get("agent", {}) if isinstance(evidence, dict) else {}
            )
            verification_confidence = (
                "direct_email_sent_verified"
                if isinstance(agent_evidence, dict)
                and agent_evidence.get("channel") == "direct_email"
                else "visible_confirmation"
            )
            cursor = conn.execute(f"""
                UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                               apply_error = NULL, agent_id = NULL,
                               apply_retry_blocked = 0, apply_retry_reason = NULL,
                               apply_duration_ms = ?, apply_task_id = ?,
                               verification_confidence = ?,
                               application_evidence = ?, application_recorded_at = ?,
                               submission_observation_json = ?, submission_observed_at = ?
                {where}
            """, (
                now,
                duration_ms,
                task_id,
                verification_confidence,
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



def release_lock(
    connection: sqlite3.Connection,
    url: str,
    task_id: str | None = None,
) -> None:
    """Release the in_progress lock without changing status."""
    conn = connection
    with write_transaction(conn):
        cursor = conn.execute(
            "UPDATE jobs SET apply_status = NULL, agent_id = NULL "
            "WHERE url = ? AND apply_status = 'in_progress' "
            "AND (? IS NULL OR apply_task_id = ?)",
            (url, task_id, task_id),
        )
        if cursor.rowcount:
            from applypilot.database import finalize_application_attempt

            finalize_application_attempt(task_id, "released", conn=conn)



def restore_preview_state(connection: sqlite3.Connection, job: dict) -> None:
    """Restore the exact application state captured before a dry-run lock."""
    conn = connection
    with write_transaction(conn):
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
            evidence = job.get("_preview_attempt_evidence")
            finalize_application_attempt(
                job.get("_attempt_id"),
                "previewed",
                evidence=evidence if isinstance(evidence, dict) else None,
                conn=conn,
            )



def mark_runtime_cover_not_required(
    connection: sqlite3.Connection,
    job: dict,
) -> dict:
    """Persist an ATS observation that this exact form has no required cover letter."""
    conn = connection
    with write_transaction(conn):
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "UPDATE jobs SET cover_letter_status='not_required', cover_letter_error=NULL, "
            "cover_letter_approved_at=?, cover_letter_approved_by='runtime_form_observation' "
            "WHERE url=?",
            (now, job["url"]),
        )
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
) -> str:
    """Manually mark a job's apply status in the database.

    Args:
        url: Job URL to mark.
        status: Either 'applied' or 'failed'.
        reason: Failure reason (only for status='failed').

    Returns:
        The canonical job URL that was updated.

    Raises:
        LookupError: No job matches the supplied canonical or application URL.
        ValueError: The status is invalid or an application URL is ambiguous.
    """
    if status not in {"applied", "failed"}:
        raise ValueError("status must be 'applied' or 'failed'")

    conn = connection
    with write_transaction(conn):
        row = conn.execute("SELECT url FROM jobs WHERE url = ?", (url,)).fetchone()
        if row is not None:
            canonical_url = str(row[0])
        else:
            rows = conn.execute(
                "SELECT url FROM jobs WHERE application_url = ? ORDER BY url LIMIT 2",
                (url,),
            ).fetchall()
            if not rows:
                raise LookupError(f"No job found for URL: {url}")
            if len(rows) > 1:
                raise ValueError(
                    "Application URL matches multiple jobs; use the canonical job URL instead."
                )
            canonical_url = str(rows[0][0])

        now = datetime.now(UTC).isoformat()
        if status == "applied":
            cursor = conn.execute("""
                UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                               apply_error = NULL, agent_id = NULL,
                               apply_retry_blocked = 0, apply_retry_reason = NULL,
                               verification_confidence = 'manual_visual_confirmation',
                               application_evidence = 'manually_marked_applied',
                               application_recorded_at = ?
                WHERE url = ?
            """, (now, now, canonical_url))
        else:
            cursor = conn.execute("""
                UPDATE jobs SET apply_status = 'failed', apply_error = ?,
                               apply_retry_blocked = 1, apply_retry_reason = ?,
                               agent_id = NULL
                WHERE url = ?
            """, (reason or "manual", reason or "manual", canonical_url))
        if cursor.rowcount != 1:
            raise LookupError(f"Job disappeared before status update: {canonical_url}")
        return canonical_url



def reset_failed(connection: sqlite3.Connection, url: str | None = None) -> int:
    """Reset failed jobs so they can be retried, optionally scoped to one URL.

    Returns:
        Number of jobs reset.
    """
    conn = connection
    with write_transaction(conn):
        parameters: tuple[object, ...] = ()
        exact_clause = ""
        if url:
            rows = conn.execute(
                "SELECT url FROM jobs WHERE url = ? OR application_url = ? ORDER BY url LIMIT 2",
                (url, url),
            ).fetchall()
            if not rows:
                raise LookupError(f"No job found for URL: {url}")
            if len(rows) > 1:
                raise ValueError(
                    "Application URL matches multiple jobs; use the canonical job URL instead."
                )
            exact_clause = " AND url = ?"
            parameters = (str(rows[0][0]),)
        cursor = conn.execute(f"""
            UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                           apply_attempts = 0, apply_retry_blocked = 0,
                           apply_retry_reason = NULL, agent_id = NULL
            WHERE (apply_status = 'failed'
              OR (apply_status IS NOT NULL AND apply_status NOT IN (
                  'applied', 'in_progress', 'submission_uncertain'
              ))){exact_clause}
        """, parameters)
        return cursor.rowcount
