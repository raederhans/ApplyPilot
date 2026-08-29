"""Small, deterministic gate for deciding whether a job can be applied to."""

from __future__ import annotations

import json

from applypilot.apply.authorization import compute_job_fingerprint
from applypilot.apply.failure_taxonomy import classify_failure

_PERMANENT_IGNORE_TOKENS = {"expired", "duplicate", "already_applied"}
_DO_NOT_APPLY_READINESS = {
    "contradiction",
    "do-not-apply",
    "do_not_apply",
    "explicit_contradiction",
    "ineligible",
}


def _has_unanswered_questions(value: object) -> bool:
    if isinstance(value, dict):
        # Ordinary unknowns are resolved in the live form. Only an explicitly
        # required, direct-impact question remains a pre-acquisition blocker.
        return value.get("required") is True and value.get("direct_impact") is True
    if isinstance(value, (list, tuple, set)):
        return any(_has_unanswered_questions(item) for item in value)
    if value is None:
        return False
    text = str(value).strip()
    if not text or text == "[]" or text == "{}":
        return False
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        # Legacy free-form records lack impact metadata. Revisit them in the
        # live resolver instead of treating every historical unknown as fatal.
        return False
    return _has_unanswered_questions(parsed)


def evaluate(
    job: dict,
    *,
    minimum_fit_score: int = 4,
    allow_runtime_readiness: bool = False,
    allow_runtime_cover_letter: bool = False,
) -> dict[str, str]:
    """Return a conservative, human-readable application decision.

    The function deliberately performs no I/O. It only evaluates the supplied
    job snapshot so callers can persist or display the decision as appropriate.
    """
    if job.get("applied_at") or str(job.get("apply_status") or "").casefold() == "applied":
        return {"decision": "ignore", "reason": "Job is already recorded as applied."}

    apply_status = str(job.get("apply_status") or "").strip().casefold()
    if str(job.get("eligibility_status") or "").casefold() == "ineligible":
        reason = str(job.get("eligibility_reason") or "explicit eligibility failure").strip()
        return {"decision": "ignore", "reason": f"Job is ineligible: {reason}"}
    if apply_status in _PERMANENT_IGNORE_TOKENS:
        return {"decision": "ignore", "reason": "Job is expired or an explicit duplicate."}
    if job.get("possible_repost_of") and str(job.get("dedupe_status") or "").casefold() in {
        "duplicate",
        "confirmed_duplicate",
    }:
        return {"decision": "ignore", "reason": "Job is an explicit duplicate."}

    if isinstance(minimum_fit_score, bool) or not isinstance(minimum_fit_score, int):
        raise TypeError("minimum_fit_score must be an integer")
    minimum_fit_score = max(1, min(minimum_fit_score, 10))
    fit_score = job.get("fit_score")
    if not isinstance(fit_score, (int, float)) or isinstance(fit_score, bool):
        return {"decision": "needs_review", "reason": "Job fit score is missing."}
    if fit_score < minimum_fit_score:
        return {
            "decision": "ignore",
            "reason": (
                f"Job fit score {fit_score:g}/10 is below the configured "
                f"application floor of {minimum_fit_score}/10."
            ),
        }

    readiness_conditions: list[str] = []
    readiness_status = str(job.get("application_readiness_status") or "").strip().casefold()
    if readiness_status in _DO_NOT_APPLY_READINESS:
        reason = str(job.get("application_readiness_reason") or "explicit contradiction").strip()
        return {"decision": "ignore", "reason": f"Application readiness failed: {reason}"}
    if readiness_status not in {"", "confirmed"}:
        readiness_conditions.append("application readiness requires runtime confirmation")
    elif not readiness_status:
        readiness_conditions.append("application readiness will be determined in the form")
    if readiness_status == "confirmed":
        readiness_reason = str(job.get("application_readiness_reason") or "").strip()
        readiness_reviewed_at = str(job.get("application_readiness_reviewed_at") or "").strip()
        if not readiness_reason or not readiness_reviewed_at:
            readiness_conditions.append("readiness evidence is incomplete")
        readiness_fingerprint = str(job.get("application_readiness_fingerprint") or "").strip()
        if not readiness_fingerprint:
            readiness_conditions.append("readiness is not bound to the current job version")
        elif readiness_fingerprint != compute_job_fingerprint(job):
            readiness_conditions.append("job details changed after readiness review")

    description = str(job.get("full_description") or "").strip()
    if not description:
        return {"decision": "needs_review", "reason": "Full job description is missing."}

    eligibility = str(job.get("eligibility_status") or "").strip().casefold()
    if eligibility not in {"eligible", "pass", "passed"}:
        readiness_conditions.append("eligibility will be confirmed during the application")

    if _has_unanswered_questions(job.get("unanswered_questions_json")) or _has_unanswered_questions(
        job.get("unanswered_questions")
    ):
        return {"decision": "needs_review", "reason": "Required application questions remain unanswered."}

    if apply_status in {"submission_uncertain", "in_progress"}:
        return {"decision": "needs_review", "reason": "Application has a structured active or uncertain state."}
    if job.get("apply_retry_blocked"):
        retry_reason = str(job.get("apply_retry_reason") or job.get("apply_error") or "")
        failure = classify_failure(retry_reason)
        if failure.recoverability in {
            "do_not_retry",
            "requires_human_boundary",
            "requires_material",
            "submission_uncertain",
        }:
            return {
                "decision": "needs_review",
                "reason": f"Application retry requires action: {failure.next_action}.",
            }

    if not str(job.get("application_url") or job.get("url") or "").strip():
        return {"decision": "needs_review", "reason": "Application URL is missing."}
    if not str(job.get("tailored_resume_path") or "").strip():
        return {"decision": "needs_review", "reason": "Tailored resume is not prepared."}
    if str(job.get("tailor_status") or "").casefold() != "machine_validated":
        return {"decision": "needs_review", "reason": "Tailored resume is not machine validated."}
    cover_status = str(job.get("cover_letter_status") or "").casefold()
    if cover_status not in {"not_required", "human_approved", "agent_validated"} and not (
        allow_runtime_cover_letter and cover_status in {"", "pending", "machine_validated", "failed_validation"}
    ):
        return {"decision": "needs_review", "reason": "Cover-letter requirement is not resolved."}

    if readiness_conditions:
        return {
            "decision": "ready_to_apply",
            "reason": "Apply with conditions: " + "; ".join(dict.fromkeys(readiness_conditions)) + ".",
        }
    return {"decision": "ready_to_apply", "reason": "Eligibility, materials, and application state are clear."}
