"""Evaluate outside the writer lock, then atomically revalidate and claim.

This workflow owns its connection's transaction boundaries. It never launches
an agent, operates a browser, authorizes Submit, or admits a receipt.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime

from applypilot import config
from applypilot.apply.job_candidates import JobCandidate, select_candidates
from applypilot.storage.transactions import write_transaction

logger = logging.getLogger(__name__)


def _elapsed_ms(started_at: float) -> float:
    return round((time.perf_counter() - started_at) * 1000, 3)


def _manifest_current(manifest: dict | None, clock: Callable[[], datetime]) -> bool:
    if manifest is None:
        return True
    try:
        expires_at = datetime.fromisoformat(str(manifest.get("expires_at") or ""))
    except ValueError:
        return False
    return expires_at.tzinfo is not None and clock() < expires_at


@contextmanager
def _claim_transaction(connection: sqlite3.Connection, metrics: dict) -> Iterator[None]:
    started = time.perf_counter()
    acquired = None
    try:
        with write_transaction(connection):
            acquired = time.perf_counter()
            metrics["transaction_wait_ms"] += (acquired - started) * 1000
            metrics["claim_transactions"] += 1
            yield
    finally:
        if acquired is not None:
            metrics["transaction_hold_ms"] += _elapsed_ms(acquired)


def _record_candidate_block(
    connection: sqlite3.Connection,
    candidate: JobCandidate,
    *,
    stale_material: bool,
    reason: str,
    metrics: dict,
) -> bool:
    with _claim_transaction(connection, metrics):
        if not candidate.still_current(connection):
            metrics["stale_candidates_skipped"] += 1
            return False
        if stale_material:
            connection.execute(
                "UPDATE jobs SET tailored_resume_path=NULL, "
                "tailor_status='stale_profile_fact', tailor_error=? WHERE url=?",
                (reason, candidate.stored["url"]),
            )
            metrics["stale_materials_retired"] += 1
        else:
            connection.execute(
                "UPDATE jobs SET apply_status='manual', apply_error=? WHERE url=?",
                (reason, candidate.stored["url"]),
            )
    return True


def _automatic_target_issue(job: dict, min_score: int, environ: Mapping[str, str]) -> str | None:
    minimum = max(min_score, int(environ.get("APPLYPILOT_AUTO_SUBMIT_MIN_SCORE", "8")))
    if not str(job.get("company_name") or "").strip():
        return "missing verified company"
    if not str(job.get("full_description") or "").strip():
        return "missing enriched job description"
    if job.get("fit_score") is None or int(job["fit_score"]) < minimum:
        return f"fit score {job.get('fit_score')} is below automatic minimum {minimum}"
    return None


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
    profile_loader: Callable[[], dict],
    clock: Callable[[], datetime],
    environ: Mapping[str, str],
) -> dict | None:
    """Claim one current candidate, preserving selection and receipt boundaries.

    Ranking/admission are advisory snapshots. A changed row is skipped rather
    than claimed from stale evidence. File bytes are not made transactional by
    a SQLite lock: the existing prepare/audit/final-submit checks remain required.
    Runtime Cell callbacks must perform transaction-participating database work
    only and must not commit, start a browser, or dispatch an agent.
    """
    if connection.in_transaction:
        raise RuntimeError("job acquisition requires a connection without an active transaction")
    started = time.perf_counter()
    metrics = performance_sink if performance_sink is not None else {}
    metrics.clear()
    metrics.update({
        "version": 1, "outcome": "pending", "candidate_rows": 0,
        "admission_rows_scanned": 0, "admission_scan_ms": 0.0,
        "transaction_wait_ms": 0.0, "transaction_hold_ms": 0.0,
        "claim_transactions": 0, "stale_candidates_skipped": 0,
        "stale_materials_retired": 0,
    })
    if runtime_cell_claim is not None:
        metrics.update(runtime_claim_rows_scanned=0, runtime_claim_conflicts=0)
    manifest = deepcopy(authorization_manifest)
    excluded = {str(url) for url in (exclude_urls or set()) if str(url)}
    from applypilot.apply.authorization import authorize_job
    from applypilot.apply.batch_progress import consumed_batch_job_urls
    from applypilot.apply.submission_admission import evaluate_submission_admission, resolve_max_apply_attempts
    from applypilot.database import recover_stale_application_attempts, start_application_attempt
    from applypilot.eligibility import refresh_job_eligibility
    from applypilot.storage.runtime_cells import RuntimeCellConflictError

    batch_id = str((manifest or {}).get("batch_id") or "").strip()
    try:
        phase_started = time.perf_counter()
        recover_stale_application_attempts(connection)
        if batch_id and not preview_only:
            consumed = consumed_batch_job_urls(connection, batch_id)
            excluded.update(consumed)
            metrics["consumed_batch_jobs_excluded"] = len(consumed)
        metrics["stale_recovery_ms"] = _elapsed_ms(phase_started)
        phase_started = time.perf_counter()
        try:
            profile = profile_loader()
        except FileNotFoundError:
            profile = {}
        metrics["profile_load_ms"] = _elapsed_ms(phase_started)
        max_attempts = resolve_max_apply_attempts(profile)
        phase_started = time.perf_counter()
        refresh_job_eligibility(connection, profile=profile)
        metrics["eligibility_refresh_ms"] = _elapsed_ms(phase_started)
        policy = profile.get("submission_policy", {})
        allow_cover = bool(isinstance(policy, dict) and policy.get("allow_runtime_cover_letter_discovery", False))
        blocked_sites, blocked_patterns = ([], []) if target_url else load_blocked()
        phase_started = time.perf_counter()
        candidates = select_candidates(
            connection, target_url=target_url, min_score=min_score,
            max_apply_attempts=max_attempts, preview_only=preview_only,
            allow_runtime_cover=allow_cover, excluded=excluded,
            blocked_sites=blocked_sites, blocked_patterns=blocked_patterns,
        )
        metrics["candidate_fetch_ms"] = _elapsed_ms(phase_started)
        metrics["candidate_rows"] = len(candidates)
        minimum = max(1, min(int(min_score), 10))
        if not _manifest_current(manifest, clock):
            metrics["outcome"] = "empty"
            return None
        for candidate in candidates:
            job = candidate.job
            phase_started = time.perf_counter()
            metrics["admission_rows_scanned"] += 1
            try:
                admission = evaluate_submission_admission(
                    job, profile, minimum_fit_score=minimum, preview_only=preview_only,
                )
                metadata = admission.get("metadata")
                freshness = metadata.get("profile_resume_fact_freshness") if isinstance(metadata, dict) else None
                stale_material = isinstance(freshness, dict) and freshness.get("state") == "stale_profile_fact"
                portal_reason = config.portal_application_gate(
                    job["application_url"], source_site=job.get("source_site"),
                    site=job.get("site"), preview_only=preview_only,
                )
                authorized = None
                if admission.get("admitted") and manifest is not None and not stale_material:
                    try:
                        authorized = authorize_job(manifest, job)
                    except (KeyError, PermissionError, RuntimeError, ValueError):
                        continue
                    if authorized is None:
                        continue
            finally:
                metrics["admission_scan_ms"] += _elapsed_ms(phase_started)
            if stale_material:
                _record_candidate_block(
                    connection, candidate, stale_material=True,
                    reason=str(admission.get("reason") or "stale_profile_fact"), metrics=metrics,
                )
                continue
            if portal_reason and (admission.get("admitted") or admission.get("reason") == portal_reason):
                recorded = _record_candidate_block(
                    connection, candidate, stale_material=False, reason=portal_reason, metrics=metrics,
                )
                if recorded and target_url:
                    metrics["outcome"] = "blocked"
                    return None
                continue
            if not admission.get("admitted"):
                continue
            if target_url and not preview_only and environ.get("APPLYPILOT_AUTO_SUBMIT") == "1":
                issue = _automatic_target_issue(job, minimum, environ)
                if issue:
                    metrics["outcome"] = "blocked"
                    logger.warning("Automatic submission paused for %s: %s", job["url"], issue)
                    return None
            hint = "manual_boundary_likely" if config.is_manual_ats(job["application_url"]) else None
            try:
                with _claim_transaction(connection, metrics):
                    if not candidate.still_current(connection):
                        metrics["stale_candidates_skipped"] += 1
                        continue
                    # A queue snapshot and earlier authorization check are not
                    # sufficient after waiting for the writer lock.
                    if not _manifest_current(manifest, clock):
                        metrics["outcome"] = "blocked"
                        return None
                    if batch_id and not preview_only and job["url"] in consumed_batch_job_urls(connection, batch_id):
                        continue
                    with write_transaction(connection):
                        attempt_id = start_application_attempt(
                            job["url"], f"worker-{worker_id}", batch_id=(manifest or {}).get("batch_id"),
                            lease_minutes=application_lease_minutes, conn=connection,
                        )
                        lease = None
                        if runtime_cell_claim is not None:
                            metrics["runtime_claim_rows_scanned"] += 1
                            lease = runtime_cell_claim(connection, job, attempt_id)
                        connection.execute(
                            "UPDATE jobs SET apply_status='in_progress', agent_id=?, "
                            "last_attempted_at=?, apply_task_id=? WHERE url=?",
                            (f"worker-{worker_id}", clock().isoformat(), attempt_id, job["url"]),
                        )
            except RuntimeCellConflictError:
                if runtime_cell_claim is None:
                    raise
                metrics["runtime_claim_conflicts"] += 1
                continue
            acquired = dict(job)
            acquired["_attempt_id"] = attempt_id
            if runtime_cell_claim is not None:
                acquired["_runtime_cell_lease"] = lease
            if authorized is not None:
                acquired["_authorization_entry"] = dict(authorized)
            if hint and runtime_cell_claim is None:
                acquired["_ats_capability_hint"] = hint
            metrics["outcome"] = "acquired"
            metrics["total_ms"] = _elapsed_ms(started)
            acquired["_acquisition_performance"] = dict(metrics)
            return acquired
        metrics["outcome"] = "empty"
        return None
    except BaseException:
        metrics["outcome"] = "error"
        raise
    finally:
        metrics["total_ms"] = _elapsed_ms(started)
