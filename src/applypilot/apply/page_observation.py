"""Browser-page observation and application form audit contracts.

This module owns browser-derived facts only.  It does not acquire jobs, mutate
application ledgers, launch workers, or decide batch progress.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from urllib.parse import parse_qs, unquote, urlparse

from applypilot import config
from applypilot.apply import ats as ats_mod
from applypilot.apply import linkedin_page_observation as linkedin_observation_mod
from applypilot.apply import page_surfaces as page_surfaces_mod
from applypilot.apply import post_submit_observation as post_submit_observation_mod
from applypilot.apply import prompt as prompt_mod
from applypilot.apply.answer_provenance import audit_pre_submit_answer_provenance
from applypilot.apply.identity_materials import classify_identity_requirement
from applypilot.apply.prepared_state import current_prepared_observations
from applypilot.apply.specialists import (
    ATS_FORM_SNAPSHOT_SCHEMA_VERSION,
    freeze_ats_fill_plan_snapshot,
)
from applypilot.apply.stateful_control_coverage import (
    STATEFUL_CONTROL_COVERAGE_SCRIPT,
    stateful_control_coverage_error,
)
from applypilot.apply.workday_state import (
    ProgressAction,
    evaluate_page_progress,
    observation_from_mapping,
)

logger = logging.getLogger(__name__)

_APPLICATION_SURFACE_SIGNALS = page_surfaces_mod.APPLICATION_SURFACE_SIGNALS
_allowed_application_surface_signals = page_surfaces_mod.allowed_application_surface_signals
_application_surface_is_allowed = page_surfaces_mod.application_surface_is_allowed
_application_surface_score = page_surfaces_mod.application_surface_score
_application_surface_selection_score = page_surfaces_mod.application_surface_selection_score
_bound_application_pages = page_surfaces_mod.bound_application_pages
_http_origin = page_surfaces_mod.http_origin
_merge_same_page_submit_evidence = page_surfaces_mod.merge_same_page_submit_evidence
_score_application_page = page_surfaces_mod.score_application_page
_select_application_frame = page_surfaces_mod.select_application_frame
_select_application_page = page_surfaces_mod.select_application_page
_select_application_page_and_frame = page_surfaces_mod.select_application_page_and_frame

_linkedin_external_handoff_pages = linkedin_observation_mod.linkedin_external_handoff_pages
_linkedin_job_id = linkedin_observation_mod.linkedin_job_id
_linkedin_page_matches_job_id = linkedin_observation_mod.linkedin_page_matches_job_id
_linkedin_authwall_redirect_job_id = linkedin_observation_mod.linkedin_authwall_redirect_job_id
_target_infos = linkedin_observation_mod.target_infos
_external_https_target = linkedin_observation_mod.external_https_target
_classify_linkedin_causal_target = linkedin_observation_mod.classify_linkedin_causal_target
_target_id_digest = linkedin_observation_mod.target_id_digest
_admit_linkedin_causal_events = linkedin_observation_mod.admit_linkedin_causal_events
_resolve_linkedin_click_epoch = linkedin_observation_mod.resolve_linkedin_click_epoch
_page_target_id = linkedin_observation_mod.page_target_id
_linkedin_source_page_is_still_exact = linkedin_observation_mod.linkedin_source_page_is_still_exact
_wait_for_linkedin_main_apply_control = linkedin_observation_mod.wait_for_linkedin_main_apply_control
_linkedin_main_apply_handle = linkedin_observation_mod.linkedin_main_apply_handle
_linkedin_app_promo_dismiss_handle = linkedin_observation_mod.linkedin_app_promo_dismiss_handle
_dismiss_linkedin_app_promo = linkedin_observation_mod.dismiss_linkedin_app_promo
_linkedin_click_page_state = linkedin_observation_mod.linkedin_click_page_state
_click_linkedin_main_apply_causally = linkedin_observation_mod.click_linkedin_main_apply_causally
_verify_linkedin_post_login_state = linkedin_observation_mod.verify_linkedin_post_login_state
_linkedin_external_page_identity = linkedin_observation_mod.linkedin_external_page_identity
_observe_linkedin_external_handoff_page = linkedin_observation_mod.observe_linkedin_external_handoff_page

_HISTORICAL_DUPLICATE_RE = post_submit_observation_mod.HISTORICAL_DUPLICATE_RE
_classify_post_submit_observation = post_submit_observation_mod.classify_post_submit_observation
_is_historical_duplicate_text = post_submit_observation_mod.is_historical_duplicate_text
_looks_like_submission_receipt_text = post_submit_observation_mod.looks_like_submission_receipt_text
_observe_post_submit_page = post_submit_observation_mod.observe_post_submit_page
_submission_evidence_consistent = post_submit_observation_mod.submission_evidence_consistent

__all__ = (
    "_APPLICATION_SURFACE_SIGNALS",
    "_HISTORICAL_DUPLICATE_RE",
    "_admit_linkedin_causal_events",
    "_allowed_application_surface_signals",
    "_application_surface_is_allowed",
    "_application_surface_score",
    "_application_surface_selection_score",
    "_bound_application_pages",
    "_classify_linkedin_causal_target",
    "_classify_post_submit_observation",
    "_click_linkedin_main_apply_causally",
    "_dismiss_linkedin_app_promo",
    "_external_https_target",
    "_http_origin",
    "_is_historical_duplicate_text",
    "_linkedin_app_promo_dismiss_handle",
    "_linkedin_authwall_redirect_job_id",
    "_linkedin_click_page_state",
    "_linkedin_external_handoff_pages",
    "_linkedin_external_page_identity",
    "_linkedin_job_id",
    "_linkedin_main_apply_handle",
    "_linkedin_page_matches_job_id",
    "_linkedin_source_page_is_still_exact",
    "_looks_like_submission_receipt_text",
    "_merge_same_page_submit_evidence",
    "_observe_linkedin_external_handoff_page",
    "_observe_post_submit_page",
    "_page_target_id",
    "_resolve_linkedin_click_epoch",
    "_score_application_page",
    "_select_application_frame",
    "_select_application_page",
    "_select_application_page_and_frame",
    "_submission_evidence_consistent",
    "_target_id_digest",
    "_target_infos",
    "_verify_linkedin_post_login_state",
    "_wait_for_linkedin_main_apply_control",
)

_STATEFUL_CONTROL_COVERAGE_SCRIPT = STATEFUL_CONTROL_COVERAGE_SCRIPT


def _verified_agent_resume_upload(job: dict) -> bool:
    """Accept a compact same-turn upload proof, never a bare status claim."""
    observations = current_prepared_observations(job)
    if not observations and "_prepared_state_evidence" not in job:
        # Compatibility for callers that predate host-side lease binding.
        observations = job.get("_agent_observations")
    if not isinstance(observations, dict):
        return False
    proof = observations.get("resume_upload")
    if not isinstance(proof, dict):
        return False
    label = str(proof.get("field_label") or "").casefold()
    visible_filename = proof.get("visible_filename")
    filename_is_visible = bool(
        visible_filename is True
        or (isinstance(visible_filename, str) and visible_filename.strip())
    )
    resume_label = bool(re.search(r"\b(?:resume|curriculum vitae|cv)\b", label))
    return proof.get("verified") is True and filename_is_visible and resume_label


def _authorized_identity_material(job: Mapping[str, object], label: str) -> bool:
    """Accept only a verified, explicitly authorized artifact matching this field."""
    normalized_label = " ".join(str(label or "").casefold().split())
    sources: list[object] = []
    configured = job.get("_authorized_identity_materials")
    if isinstance(configured, list):
        sources.extend(configured)
    observations = job.get("_agent_observations")
    if isinstance(observations, Mapping):
        observed = observations.get("identity_material_uploads")
        if isinstance(observed, list):
            sources.extend(observed)
    for item in sources:
        if not isinstance(item, Mapping):
            continue
        field_label = " ".join(
            str(item.get("field_label") or item.get("label") or "")
            .casefold()
            .split()
        )
        label_matches = bool(
            normalized_label
            and field_label
            and (
                normalized_label in field_label
                or field_label in normalized_label
            )
        )
        if (
            label_matches
            and item.get("verified") is True
            and item.get("explicitly_authorized") is True
        ):
            return True
    return False


def _same_bound_application_flow(
    expected_url: str,
    actual_url: str,
    snapshot: dict,
    binding: dict | None = None,
) -> bool:
    """Accept a proven same-tenant ATS review route, not arbitrary path drift."""
    expected = urlparse(expected_url)
    actual = urlparse(actual_url)
    if (
        expected.scheme.casefold() != "https"
        or actual.scheme.casefold() != "https"
        or not expected.hostname
        or expected.hostname.casefold() != (actual.hostname or "").casefold()
    ):
        return False

    if (
        ats_mod.detect_ats_site(expected_url) == "smartrecruiters"
        and ats_mod.detect_ats_site(actual_url) == "smartrecruiters"
    ):
        expected_oneclick = _smartrecruiters_oneclick_route(expected_url)
        actual_oneclick = _smartrecruiters_oneclick_route(actual_url)
        expected_is_oneclick = unquote(expected.path).casefold().startswith(
            "/oneclick-ui/company/"
        )
        actual_is_oneclick = unquote(actual.path).casefold().startswith(
            "/oneclick-ui/company/"
        )
        if expected_is_oneclick:
            return bool(
                expected_oneclick
                and expected_oneclick[2] == ()
                and actual_oneclick
                and expected_oneclick[:2] == actual_oneclick[:2]
            )
        if not actual_is_oneclick:
            return _same_exact_application_path(expected_url, actual_url)

        expected_parts = [part for part in expected.path.split("/") if part]
        expected_tenant = expected_parts[0] if len(expected_parts) >= 2 else ""
        expected_posting_id = (
            expected_parts[1].split("-", 1)[0] if len(expected_parts) >= 2 else ""
        )
        actual_tenant = actual_oneclick[0] if actual_oneclick else ""
        actual_publication_id = actual_oneclick[1] if actual_oneclick else ""
        return bool(
            expected_tenant
            and expected_tenant.casefold() == actual_tenant.casefold()
            and isinstance(binding, dict)
            and binding.get("resolved") is True
            and str(binding.get("provider") or "").casefold() == "smartrecruiters"
            and str(binding.get("tenant") or "").casefold()
            == expected_tenant.casefold()
            and str(binding.get("posting_id") or "") == expected_posting_id
            and str(binding.get("publication_id") or "").casefold()
            == actual_publication_id.casefold()
        )

    # Shopee moves an exact public job-detail route onto its same-host apply
    # route.  Bind the change to the immutable job id carried by both routes;
    # another job, a generic careers page, or a cross-host redirect still fails.
    if expected.hostname.casefold() == "careers.shopee.sg":
        expected_match = re.fullmatch(
            r"/job-detail/([^/]+)/[^/]+/?", unquote(expected.path), re.IGNORECASE
        )
        actual_id = parse_qs(actual.query).get("id", [])
        if (
            expected_match
            and unquote(actual.path).rstrip("/").casefold() == "/apply"
            and len(actual_id) == 1
            and actual_id[0].casefold() == expected_match.group(1).casefold()
        ):
            return True

    # Workato's careers page rewrites a gh_jid query into a same-host slugged
    # job route while retaining that exact Greenhouse id.  Require the id in
    # both query strings and at the end of the rewritten path.
    if expected.hostname.casefold() == "www.workato.com":
        expected_ids = parse_qs(expected.query).get("gh_jid", [])
        actual_ids = parse_qs(actual.query).get("gh_jid", [])
        if (
            len(expected_ids) == 1
            and len(actual_ids) == 1
            and expected_ids[0] == actual_ids[0]
            and re.fullmatch(r"\d+", expected_ids[0])
            and re.search(rf"-{re.escape(expected_ids[0])}/?$", unquote(actual.path))
        ):
            return True

    expected_path = unquote(expected.path).rstrip("/").removesuffix("/apply")
    actual_path = unquote(actual.path).rstrip("/").removesuffix("/apply")
    expected_tokens = tuple(re.findall(r"[a-z0-9]+", expected_path.casefold()))
    actual_tokens = tuple(re.findall(r"[a-z0-9]+", actual_path.casefold()))
    if expected_tokens and expected_tokens == actual_tokens:
        return True

    # Workday changes the URL from the public job route to a tenant-local
    # application/review route.  Bind that path change to the same HTTPS
    # tenant plus the independently observed final review state and Submit
    # control; a login, home, or unrelated same-host page remains rejected.
    workday = snapshot.get("workday_observation")
    job_reference_matches = any(
        _same_exact_application_path(expected_url, str(reference or ""))
        for reference in snapshot.get("job_reference_urls", [])
    )
    return bool(
        ats_mod.detect_ats_site(expected_url) == "workday"
        and ats_mod.detect_ats_site(actual_url) == "workday"
        and isinstance(workday, dict)
        and job_reference_matches
        and str(workday.get("page_kind") or "").casefold() == "review"
        and workday.get("has_submit") is True
        and workday.get("has_manual_gate") is not True
        and int(snapshot.get("submit_control_count") or 0) > 0
    )


def _same_exact_application_path(expected_url: str, actual_url: str) -> bool:
    expected = urlparse(expected_url)
    actual = urlparse(actual_url)
    if (
        expected.scheme.casefold() != "https"
        or actual.scheme.casefold() != "https"
        or not expected.hostname
        or expected.hostname.casefold() != (actual.hostname or "").casefold()
    ):
        return False
    expected_path = unquote(expected.path).rstrip("/").removesuffix("/apply")
    actual_path = unquote(actual.path).rstrip("/").removesuffix("/apply")
    expected_tokens = tuple(re.findall(r"[a-z0-9]+", expected_path.casefold()))
    actual_tokens = tuple(re.findall(r"[a-z0-9]+", actual_path.casefold()))
    return bool(expected_tokens and expected_tokens == actual_tokens)


def _application_fact_value(profile: dict, key: str) -> object | None:
    """Return the newest confirmed profile fact for one stable key."""
    for fact in reversed(profile.get("application_facts", [])):
        if isinstance(fact, dict) and str(fact.get("key") or "").strip() == key:
            return fact.get("value")
    return None


def _contextual_application_fact_value(
    profile: dict, key: str, context: str
) -> object | None:
    """Return an exact confirmed option scoped to one semantic context."""
    expected_context = " ".join(str(context or "").casefold().split())
    for fact in reversed(profile.get("application_facts", [])):
        if not isinstance(fact, dict) or str(fact.get("key") or "").strip() != key:
            continue
        fact_context = " ".join(str(fact.get("context") or "").casefold().split())
        if fact_context == expected_context:
            return fact.get("value")
    return None


def _normalize_credential_text(value: object) -> str:
    text = re.sub(r"[’']s\b", "s", str(value or "").casefold())
    return " ".join(
        re.sub(r"[^a-z0-9]+", " ", text).split()
    )


def _degree_level(value: object) -> str | None:
    text = _normalize_credential_text(value)
    for level, pattern in (
        ("doctorate", r"\b(?:doctor|doctorate|phd)\b"),
        ("master", r"\b(?:master|masters|msc|mcomp|ma)\b"),
        ("bachelor", r"\b(?:bachelor|bachelors|bsc|ba)\b"),
        ("diploma", r"\bdiploma\b"),
    ):
        if re.search(pattern, text):
            return level
    return None


def _degree_value_supported(
    selected: object,
    expected: object,
    *,
    explicit_option: object | None = None,
) -> bool:
    """Accept an exact credential or a lossy selector at the same degree level.

    ATS degree selectors frequently collapse distinct awards (for example an
    MComp into an MSc/Master's bucket). Selecting that bucket is a taxonomy
    mapping, not a claim that the confirmed award title changed. Different
    degree levels remain a hard mismatch.
    """
    actual = _normalize_credential_text(selected)
    source = _normalize_credential_text(expected)
    exact = _normalize_credential_text(explicit_option)
    if not actual or not source:
        return False
    if actual == source or (exact and actual == exact):
        return True

    expected_level = _degree_level(source)
    return bool(expected_level and _degree_level(actual) == expected_level)


def _same_institution(left: object, right: object) -> bool:
    first = _normalize_credential_text(left)
    second = _normalize_credential_text(right)
    return bool(
        first
        and second
        and (first == second or (min(len(first), len(second)) >= 8 and (first in second or second in first)))
    )


def _highest_confirmed_degree_level(education: list[dict]) -> str | None:
    rank = {"diploma": 1, "bachelor": 2, "master": 3, "doctorate": 4}
    completed = []
    for item in education:
        status = " ".join(str(item.get("status") or "").casefold().split())
        in_progress = bool(re.search(
            r"\b(?:current(?:ly)?|enrolled|in progress|ongoing|pursuing)\b",
            status,
        )) or bool(item.get("expected_graduation") and not item.get("graduation"))
        if not in_progress:
            completed.append(item)
    levels = [
        level
        for item in completed
        if (level := _degree_level(item.get("degree"))) is not None
    ]
    return max(levels, key=rank.__getitem__) if levels else None


def _is_generic_education_level_question(value: object) -> bool:
    text = " ".join(str(value or "").casefold().split())
    return bool(re.search(
        r"\b(?:highest\s+)?(?:education\s+level|level\s+of\s+education)\b|"
        r"\bhighest\s+(?:academic\s+)?(?:degree|qualification)\b",
        text,
    ))


def _yes_no_value(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().casefold()
    if re.match(r"^(?:yes|true)\b", text):
        return True
    if re.match(r"^(?:no|false|none|not applicable|n/?a)\b", text):
        return False
    return None


def _selected_matches_boolean(selected: object, expected: bool) -> bool:
    text = " ".join(str(selected or "").strip().casefold().split())
    if expected:
        return bool(re.match(r"^(?:yes|true)\b", text))
    return bool(
        re.match(r"^(?:no|false|none|neither|not applicable|n/?a)\b", text)
        or "none of the above" in text
        or "citizen of a different country" in text
    )


def _field_required(field: dict) -> bool:
    """Preserve compatibility with older snapshots while honoring new metadata."""
    return bool(field.get("required", True))


def _is_singapore_location(value: object) -> bool:
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()
    return bool(text and ("singapore" in text.split() or text in {"sg", "sgp"}))


def _is_post_graduation_work_context(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:upon|following)\s+(?:your\s+)?graduation\b|"
            r"\bafter\s+(?:(?:your|you)\s+)?graduat(?:ion|e|ing)\b|"
            r"\b(?:when|once)\s+you\s+graduate\b|"
            r"\bwill\s+you\s+need\s+in\s+the\s+future\b|"
            r"\bpost[ -]?graduation\b",
            text,
        )
    )


def _smartrecruiters_oneclick_route(
    url: str,
) -> tuple[str, str, tuple[str, ...]] | None:
    parsed = urlparse(url)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if (
        len(parts) < 5
        or [part.casefold() for part in parts[:2]] != ["oneclick-ui", "company"]
        or parts[3].casefold() != "publication"
    ):
        return None
    suffix = tuple(part.casefold() for part in parts[5:])
    if suffix not in {(), ("screening",)}:
        return None
    tenant = parts[2]
    publication_id = parts[4]
    query_tenants = parse_qs(parsed.query).get("dcr_ci") or []
    if (
        len(query_tenants) != 1
        or query_tenants[0].casefold() != tenant.casefold()
    ):
        return None
    return tenant.casefold(), publication_id.casefold(), suffix


def _work_authorization_question_semantic(text: str) -> str | None:
    sponsorship = bool(re.search(r"sponsor|sponsorship", text))
    authorization = bool(re.search(
        r"(?:authori[sz]ed|legal(?:ly)? (?:eligible|entitled)|right) to work",
        text,
    ))
    without_sponsorship = bool(re.search(
        r"\b(?:(?:be\s+)?(?:able|eligible|authori[sz]ed|entitled)\s+to\s+|"
        r"(?:can|could|may|will|would)\s+you\s+(?:legally\s+)?)work\s+"
        r"without\s+(?:(?:requiring|the\s+need\s+for)\s+)?"
        r"(?:visa\s+)?sponsor(?:ship)?\b",
        text,
    ))
    if without_sponsorship:
        return "work_without_sponsorship"
    if sponsorship and authorization:
        return "ambiguous"
    if sponsorship:
        return "requires_sponsorship"
    if authorization:
        return "legally_authorized_to_work"
    return None


def _work_authorization_answers(
    profile: dict,
    job: dict,
    *,
    question: object = "",
) -> tuple[bool, bool] | None:
    """Return (authorized, sponsorship-needed) for a clearly classified role."""
    policy = profile.get("work_authorization", {}).get("form_answer_policy", {})
    question_text = " ".join(str(question or "").casefold().split())
    post_graduation_context = _is_post_graduation_work_context(question_text)
    job_text = " ".join(
        str(job.get(field) or "").casefold()
        for field in ("title", "full_description", "application_readiness_reason")
    )
    branch = None
    if post_graduation_context:
        branch = policy.get("post_graduation_full_time")
    elif "intern" in job_text:
        explicit_non_qualifying_route = re.search(
            r"\bpart[ -]?time\b|\bnon[ -]?credit\b|"
            r"\bnot\s+(?:eligible\s+)?for\s+academic\s+credit\b|"
            r"\bnot\s+credit[ -]?bearing\b|\bno\s+academic\s+credit\b",
            job_text,
        )
        if explicit_non_qualifying_route:
            return None
        branch = policy.get("programme_credit_bearing_internship")
    elif any(term in job_text for term in ("full-time", "full time", "permanent")):
        branch = policy.get("post_graduation_full_time")
    if not isinstance(branch, dict):
        return None
    authorized = _yes_no_value(branch.get("legally_authorized"))
    sponsorship = _yes_no_value(branch.get("requires_sponsorship"))
    if authorized is None or sponsorship is None:
        return None
    return authorized, sponsorship


def _is_prior_target_employer_question(text: str, job: dict) -> bool:
    """Identify employer-history questions without mistaking skill experience for one."""
    history_language = bool(
        (
            re.search(r"\b(?:ever|previously|formerly|prior|before)\b", text)
            and re.search(r"\b(?:work(?:ed)?|employ(?:ed|ee|ment)?)\b", text)
        )
        or re.search(r"\bformer\s+employee\b", text)
    )
    if not history_language:
        return False

    employer_scope = bool(
        re.search(
            r"\b(?:for|with|by|at)\s+(?:us|this company|the company|our company|"
            r"this organization|the organization|our organization)\b|"
            r"\bour\s+(?:affiliate|affiliates|subsidiary|subsidiaries)\b|"
            r"\bhere\s+before\b",
            text,
        )
    )
    company = re.sub(
        r"[^a-z0-9]+", " ", str(job.get("company_name") or "").casefold()
    ).strip()
    if company:
        company_aliases = {company}
        without_suffix = re.sub(
            r"\s+(?:pte\s+ltd|private\s+limited|limited|ltd|inc|corp|corporation|llc)$",
            "",
            company,
        ).strip()
        if without_suffix:
            company_aliases.add(without_suffix)
        normalized_question = re.sub(r"[^a-z0-9]+", " ", text).strip()
        employer_scope = employer_scope or any(
            len(alias) >= 3
            and re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", normalized_question)
            for alias in company_aliases
        )
    return employer_scope


def _expected_screening_answer(
    question: object, profile: dict, job: dict
) -> tuple[str, bool] | None:
    """Map common legal/screening questions to confirmed, contextual facts."""
    text = " ".join(str(question or "").casefold().split())
    if not text:
        return None

    if re.search(r"\bf[\s-]?1\b|\bcpt\b|\bopt\b", text):
        expected = _yes_no_value(_application_fact_value(profile, "f1_student_status"))
        return ("f1_student_status", expected) if expected is not None else None
    if re.search(r"\bu\.?s\.? person\b|\bunited states person\b", text):
        expected = _yes_no_value(
            _application_fact_value(profile, "united_states_person_status")
        )
        return ("united_states_person_status", expected) if expected is not None else None

    semantic = _work_authorization_question_semantic(text)
    work_answers = _work_authorization_answers(profile, job, question=text)
    if semantic == "work_without_sponsorship" and work_answers is not None:
        return semantic, work_answers[0] and not work_answers[1]
    if semantic == "requires_sponsorship" and work_answers is not None:
        return "requires_sponsorship", work_answers[1]
    if semantic == "legally_authorized_to_work" and work_answers is not None:
        return "legally_authorized_to_work", work_answers[0]

    if _is_prior_target_employer_question(text, job):
        value = _application_fact_value(
            profile, "prior_target_employer_history_policy"
        ) or profile.get("screening", {}).get("previously_worked_for_target_employer")
        expected = _yes_no_value(value)
        return (
            ("previously_worked_for_target_employer", expected)
            if expected is not None
            else None
        )

    if re.search(r"non[ -]?compete|non[ -]?solicitation|contractual .*restrict|legal .*restrict", text):
        value = _application_fact_value(
            profile, "employment_or_non_compete_restrictions"
        ) or profile.get("screening", {}).get("employment_or_non_compete_restrictions")
        expected = _yes_no_value(value)
        return ("employment_or_non_compete_restrictions", expected) if expected is not None else None
    if re.search(r"criminal|convict", text):
        value = _application_fact_value(
            profile, "criminal_convictions_to_disclose"
        ) or profile.get("screening", {}).get("criminal_convictions_to_disclose")
        expected = _yes_no_value(value)
        return ("criminal_convictions_to_disclose", expected) if expected is not None else None
    if "background check" in text:
        expected = _yes_no_value(
            profile.get("screening", {}).get("willing_to_complete_background_check")
        )
        return ("background_check", expected) if expected is not None else None
    return None


def _screening_answer_issue(
    question: object,
    selected: object,
    profile: dict,
    job: dict,
) -> str | None:
    """Return a stable blocker for an unsupported or contradictory answer."""
    text = " ".join(str(question or "").casefold().split())
    semantic = _work_authorization_question_semantic(text)
    post_graduation = _is_post_graduation_work_context(text)
    if semantic == "ambiguous":
        context = "post_graduation" if post_graduation else "role_context"
        return f"ambiguous_work_authorization_question:{context}"
    if (
        post_graduation
        and semantic is not None
        and _work_authorization_answers(profile, job, question=text) is None
    ):
        return "work_authorization_policy_unavailable:post_graduation_full_time"

    expected = _expected_screening_answer(text, profile, job)
    if expected is None:
        return None
    key, value = expected
    if not _selected_matches_boolean(selected, value):
        return f"hard_answer_mismatch:{key}"
    return None


def _combined_legal_category_issue(
    question: object, selected: object, profile: dict, job: dict
) -> str | None:
    """Require an exact fact for ATS selectors that collapse distinct legal states."""
    text = " ".join(str(question or "").casefold().split())
    answer = " ".join(str(selected or "").casefold().split())
    if not answer or "citizenship" not in text or not re.search(
        r"\b(?:visa|work pass|permit|status)\b", text
    ):
        return None

    exact = _application_fact_value(profile, "citizenship_visa_status_option")
    if exact is not None and " ".join(str(exact).casefold().split()) == answer:
        return None

    work_auth = profile.get("work_authorization", {})
    if re.fullmatch(r"(?:singapore )?citizen", answer):
        expected = _yes_no_value(work_auth.get("singapore_citizen"))
        return None if expected is True else "hard_answer_mismatch:singapore_citizen"
    if "permanent resident" in answer:
        expected = _yes_no_value(work_auth.get("singapore_permanent_resident"))
        return (
            None
            if expected is True
            else "hard_answer_mismatch:singapore_permanent_resident"
        )
    if re.search(r"\brequir(?:e|es|ed|ing)\b.*\b(?:visa|pass|permit)\b", answer):
        work_answers = _work_authorization_answers(profile, job)
        if work_answers is not None:
            return (
                None
                if work_answers[1]
                else "hard_answer_mismatch:requires_sponsorship"
            )

    # Some ATS selectors collapse an existing conditional work status into a
    # generic "possess work visa/pass" bucket. When the role-specific profile
    # branch confirms both current authorization and no sponsorship need, that
    # is the closest non-contradictory category and may proceed with an audit
    # record. Unknown or contradictory branches still stop here.
    if re.search(r"\b(?:possess|hold|have)\b.*\b(?:visa|pass|permit)\b", answer):
        work_answers = _work_authorization_answers(profile, job)
        if work_answers == (True, False):
            return None

    return "ambiguous_legal_category:citizenship_visa_status"


def _collect_lossy_answer_mappings(snapshot: dict, profile: dict, job: dict) -> list[dict]:
    """Return compact audit records for accepted non-exact ATS categories."""
    mappings: list[dict] = []
    expected_education = [
        item for item in profile.get("education", []) if isinstance(item, dict)
    ]
    for observed in snapshot.get("education_entries", []):
        if not isinstance(observed, dict):
            continue
        selected = observed.get("degree")
        institution = observed.get("institution")
        expected = next(
            (
                item
                for item in expected_education
                if _same_institution(institution, item.get("institution"))
            ),
            None,
        )
        if not expected or not selected:
            continue
        selected_text = _normalize_credential_text(selected)
        expected_text = _normalize_credential_text(expected.get("degree"))
        if (
            selected_text
            and selected_text != expected_text
            and _degree_level(selected_text) == _degree_level(expected_text)
            and _degree_value_supported(selected, expected.get("degree"))
        ):
            mappings.append({
                "field_semantic": "education_degree",
                "relation": "same_level_taxonomy",
                "selected_option": str(selected)[:120],
                "confirmed_level": _degree_level(expected_text),
            })

    questions = list(snapshot.get("radio_questions", [])) + list(
        snapshot.get("select_fields", [])
    )
    for field in questions:
        if not isinstance(field, dict):
            continue
        question = str(field.get("text") or "")
        selected = str(field.get("selected") or "")
        normalized_question = " ".join(question.casefold().split())
        if (
            "citizenship" in normalized_question
            and re.search(r"\b(?:visa|work pass|permit|status)\b", normalized_question)
            and re.search(
                r"\b(?:possess|hold|have)\b.*\b(?:visa|pass|permit)\b",
                selected.casefold(),
            )
            and _work_authorization_answers(profile, job) == (True, False)
        ):
            mappings.append({
                "field_semantic": "citizenship_visa_status",
                "relation": "closest_non_contradictory_category",
                "selected_option": selected[:120],
                "confirmed_authorized": True,
                "confirmed_sponsorship_required": False,
            })
        if (
            _is_generic_education_level_question(question)
            and selected
            and (confirmed_level := _highest_confirmed_degree_level(expected_education))
            and _degree_level(selected) == confirmed_level
        ):
            exact = _application_fact_value(profile, "highest_education_level_option")
            if not exact or _normalize_credential_text(selected) != _normalize_credential_text(exact):
                mappings.append({
                    "field_semantic": "highest_education_level",
                    "relation": "same_level_taxonomy",
                    "selected_option": selected[:120],
                    "confirmed_level": confirmed_level,
                })

    raw_location_fields = snapshot.get("current_location_fields")
    if isinstance(raw_location_fields, list):
        for field in raw_location_fields:
            if not isinstance(field, dict):
                continue
            selected = str(field.get("value") or "").strip()
            if selected and _is_singapore_location(selected) and selected.casefold() != "singapore":
                mappings.append({
                    "field_semantic": "current_location",
                    "relation": "normalized_location_alias",
                    "selected_option": selected[:120],
                    "confirmed_location": "Singapore",
                })
    for field in snapshot.get("select_fields", []):
        if not isinstance(field, dict):
            continue
        question = str(field.get("text") or "").casefold()
        selected = str(field.get("selected") or "").strip()
        if (
            "currently based" in question
            and "legal right to work" in question
            and _is_singapore_location(selected)
            and selected.casefold() != "singapore"
        ):
            mappings.append({
                "field_semantic": "work_location",
                "relation": "normalized_location_alias",
                "selected_option": selected[:120],
                "confirmed_location": "Singapore",
            })
    unique: list[dict] = []
    seen: set[str] = set()
    for mapping in mappings:
        key = json.dumps(mapping, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            unique.append(mapping)
    return unique


def _partition_pre_submit_issues(
    issues: list[str],
) -> tuple[list[str], list[str], list[str]]:
    """Split direct blockers from one-turn repairs and non-blocking uncertainty."""
    advisory_exact = {"education_state_unconfirmed"}
    repair_prefixes = (
        "required_field_empty:",
        "answer_provenance_missing:",
        "answer_provenance_binding_mismatch",
        "resume_state_unconfirmed",
        "resume_not_uploaded",
        "submit_control_missing",
        "workday_stuck",
    )
    blockers: list[str] = []
    repairable: list[str] = []
    advisories: list[str] = []
    for issue in dict.fromkeys(issues):
        if issue in advisory_exact:
            advisories.append(issue)
        elif issue.startswith(repair_prefixes):
            repairable.append(issue)
        else:
            blockers.append(issue)
    return blockers, repairable, advisories


def _visible_captcha_overlay(page) -> bool:
    """Detect a user-visible CAPTCHA challenge without reading its contents."""
    for iframe in page.locator("iframe").all():
        try:
            title = (iframe.get_attribute("title") or "").casefold()
            source = (iframe.get_attribute("src") or "").casefold()
            if not iframe.is_visible():
                continue
            box = iframe.bounding_box()
            if (
                box
                and box["width"] >= 200
                and box["height"] >= 150
                and ("captcha" in title or "captcha" in source)
            ):
                return True
        except Exception:
            logger.debug("Unable to inspect a CAPTCHA iframe", exc_info=True)
            continue
    return False


def _captcha_response_present(page) -> bool:
    """Return true after the applicant has produced a CAPTCHA response token."""
    selector = (
        'textarea[name*="captcha" i], textarea[name*="recaptcha" i], '
        'input[name*="captcha" i], input[name*="recaptcha" i]'
    )
    try:
        response_fields = page.locator(selector).all()
    except Exception:
        logger.debug("Unable to enumerate CAPTCHA response fields", exc_info=True)
        return False
    for field in response_fields:
        try:
            if (field.input_value(timeout=500) or "").strip():
                return True
        except Exception:
            logger.debug("Unable to read a CAPTCHA response field", exc_info=True)
            continue
    return False


def _visible_verification_gate(page) -> bool:
    """Detect a visible CAPTCHA or email/OTP gate without reading its value."""
    if _visible_captcha_overlay(page):
        return True
    try:
        return bool(page.evaluate(
            r"""() => {
              const visible = (el) => {
                const style = getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden' &&
                  el.getClientRects().length > 0;
              };
              const verification = /security code|verification code|one[- ]time (?:code|password)|\botp\b|verify (?:your )?email|email verification|验证码|校验码|验证邮箱/i;
              const inputs = [...document.querySelectorAll('input')].filter(visible);
              const codeInputs = inputs.filter((el) => {
                const maxLength = Number(el.maxLength || 0);
                return maxLength === 1 || /otp|verification|security.?code/i.test(
                  `${el.name || ''} ${el.id || ''} ${el.autocomplete || ''}`
                );
              });
              if (codeInputs.length < 4) return false;
              return [...document.querySelectorAll('form,section,dialog,[role="dialog"]')]
                .filter(visible).some((el) => verification.test(el.innerText || ''));
            }"""
        ))
    except Exception:
        logger.debug("Unable to inspect a verification gate", exc_info=True)
        return False


def _verification_clear_state_stable(page) -> bool:
    """Require a normal form or receipt after a gate disappears, not a blank page."""
    try:
        return bool(
            page.evaluate(
                r"""() => {
                  if (!document.body || location.href === 'about:blank') return false;
                  const visible = (el) => {
                    const style = getComputedStyle(el);
                    return style.display !== 'none' && style.visibility !== 'hidden' &&
                      el.getClientRects().length > 0;
                  };
                  const text = (document.body.innerText || '').replace(/\s+/g, ' ').trim();
                  const receipt = /application (?:was |has been )?(?:successfully )?(?:submitted|received)|thank you for (?:applying|submitting your application)|申请已提交|投递成功|申请成功/i.test(text);
                  const ordinaryForm = [...document.querySelectorAll('form,input,select,textarea,button')]
                    .some(visible);
                  return text.length >= 40 && (ordinaryForm || receipt);
                }"""
            )
        )
    except Exception:
        logger.debug("Unable to confirm stable post-verification state", exc_info=True)
        return False


def _validate_pre_submit_snapshot(snapshot: dict, profile: dict, job: dict) -> list[str]:
    """Return browser-observed attention signals for the next agent turn."""
    issues: list[str] = []
    expected_url = job.get("application_url") or job.get("url") or ""
    actual_url = snapshot.get("url", "")
    if expected_url and actual_url and not _same_bound_application_flow(
        expected_url,
        actual_url,
        snapshot,
        job.get("_ats_application_binding"),
    ):
        issues.append("unexpected_application_url")

    if snapshot.get("captcha_visible"):
        issues.append("visible_captcha")
    if snapshot.get("verification_visible"):
        issues.append("verification_required")

    issues.extend(
        f"required_field_empty:{label[:80]}"
        for label in snapshot.get("required_unfilled", [])
    )

    for raw_label in snapshot.get("sensitive_required_unknown", []):
        label = str(raw_label or "")[:80]
        requirement = classify_identity_requirement(label)
        if requirement.kind == "ordinary_fact":
            issues.append(f"required_field_empty:confirmed_identity_fact:{label}")
        elif requirement.kind == "protected_identifier":
            issues.append(f"protected_identifier_source_required:{label}")
        else:
            issues.append(f"sensitive_required_unknown:{label}")

    for raw_field in snapshot.get("file_fields", []):
        if not isinstance(raw_field, Mapping) or raw_field.get("required") is not True:
            continue
        label = str(raw_field.get("text") or "")[:120]
        requirement = classify_identity_requirement(label, field_type="file_upload")
        if requirement.kind == "not_identity":
            continue
        uploaded = int(raw_field.get("count") or 0) > 0
        authorized = _authorized_identity_material(job, label)
        if requirement.kind in {"biometric_or_media", "financial_identity"}:
            issues.append(f"unsafe_identity_material_request:{requirement.kind}:{label}")
        elif requirement.kind == "document_artifact" and not uploaded:
            if authorized:
                issues.append(f"required_field_empty:authorized_identity_material:{label}")
            else:
                issues.append(f"identity_material_missing:{label}")
        elif requirement.kind == "document_artifact" and not authorized:
            issues.append(f"identity_material_authorization_unconfirmed:{label}")

    if snapshot.get("assessment_visible"):
        issues.append("assessment_present")

    if "resume_field_present" in snapshot:
        if not snapshot.get("resume_field_present"):
            if not (
                _verified_agent_resume_upload(job)
                and int(snapshot.get("submit_control_count") or 0) > 0
            ):
                issues.append("resume_state_unconfirmed")
        elif not snapshot.get("resume_uploaded"):
            agent_upload_confirmed = (
                _verified_agent_resume_upload(job)
                and int(snapshot.get("submit_control_count") or 0) > 0
            )
            if not agent_upload_confirmed:
                resume_cards = [
                    str(value or "") for value in snapshot.get("resume_card_texts", [])
                ]
                expected_variant = prompt_mod._linkedin_resume_preference(profile, job)
                expected_text = str(expected_variant or "").casefold()
                existing_document_confirmed = any(
                    re.search(r"\.(?:pdf|docx?)\b", text, re.IGNORECASE)
                    and (not expected_text or expected_text in text.casefold())
                    for text in resume_cards
                )
                if not existing_document_confirmed:
                    issues.append("resume_not_uploaded")

    personal = profile.get("personal", {})
    legal_name = personal.get("full_name", "").strip().casefold()
    for value in snapshot.get("full_name_values", []):
        if legal_name and value.strip().casefold() != legal_name:
            issues.append("legal_name_mismatch")
            break

    expected_email = personal.get("email", "").strip().casefold()
    for value in snapshot.get("email_values", []):
        if expected_email and value.strip().casefold() != expected_email:
            issues.append("email_mismatch")
            break

    raw_location_fields = snapshot.get("current_location_fields")
    location_fields = (
        raw_location_fields
        if isinstance(raw_location_fields, list)
        else [
            {"value": value, "required": True}
            for value in snapshot.get("current_location_values", [])
        ]
    )
    for field in location_fields:
        if not isinstance(field, dict):
            continue
        value = str(field.get("value") or "").strip()
        if not value and not _field_required(field):
            continue
        if not value or not _is_singapore_location(value):
            issues.append("current_location_not_singapore")
            break

    expected_education = [
        item for item in profile.get("education", []) if isinstance(item, dict)
    ]
    explicit_education_level_option = _application_fact_value(
        profile, "highest_education_level_option"
    )
    expected_education_level = _highest_confirmed_degree_level(expected_education)
    observed_education = snapshot.get("education_entries", [])
    verifiable_education_count = 0
    for observed in observed_education:
        if not isinstance(observed, dict):
            continue
        institution = str(observed.get("institution") or "").strip()
        selected_degree = observed.get("degree")
        expected = next(
            (
                item
                for item in expected_education
                if _same_institution(institution, item.get("institution"))
            ),
            None,
        )
        if expected is None or not selected_degree:
            continue
        verifiable_education_count += 1
        explicit = _contextual_application_fact_value(
            profile, "education_degree_option", institution
        )
        if not _degree_value_supported(
            selected_degree,
            expected.get("degree"),
            explicit_option=explicit,
        ):
            issues.append("education_degree_mismatch")
    if snapshot.get("education_field_present") and expected_education:
        try:
            visible_education_count = max(
                1, int(snapshot.get("education_record_count") or 0)
            )
        except (TypeError, ValueError):
            visible_education_count = 1
        if verifiable_education_count < visible_education_count:
            issues.append("education_state_unconfirmed")

    screening = profile.get("screening", {})
    hard_answers = {
        "startup_internship": screening.get(
            "prior_internship_product_startup_logistics_ecommerce_b2b_saas"
        ),
    }
    for question in snapshot.get("radio_questions", []):
        text = question.get("text", "").casefold()
        selected = question.get("selected", "").strip().casefold()
        if not selected and not _field_required(question):
            continue
        legal_issue = _combined_legal_category_issue(text, selected, profile, job)
        if legal_issue:
            issues.append(legal_issue)
        expected: bool | None = None
        key = ""
        if (
            "prior internship" in text
            and "product-based startup" in text
            and any(term in text for term in ("logistics", "ecommerce", "b2b saas"))
        ):
            key = "startup_internship"
            expected = hard_answers[key]
        if selected and expected is not None and not _selected_matches_boolean(selected, expected):
            issues.append(f"hard_answer_mismatch:{key}")

        screening_issue = _screening_answer_issue(text, selected, profile, job)
        if screening_issue:
            issues.append(screening_issue)

    for field in snapshot.get("select_fields", []):
        text = field.get("text", "").casefold()
        selected = field.get("selected", "").strip().casefold()
        if not selected and not _field_required(field):
            continue
        if _is_generic_education_level_question(text):
            if explicit_education_level_option is not None:
                exact_match = _normalize_credential_text(selected) == _normalize_credential_text(
                    explicit_education_level_option
                )
                same_level = bool(
                    selected
                    and expected_education_level
                    and _degree_level(selected) == expected_education_level
                )
                if not exact_match and not same_level:
                    issues.append("education_level_mismatch")
            elif (
                expected_education_level
                and _degree_level(selected) != expected_education_level
            ):
                issues.append("education_level_mismatch")
        legal_issue = _combined_legal_category_issue(text, selected, profile, job)
        if legal_issue:
            issues.append(legal_issue)
        if (
            selected
            and "currently based" in text
            and "legal right to work" in text
            and not _is_singapore_location(selected)
        ):
            issues.append("work_location_selection_not_singapore")
        screening_issue = _screening_answer_issue(text, selected, profile, job)
        if screening_issue:
            issues.append(screening_issue)

    if snapshot.get("submit_control_count", 0) < 1:
        issues.append("submit_control_missing")
    return list(dict.fromkeys(issues))


def _adapter_observation_context(
    snapshot: dict, job: dict
) -> tuple[dict[str, object], dict[str, object] | None, list[str]]:
    """Convert one live structural snapshot into bounded adapter decisions."""
    form = ats_mod.build_form_ir(
        str(snapshot.get("url") or job.get("application_url") or job.get("url") or ""),
        snapshot.get("form_fields") or (),
    )
    ats_context = ats_mod.adapter_prompt_context(form)
    raw_fields = snapshot.get("form_fields")
    raw_by_key = {
        str(item.get("field_key") or ""): item
        for item in raw_fields
        if isinstance(item, Mapping) and str(item.get("field_key") or "")
    } if isinstance(raw_fields, list) else {}
    context_fields = ats_context.get("fields")
    if isinstance(context_fields, list):
        for context_field in context_fields:
            if not isinstance(context_field, dict):
                continue
            raw_field = raw_by_key.get(str(context_field.get("field_key") or ""))
            if not isinstance(raw_field, Mapping):
                continue
            options = raw_field.get("options")
            option_values = options if isinstance(options, list) else []
            source_count = raw_field.get("option_count")
            if not isinstance(source_count, int) or isinstance(source_count, bool):
                source_count = len(option_values)
            source_truncated = raw_field.get("options_truncated") is True or source_count > len(
                option_values
            )
            context_field["options_source_count"] = source_count
            context_field["options_source_truncated"] = source_truncated
            if not source_truncated:
                context_field["options_full_sha256"] = hashlib.sha256(
                    json.dumps(
                        option_values,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
    workday_context: dict[str, object] | None = None
    issues: list[str] = []
    if form.adapter != "workday" or not isinstance(
        snapshot.get("workday_observation"), dict
    ):
        return ats_context, workday_context, issues

    observation = observation_from_mapping(snapshot["workday_observation"])
    previous_signature = None
    repair_used = False
    previous_context = job.get("_browser_observation")
    if isinstance(previous_context, dict):
        previous_workday = previous_context.get("workday_state")
        if isinstance(previous_workday, dict):
            previous_signature = str(previous_workday.get("signature") or "") or None
            repair_used = bool(previous_workday.get("repair_used", False))
    decision = evaluate_page_progress(
        previous_signature,
        observation,
        repair_used=repair_used,
        submit_started=False,
    )
    workday_context = {
        "state": decision.state.value,
        "action": decision.action.value,
        "signature": decision.signature,
        "repeated": decision.repeated,
        "repair_used": decision.repair_used,
        "runtime_switch_allowed": decision.runtime_switch_allowed,
    }
    if decision.action is ProgressAction.STOP_STUCK:
        issues.append("workday_stuck")
    elif decision.action is ProgressAction.STOP_MANUAL:
        issues.append("workday_manual_gate")
    return ats_context, workday_context, issues


def _build_ats_fill_plan_snapshot(
    snapshot: Mapping[str, object],
    job: Mapping[str, object],
) -> dict[str, object] | None:
    """Build a launcher-owned structural snapshot for one repair-only turn."""
    target_url = str(snapshot.get("url") or "").strip()
    parsed = urlparse(target_url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme.casefold() != "https" or host == "linkedin.com" or host.endswith(
        ".linkedin.com"
    ):
        return None
    raw_fields = snapshot.get("form_fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        return None
    allowed_field_keys = {
        "field_key",
        "id",
        "name",
        "selector",
        "label",
        "aria_label",
        "type",
        "tag",
        "control",
        "autocomplete",
        "placeholder",
        "required",
        "disabled",
        "readonly",
        "options",
        "minlength",
        "maxlength",
        "min",
        "max",
        "pattern",
        "multiple",
    }
    projected_fields = [
        {key: value for key, value in raw.items() if key in allowed_field_keys}
        for raw in raw_fields
        if isinstance(raw, Mapping)
    ]
    if not projected_fields:
        return None
    ats_context = job.get("_ats_adapter_context")
    facts = (
        ats_context.get("available_fact_names", [])
        if isinstance(ats_context, Mapping)
        else []
    )
    if not isinstance(facts, list):
        facts = []
    return freeze_ats_fill_plan_snapshot(
        {
            "schema_version": ATS_FORM_SNAPSHOT_SCHEMA_VERSION,
            "target_url": target_url,
            "form_fields": projected_fields,
            "available_fact_names": facts,
        }
    )


_PROTECTED_IDENTIFIER_LABEL_RE = re.compile(
    r"(?:^|\b)(?:nric\s*(?:/|or)\s*fin|nric|fin)"
    r"(?:\s+(?:identification\s+)?(?:no\.?|number))?(?:\b|$)|"
    r"passport\s+(?:no\.?|number)|"
    r"national\s+id(?:entification)?\s+(?:no\.?|number)",
    re.IGNORECASE,
)


def _redact_protected_identifier_snapshot(snapshot: dict[str, object]) -> None:
    """Remove protected identifier values before validation or durable projection."""
    for collection_name in (
        "text_fields",
        "select_fields",
        "radio_questions",
        "current_location_fields",
    ):
        collection = snapshot.get(collection_name)
        if not isinstance(collection, list):
            continue
        for raw_field in collection:
            if not isinstance(raw_field, dict):
                continue
            descriptor = " ".join(
                str(raw_field.get(key) or "")
                for key in ("text", "label", "name", "id", "placeholder", "aria_label")
            )
            explicitly_protected = raw_field.get("protected_identifier") is True
            if not explicitly_protected and not _PROTECTED_IDENTIFIER_LABEL_RE.search(
                descriptor
            ):
                continue
            for value_key in ("value", "selected"):
                if value_key not in raw_field:
                    continue
                present = bool(str(raw_field.get(value_key) or "").strip())
                raw_field[value_key] = "[redacted-present]" if present else ""
                raw_field["value_present"] = present
            raw_field["protected_identifier"] = True


def _audit_live_pre_submit_page(
    port: int, worker_id: int, job: dict
) -> tuple[str | None, dict]:
    """Observe the visible form without changing it or deciding whether to proceed."""
    from playwright.sync_api import sync_playwright

    profile = config.load_profile()
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        pages = [page for context in browser.contexts for page in context.pages]
        pages = _bound_application_pages(browser, pages, job)
        if not pages:
            return "pre_submit_audit:no_bound_application_page", {}
        page, application_surface = _select_application_page_and_frame(pages)
        page.bring_to_front()
        expected_education = [
            {
                "institution": str(item.get("institution") or "").strip(),
                "degree": str(item.get("degree") or "").strip(),
            }
            for item in profile.get("education", [])
            if isinstance(item, dict) and str(item.get("institution") or "").strip()
        ]
        snapshot = application_surface.evaluate(
            r"""(expectedEducation) => {
              const visible = (el) => {
                const style = getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden' &&
                  el.getClientRects().length > 0 && !el.disabled;
              };
              const deepRoots = [document];
              const deepElements = [];
              for (let index = 0; index < deepRoots.length; index += 1) {
                const elements = [...deepRoots[index].querySelectorAll('*')];
                deepElements.push(...elements);
                for (const element of elements) {
                  if (element.shadowRoot) deepRoots.push(element.shadowRoot);
                }
              }
              const deepAll = (selector) => deepElements.filter(
                (element) => element.matches(selector)
              );
              const context = (el) => el.closest(
                'li, fieldset, [data-qa*="field"], [class*="application-field"], [class*="form-item"], [class*="question"]'
              ) || el.parentElement;
              const labelText = (el) => {
                const node = context(el);
                const associated = [...(el.labels || [])].find(visible);
                const wrapping = el.closest('label');
                const usefulText = (value) => {
                  const normalized = String(value || '').replace(/\s+/g, ' ').trim();
                  return Boolean(normalized.replace(/[✱*]/g, '').trim()) &&
                    !['on', 'true', 'false'].includes(normalized.toLowerCase());
                };
                let ancestorText = '';
                for (let ancestor = el.parentElement, depth = 0;
                  ancestor && ancestor !== document.body && depth < 6;
                  ancestor = ancestor.parentElement, depth += 1) {
                  const candidate = String(ancestor.innerText || ancestor.textContent || '')
                    .replace(/\s+/g, ' ').trim();
                  if (usefulText(candidate) && candidate.length <= 500) {
                    ancestorText = candidate;
                    break;
                  }
                }
                const direct = (
                  (associated && associated.innerText) ||
                  el.getAttribute('aria-label') ||
                  (wrapping && wrapping.innerText) ||
                  (node && node.innerText) ||
                  el.name || ''
                ).replace(/\s+/g, ' ').trim();
                if (
                  location.protocol === 'https:' &&
                  location.hostname.toLowerCase() === 'jobs.smartrecruiters.com' &&
                  !usefulText(direct)
                ) {
                  return (ancestorText || direct).slice(0, 500);
                }
                return (direct || ancestorText).slice(0, 500);
              };
              const required = (el) => el.required || el.getAttribute('aria-required') === 'true' || /[✱*]/.test(labelText(el));
              const responseSelector =
                'textarea[name*="captcha" i],textarea[name*="recaptcha" i],input[name*="captcha" i],input[name*="recaptcha" i]';
              const responseFields = deepAll(responseSelector);
              const inputs = deepAll(
                'input:not([type=hidden]):not([type=radio]):not([type=checkbox]):not([type=file]):not([type=submit]):not([type=button]), textarea, select'
              ).filter((el) => visible(el) && !el.matches(responseSelector));
              const requiredUnfilled = [];
              const sensitiveRequiredUnknown = [];
              const fullNameValues = [];
              const emailValues = [];
               const currentLocationValues = [];
               const currentLocationFields = [];
              const selectFields = [];
              const textFields = [];
              const provenanceFields = [];
              const protectedIdentifier = (el, text) => {
                if (el.hasAttribute('data-applypilot-protected')) return true;
                if (/\b(?:first|last|full|legal|preferred)\s+name\b/i.test(text)) return false;
                const descriptor = [
                  text,
                  el.getAttribute('aria-label'),
                  el.getAttribute('name'),
                  el.getAttribute('id'),
                  el.getAttribute('placeholder')
                ].filter(Boolean).join(' ');
                return /(?:^|\b)(?:nric\s*(?:\/|or)\s*fin|nric|fin)(?:\s+(?:identification\s+)?(?:no\.?|number))?(?:\b|$)|passport\s+(?:no\.?|number)|national\s+id(?:entification)?\s+(?:no\.?|number)/i.test(descriptor);
              };
              for (const el of inputs) {
                const text = labelText(el);
                const value = el.tagName === 'SELECT'
                  ? (el.selectedOptions[0] ? el.selectedOptions[0].textContent.trim() : '')
                  : (el.value || '').trim();
                if (required(el) && (!value || /^(select|choose)(\.\.\.)?$/i.test(value))) {
                  requiredUnfilled.push(text);
                }
                if (
                  required(el) &&
                  /work (authorization|authorisation)|right to work|visa|sponsorship|citizenship|legal identity|passport|national id/i.test(text) &&
                  (!value || /^(select|choose|unknown|not sure|prefer not)(\.\.\.)?$/i.test(value))
                ) sensitiveRequiredUnknown.push(text);
                if (/\b(full|legal) name\b/i.test(text) && !/preferred|display/i.test(text)) {
                  fullNameValues.push(value);
                }
                if (el.type === 'email' || /\bemail(?: address)?\b/i.test(text)) emailValues.push(value);
                 if (/current location/i.test(text)) {
                   currentLocationValues.push(value);
                   currentLocationFields.push({text, value, required: required(el)});
                 }
                 if (el.tagName === 'SELECT') selectFields.push({
                   field_key: String(el.id || el.name || `select-${selectFields.length + 1}`).slice(0, 160),
                   text,
                   selected: value,
                   required: required(el),
                   options: [...el.options].slice(0, 100).map((option) =>
                     String(option.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120)
                   ),
                   option_count: el.options.length,
                   options_truncated: el.options.length > 100
                 });
                 else {
                   const sensitiveValue = protectedIdentifier(el, text);
                  textFields.push({
                    field_key: String(el.id || el.name || `text-${textFields.length + 1}`).slice(0, 160),
                    control: el.tagName === 'TEXTAREA' ? 'textarea' : String(el.type || 'text').toLowerCase(),
                    text,
                    value: sensitiveValue ? (value ? '[redacted-present]' : '') : value,
                    value_present: Boolean(value),
                    required: required(el),
                    protected_identifier: sensitiveValue
                  });
                }
                if (value) provenanceFields.push({
                  field_key: String(el.id || el.name || `control-${provenanceFields.length + 1}`).slice(0, 160),
                  control: el.tagName === 'SELECT'
                    ? 'select'
                    : el.tagName === 'TEXTAREA' ? 'textarea' : String(el.type || 'text').toLowerCase(),
                  text,
                  selected: protectedIdentifier(el, text) ? '[redacted-present]' : value,
                  required: required(el),
                  protected_identifier: protectedIdentifier(el, text),
                   options: el.tagName === 'SELECT'
                    ? [...el.options].slice(0, 100).map((option) =>
                        String(option.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120)
                      )
                    : [],
                  option_count: el.tagName === 'SELECT' ? el.options.length : 0,
                  options_truncated: el.tagName === 'SELECT' && el.options.length > 100
                });
              }
              const nearbyUploadText = (el) => {
                let node = el;
                for (let depth = 0; node && node !== document.body && depth < 7; depth += 1) {
                  const text = (node.innerText || '').replace(/\s+/g, ' ').trim();
                  if (/\.(?:pdf|docx?)\b|uploaded|replace|remove|download/i.test(text)) return text;
                  node = node.parentElement;
                }
                return '';
              };
              const fileFields = deepAll('input[type=file]')
                .map((el) => ({
                  text: labelText(el),
                  nearby_text: nearbyUploadText(el),
                  count: el.files ? el.files.length : 0,
                  required: required(el)
                }));
              const resumeFields = fileFields.filter((f) => /\bresume\b|\bcv\b/i.test(f.text));
              const attributedResumeCards = deepAll(
                '[data-qa*="resume" i],[data-testid*="resume" i],[class*="resume" i],[aria-label*="resume" i],[aria-label*="cv" i]'
              ).filter(visible).map((el) => (el.innerText || el.getAttribute('aria-label') || '').trim());
              const reviewResumeCards = deepAll('h1,h2,h3,h4')
                .filter((el) => visible(el) && /^(resume|cv)$/i.test((el.innerText || '').trim()))
                .map((heading) => {
                  let node = heading.parentElement;
                  for (let depth = 0; node && node !== document.body && depth < 5; depth += 1) {
                    const text = (node.innerText || '').replace(/\s+/g, ' ').trim();
                    if (/\.(?:pdf|docx?)\b/i.test(text)) return text;
                    node = node.parentElement;
                  }
                  return '';
                }).filter(Boolean);
              const resumeCards = [...attributedResumeCards, ...reviewResumeCards];
              const resumeUploaded = resumeFields.some((f) =>
                f.count > 0 || /success|uploaded|replace|remove|\.(?:pdf|docx?)/i.test(
                  `${f.text} ${f.nearby_text}`
                )
              ) || resumeCards.some((text) => /\b[^\s]+\.(?:pdf|docx?)\b|uploaded|replace|remove|download/i.test(text));
              const radios = deepAll('input[type=radio]').filter(visible);
              const seen = new Set();
              const radioQuestions = [];
              for (const radio of radios) {
                const node = context(radio);
                const key = radio.name || (node ? node.innerText : '') || String(radioQuestions.length);
                if (seen.has(key)) continue;
                seen.add(key);
                const group = node ? [...node.querySelectorAll('input[type=radio]')] : [radio];
                const radioOptionText = (item) => {
                  const explicit = item.id
                    ? deepAll('label').find((label) => label.getAttribute('for') === item.id)
                    : null;
                  const wrapping = item.closest('label');
                  return String(
                    (explicit && explicit.innerText) ||
                    (wrapping && wrapping.innerText) ||
                    item.getAttribute('aria-label') || item.value || ''
                  ).replace(/\s+/g, ' ').trim().slice(0, 120);
                };
                const checked = group.find((item) => item.checked);
                let selected = '';
                if (checked) selected = radioOptionText(checked);
                const text = labelText(radio);
                if (required(radio) && !checked) requiredUnfilled.push(text);
                if (
                  required(radio) && !checked &&
                  /work (authorization|authorisation)|right to work|visa|sponsorship|citizenship|legal identity|passport|national id/i.test(text)
                ) sensitiveRequiredUnknown.push(text);
                 radioQuestions.push({
                   field_key: String(radio.name || radio.id || `radio-${radioQuestions.length + 1}`).slice(0, 160),
                   text,
                   selected,
                   required: required(radio),
                   options: group.slice(0, 100).map(radioOptionText).filter(Boolean),
                   option_count: group.length,
                   options_truncated: group.length > 100
                 });
                 if (selected) provenanceFields.push({
                   field_key: String(radio.name || radio.id || `radio-${provenanceFields.length + 1}`).slice(0, 160),
                   control: 'radio',
                   text,
                   selected,
                   required: required(radio),
                   protected_identifier: false,
                   options: group.slice(0, 100).map(radioOptionText).filter(Boolean),
                   option_count: group.length,
                   options_truncated: group.length > 100
                 });
              }
              const visibleChecks = deepAll('input[type=checkbox]').filter(visible);
              for (const checkbox of visibleChecks) {
                if (required(checkbox) && !checkbox.checked) requiredUnfilled.push(labelText(checkbox));
                if (checkbox.checked) provenanceFields.push({
                  field_key: String(checkbox.id || checkbox.name || `checkbox-${provenanceFields.length + 1}`).slice(0, 160),
                  control: 'checkbox',
                  text: labelText(checkbox),
                  selected: 'checked',
                  required: required(checkbox),
                  protected_identifier: false,
                  options: ['checked']
                });
              }
              const ariaComboboxes = deepAll('[role="combobox"]')
                .filter((el) => visible(el) && !['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName));
              for (const combobox of ariaComboboxes) {
                const text = labelText(combobox);
                const selected = String(
                  combobox.getAttribute('aria-valuetext') ||
                  combobox.getAttribute('data-value') ||
                  combobox.textContent || ''
                ).replace(/\s+/g, ' ').trim().slice(0, 240);
                if (required(combobox) && !selected) requiredUnfilled.push(text);
                if (selected) provenanceFields.push({
                  field_key: String(
                    combobox.id || combobox.getAttribute('name') ||
                    combobox.getAttribute('aria-controls') || `combobox-${provenanceFields.length + 1}`
                  ).slice(0, 160),
                  control: 'combobox',
                  text,
                  selected,
                  required: required(combobox),
                  protected_identifier: protectedIdentifier(combobox, text),
                  options: []
                });
              }
              const submitControls = deepAll(
                'button,input[type=submit],[role="button"]'
              )
                .filter((el) => visible(el) && /submit|send application|finish|complete application/i.test(
                  (el.innerText || el.value || el.getAttribute('aria-label') || '').trim()
                ));
              const captchaCandidates = deepAll(
                'iframe,[class*="turnstile" i],[id*="turnstile" i],[class*="hcaptcha" i],[class*="recaptcha" i],[data-sitekey]'
              ).map((el) => {
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                const marker = `${el.title || ''} ${el.src || ''} ${el.id || ''} ${el.className || ''}`.toLowerCase();
                return {
                  marker: marker.slice(0, 240),
                  width: Math.round(rect.width),
                  height: Math.round(rect.height),
                  display: style.display,
                  visibility: style.visibility,
                  opacity: style.opacity,
                  aria_hidden: el.getAttribute('aria-hidden') || '',
                  visible: visible(el) && rect.width >= 80 && rect.height >= 40 &&
                    /captcha|turnstile|challenge/.test(marker)
                };
              });
              const captchaVisible = captchaCandidates.some((candidate) => candidate.visible);
              const visibleText = document.body ? document.body.innerText : '';
              const jobReferenceUrls = [...new Set(
                deepAll('a[href]')
                  .map((el) => String(el.href || '').trim())
                  .filter((href) => {
                    try {
                      const candidate = new URL(href, location.href);
                      return candidate.protocol === 'https:' &&
                        candidate.hostname === location.hostname &&
                        /\/job\//i.test(candidate.pathname);
                    } catch (_error) {
                      return false;
                    }
                  })
              )].slice(0, 20);
              const educationEntries = [];
              const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
              const visibleNodes = deepAll('*').filter(visible);
              const educationLabelPattern = /^degree(?:\s+(?:type|name|title|\/\s*credential))?(?:\s*\([^)]*\))?\s*\*?(?:\s*[:\-]\s*.+)?$/i;
              const educationRecordLabels = visibleText
                .split(/\r?\n/)
                .map((line) => line.replace(/\s+/g, ' ').trim())
                .filter((line) => educationLabelPattern.test(line));
              const educationFieldPresent = educationRecordLabels.length > 0;
              const educationRecordCount = educationRecordLabels.length;
              for (const expected of (expectedEducation || []).slice(0, 20)) {
                const institution = String(expected.institution || '').trim();
                const target = normalize(institution);
                if (!target) continue;
                const matches = visibleNodes
                  .map((el) => ({el, text: (el.innerText || '').trim()}))
                  .filter((entry) => normalize(entry.text).includes(target))
                  .sort((left, right) => left.text.length - right.text.length);
                if (!matches.length) continue;
                let node = matches[0].el;
                let sectionText = '';
                for (let depth = 0; node && node !== document.body && depth < 9; depth += 1) {
                  const text = (node.innerText || '').replace(/\r/g, '').trim();
                  if (/(^|\n)\s*degree(?:\s+(?:type|name|title|\/\s*credential))?(?:\s*\([^)]*\))?\s*\*?(?:\s*[:\-]\s*\S.+)?\s*(?:\n|$)/i.test(text) && text.length <= 5000) {
                    sectionText = text;
                    break;
                  }
                  node = node.parentElement;
                }
                if (!sectionText) continue;
                const lines = sectionText.split('\n').map((line) => line.trim()).filter(Boolean);
                const degreeLabel = /^degree(?:\s+(?:type|name|title|\/\s*credential))?(?:\s*\([^)]*\))?\s*\*?$/i;
                const degreeIndex = lines.findIndex((line) => degreeLabel.test(line));
                const inlineDegree = lines
                  .map((line) => line.match(/^degree(?:\s+(?:type|name|title|\/\s*credential))?(?:\s*\([^)]*\))?\s*\*?\s*[:\-]\s*(.+)$/i))
                  .find((match) => match && String(match[1] || '').trim());
                const degree = degreeIndex >= 0
                  ? String(lines[degreeIndex + 1] || '').trim()
                  : String((inlineDegree && inlineDegree[1]) || '').trim();
                if (degree) educationEntries.push({institution, degree: degree.slice(0, 240)});
              }
              const assessmentVisible = /\b(complete|take|start) (an? )?(online |coding |video )?assessment\b|\bcoding assessment\b|\bonline assessment\b/i.test(visibleText);
              const labelsByControlId = new Map();
              for (const label of deepAll('label[for]')) {
                const controlId = String(label.getAttribute('for') || '');
                if (controlId && !labelsByControlId.has(controlId)) {
                  labelsByControlId.set(controlId, label);
                }
              }
              const structuralLabel = (el) => {
                const explicit = el.id ? labelsByControlId.get(el.id) : null;
                const wrapping = el.closest('label');
                const legend = el.closest('fieldset')?.querySelector('legend');
                return (
                  (explicit && explicit.innerText) ||
                  el.getAttribute('aria-label') ||
                  (wrapping && wrapping.innerText) ||
                  (legend && legend.innerText) ||
                  el.placeholder || el.name || el.id || ''
                ).replace(/\s+/g, ' ').trim().slice(0, 240);
              };
              const structuralFields = deepAll('input,textarea,select,[role="combobox"]')
                .filter((el) => visible(el) && el.type !== 'hidden' && !el.matches(responseSelector))
                .slice(0, 200)
                .map((el, index) => ({
                  field_key: String(el.id || el.name || `field-${index + 1}`).slice(0, 160),
                  label: structuralLabel(el),
                  control: el.tagName === 'SELECT'
                    ? 'select'
                    : el.getAttribute('role') === 'combobox'
                      ? 'combobox'
                      : el.tagName === 'TEXTAREA' ? 'textarea' : String(el.type || 'text'),
                  required: required(el),
                  disabled: Boolean(el.disabled),
                  readonly: Boolean(el.readOnly),
                  autocomplete: String(el.autocomplete || '').slice(0, 120),
                  placeholder: String(el.placeholder || '').slice(0, 240),
                  protected_identifier: protectedIdentifier(el, structuralLabel(el)),
                  options: el.tagName === 'SELECT'
                    ? [...el.options].slice(0, 100).map((option) =>
                        String(option.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120)
                      )
                    : [],
                  option_count: el.tagName === 'SELECT' ? el.options.length : 0,
                  options_truncated: el.tagName === 'SELECT' && el.options.length > 100
                }));
              let workdayObservation = null;
              if (/(^|\.)myworkday(?:jobs|site)\.com$/i.test(location.hostname)) {
                const compactText = visibleText.replace(/\s+/g, ' ').trim();
                const invalidControls = deepAll('input:invalid,textarea:invalid,select:invalid')
                  .filter(visible);
                const actionLabels = deepAll('button,input[type=submit],[role="button"]')
                  .filter(visible)
                  .map((el) => (el.innerText || el.value || el.getAttribute('aria-label') || '').trim());
                const receipt = /application (?:was |has been )?(?:successfully )?(?:submitted|received)|thank you for (?:applying|submitting your application)/i.test(compactText);
                const verification = /security code|verification code|one[- ]time (?:code|password)|\botp\b|verify (?:your )?email/i.test(compactText);
                const passwordVisible = deepAll('input[type=password]').some(visible);
                let pageKind = 'my_information';
                if (receipt) pageKind = 'confirmation';
                else if (captchaVisible || assessmentVisible || verification) pageKind = 'manual_gate';
                else if (passwordVisible && /sign in|log in/i.test(compactText)) pageKind = 'sign_in';
                else if (/review (?:your |my )?application|application review/i.test(compactText)) pageKind = 'review';
                else if (resumeFields.length > 0 && /resume|curriculum vitae|\bcv\b/i.test(compactText)) pageKind = 'resume_upload';
                else if (/self identification/i.test(compactText)) pageKind = 'self_identification';
                else if (/voluntary disclosure|disability status|veteran status/i.test(compactText)) pageKind = 'voluntary_disclosures';
                else if (/my experience|work experience/i.test(compactText)) pageKind = 'my_experience';
                else if (/application questions/i.test(compactText)) pageKind = 'application_questions';
                else if (invalidControls.length > 0) pageKind = 'validation_error';
                const kindFor = (field) => {
                  const control = String(field.control || '').toLowerCase();
                  if (control === 'textarea') return 'textarea';
                  if (control === 'select') return 'select';
                  if (control === 'file') return 'file_upload';
                  if (control === 'checkbox') return 'checkbox';
                  if (control === 'radio') return 'radio';
                  if (control === 'date') return 'date';
                  if (control === 'email') return 'email';
                  if (control === 'tel') return 'phone';
                  return 'text';
                };
                workdayObservation = {
                  page_kind: pageKind,
                  visible_controls: structuralFields.slice(0, 128).map(kindFor),
                  required_count: Math.min(64, structuralFields.filter((field) => field.required).length),
                  invalid_count: Math.min(64, invalidControls.length),
                  has_next: actionLabels.some((label) => /^(next|continue)$/i.test(label)),
                  has_review: actionLabels.some((label) => /review/i.test(label)),
                  has_submit: actionLabels.some((label) => /submit|send application|finish|complete application/i.test(label)),
                  has_confirmation: receipt,
                  has_manual_gate: captchaVisible || assessmentVisible || verification,
                  repairable_validation: invalidControls.length > 0 && !(captchaVisible || assessmentVisible || verification)
                };
              }
              return {
                url: location.href,
                required_unfilled: requiredUnfilled,
                sensitive_required_unknown: sensitiveRequiredUnknown,
                resume_field_present: resumeFields.length > 0 || resumeCards.length > 0,
                resume_uploaded: resumeUploaded,
                resume_card_texts: resumeCards,
                full_name_values: fullNameValues,
                email_values: emailValues,
                current_location_values: currentLocationValues,
                current_location_fields: currentLocationFields,
                education_field_present: educationFieldPresent,
                education_record_count: educationRecordCount,
                education_entries: educationEntries,
                select_fields: selectFields,
                text_fields: textFields,
                radio_questions: radioQuestions,
                provenance_fields: provenanceFields.slice(0, 129),
                provenance_field_count: provenanceFields.length,
                file_fields: fileFields,
                submit_control_count: submitControls.length,
                assessment_visible: assessmentVisible,
                captcha_visible: captchaVisible,
                captcha_candidates: captchaCandidates,
                captcha_token_present: responseFields.some((el) => (el.value || '').trim().length > 0),
                job_reference_urls: jobReferenceUrls,
                form_fields: structuralFields,
                workday_observation: workdayObservation
              };
            }""",
            expected_education,
        )
        snapshot["stateful_control_coverage"] = application_surface.evaluate(
            _STATEFUL_CONTROL_COVERAGE_SCRIPT
        )
        _merge_same_page_submit_evidence(snapshot, page, application_surface)
        _redact_protected_identifier_snapshot(snapshot)
        snapshot["document_url"] = snapshot.get("url", "")
        snapshot["url"] = page.url
        issues = _validate_pre_submit_snapshot(snapshot, profile, job)
        coverage_error = stateful_control_coverage_error(snapshot)
        if coverage_error is not None:
            issues.append(coverage_error)
        ats_context, workday_context, adapter_issues = _adapter_observation_context(
            snapshot, job
        )
        issues.extend(adapter_issues)
        provenance_audit = audit_pre_submit_answer_provenance(
            snapshot,
            profile,
            job,
            existing_issues=issues,
        )
        issues.extend(provenance_audit.issues)
        issues = list(dict.fromkeys(issues))
        blockers, repairable, advisories = _partition_pre_submit_issues(issues)
        lossy_mappings = _collect_lossy_answer_mappings(snapshot, profile, job)
        if blockers:
            disposition = "block"
        elif repairable:
            disposition = "retry_prepare"
        elif advisories or lossy_mappings:
            disposition = "proceed_with_advisories"
        else:
            disposition = "clear"
        report = {
            "status": "clear" if disposition in {"clear", "proceed_with_advisories"} else "attention",
            "disposition": disposition,
            "page_url": snapshot.get("url", ""),
            "issues": issues,
            "blocking_issues": blockers,
            "repairable_issues": repairable,
            "advisory_issues": advisories,
            "lossy_answer_mappings": lossy_mappings,
            "answer_provenance": provenance_audit.report,
            "stateful_control_coverage": snapshot.get("stateful_control_coverage"),
            "advisory_only": disposition == "proceed_with_advisories",
            "submission_gate": True,
            "required_unfilled_count": len(snapshot.get("required_unfilled", [])),
            "resume_field_present": snapshot.get("resume_field_present", False),
            "resume_uploaded": snapshot.get("resume_uploaded", False),
            "agent_resume_upload_verified": _verified_agent_resume_upload(job),
            "submit_control_count": snapshot.get("submit_control_count", 0),
            "captcha_token_present": snapshot.get("captcha_token_present", False),
            "captcha_candidates": snapshot.get("captcha_candidates", []),
            "assessment_visible": snapshot.get("assessment_visible", False),
            "verification_visible": snapshot.get("verification_visible", False),
            "sensitive_required_unknown_count": len(
                snapshot.get("sensitive_required_unknown", [])
            ),
            "ats_adapter_context": ats_context,
            **({"workday_state": workday_context} if workday_context else {}),
        }
        ordinary_dynamic_repair = any(
            str(issue).startswith("required_field_empty:") for issue in repairable
        )
        if ordinary_dynamic_repair:
            fill_snapshot = _build_ats_fill_plan_snapshot(snapshot, job)
            if fill_snapshot is not None:
                report["ats_fill_plan_snapshot"] = fill_snapshot
        report_path = (
            config.APPLY_WORKER_DIR / f"worker-{worker_id}" / "pre-submit-audit.json"
        )
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        if "visible_captcha" in blockers:
            return "visible_captcha", report
        if blockers:
            return "pre_submit_audit:" + ",".join(blockers[:5]), report
        if repairable:
            return "pre_submit_repair:" + ",".join(repairable[:5]), report
        return None, report
    except Exception as exc:
        logger.exception("Pre-submit browser audit failed")
        return f"pre_submit_audit_error:{type(exc).__name__}", {}
    finally:
        playwright.stop()
