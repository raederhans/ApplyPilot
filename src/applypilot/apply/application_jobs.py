"""Application-job acquisition and status persistence.

The orchestration layer supplies the connection and runtime policy inputs.
This module owns atomic job selection and durable job-status mutations.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime

from applypilot import config

logger = logging.getLogger(__name__)


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 3)


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
    """Atomically acquire the next job to apply to.

    Args:
        target_url: Apply to a specific URL instead of picking from queue.
        min_score: Minimum fit_score threshold.
        worker_id: Worker claiming this job (for tracking).
        exclude_urls: Exact job URLs already attempted in this command.

    Returns:
        Job dict or None if the queue is empty.
    """
    acquisition_started = time.perf_counter()
    acquisition_performance = (
        performance_sink if performance_sink is not None else {}
    )
    acquisition_performance.clear()
    acquisition_performance.update({"version": 1, "outcome": "pending"})
    conn = connection
    excluded = {str(url) for url in (exclude_urls or set()) if str(url)}
    from applypilot.apply.submission_admission import resolve_max_apply_attempts
    from applypilot.database import (
        recover_stale_application_attempts,
        start_application_attempt,
    )
    from applypilot.eligibility import ELIGIBLE_SQL, refresh_job_eligibility
    phase_started = time.perf_counter()
    recover_stale_application_attempts(conn)
    if authorization_manifest is not None and not preview_only:
        from applypilot.apply.batch_progress import consumed_batch_job_urls

        batch_id = str(authorization_manifest.get("batch_id") or "").strip()
        if batch_id:
            # Reservations are permanent, including failed and uncertain
            # attempts. Avoid spending another prepare turn on an occupied slot.
            consumed = consumed_batch_job_urls(conn, batch_id)
            excluded.update(consumed)
            acquisition_performance["consumed_batch_jobs_excluded"] = len(consumed)
    acquisition_performance["stale_recovery_ms"] = _elapsed_ms(phase_started)
    phase_started = time.perf_counter()
    try:
        profile = config.load_profile()
    except FileNotFoundError:
        profile = {}
    acquisition_performance["profile_load_ms"] = _elapsed_ms(phase_started)
    max_apply_attempts = resolve_max_apply_attempts(profile)
    phase_started = time.perf_counter()
    refresh_job_eligibility(conn, profile=profile)
    acquisition_performance["eligibility_refresh_ms"] = _elapsed_ms(phase_started)
    try:
        phase_started = time.perf_counter()
        conn.execute("BEGIN IMMEDIATE")
        acquisition_performance["transaction_wait_ms"] = _elapsed_ms(phase_started)

        submission_policy = profile.get("submission_policy", {})
        if not isinstance(submission_policy, dict):
            submission_policy = {}
        allow_runtime_cover = bool(
            submission_policy.get("allow_runtime_cover_letter_discovery", False)
        )
        phase_started = time.perf_counter()
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
            minimum_fit_score = max(1, min(int(min_score), 10))
            target_params = (
                target_url,
                target_url,
                max_apply_attempts,
                minimum_fit_score,
            )
            rows = conn.execute(f"""
                SELECT *
                FROM jobs
                WHERE {target_match}
                  {material_clause}
                  AND (apply_status IS NULL OR apply_status IN ('failed', 'previewed'))
                  AND (apply_attempts IS NULL OR apply_attempts < ?)
                  AND fit_score >= ?
                  AND {ELIGIBLE_SQL}
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
                  AND fit_score >= ?
                  AND {ELIGIBLE_SQL}
                  {excluded_clause}
                  {site_clause}
                  {url_clauses}
                ORDER BY fit_score DESC, url
            """, [max_apply_attempts] + params).fetchall()
        acquisition_performance["candidate_fetch_ms"] = _elapsed_ms(phase_started)
        acquisition_performance["candidate_rows"] = len(rows)

        row = None
        authorized_entry = None
        runtime_candidates: list[tuple[dict, dict | None]] = []
        from applypilot.apply.submission_admission import evaluate_submission_admission

        minimum_fit_score = max(1, min(int(min_score), 10))
        if authorization_manifest is not None:
            from applypilot.apply.authorization import authorize_job

            try:
                expires_at = datetime.fromisoformat(
                    str(authorization_manifest.get("expires_at") or "")
                )
            except ValueError:
                expires_at = datetime.min.replace(tzinfo=UTC)
            if expires_at.tzinfo is None or datetime.now(UTC) >= expires_at:
                rows = []

        admission_rows_scanned = 0
        retired_stale_materials = 0
        phase_started = time.perf_counter()
        for candidate in rows:
            admission_rows_scanned += 1
            candidate_job = dict(candidate)
            candidate_job["application_url"] = (
                candidate_job.get("application_url") or candidate_job.get("url")
            )
            candidate_admission = evaluate_submission_admission(
                candidate_job,
                profile,
                minimum_fit_score=minimum_fit_score,
                preview_only=preview_only,
            )
            admission_metadata = candidate_admission.get("metadata")
            profile_resume_freshness = (
                admission_metadata.get("profile_resume_fact_freshness")
                if isinstance(admission_metadata, dict)
                else None
            )
            if isinstance(profile_resume_freshness, dict) and (
                profile_resume_freshness.get("state") == "stale_profile_fact"
            ):
                material_error = str(candidate_admission.get("reason") or "stale_profile_fact")
                conn.execute(
                    "UPDATE jobs SET tailored_resume_path=NULL, "
                    "tailor_status='stale_profile_fact', tailor_error=? WHERE url=?",
                    (material_error, candidate_job["url"]),
                )
                retired_stale_materials += 1
                logger.warning(
                    "Retiring stale application resume for %s: %s",
                    candidate_job["url"][:80],
                    material_error,
                )
                continue
            if not candidate_admission.get("admitted"):
                portal_reason = config.portal_application_gate(
                    candidate_job["application_url"],
                    source_site=candidate_job.get("source_site"),
                    site=candidate_job.get("site"),
                    preview_only=preview_only,
                )
                if portal_reason and candidate_admission.get("reason") == portal_reason:
                    conn.execute(
                        "UPDATE jobs SET apply_status = 'manual', apply_error = ? WHERE url = ?",
                        (portal_reason, candidate_job["url"]),
                    )
                    conn.commit()
                    logger.info("Portal policy paused browser application: %s", candidate_job["url"][:80])
                    if target_url:
                        acquisition_performance["outcome"] = "blocked"
                        return None
                continue
            if authorization_manifest is not None:
                try:
                    authorized = authorize_job(authorization_manifest, candidate_job)
                except (KeyError, PermissionError, RuntimeError, ValueError):
                    continue
                if authorized is None:
                    continue
                authorized_entry = authorized
            if runtime_cell_claim is not None:
                # Preserve the normalized, admitted first-pass result. The
                # second pass performs only atomic attempt+Cell claims.
                runtime_candidates.append((candidate_job, authorized_entry))
                authorized_entry = None
                continue
            row = candidate
            break
        acquisition_performance["admission_scan_ms"] = _elapsed_ms(phase_started)
        acquisition_performance["admission_rows_scanned"] = admission_rows_scanned
        acquisition_performance["stale_materials_retired"] = retired_stale_materials

        if runtime_cell_claim is not None and runtime_candidates:
            # The attempt and Runtime Cell/domain lease share one savepoint.
            # A same-domain conflict rolls back the provisional attempt and
            # continues scanning without leaving an orphan attempt.
            from applypilot.storage.runtime_cells import RuntimeCellConflictError

            runtime_claim_rows_scanned = 0
            runtime_claim_conflicts = 0
            for candidate, candidate_authorization in runtime_candidates:
                runtime_claim_rows_scanned += 1
                candidate_job = dict(candidate)
                apply_url = candidate_job["application_url"]
                portal_gate = config.portal_application_gate(
                    apply_url,
                    source_site=candidate_job.get("source_site"),
                    site=candidate_job.get("site"),
                    preview_only=preview_only,
                )
                if portal_gate:
                    conn.execute(
                        "UPDATE jobs SET apply_status='manual',apply_error=? WHERE url=?",
                        (portal_gate, candidate_job["url"]),
                    )
                    if target_url:
                        conn.commit()
                        acquisition_performance["outcome"] = "blocked"
                        return None
                    continue
                if target_url and not preview_only and os.environ.get("APPLYPILOT_AUTO_SUBMIT") == "1":
                    policy_min_score = max(
                        minimum_fit_score,
                        int(os.environ.get("APPLYPILOT_AUTO_SUBMIT_MIN_SCORE", "8")),
                    )
                    if (
                        not str(candidate_job.get("company_name") or "").strip()
                        or not str(candidate_job.get("full_description") or "").strip()
                        or candidate_job.get("fit_score") is None
                        or int(candidate_job["fit_score"]) < policy_min_score
                    ):
                        if target_url:
                            conn.rollback()
                            acquisition_performance["outcome"] = "blocked"
                            return None
                        continue
                conn.execute("SAVEPOINT runtime_cell_job_acquisition")
                try:
                    attempt_id = start_application_attempt(
                        candidate_job["url"],
                        f"worker-{worker_id}",
                        batch_id=(authorization_manifest or {}).get("batch_id"),
                        lease_minutes=application_lease_minutes,
                        conn=conn,
                    )
                    runtime_lease = runtime_cell_claim(conn, candidate_job, attempt_id)
                    now = datetime.now(UTC).isoformat()
                    conn.execute(
                        "UPDATE jobs SET apply_status='in_progress',agent_id=?,"
                        "last_attempted_at=?,apply_task_id=? WHERE url=?",
                        (f"worker-{worker_id}", now, attempt_id, candidate_job["url"]),
                    )
                    conn.execute("RELEASE SAVEPOINT runtime_cell_job_acquisition")
                except RuntimeCellConflictError:
                    conn.execute("ROLLBACK TO SAVEPOINT runtime_cell_job_acquisition")
                    conn.execute("RELEASE SAVEPOINT runtime_cell_job_acquisition")
                    runtime_claim_conflicts += 1
                    continue
                except Exception:
                    conn.execute("ROLLBACK TO SAVEPOINT runtime_cell_job_acquisition")
                    conn.execute("RELEASE SAVEPOINT runtime_cell_job_acquisition")
                    raise
                conn.commit()
                acquired = candidate_job
                acquired["_attempt_id"] = attempt_id
                acquired["_runtime_cell_lease"] = runtime_lease
                if candidate_authorization is not None:
                    acquired["_authorization_entry"] = dict(candidate_authorization)
                acquisition_performance["outcome"] = "acquired"
                acquisition_performance["runtime_claim_rows_scanned"] = (
                    runtime_claim_rows_scanned
                )
                acquisition_performance["runtime_claim_conflicts"] = (
                    runtime_claim_conflicts
                )
                acquisition_performance["total_ms"] = _elapsed_ms(acquisition_started)
                acquired["_acquisition_performance"] = dict(acquisition_performance)
                return acquired
            acquisition_performance["runtime_claim_rows_scanned"] = (
                runtime_claim_rows_scanned
            )
            acquisition_performance["runtime_claim_conflicts"] = (
                runtime_claim_conflicts
            )

        if not row:
            acquisition_performance["outcome"] = "empty"
            if retired_stale_materials:
                conn.commit()
            else:
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
            acquisition_performance["outcome"] = "blocked"
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
            policy_min_score = max(
                minimum_fit_score,
                int(os.environ.get("APPLYPILOT_AUTO_SUBMIT_MIN_SCORE", "8")),
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
                acquisition_performance["outcome"] = "blocked"
                conn.rollback()
                logger.warning("Automatic submission paused for %s: %s", row["url"], auto_issue)
                return None

        # A historically difficult ATS is a runtime hint. The visible agent may
        # still complete ordinary preparation before the actual human boundary.
        from applypilot.config import is_manual_ats
        ats_capability_hint = "manual_boundary_likely" if is_manual_ats(apply_url) else None

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
        acquired["application_url"] = (
            acquired.get("application_url") or acquired.get("url")
        )
        acquired["_attempt_id"] = attempt_id
        acquisition_performance["outcome"] = "acquired"
        acquisition_performance["total_ms"] = _elapsed_ms(acquisition_started)
        acquired["_acquisition_performance"] = dict(acquisition_performance)
        if authorized_entry is not None:
            acquired["_authorization_entry"] = dict(authorized_entry)
        if ats_capability_hint:
            acquired["_ats_capability_hint"] = ats_capability_hint
        return acquired
    except Exception:
        acquisition_performance["outcome"] = "error"
        conn.rollback()
        raise
    finally:
        acquisition_performance["total_ms"] = _elapsed_ms(acquisition_started)


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
        evidence = job.get("_preview_attempt_evidence")
        finalize_application_attempt(
            job.get("_attempt_id"),
            "previewed",
            evidence=evidence if isinstance(evidence, dict) else None,
            conn=conn,
        )
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
        conn.rollback()
        raise LookupError(f"Job disappeared before status update: {canonical_url}")
    conn.commit()
    return canonical_url


def reset_failed(connection: sqlite3.Connection, url: str | None = None) -> int:
    """Reset failed jobs so they can be retried, optionally scoped to one URL.

    Returns:
        Number of jobs reset.
    """
    conn = connection
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
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------
