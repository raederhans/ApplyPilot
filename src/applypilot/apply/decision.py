"""Small, deterministic gate for deciding whether a job can be applied to."""

from __future__ import annotations

import json
import re

from applypilot.apply.authorization import compute_job_fingerprint

_BLOCKING_STATUS_TOKENS = {
    "submission_uncertain",
    "captcha",
    "assessment",
    "login",
    "manual",
    "blocked",
    "in_progress",
}
_PERMANENT_IGNORE_TOKENS = {"expired", "duplicate", "already_applied"}
_SENIOR_TITLE = re.compile(
    r"(?i)\b(?:senior|sr\.?|staff|principal|lead|director|head|vice president|vp|chief)\b"
)
_EXPERIENCE_REQUIREMENT = re.compile(
    r"(?i)\b(?:at least|minimum(?:\s+of)?|min\.?)?\s*(\d{1,2})\s*\+?\s*years?"
)


def _has_unanswered_questions(value: object) -> bool:
    if isinstance(value, dict):
        # Browser previews also report optional fields that were deliberately
        # left blank.  Only unresolved required/unknown questions should block
        # a submission decision.
        return value.get("required") is not False
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
        return True
    return _has_unanswered_questions(parsed)


def _status_text(job: dict) -> str:
    return " ".join(
        str(job.get(field) or "").strip().casefold()
        for field in (
            "apply_status",
            "apply_error",
            "apply_retry_reason",
            "detail_error",
            "dedupe_status",
        )
    )


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

    status_text = _status_text(job)
    if str(job.get("eligibility_status") or "").casefold() == "ineligible":
        reason = str(job.get("eligibility_reason") or "explicit eligibility failure").strip()
        return {"decision": "ignore", "reason": f"Job is ineligible: {reason}"}
    if any(token in status_text for token in _PERMANENT_IGNORE_TOKENS):
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

    readiness_status = str(job.get("application_readiness_status") or "").strip().casefold()
    if readiness_status not in {"", "confirmed"}:
        return {
            "decision": "needs_review",
            "reason": (
                "Application readiness is not confirmed from evidence for work authorization, "
                "availability, location, and other hard requirements."
            ),
        }
    if not readiness_status and not allow_runtime_readiness:
        return {
            "decision": "needs_review",
            "reason": (
                "Application readiness is not confirmed from evidence for work authorization, "
                "availability, location, and other hard requirements."
            ),
        }
    if readiness_status == "confirmed":
        readiness_reason = str(job.get("application_readiness_reason") or "").strip()
        readiness_reviewed_at = str(job.get("application_readiness_reviewed_at") or "").strip()
        if not readiness_reason or not readiness_reviewed_at:
            return {
                "decision": "needs_review",
                "reason": "Confirmed application readiness requires a reason and review timestamp.",
            }
        readiness_fingerprint = str(job.get("application_readiness_fingerprint") or "").strip()
        if not readiness_fingerprint:
            return {
                "decision": "needs_review",
                "reason": "Application readiness is not bound to the reviewed job version.",
            }
        if readiness_fingerprint != compute_job_fingerprint(job):
            return {
                "decision": "needs_review",
                "reason": "Job details changed after the application-readiness review.",
            }

    description = str(job.get("full_description") or "").strip()
    if not description:
        return {"decision": "needs_review", "reason": "Full job description is missing."}

    eligibility = str(job.get("eligibility_status") or "").strip().casefold()
    if eligibility not in {"eligible", "pass", "passed"}:
        return {"decision": "needs_review", "reason": "Eligibility is unknown or not confirmed."}

    if _has_unanswered_questions(job.get("unanswered_questions_json")) or _has_unanswered_questions(
        job.get("unanswered_questions")
    ):
        return {"decision": "needs_review", "reason": "Required application questions remain unanswered."}

    if job.get("apply_retry_blocked") or any(token in status_text for token in _BLOCKING_STATUS_TOKENS):
        return {"decision": "needs_review", "reason": "Application is blocked or requires manual review."}

    title = str(job.get("title") or "")
    if _SENIOR_TITLE.search(title):
        return {"decision": "needs_review", "reason": "Title indicates a clearly senior role."}
    experience_years = [int(match.group(1)) for match in _EXPERIENCE_REQUIREMENT.finditer(description)]
    if experience_years and max(experience_years) >= 4:
        return {
            "decision": "needs_review",
            "reason": f"Job description includes a {max(experience_years)}+ year experience threshold.",
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

    return {"decision": "ready_to_apply", "reason": "Eligibility, materials, and application state are clear."}
