"""Pure, privacy-bounded view models for the packaged frontend."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from applypilot.apply.authorization import compute_job_fingerprint
from applypilot.apply.decision import evaluate

_READY_COVER_STATUSES = {"agent_validated", "human_approved", "not_required"}
_FAILED_TOKENS = ("failed", "error", "invalid", "rejected")
_BROWSER_CONFIRMATIONS = {
    "browser_observation",
    "manual_visual_confirmation",
    "visible_confirmation",
}


def build_discover_item(record: Mapping[str, object]) -> dict[str, Any]:
    """Adapt one discovered listing or unresolved lead without raw lineage payloads."""
    kind = _text(record.get("kind"), limit=24).casefold()
    is_lead = kind == "lead"
    verified = bool(record.get("verified_official")) and not is_lead
    if verified:
        state = "verified"
        label = "Official listing verified"
    elif is_lead:
        state = "lead"
        label = "Lead awaiting an official listing"
    else:
        state = "observed"
        label = "Listing observed; official lineage not recorded"

    fit_score = record.get("fit_score")
    eligibility = _text(record.get("eligibility_status"), limit=40).casefold()
    if eligibility == "ineligible":
        pipeline_state = "excluded"
        pipeline_label = "Excluded by recorded eligibility"
    elif isinstance(fit_score, (int, float)) and fit_score >= 5:
        pipeline_state = "shortlisted"
        pipeline_label = "Available in the decision queue"
    elif isinstance(fit_score, (int, float)):
        pipeline_state = "scored"
        pipeline_label = "Scored below the current shortlist"
    else:
        pipeline_state = "new"
        pipeline_label = "Needs enrichment and fit evidence"

    return {
        "state": state,
        "label": label,
        "kind": "lead" if is_lead else "listing",
        "title": _text(record.get("title"), limit=180) or "Untitled opportunity",
        "company": _text(record.get("company_name"), limit=140) or "Unknown employer",
        "location": _text(record.get("location"), limit=120),
        "url": _text(record.get("url"), limit=2_000),
        "source": _text(record.get("source"), limit=120) or "Unknown source",
        "provider": _text(record.get("provider"), limit=80),
        "sourceCount": max(0, int(record.get("source_count") or 0)),
        "publishedAt": _text(record.get("published_at"), limit=80),
        "firstSeenAt": _text(record.get("first_seen_at"), limit=80),
        "lastSeenAt": _text(record.get("last_seen_at"), limit=80),
        "pipeline": {"state": pipeline_state, "label": pipeline_label},
    }


def build_discover_summary(
    items: list[Mapping[str, object]],
    sources: list[Mapping[str, object]],
    *,
    lineage_available: bool,
) -> dict[str, Any]:
    """Summarize read-only discovery lineage and source-run health."""
    states = [str(item.get("state") or "observed") for item in items]
    source_issues = sum(
        1
        for source in sources
        if source.get("status") != "complete"
        or source.get("paginationComplete") is False
        or source.get("hasError") is True
    )
    stats = {
        "candidates": len(items),
        "verified": states.count("verified"),
        "leads": states.count("lead"),
        "sourceIssues": source_issues,
    }
    if not items and not sources:
        system = {
            "state": "empty",
            "title": "No discovery snapshot yet",
            "message": "Collect current openings to create a local source snapshot. This page does not fetch or promote listings by itself.",
            "actions": [{"label": "Collect openings", "command": "applypilot radar collect"}],
            "detail": "",
        }
    elif not lineage_available:
        system = {
            "state": "needs_evidence",
            "title": "Source lineage is not available",
            "message": "Existing job records remain visible, but provider runs and unresolved leads cannot be verified from this workspace snapshot.",
            "actions": [{"label": "Collect source evidence", "command": "applypilot radar collect"}],
            "detail": "",
        }
    elif source_issues:
        system = {
            "state": "needs_evidence",
            "title": f"{source_issues} source runs need attention",
            "message": "Partial or incomplete source runs are kept visible and are not treated as complete coverage.",
            "actions": [{"label": "Refresh sources", "command": "applypilot radar collect"}],
            "detail": "",
        }
    else:
        system = {
            "state": "ready",
            "title": "Discovery snapshot ready",
            "message": "Listings, unresolved leads, and provider-run state reflect persisted local source evidence.",
            "actions": [],
            "detail": "",
        }
    return {"system": system, "stats": stats}


def _text(value: object, *, limit: int = 240) -> str:
    return str(value or "").strip()[:limit]


def _file_label(value: object) -> str:
    """Return only a filename, even when a Windows path is rendered on Linux."""
    return _text(value, limit=2_000).replace("\\", "/").rsplit("/", 1)[-1]


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return [_text(item, limit=120) for item in value[:8] if _text(item, limit=120)]


def _resume_contract(job: Mapping[str, object]) -> dict[str, Any]:
    path = _text(job.get("tailored_resume_path"), limit=2_000)
    status = _text(job.get("tailor_status"), limit=80).casefold()
    if path and status == "machine_validated":
        state = "ready"
        label = "Validated resume recorded"
    elif status and any(token in status for token in _FAILED_TOKENS):
        state = "blocked"
        label = "Resume validation did not pass"
    elif path or status:
        state = "review"
        label = "Resume evidence is incomplete"
    else:
        state = "missing"
        label = "No prepared resume recorded"
    return {
        "state": state,
        "label": label,
        "status": status or "not_recorded",
        "artifactName": _file_label(path),
        "recordedAt": _text(job.get("tailored_at"), limit=80),
    }


def _cover_contract(job: Mapping[str, object]) -> dict[str, Any]:
    path = _text(job.get("cover_letter_path"), limit=2_000)
    status = _text(job.get("cover_letter_status"), limit=80).casefold()
    if status == "not_required":
        state = "ready"
        label = "Cover letter recorded as not required"
    elif status in _READY_COVER_STATUSES and path:
        state = "ready"
        label = "Validated cover letter recorded"
    elif status and any(token in status for token in _FAILED_TOKENS):
        state = "blocked"
        label = "Cover-letter validation did not pass"
    elif path or status:
        state = "review"
        label = "Cover-letter evidence is unresolved"
    else:
        state = "missing"
        label = "Cover-letter requirement is not recorded"
    return {
        "state": state,
        "label": label,
        "status": status or "not_recorded",
        "artifactName": _file_label(path),
        "recordedAt": _text(job.get("cover_letter_approved_at") or job.get("cover_letter_at"), limit=80),
    }


def _route_contract(job: Mapping[str, object], assignment: Mapping[str, object] | None) -> dict[str, Any]:
    if assignment is None:
        return {
            "state": "unrecorded",
            "decision": "not_recorded",
            "reason": "No persisted resume route is available for this job version.",
            "gaps": [],
        }

    current = _text(assignment.get("job_fingerprint"), limit=80) == compute_job_fingerprint(dict(job))
    if not current:
        return {
            "state": "stale",
            "decision": _text(assignment.get("decision"), limit=80) or "not_recorded",
            "reason": "The recorded resume route belongs to an older version of this job.",
            "gaps": [],
            "recordedAt": _text(assignment.get("recorded_at"), limit=80),
        }

    artifact = {
        "kind": _text(assignment.get("artifact_kind"), limit=80),
        "track": _text(assignment.get("artifact_track"), limit=120),
        "validationStatus": _text(assignment.get("artifact_validation_status"), limit=80),
        "validatedAt": _text(assignment.get("artifact_validated_at"), limit=80),
        "hasPdfBinding": bool(
            assignment.get("artifact_pdf_path")
            and assignment.get("artifact_pdf_sha256")
            and assignment.get("artifact_pdf_size")
        ),
        "hasValidationReport": bool(assignment.get("artifact_validation_report_path")),
    }
    return {
        "state": "current",
        "decision": _text(assignment.get("decision"), limit=80) or "not_recorded",
        "reason": _text(assignment.get("reason")),
        "gaps": _string_list(assignment.get("hard_gaps_json")),
        "requiredCoverage": assignment.get("required_coverage"),
        "overallScore": assignment.get("overall_score"),
        "recordedAt": _text(assignment.get("recorded_at"), limit=80),
        "artifact": artifact,
    }


def build_prepare_job(job: Mapping[str, object], assignment: Mapping[str, object] | None = None) -> dict[str, Any]:
    """Adapt persisted job/material evidence without reading files or writing state."""
    resume = _resume_contract(job)
    cover = _cover_contract(job)
    route = _route_contract(job, assignment)
    material_states = {resume["state"], cover["state"]}
    route_artifact = route.get("artifact", {})
    route_ready = (
        route.get("state") == "current"
        and route.get("decision") == "reuse_exact"
        and route_artifact.get("validationStatus") == "machine_validated"
        and route_artifact.get("hasPdfBinding") is True
        and not route.get("gaps")
    )
    if material_states == {"ready"} and route_ready:
        material_state = "ready"
        material_label = "Current bound material evidence recorded"
    elif "blocked" in material_states:
        material_state = "blocked"
        material_label = "Material evidence needs review"
    elif "review" in material_states or material_states == {"ready"}:
        material_state = "review"
        material_label = "Current bound material evidence is incomplete"
    else:
        material_state = "missing"
        material_label = "No complete material set is recorded"

    application_decision = evaluate(dict(job))
    return {
        "state": material_state,
        "label": material_label,
        "resume": resume,
        "coverLetter": cover,
        "route": route,
        "applicationDecision": {
            "state": application_decision["decision"],
            "reason": _text(application_decision["reason"]),
        },
    }


def build_prepare_summary(jobs: list[Mapping[str, object]], *, library_available: bool) -> dict[str, Any]:
    states = [str(job.get("prepare", {}).get("state") or "missing") for job in jobs]
    stats = {
        "shortlisted": len(jobs),
        "ready": states.count("ready"),
        "attention": states.count("review") + states.count("blocked"),
        "unrecorded": states.count("missing"),
    }
    if not jobs:
        system = {
            "state": "empty",
            "title": "Nothing to prepare yet",
            "message": "Build and review a shortlist first; preparation evidence appears only after it is persisted locally.",
            "actions": [],
            "detail": "",
        }
    elif not library_available:
        system = {
            "state": "needs_evidence",
            "title": "Resume routing evidence is not available",
            "message": "Legacy material status is still shown, but no current resume-library records were found. This page did not create or migrate them.",
            "actions": [{"label": "Inspect library", "command": "applypilot resume-library-status"}],
            "detail": "",
        }
    elif stats["ready"] == len(jobs):
        system = {
            "state": "ready",
            "title": "Preparation evidence ready",
            "message": "Every shortlisted role has a current resume route, a validated PDF binding, no recorded hard gap, and a resolved cover-letter requirement.",
            "actions": [],
            "detail": "",
        }
    else:
        system = {
            "state": "needs_evidence",
            "title": f"{len(jobs) - stats['ready']} shortlisted roles need material evidence",
            "message": "Review the recorded gaps before running any preparation command. Nothing is generated or changed from this page.",
            "actions": [{"label": "Inspect library", "command": "applypilot resume-library-status"}],
            "detail": "",
        }
    return {"system": system, "stats": stats}


def _json_mapping(value: object) -> tuple[dict[str, object], bool]:
    if not value:
        return {}, True
    if isinstance(value, Mapping):
        return dict(value), True
    if not isinstance(value, str):
        return {}, False
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return {}, False
    return (dict(loaded), True) if isinstance(loaded, Mapping) else ({}, False)


def _batch_contract(ledgers: list[Mapping[str, object]]) -> dict[str, Any]:
    if not ledgers:
        return {
            "state": "unrecorded",
            "label": "No authorization reservation ledger recorded",
            "count": 0,
        }
    latest = ledgers[0]
    return {
        "state": "recorded",
        "label": "One-shot authorization reservation recorded",
        "count": len(ledgers),
        "latestStatus": _text(latest.get("status"), limit=80) or "unknown",
        "reservedAt": _text(latest.get("reserved_at"), limit=80),
        "updatedAt": _text(latest.get("updated_at"), limit=80),
    }


def _observation_contract(job: Mapping[str, object]) -> dict[str, Any]:
    raw = job.get("submission_observation_json")
    observation, readable = _json_mapping(raw)
    recorded_at = _text(job.get("submission_observed_at"), limit=80)
    if not raw and not recorded_at:
        return {"state": "unrecorded", "label": "No browser observation recorded"}
    if not readable:
        return {
            "state": "unreadable",
            "label": "Recorded observation cannot be safely summarized",
            "recordedAt": recorded_at,
        }
    source = _text(observation.get("source"), limit=80).casefold()
    receipt_reference_recorded = bool(observation.get("receipt_id"))
    return {
        "state": "recorded",
        "label": "Receipt reconciliation record stored"
        if receipt_reference_recorded
        else "Execution observation recorded — not a receipt by itself",
        "submitClicked": observation.get("submit_clicked") is True,
        "receiptVisible": observation.get("receipt_visible") is True,
        "appliedBadgeVisible": observation.get("applied_badge_visible") is True,
        "captchaVisible": observation.get("captcha_visible") is True,
        "receiptReferenceRecorded": receipt_reference_recorded,
        "source": source
        if source in {"browser_receipt", "candidate_portal", "confirmation_email"}
        else "browser_observation",
        "recordedAt": recorded_at or _text(observation.get("observed_at"), limit=80),
    }


def _receipt_contract(job: Mapping[str, object]) -> dict[str, Any]:
    apply_status = _text(job.get("apply_status"), limit=80).casefold()
    confidence = _text(job.get("verification_confidence"), limit=80).casefold()
    recorded_at = _text(job.get("application_recorded_at") or job.get("applied_at"), limit=80)
    if apply_status == "submission_uncertain":
        state = "pending"
        label = "No decisive receipt admitted; reconciliation is required"
    elif apply_status == "applied" and confidence == "durable_receipt_reconciled":
        state = "durable"
        label = "Decisive durable receipt reconciled"
    elif apply_status == "applied" and confidence in _BROWSER_CONFIRMATIONS:
        state = "confirmed"
        label = "Decisive browser confirmation recorded"
    elif apply_status == "applied" and confidence == "platform_export":
        state = "reported"
        label = "Platform export reports this role as applied"
    elif apply_status == "applied":
        state = "unclassified"
        label = "Applied status is recorded without classified receipt evidence"
    else:
        state = "unrecorded"
        label = "No decisive receipt recorded"
    return {
        "state": state,
        "label": label,
        "confidence": confidence or "not_recorded",
        "recordedAt": recorded_at,
    }


def build_verify_job(job: Mapping[str, object], ledgers: list[Mapping[str, object]] | None = None) -> dict[str, Any]:
    """Summarize persisted execution evidence without admitting new receipts."""
    batch = _batch_contract(ledgers or [])
    observation = _observation_contract(job)
    receipt = _receipt_contract(job)
    reported_status = _text(job.get("apply_status"), limit=80).casefold() or "not_recorded"

    if receipt["state"] == "durable":
        state = "reconciled"
        label = "Receipt reconciled"
    elif receipt["state"] == "confirmed":
        state = "confirmed"
        label = "Confirmation evidence recorded"
    elif receipt["state"] == "reported":
        state = "reported"
        label = "Platform report recorded"
    elif receipt["state"] in {"pending", "unclassified"} or observation["state"] in {
        "recorded",
        "unreadable",
    }:
        state = "action_needed"
        label = "Evidence needs reconciliation"
    elif reported_status in {"failed", "captcha", "login_issue"} or reported_status.startswith("failed:"):
        state = "action_needed"
        label = "No confirmed outcome recorded"
    elif batch["state"] == "recorded":
        state = "recorded"
        label = "Authorization reservation recorded"
    else:
        state = "action_needed"
        label = "No durable verification record"

    return {
        "state": state,
        "label": label,
        "reportedStatus": reported_status,
        "batch": batch,
        "authorization": {
            "state": "reservation_recorded" if batch["state"] == "recorded" else "not_durably_available",
            "label": "Past authorization reservation recorded; active authorization is not inferred"
            if batch["state"] == "recorded"
            else "Authorization is not durably available in this workspace view",
        },
        "observation": observation,
        "receipt": receipt,
        "attempt": {
            "count": int(job.get("apply_attempts") or 0),
            "lastAttemptedAt": _text(job.get("last_attempted_at"), limit=80),
            "retryBlocked": bool(job.get("apply_retry_blocked")),
        },
    }


def build_verify_summary(
    jobs: list[Mapping[str, object]],
    ledgers: list[Mapping[str, object]],
    *,
    ledger_available: bool,
) -> dict[str, Any]:
    states = [str(job.get("verify", {}).get("state") or "action_needed") for job in jobs]
    batch_ids = {str(row.get("batch_id") or "") for row in ledgers if row.get("batch_id")}
    stats = {
        "records": len(jobs),
        "authorizedReservations": len(ledgers),
        "needsReview": states.count("action_needed") + states.count("recorded"),
        "reconciled": states.count("reconciled"),
        "browserConfirmed": states.count("confirmed"),
        "batches": len(batch_ids),
    }
    if not jobs and not ledgers:
        system = {
            "state": "empty",
            "title": "Nothing to verify yet",
            "message": "Verification evidence appears only after local records are persisted. Reviewing or opening a form does not create it.",
            "actions": [],
            "detail": "",
        }
    elif not ledger_available:
        system = {
            "state": "needs_evidence",
            "title": "Batch reservation evidence is not available",
            "message": "Application observations are still shown, but this page did not create or migrate the missing ledger.",
            "actions": [],
            "detail": "",
        }
    elif stats["needsReview"]:
        system = {
            "state": "needs_evidence",
            "title": f"{stats['needsReview']} application records need evidence review",
            "message": "A click, preview, security code, or stored status is not a decisive receipt. Reconcile only evidence bound to the exact role.",
            "actions": [
                {
                    "label": "Reconcile receipts",
                    "command": "applypilot reconcile-receipts --file <receipt.json>",
                }
            ],
            "detail": "",
        }
    else:
        system = {
            "state": "ready",
            "title": "Recorded outcomes have classified evidence",
            "message": "This view reports persisted evidence only; it does not claim employer acceptance or a hiring outcome.",
            "actions": [],
            "detail": "",
        }
    return {"system": system, "stats": stats}
