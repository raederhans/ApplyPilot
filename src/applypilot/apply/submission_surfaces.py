"""Canonical submission-surface classification and policy helpers.

Discovery provenance and the place where an application is actually submitted
are separate facts.  This module keeps the classifier deterministic and free
of browser or database side effects so admission callers can share it.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

from applypilot import config
from applypilot.apply.ats import detect_ats_site

SURFACES = frozenset(
    {
        "linkedin_native_easy_apply",
        "linkedin_to_official_ats",
        "official_company_careers",
        "official_ats",
        "official_direct_email",
        "restricted_portal_manual",
        "restricted_portal_review",
        "restricted_portal_authorized_handoff",
        "manual_ats",
        "unknown",
    }
)

LEGACY_SURFACE_ALIASES = {
    "official_careers": "official_company_careers",
    "company_careers": "official_company_careers",
    "official_company_careers": "official_company_careers",
    "official_ats": "official_ats",
    "direct_email": "official_direct_email",
    "official_direct_email": "official_direct_email",
    "linkedin": "linkedin_native_easy_apply",
    "linkedin_easy_apply": "linkedin_native_easy_apply",
    "linkedin_to_ats": "linkedin_to_official_ats",
    "linkedin_to_official_ats": "linkedin_to_official_ats",
    "manual": "restricted_portal_manual",
    "review_only": "restricted_portal_review",
    "manual_ats": "manual_ats",
    "unknown": "unknown",
}

# Used only when the profile predates ``allowed_submission_surfaces``.  A
# missing policy must retain existing normal channels, while restricted and
# unknown surfaces remain governed by their dedicated gates.
DEFAULT_ALLOWED_SURFACES = frozenset(
    {
        "linkedin_native_easy_apply",
        "linkedin_to_official_ats",
        "official_company_careers",
        "official_ats",
        "official_direct_email",
    }
)


def _text(value: object) -> str:
    return str(value or "").strip().casefold()


def _host(url: object) -> str:
    try:
        return (_text(urlsplit(str(url or "")).hostname) or "").rstrip(".")
    except ValueError:
        return ""


def _configured_host(value: object) -> str:
    text = _text(value)
    if not text:
        return ""
    return _host(text) or _host(f"https://{text}")


def _is_linkedin_source(job: Mapping[str, object]) -> bool:
    source = " ".join(_text(job.get(key)) for key in ("source_site", "site"))
    return "linkedin" in source


def _email_route(job: Mapping[str, object]) -> bool:
    for value in (job.get("email_application"), job.get("_email_application")):
        if isinstance(value, Mapping) and _text(value.get("route")) == "direct_email":
            return True
    observation = job.get("_browser_observation")
    if isinstance(observation, Mapping):
        email = observation.get("email_application")
        if isinstance(email, Mapping) and _text(email.get("route")) == "direct_email":
            return True
    text = " ".join(
        _text(job.get(key)) for key in ("url", "application_url", "description", "full_description")
    )
    return "mailto:" in text or "apply by email" in text


def _portal_surface(job: Mapping[str, object], application_url: str) -> str | None:
    policy = config.get_portal_policy(
        application_url or str(job.get("url") or ""),
        source_site=str(job.get("source_site") or ""),
        site=str(job.get("site") or ""),
    )
    if not policy:
        return None
    mode = _text(policy.get("application_mode"))
    if policy.get("external_application_mode") == "continue_when_authorized":
        return "restricted_portal_authorized_handoff"
    if mode == "manual_only":
        return "restricted_portal_manual"
    if mode == "review_only":
        return "restricted_portal_review"
    return "unknown"


def classify_submission_surface(job: Mapping[str, object]) -> str:
    """Return one canonical surface without changing job or policy state."""
    if _email_route(job):
        return "official_direct_email"

    application_url = str(job.get("application_url") or job.get("url") or "").strip()
    target_host = _host(application_url)
    portal_surface = _portal_surface(job, application_url)
    if portal_surface and portal_surface != "restricted_portal_authorized_handoff":
        return portal_surface

    if config.is_manual_ats(application_url):
        return "manual_ats"

    ats = detect_ats_site(application_url)
    source_is_linkedin = _is_linkedin_source(job)
    if source_is_linkedin:
        if target_host == "linkedin.com" or target_host.endswith(".linkedin.com"):
            return "linkedin_native_easy_apply"
        if ats != "generic" or portal_surface == "restricted_portal_authorized_handoff":
            return "linkedin_to_official_ats"
        # Keep the source/target distinction even when the target host is a
        # private employer domain whose ATS cannot be identified from the URL.
        # The browser observer remains responsible for stopping an unapproved
        # handoff; collapsing this into ``unknown`` would reject legacy
        # LinkedIn-to-employer flows before that observation can occur.
        return "linkedin_to_official_ats"

    if ats != "generic":
        return "official_ats"

    source = " ".join(_text(job.get(key)) for key in ("source_site", "site"))
    if any(token in source for token in ("official", "career", "company")):
        return "official_company_careers"
    if any(token in source for token in ("greenhouse", "lever", "ashby", "smartrecruiters", "workday")):
        return "official_ats"
    if "career" in application_url.casefold() or "/jobs/" in application_url.casefold():
        return "official_company_careers"
    return "unknown"


def normalize_allowed_submission_surfaces(profile: Mapping[str, object]) -> frozenset[str]:
    """Normalize explicit or legacy profile values into canonical names."""
    policy = profile.get("submission_policy", {})
    if not isinstance(policy, Mapping):
        return DEFAULT_ALLOWED_SURFACES
    if "allowed_submission_surfaces" not in policy:
        return DEFAULT_ALLOWED_SURFACES
    raw = policy["allowed_submission_surfaces"]
    if isinstance(raw, str):
        raw = (raw,)
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset()
    normalized = {
        LEGACY_SURFACE_ALIASES.get(_text(value), _text(value))
        for value in raw
        if _text(value)
    }
    return frozenset(value for value in normalized if value in SURFACES)


def linkedin_target_verification(
    job: Mapping[str, object], profile: Mapping[str, object]
) -> tuple[bool, str]:
    """Verify a LinkedIn external target using an ATS or explicit host trust."""
    application_url = str(job.get("application_url") or job.get("url") or "").strip()
    target_host = _host(application_url)
    if detect_ats_site(application_url) != "generic":
        return True, "recognized_ats"
    portal = _portal_surface(job, application_url)
    if portal == "restricted_portal_authorized_handoff":
        return True, "authorized_portal_handoff"
    policy = profile.get("submission_policy", {})
    trusted = policy.get("trusted_external_application_hosts", ()) if isinstance(policy, Mapping) else ()
    if isinstance(trusted, str):
        trusted = (trusted,)
    if isinstance(trusted, (list, tuple, set, frozenset)):
        trusted_hosts = {
            _configured_host(value) for value in trusted if _configured_host(value)
        }
        if target_host and target_host in trusted_hosts:
            return True, "explicitly_trusted_external_host"
    return False, "unverified_linkedin_external_target"


def surface_allowed(
    surface: str,
    profile: Mapping[str, object],
    *,
    direct_email_send_authorized: bool = False,
) -> tuple[bool, str]:
    """Apply the profile surface whitelist plus the independent email gate."""
    if surface == "official_direct_email" and not direct_email_send_authorized:
        return False, "direct_email_requires_independent_authorization"
    allowed = normalize_allowed_submission_surfaces(profile)
    if surface not in allowed:
        return False, f"submission_surface_not_allowed:{surface}"
    return True, "submission_surface_allowed"
