"""Shared deterministic admission predicate for application authorization."""

from __future__ import annotations

from collections.abc import Mapping

from applypilot import config
from applypilot.apply import decision
from applypilot.apply.material_readiness import evaluate_profile_resume_fact_freshness
from applypilot.apply.submission_surfaces import (
    classify_submission_surface,
    linkedin_target_verification,
    surface_allowed,
)


def resolve_max_apply_attempts(profile: Mapping[str, object]) -> int:
    """Return the configured attempt ceiling, failing closed on bad policy."""
    policy = profile.get("submission_policy", {})
    raw = (
        policy.get("maximum_apply_attempts", config.DEFAULTS["max_apply_attempts"])
        if isinstance(policy, Mapping)
        else config.DEFAULTS["max_apply_attempts"]
    )
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ValueError(
            "submission_policy.maximum_apply_attempts must be a positive integer"
        )
    return raw


def evaluate_submission_admission(
    job: Mapping[str, object],
    profile: Mapping[str, object],
    *,
    minimum_fit_score: int,
    preview_only: bool = False,
) -> dict[str, object]:
    """Return one stable admission result used by manifest and queue callers.

    This function performs no writes.  It intentionally composes the existing
    pure decision and portal gates, adding the attempt ceiling and canonical
    submission surface in one place.
    """
    surface = classify_submission_surface(job)
    surface_metadata: dict[str, object] = {"surface": surface}
    profile_resume_freshness = evaluate_profile_resume_fact_freshness(job, profile)
    surface_metadata["profile_resume_fact_freshness"] = profile_resume_freshness
    if profile_resume_freshness["state"] == "stale_profile_fact":
        errors = profile_resume_freshness["fact_errors"]
        assert isinstance(errors, list)
        return {
            "admitted": False,
            "decision": "needs_review",
            "reason": "stale_profile_fact:" + "; ".join(str(error) for error in errors),
            "surface": surface,
            "metadata": surface_metadata,
        }
    if surface == "linkedin_apply_entry":
        surface_metadata["requires_runtime_linkedin_apply_route_resolution"] = True
    if surface == "linkedin_native_easy_apply":
        surface_metadata["requires_runtime_easy_apply_verification"] = True
    if surface == "linkedin_to_official_ats":
        verified, verification = linkedin_target_verification(job, profile)
        surface_metadata["target_verification"] = verification
        if not verified:
            return {
                "admitted": False,
                "decision": "needs_review",
                "reason": "unverified_linkedin_external_target",
                "surface": surface,
                "metadata": surface_metadata,
            }

    if job.get("apply_retry_blocked"):
        return {
            "admitted": False,
            "decision": "needs_review",
            "reason": "apply_retry_blocked_requires_review",
            "surface": surface,
            "metadata": surface_metadata,
        }

    result = decision.evaluate(
        dict(job),
        minimum_fit_score=minimum_fit_score,
        allow_runtime_readiness=bool(
            isinstance(profile.get("submission_policy"), Mapping)
            and profile["submission_policy"].get("allow_runtime_readiness_review", False)
        ),
        allow_runtime_cover_letter=preview_only or bool(
            isinstance(profile.get("submission_policy"), Mapping)
            and profile["submission_policy"].get(
                "allow_runtime_cover_letter_discovery", False
            )
        ),
    )
    if result.get("decision") != "ready_to_apply":
        return {
            "admitted": False,
            "decision": result.get("decision"),
            "reason": result.get("reason") or "application decision did not pass",
            "surface": surface,
            "metadata": surface_metadata,
        }

    try:
        max_attempts = resolve_max_apply_attempts(profile)
    except ValueError as exc:
        return {
            "admitted": False,
            "decision": "needs_review",
            "reason": str(exc),
            "surface": surface,
            "metadata": surface_metadata,
        }
    try:
        attempts = int(job.get("apply_attempts") or 0)
    except (TypeError, ValueError):
        return {
            "admitted": False,
            "decision": "needs_review",
            "reason": "apply_attempts is malformed",
            "surface": surface,
            "metadata": surface_metadata,
        }
    if attempts >= max_attempts:
        return {
            "admitted": False,
            "decision": "needs_review",
            "reason": "maximum application attempts reached",
            "surface": surface,
            "metadata": surface_metadata,
        }

    application_url = str(job.get("application_url") or job.get("url") or "")
    portal_reason = config.portal_application_gate(
        application_url,
        source_site=str(job.get("source_site") or ""),
        site=str(job.get("site") or ""),
        preview_only=preview_only,
    )
    if portal_reason:
        return {
            "admitted": False,
            "decision": "needs_review",
            "reason": portal_reason,
            "surface": surface,
            "metadata": surface_metadata,
        }

    # Manual ATS and explicitly review-only/authorized portal routes retain
    # their existing prepare boundary.  The browser worker may inspect them,
    # but their own gate still controls whether a real submit is possible.
    if surface == "manual_ats" and preview_only:
        return {
            "admitted": True,
            "decision": "ready_to_apply",
            "reason": "manual ATS remains a runtime capability boundary",
            "surface": surface,
            "metadata": surface_metadata,
        }
    if surface == "manual_ats":
        return {
            "admitted": False,
            "decision": "needs_review",
            "reason": "manual ATS requires candidate-operated submission",
            "surface": surface,
            "metadata": surface_metadata,
        }
    if surface.startswith("restricted_portal_"):
        return {
            "admitted": True,
            "decision": "ready_to_apply",
            "reason": "restricted portal gate permits this bounded route",
            "surface": surface,
            "metadata": surface_metadata,
        }

    direct_email_authorized = bool(
        isinstance(profile.get("submission_policy"), Mapping)
        and profile["submission_policy"].get("direct_email_application_authorized", False)
    )
    allowed, allowed_reason = surface_allowed(
        surface,
        profile,
        direct_email_send_authorized=direct_email_authorized,
    )
    if not allowed:
        return {
            "admitted": False,
            "decision": "needs_review",
            "reason": allowed_reason,
            "surface": surface,
            "metadata": surface_metadata,
        }
    return {
        "admitted": True,
        "decision": "ready_to_apply",
        "reason": (
            "requires_runtime_linkedin_apply_route_resolution"
            if surface == "linkedin_apply_entry"
            else (
                "requires_runtime_easy_apply_verification"
                if surface == "linkedin_native_easy_apply"
                else result.get("reason") or "submission admission passed"
            )
        ),
        "surface": surface,
        "metadata": surface_metadata,
    }


