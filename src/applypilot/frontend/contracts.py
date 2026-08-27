"""Pure, privacy-bounded view models for the packaged frontend."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from applypilot.apply.authorization import compute_job_fingerprint
from applypilot.apply.decision import evaluate

_READY_COVER_STATUSES = {"agent_validated", "human_approved", "not_required"}
_FAILED_TOKENS = ("failed", "error", "invalid", "rejected")


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
