"""Single authority boundary for final application effects.

The launcher remains the composition root and passes live dependencies at call
time. Browser and direct-email submit paths consume these same functions
through WorkerSubmissionPorts.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any


def acquire_submit_writer_lane(host: Any, worker_id: int) -> bool:
    """Wait interruptibly for the sole final-submit/receipt ownership lane."""
    update_state = host.update_state
    _stop_event = host.stop_event
    _submit_writer_lane = host.submit_writer_lane
    update_state(
        worker_id,
        status="waiting",
        last_action="waiting for final submit lane",
    )
    while not _stop_event.is_set():
        if _submit_writer_lane.acquire(timeout=0.5):
            return True
    return False


def reserve_manifest_submission(
    host: Any,
    manifest: dict | None,
    job: dict,
    audit_report: Mapping[str, object] | None = None,
    *,
    success_target: int | None = None,
) -> tuple[bool, str]:
    """Re-authorize bytes and atomically claim final submission authority."""
    application_jobs_mod = host.application_jobs_mod
    application_plan_mod = host.application_plan_mod
    application_plan_runtime_mod = host.application_plan_runtime_mod
    _application_plan_audit_issuer = host.application_plan_audit_issuer
    _authorize_linkedin_runtime_route = host.authorize_linkedin_runtime_route
    _runtime_linkedin_route_gate = host.runtime_linkedin_route_gate
    _submission_audit_fingerprint = host.submission_audit_fingerprint
    config = host.config
    get_connection = host.get_connection
    logger = host.logger
    if manifest is None:
        return False, "authorization_manifest_required"
    try:
        expires_at = datetime.fromisoformat(str(manifest.get("expires_at") or ""))
        if expires_at.tzinfo is None or datetime.now(UTC) >= expires_at:
            return False, "authorization_manifest_expired"
        from applypilot.apply.authorization import authorize_job, freeze_submission_materials
        from applypilot.database import claim_submission_gate, reserve_batch_submission

        profile = config.load_profile()
        if authorize_job(manifest, job) is None:
            if not isinstance(job.get("_linkedin_runtime_route_binding"), Mapping):
                return False, "authorization_manifest_job_mismatch"
            route_authorized, route_authorization_reason = _authorize_linkedin_runtime_route(manifest, job, profile)
            if not route_authorized:
                return False, route_authorization_reason
        runtime_route_allowed, runtime_route_reason = _runtime_linkedin_route_gate(
            job,
            audit_report,
            profile,
        )
        if not runtime_route_allowed:
            return False, runtime_route_reason
        material_binding = freeze_submission_materials(job, profile)
        job["_bound_submission_materials"] = material_binding
        plan = job.get("_application_plan")
        if isinstance(plan, application_plan_mod.ApplicationPlan):
            job["_application_plan_shadow"] = application_plan_runtime_mod.application_plan_shadow_result(
                plan,
                job,
                profile,
                audit_report if isinstance(audit_report, Mapping) else {},
                issuer=_application_plan_audit_issuer,
            )
        attempt_id = str(job.get("_attempt_id") or "").strip()
        if attempt_id:
            policy = profile.get("submission_policy", {})
            if not isinstance(policy, Mapping):
                policy = {}
            fingerprint = _submission_audit_fingerprint(job, audit_report)
            connection = get_connection()
            if connection.in_transaction:
                return False, "submission_gate_transaction_busy"
            connection.execute("BEGIN IMMEDIATE")
            try:
                duplicate_check = application_jobs_mod.revalidate_duplicate_before_submit(
                    connection,
                    str(job.get("url") or ""),
                )
                job["_duplicate_revalidation"] = dict(duplicate_check)
                if duplicate_check.get("clear") is not True:
                    connection.rollback()
                    return False, str(duplicate_check.get("reason") or "duplicate_revalidation_failed")
                claim = claim_submission_gate(
                    str(manifest.get("batch_id") or ""),
                    str(job.get("url") or ""),
                    int(manifest.get("max_submissions") or 0),
                    attempt_id,
                    success_target=success_target,
                    hourly_maximum=int(policy.get("maximum_verified_submissions_per_rolling_hour", 15)),
                    minimum_gap_seconds=float(policy.get("minimum_seconds_between_verified_submissions", 20)),
                    audit_fingerprint=fingerprint,
                    conn=connection,
                )
                job["_submission_gate"] = dict(claim)
                if claim.get("claimed") is not True:
                    connection.rollback()
                    return False, str(claim.get("reason") or "submission_gate_denied")
                job["_submission_gate_binding"] = {
                    "gate_id": str(claim.get("gate_id") or ""),
                    "batch_id": str(manifest.get("batch_id") or ""),
                    "job_url": str(job.get("url") or ""),
                    "attempt_id": attempt_id,
                }
                connection.commit()
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise
            return True, str(claim.get("reason") or "submission_gate_claimed")
        reserved = reserve_batch_submission(
            str(manifest.get("batch_id") or ""),
            str(job.get("url") or ""),
            int(manifest.get("max_submissions") or 0),
        )
        if reserved is not True:
            return False, "authorization_batch_reservation_denied"
        return True, "reserved"
    except Exception as exc:
        logger.exception("Batch submission reservation failed")
        return False, f"authorization_batch_reservation_error:{type(exc).__name__}"


def update_submission_ledger(
    host: Any,
    manifest: dict | None,
    job: dict,
    status: str,
    evidence: dict | None = None,
) -> bool:
    logger = host.logger
    if manifest is None:
        return True
    try:
        from applypilot.database import (
            update_batch_submission_status,
            update_submission_gate_state,
        )

        ledger_evidence = dict(evidence or {})
        if job.get("_bound_submission_materials"):
            ledger_evidence["material_binding"] = job["_bound_submission_materials"]
        update_batch_submission_status(
            str(manifest.get("batch_id") or ""),
            str(job.get("url") or ""),
            status,
            evidence=ledger_evidence,
        )
        attempt_id = str(job.get("_attempt_id") or "").strip()
        if attempt_id:
            update_submission_gate_state(
                attempt_id,
                status,
                {
                    "receipt_confirmed": status == "applied",
                    "submit_started": bool(isinstance(evidence, Mapping) and evidence.get("submit_started", True)),
                },
            )
        return True
    except Exception:
        logger.exception("Batch submission ledger update failed")
        return False


def has_admitted_submission_receipt(
    host: Any,
    manifest: Mapping[str, object] | None,
    job: Mapping[str, object],
) -> bool:
    """Check durable receipt admission for this exact authorized attempt."""
    get_connection = host.get_connection
    if not isinstance(manifest, Mapping):
        return False
    from applypilot.database import has_admitted_submission_receipt

    return has_admitted_submission_receipt(
        str(manifest.get("batch_id") or ""),
        str(job.get("url") or ""),
        str(job.get("_attempt_id") or ""),
        conn=get_connection(),
    )


def admit_direct_email_receipt(job: dict, receipt: object) -> dict[str, object]:
    """Admit one provider message id before a direct-email success is recorded."""
    if not isinstance(receipt, dict):
        return {"status": "rejected", "reason": "sent_receipt_required"}
    from applypilot.database import admit_direct_email_sent_receipt

    return admit_direct_email_sent_receipt(
        str(job.get("url") or ""),
        receipt,
        gate_binding=(
            job.get("_submission_gate_binding") if isinstance(job.get("_submission_gate_binding"), Mapping) else None
        ),
    )