def summarize_worker_allocation(
    connection,
    profile: Mapping[str, object],
    manifest: Mapping[str, object] | None,
    *,
    requested_workers: int,
    minimum_fit_score: int,
    preview_only: bool = False,
) -> dict[str, int]:
    """Count bound/executable jobs and derive the worker count before launch."""
    if manifest is None:
        raise ValueError("worker allocation requires an authorization manifest")
    entries = manifest.get("jobs", []) if isinstance(manifest, Mapping) else []
    if not isinstance(entries, list):
        entries = []
    bound = len(entries)
    executable = 0
    from applypilot.apply.authorization import authorize_job

    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        rows = connection.execute(
            "SELECT * FROM jobs WHERE url = ? OR application_url = ?",
            (entry.get("url"), entry.get("application_url")),
        ).fetchall()
        if len(rows) != 1:
            continue
        job = dict(rows[0])
        job["application_url"] = job.get("application_url") or job.get("url")
        if authorize_job(dict(manifest), job) is None:
            continue
        if evaluate_submission_admission(
            job,
            profile,
            minimum_fit_score=minimum_fit_score,
            preview_only=preview_only,
        ).get("admitted"):
            executable += 1
    try:
        profile_cap = int(
            profile.get("submission_policy", {}).get("maximum_workers", requested_workers)
            if isinstance(profile.get("submission_policy"), Mapping)
            else requested_workers
        )
    except (TypeError, ValueError):
        profile_cap = requested_workers
    requested = max(0, int(requested_workers))
    effective = min(requested, max(0, profile_cap), executable)
    return {
        "requested_workers": requested,
        "bound_candidates": bound,
        "executable_candidates": executable,
        "blocked_candidates": max(0, bound - executable),
        "effective_workers": effective,
    }
