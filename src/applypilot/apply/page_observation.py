"""Browser-page observation and application form audit contracts.

This module owns browser-derived facts only.  It does not acquire jobs, mutate
application ledgers, launch workers, or decide batch progress.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from collections.abc import Mapping
from urllib.parse import parse_qs, unquote, urlparse

from applypilot import config
from applypilot.apply import ats as ats_mod
from applypilot.apply import prompt as prompt_mod
from applypilot.apply.answer_provenance import audit_pre_submit_answer_provenance
from applypilot.apply.identity_materials import classify_identity_requirement
from applypilot.apply.specialists import (
    ATS_FORM_SNAPSHOT_SCHEMA_VERSION,
    freeze_ats_fill_plan_snapshot,
)
from applypilot.apply.workday_state import (
    ProgressAction,
    evaluate_page_progress,
    observation_from_mapping,
)
from applypilot.storage.job_identity import extract_platform_job_id

logger = logging.getLogger(__name__)


def _verified_agent_resume_upload(job: dict) -> bool:
    """Accept a compact same-turn upload proof, never a bare status claim."""
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

    expected_path = unquote(expected.path).rstrip("/").removesuffix("/apply")
    actual_path = unquote(actual.path).rstrip("/").removesuffix("/apply")
    expected_tokens = tuple(re.findall(r"[a-z0-9]+", expected_path.casefold()))
    actual_tokens = tuple(re.findall(r"[a-z0-9]+", actual_path.casefold()))
    if expected_tokens and expected_tokens == actual_tokens:
        return True

    if (
        ats_mod.detect_ats_site(expected_url) == "smartrecruiters"
        and ats_mod.detect_ats_site(actual_url) == "smartrecruiters"
    ):
        expected_parts = [part for part in expected.path.split("/") if part]
        actual_parts = [part for part in actual.path.split("/") if part]
        expected_tenant = expected_parts[0] if len(expected_parts) >= 2 else ""
        expected_posting_id = (
            expected_parts[1].split("-", 1)[0] if len(expected_parts) >= 2 else ""
        )
        actual_tenant = (
            actual_parts[2]
            if len(actual_parts) >= 5
            and actual_parts[:2] == ["oneclick-ui", "company"]
            and actual_parts[3] == "publication"
            else ""
        )
        actual_publication_id = actual_parts[4] if len(actual_parts) >= 5 else ""
        query_tenant = (parse_qs(actual.query).get("dcr_ci") or [""])[0]
        return bool(
            expected_tenant
            and expected_tenant.casefold() == actual_tenant.casefold()
            and actual_tenant.casefold() == query_tenant.casefold()
            and isinstance(binding, dict)
            and binding.get("resolved") is True
            and str(binding.get("provider") or "").casefold() == "smartrecruiters"
            and str(binding.get("tenant") or "").casefold()
            == expected_tenant.casefold()
            and str(binding.get("posting_id") or "") == expected_posting_id
            and str(binding.get("publication_id") or "").casefold()
            == actual_publication_id.casefold()
        )

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


def _work_authorization_answers(profile: dict, job: dict) -> tuple[bool, bool] | None:
    """Return (authorized, sponsorship-needed) for a clearly classified role."""
    policy = profile.get("work_authorization", {}).get("form_answer_policy", {})
    job_text = " ".join(
        str(job.get(field) or "").casefold()
        for field in ("title", "full_description", "application_readiness_reason")
    )
    branch = None
    if "intern" in job_text:
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

    work_answers = _work_authorization_answers(profile, job)
    if re.search(r"sponsor|sponsorship", text) and work_answers is not None:
        return "requires_sponsorship", work_answers[1]
    if re.search(
        r"(?:authori[sz]ed|legal(?:ly)? (?:eligible|entitled)|right) to work",
        text,
    ) and work_answers is not None:
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

        generic_expected = _expected_screening_answer(text, profile, job)
        if generic_expected is not None:
            generic_key, generic_value = generic_expected
            if not _selected_matches_boolean(selected, generic_value):
                issues.append(f"hard_answer_mismatch:{generic_key}")

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
        generic_expected = _expected_screening_answer(text, profile, job)
        if generic_expected is not None:
            generic_key, generic_value = generic_expected
            if not _selected_matches_boolean(selected, generic_value):
                issues.append(f"hard_answer_mismatch:{generic_key}")

    if snapshot.get("submit_control_count", 0) < 1:
        issues.append("submit_control_missing")
    return list(dict.fromkeys(issues))


_APPLICATION_SURFACE_SIGNALS = r"""() => {
  const visible = (el) => {
    const style = getComputedStyle(el);
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      el.getClientRects().length > 0;
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
  const deepAll = (selector) => deepElements.filter((element) => element.matches(selector));
  const text = document.body ? document.body.innerText : '';
  const receipt = /your application has been submitted|application (?:was |has been )?(?:successfully )?submitted|thank you for (?:applying|submitting your application)|we (?:have )?received your application|申请已提交|投递成功|申请成功/i.test(text);
  const finalSubmit = deepAll(
    'button,input[type=submit],[role="button"]'
  ).some((el) => visible(el) && /^(submit|submit application|send application|finish|complete application|提交申请|投递)$/i.test(
    (el.innerText || el.value || el.getAttribute('aria-label') || '').trim()
  ));
  const review = /review your application/i.test(text);
  const dialog = deepAll('dialog,[role="dialog"]').some(visible);
  const formControls = deepAll(
    'input,select,textarea,button,[role="button"],[role="radio"],[role="checkbox"]'
  ).filter(visible).length;
  return {
    receipt,
    final_submit: finalSubmit,
    review,
    dialog,
    form_controls: formControls,
    text_length: text.trim().length
  };
}"""


def _application_surface_score(signals: dict) -> int:
    return (
        100 * int(bool(signals.get("receipt")))
        + 50 * int(bool(signals.get("final_submit")))
        + 20 * int(bool(signals.get("review")))
        + 10 * int(bool(signals.get("dialog")))
        + min(int(signals.get("form_controls") or 0), 20)
        + min(int(signals.get("text_length") or 0) // 500, 9)
    )


def _select_application_frame(page):
    """Choose the populated application surface across the page and child frames."""
    selected, _score = _score_application_page(page)
    return selected


def _score_application_page(page):
    """Return one page's best surface and score after evaluating each surface once."""
    candidates = list(getattr(page, "frames", ()) or (page,))
    selected = candidates[0]
    selected_score = -1
    for frame in candidates:
        try:
            score = _application_surface_score(
                frame.evaluate(_APPLICATION_SURFACE_SIGNALS)
            )
            if score > selected_score:
                selected = frame
                selected_score = score
        except Exception:
            logger.debug("Unable to score browser frame for application evidence", exc_info=True)
    return selected, selected_score


def _select_application_page_and_frame(pages: list):
    """Choose a page and its best surface in one scoring pass."""
    selected_page = pages[-1]
    selected_surface = selected_page
    selected_score = -1
    for page in pages:
        try:
            surface, score = _score_application_page(page)
            if score > selected_score:
                selected_page = page
                selected_surface = surface
                selected_score = score
        except Exception:
            logger.debug("Unable to score browser page for application evidence", exc_info=True)
    return selected_page, selected_surface


def _select_application_page(pages: list):
    """Choose the tab carrying a review/receipt rather than relying on tab order."""
    selected, _surface = _select_application_page_and_frame(pages)
    return selected


def _bound_application_pages(browser, pages: list, job: dict) -> list:
    """Restrict browser evidence to the application's immutable target lineage."""
    if "_browser_root_target_ids" not in job:
        return pages
    roots = set(job.get("_browser_root_target_ids") or [])
    if not roots:
        return []
    session = browser.new_browser_cdp_session()
    infos = {
        str(info.get("targetId") or ""): info
        for info in session.send("Target.getTargets").get("targetInfos", [])
        if info.get("targetId")
    }
    from applypilot.apply.credential_relay import _target_descends_from

    bound = []
    for page in pages:
        try:
            info = page.context.new_cdp_session(page).send("Target.getTargetInfo")[
                "targetInfo"
            ]
        except Exception:  # noqa: BLE001, S112 - page can detach during navigation
            continue
        target_id = str(info.get("targetId") or "")
        infos[target_id] = info
        if _target_descends_from(target_id, roots, infos):
            bound.append(page)
    return bound


def _linkedin_external_handoff_pages(pages: list) -> list:
    """Return unambiguous HTTPS pages outside LinkedIn without inspecting forms."""
    external = []
    for page in pages:
        try:
            parsed = urlparse(str(page.url or "").strip())
        except ValueError:
            continue
        host = (parsed.hostname or "").casefold().rstrip(".")
        if (
            parsed.scheme == "https"
            and host
            and not parsed.username
            and not parsed.password
            and host != "linkedin.com"
            and not host.endswith(".linkedin.com")
        ):
            external.append(page)
    return external


def _linkedin_job_id(url: object) -> str:
    """Return only a complete canonical LinkedIn /jobs/view/{id} identity."""
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return ""
    host = (parsed.hostname or "").casefold().rstrip(".")
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return ""
    match = re.fullmatch(r"/jobs/view/([^/]+)/?", parsed.path)
    if not match:
        return ""
    platform_id = extract_platform_job_id(parsed.geturl())
    if platform_id.startswith("linkedin:"):
        return platform_id.split(":", 1)[1]
    # Keep compact synthetic fixtures exact; production identities are parsed
    # through the shared canonical platform-ID parser above.
    return match.group(1) if match.group(1).isdigit() else ""


def _linkedin_page_matches_job_id(url: object, expected_job_id: str) -> bool:
    return bool(expected_job_id) and _linkedin_job_id(url) == expected_job_id


def _linkedin_authwall_redirect_job_id(url: object) -> str:
    """Return the exact job ID bound to a narrow LinkedIn authwall entry.

    ``parse_qs`` performs the one allowed percent-decoding pass.  A second
    decode would turn a double-encoded, non-URL value into an admissible
    redirect and must not be performed here.
    """
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return ""
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() != "https"
        or (host != "linkedin.com" and not host.endswith(".linkedin.com"))
        or parsed.username
        or parsed.password
        or parsed.path.rstrip("/") != "/authwall"
        or parsed.fragment
    ):
        return ""
    redirects = parse_qs(parsed.query, keep_blank_values=True).get(
        "sessionRedirect", []
    )
    if len(redirects) != 1:
        return ""
    redirect = str(redirects[0] or "").strip()
    try:
        target = urlparse(redirect)
    except ValueError:
        return ""
    target_host = (target.hostname or "").casefold().rstrip(".")
    if (
        target.scheme.casefold() != "https"
        or (target_host != "linkedin.com" and not target_host.endswith(".linkedin.com"))
        or target.username
        or target.password
        or target.fragment
    ):
        return ""
    return _linkedin_job_id(redirect)


def _target_infos(session) -> dict[str, dict]:
    return {
        str(info.get("targetId") or ""): dict(info)
        for info in session.send("Target.getTargets").get("targetInfos", [])
        if info.get("targetId")
    }


def _external_https_target(info: Mapping[str, object]) -> bool:
    try:
        parsed = urlparse(str(info.get("url") or ""))
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold().rstrip(".")
    return bool(
        parsed.scheme == "https"
        and host
        and host != "linkedin.com"
        and not host.endswith(".linkedin.com")
        and not parsed.username
        and not parsed.password
    )


def _classify_linkedin_causal_target(
    before: Mapping[str, Mapping[str, object]],
    after: Mapping[str, Mapping[str, object]],
    *,
    source_target_id: str,
) -> tuple[dict[str, object] | None, str]:
    """Admit only the source target navigation or a newly opened source popup."""
    candidates: list[dict[str, object]] = []
    source_after = after.get(source_target_id)
    if isinstance(source_after, Mapping) and _external_https_target(source_after):
        candidates.append(
            {
                "target_id": source_target_id,
                "mode": "same_target_navigation",
                "initial_url": str(before.get(source_target_id, {}).get("url") or ""),
                "final_url": str(source_after.get("url") or ""),
            }
        )
    before_ids = set(before)
    for target_id, info in after.items():
        if target_id in before_ids or not _external_https_target(info):
            continue
        if str(info.get("openerId") or info.get("parentId") or "") != source_target_id:
            continue
        candidates.append(
            {
                "target_id": target_id,
                "mode": "new_popup_from_source",
                "initial_url": str(info.get("url") or ""),
                "final_url": str(info.get("url") or ""),
            }
        )
    if len(candidates) != 1:
        return None, "linkedin_apply_click:no_causal_external_target"
    return candidates[0], "linkedin_apply_click:causal_external_target"


def _target_id_digest(target_id: object) -> str:
    return hashlib.sha256(str(target_id or "").encode("utf-8")).hexdigest()


def _admit_linkedin_causal_events(
    corroborated: Mapping[str, object] | None,
    *,
    source_target_id: str,
    navigation_events: list[str],
    redirect_lineage: list[str],
    popup_event_count: int,
    popup_candidates: list[Mapping[str, object]],
) -> tuple[dict[str, object] | None, str]:
    """Require exactly one Playwright click-epoch event plus target corroboration."""
    if popup_event_count != len(popup_candidates):
        return None, "linkedin_apply_click:target_lost_or_unclassified"
    candidates: list[dict[str, object]] = []
    external_navigations = [
        url for url in navigation_events if _external_https_target({"url": url})
    ]
    if (
        external_navigations
        and corroborated is not None
        and corroborated.get("mode") == "same_target_navigation"
        and corroborated.get("target_id") == source_target_id
    ):
        candidates.append(
            {
                **corroborated,
                "final_url": external_navigations[-1],
                "redirect_lineage": redirect_lineage[-12:],
                "lineage_complete": bool(redirect_lineage)
                and len(redirect_lineage) < 12,
            }
        )
    candidates.extend(dict(candidate) for candidate in popup_candidates)
    if len(candidates) > 1:
        return None, "linkedin_apply_click:ambiguous_causal_targets"
    if not candidates:
        return None, "linkedin_apply_click:no_click_epoch_causal_target"
    candidate = candidates[0]
    if candidate.get("lineage_complete") is not True:
        return None, "linkedin_apply_click:redirect_lineage_incomplete"
    return candidate, "linkedin_apply_click:causal_external_target"


def _resolve_linkedin_click_epoch(
    causal: Mapping[str, object] | None,
    causal_reason: str,
    *,
    source_job_page_matches: bool,
    login_surface: bool,
    native_surface: bool,
) -> tuple[str | None, str]:
    """Resolve exactly one mutually exclusive outcome from one Apply click epoch."""
    fatal_reasons = {
        "linkedin_apply_click:target_lost_or_unclassified",
        "linkedin_apply_click:ambiguous_causal_targets",
        "linkedin_apply_click:redirect_lineage_incomplete",
    }
    if causal_reason in fatal_reasons:
        return causal_reason, ""

    outcomes: list[str] = []
    if causal is not None:
        outcomes.append("linkedin_external_handoff")
    if source_job_page_matches and login_surface:
        outcomes.append("linkedin_login_required")
    if source_job_page_matches and native_surface:
        outcomes.append("linkedin_native_apply_opened")
    if len(outcomes) > 1:
        return "linkedin_apply_click:ambiguous_click_epoch_results", ""
    if outcomes:
        return None, outcomes[0]
    if not source_job_page_matches:
        return "linkedin_apply_click:returned_job_identity_mismatch", ""
    return causal_reason, ""


def _page_target_id(page) -> str:
    try:
        info = page.context.new_cdp_session(page).send("Target.getTargetInfo")[
            "targetInfo"
        ]
    except Exception:  # noqa: BLE001 - a target may detach during navigation
        return ""
    return str(info.get("targetId") or "")


def _linkedin_source_page_is_still_exact(
    page,
    *,
    source_target_id: str,
    root_target_ids: set[str],
    source_job_id: str,
) -> bool:
    """Fail closed when a pre-click LinkedIn surface drifts from its bound job."""
    current_target_id = _page_target_id(page)
    return bool(
        current_target_id
        and current_target_id == source_target_id
        and current_target_id in root_target_ids
        and _linkedin_page_matches_job_id(page.url, source_job_id)
    )


def _wait_for_linkedin_main_apply_control(page) -> None:
    """Wait on the observable unique top-card Apply condition, with a hard bound."""
    page.wait_for_function(
        r"""() => {
          const visible = (element) => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none'
              && rect.width > 0 && rect.height > 0;
          };
          const tiers = [
            '.jobs-unified-top-card',
            '.job-details-jobs-unified-top-card__container--two-pane',
            '[data-job-details-top-card]',
            '.top-card-layout__card',
            '.top-card-layout',
          ];
          let topCards = [];
          for (const selector of tiers) {
            topCards = Array.from(document.querySelectorAll(selector)).filter(visible);
            if (topCards.length) break;
          }
          if (topCards.length !== 1) return false;
          const topCard = topCards[0];
          const controls = Array.from(new Set([
            ...topCard.querySelectorAll('.jobs-apply-button--top-card button.jobs-apply-button'),
            ...topCard.querySelectorAll('button.jobs-apply-button'),
            ...topCard.querySelectorAll('a.jobs-apply-button'),
            ...topCard.querySelectorAll('[data-live-test-job-apply-button]'),
            ...topCard.querySelectorAll('.top-card-layout__cta'),
            ...topCard.querySelectorAll('[data-tracking-control-name*="apply"]'),
            ...topCard.querySelectorAll('button[aria-label^="Apply to "]'),
          ])).filter((element) => {
            const label = String(element.getAttribute('aria-label') || element.textContent || '')
              .replace(/\s+/g, ' ').trim();
            const explicitApply = /\b(?:easy\s+)?apply\b/i.test(label)
              || /^(?:立即)?(?:轻松)?申请(?:此职位|职位)?$/.test(label)
              || /^申请.+(?:职位|岗位)$/.test(label);
            return visible(element) && !element.disabled && explicitApply;
          });
          return controls.length === 1;
        }""",
        timeout=10_000,
    )


def _linkedin_main_apply_handle(page):
    """Return the exact unique top-card Apply element as a Playwright handle."""
    handle = page.evaluate_handle(
        r"""() => {
          const visible = (element) => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none'
              && rect.width > 0 && rect.height > 0;
          };
          const tiers = [
            '.jobs-unified-top-card',
            '.job-details-jobs-unified-top-card__container--two-pane',
            '[data-job-details-top-card]',
            '.top-card-layout__card',
            '.top-card-layout',
          ];
          let topCards = [];
          for (const selector of tiers) {
            topCards = Array.from(document.querySelectorAll(selector)).filter(visible);
            if (topCards.length) break;
          }
          if (topCards.length !== 1) return null;
          const topCard = topCards[0];
          const controls = Array.from(new Set([
            ...topCard.querySelectorAll('.jobs-apply-button--top-card button.jobs-apply-button'),
            ...topCard.querySelectorAll('button.jobs-apply-button'),
            ...topCard.querySelectorAll('a.jobs-apply-button'),
            ...topCard.querySelectorAll('[data-live-test-job-apply-button]'),
            ...topCard.querySelectorAll('.top-card-layout__cta'),
            ...topCard.querySelectorAll('[data-tracking-control-name*="apply"]'),
            ...topCard.querySelectorAll('button[aria-label^="Apply to "]'),
          ])).filter((element) => {
            const label = String(element.getAttribute('aria-label') || element.textContent || '')
              .replace(/\s+/g, ' ').trim();
            const explicitApply = /\b(?:easy\s+)?apply\b/i.test(label)
              || /^(?:立即)?(?:轻松)?申请(?:此职位|职位)?$/.test(label)
              || /^申请.+(?:职位|岗位)$/.test(label);
            return visible(element) && !element.disabled && explicitApply;
          });
          return controls.length === 1 ? controls[0] : null;
        }"""
    )
    return handle.as_element()


def _linkedin_app_promo_dismiss_handle(page):
    """Return only the exact non-application LinkedIn app-promotion dismiss control."""
    handle = page.evaluate_handle(
        r"""() => {
          const visible = (element) => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none'
              && rect.width > 0 && rect.height > 0;
          };
          const dialogs = Array.from(document.querySelectorAll(
            '[role="dialog"].cta-modal, .cta-modal[role="dialog"]'
          )).filter(visible);
          if (dialogs.length !== 1) return null;
          const dialog = dialogs[0];
          const text = String(dialog.textContent || '').replace(/\s+/g, ' ').trim();
          if (!/LinkedIn is better on the app/i.test(text)) return null;
          if (dialog.querySelector('form, input, textarea, select')) return null;
          const controls = Array.from(dialog.querySelectorAll(
            'button, a, [role="button"]'
          )).filter((element) => {
            const label = String(
              element.getAttribute('aria-label') || element.textContent || ''
            ).replace(/\s+/g, ' ').trim();
            return visible(element) && /^Dismiss$/i.test(label);
          });
          return controls.length === 1 ? controls[0] : null;
        }"""
    )
    return handle.as_element()


def _dismiss_linkedin_app_promo(page) -> bool:
    """Dismiss one verified app-promotion modal before the Apply click epoch."""
    control = _linkedin_app_promo_dismiss_handle(page)
    if control is None:
        return False
    control.click(timeout=5_000)
    page.wait_for_function(
        r"""() => {
          const visible = (element) => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none'
              && rect.width > 0 && rect.height > 0;
          };
          return !Array.from(document.querySelectorAll(
            '[role="dialog"].cta-modal, .cta-modal[role="dialog"]'
          )).some(visible);
        }""",
        timeout=5_000,
    )
    return True


def _linkedin_click_page_state(page) -> dict[str, object]:
    result = page.evaluate(
        r"""() => {
          const visible = (element) => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none'
              && rect.width > 0 && rect.height > 0;
          };
          const dialogs = Array.from(document.querySelectorAll(
            '[role="dialog"], .artdeco-modal, [data-test-modal]'
          )).filter(visible);
          const dialogText = dialogs.map((dialog) => String(dialog.textContent || '')
            .replace(/\s+/g, ' ').trim()).join(' ').slice(0, 1000);
          const login = /continue\s+with\s+google|sign\s+in|log\s+in|通过\s*Google\s*继续|登录/i
            .test(dialogText)
            || /\/authwall\/?$|\/login\b|\/uas\/login\b/.test(location.pathname);
          const nativeApply = dialogs.some((dialog) =>
            dialog.querySelector('form, input, textarea, select, button[aria-label*="Submit"]')
          ) && /easy\s+apply|轻松申请|申请/i.test(dialogText);
          return {login, native_apply: nativeApply, page_url: location.href};
        }"""
    )
    return dict(result) if isinstance(result, Mapping) else {}


def _click_linkedin_main_apply_causally(
    port: int, worker_id: int, job: dict
) -> tuple[str | None, dict]:
    """Perform one launcher-owned Apply click and attest only its direct target."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    del worker_id
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        pages = [page for context in browser.contexts for page in context.pages]
        bound_pages = _bound_application_pages(browser, pages, job)
        if _linkedin_external_handoff_pages(bound_pages):
            return "linkedin_apply_click:preexisting_external_target", {}
        source_job_id = _linkedin_job_id(job.get("application_url") or job.get("url"))
        if not source_job_id:
            return "linkedin_apply_click:source_job_id_missing", {}
        candidates = [
            page
            for page in bound_pages
            if _linkedin_page_matches_job_id(page.url, source_job_id)
        ]
        if not candidates:
            authwall_candidates = [
                page
                for page in bound_pages
                if _linkedin_authwall_redirect_job_id(page.url) == source_job_id
            ]
            if len(bound_pages) == 1 and len(authwall_candidates) == 1:
                page = authwall_candidates[0]
                source_target_id = _page_target_id(page)
                roots = set(job.get("_browser_root_target_ids") or [])
                if not source_target_id or source_target_id not in roots:
                    return "linkedin_apply_click:source_root_mismatch", {}
                session = browser.new_browser_cdp_session()
                job["_linkedin_login_baseline_target_ids"] = sorted(
                    _target_infos(session)
                )
                job["_linkedin_login_source_job_id"] = source_job_id
                job["_linkedin_login_entry_stage"] = "pre_entry_authwall"
                return (
                    None,
                    {
                        "disposition": "linkedin_login_required",
                        "stage": "pre_entry_authwall",
                    },
                )
        if len(candidates) != 1:
            return f"linkedin_apply_click:exact_job_page_count:{len(candidates)}", {}
        page = candidates[0]
        source_target_id = _page_target_id(page)
        roots = set(job.get("_browser_root_target_ids") or [])
        if not source_target_id or source_target_id not in roots:
            return "linkedin_apply_click:source_root_mismatch", {}
        _wait_for_linkedin_main_apply_control(page)
        _dismiss_linkedin_app_promo(page)
        if not _linkedin_source_page_is_still_exact(
            page,
            source_target_id=source_target_id,
            root_target_ids=roots,
            source_job_id=source_job_id,
        ):
            return "linkedin_apply_click:source_changed_after_app_promo", {}
        session = browser.new_browser_cdp_session()
        before = _target_infos(session)
        pre_click_state = _linkedin_click_page_state(page)
        if pre_click_state.get("login") is True:
            job["_linkedin_login_baseline_target_ids"] = sorted(before)
            job["_linkedin_login_source_job_id"] = source_job_id
            job["_linkedin_login_entry_stage"] = "pre_entry_login_dialog"
            return (
                None,
                {
                    "disposition": "linkedin_login_required",
                    "stage": "pre_entry_login_dialog",
                },
            )
        control = _linkedin_main_apply_handle(page)
        if control is None:
            return "linkedin_apply_click:main_apply_not_unique", {}

        popup_events: list[object] = []
        popup_redirects: dict[int, list[str]] = {}
        navigation_events: list[str] = []
        redirect_lineage: list[str] = []

        def on_popup(popup) -> None:
            popup_events.append(popup)
            popup_redirects[id(popup)] = []

            def on_popup_request(request) -> None:
                is_navigation = request.is_navigation_request
                if callable(is_navigation):
                    is_navigation = is_navigation()
                if not is_navigation or request.frame != popup.main_frame:
                    return
                chain = []
                current = request
                while current is not None and len(chain) < 12:
                    chain.append(str(current.url or "")[:2000])
                    current = current.redirected_from
                popup_redirects[id(popup)] = list(reversed(chain))

            popup.on("request", on_popup_request)

        def on_navigation(frame) -> None:
            if frame == page.main_frame:
                navigation_events.append(str(frame.url or "")[:2000])

        def on_request(request) -> None:
            is_navigation = request.is_navigation_request
            if callable(is_navigation):
                is_navigation = is_navigation()
            if not is_navigation or request.frame != page.main_frame:
                return
            chain = []
            current = request
            while current is not None and len(chain) < 12:
                chain.append(str(current.url or "")[:2000])
                current = current.redirected_from
            redirect_lineage[:] = list(reversed(chain))

        page.on("popup", on_popup)
        page.on("framenavigated", on_navigation)
        page.on("request", on_request)
        if not _linkedin_source_page_is_still_exact(
            page,
            source_target_id=source_target_id,
            root_target_ids=roots,
            source_job_id=source_job_id,
        ):
            return "linkedin_apply_click:source_changed_before_apply", {}
        control.click(timeout=10_000)

        after = _target_infos(session)
        corroborated, reason = _classify_linkedin_causal_target(
            before, after, source_target_id=source_target_id
        )
        if corroborated is None and not popup_events and not navigation_events:
            try:
                page.wait_for_function(
                    r"""() => {
                      const host = location.hostname.toLowerCase().replace(/\.$/, '');
                      if (host !== 'linkedin.com' && !host.endsWith('.linkedin.com')) return true;
                      const visible = (element) => {
                        const style = getComputedStyle(element);
                        const rect = element.getBoundingClientRect();
                        return style.visibility !== 'hidden' && style.display !== 'none'
                          && rect.width > 0 && rect.height > 0;
                      };
                      return Array.from(document.querySelectorAll(
                        '[role="dialog"], .artdeco-modal, [data-test-modal]'
                      )).some(visible) || /\/login\b|\/uas\/login\b/.test(location.pathname);
                    }""",
                    timeout=10_000,
                )
            except PlaywrightTimeoutError:
                pass
            after = _target_infos(session)
            corroborated, reason = _classify_linkedin_causal_target(
                before, after, source_target_id=source_target_id
            )
        popup_candidates: list[dict[str, object]] = []
        for popup in popup_events:
            try:
                popup.wait_for_load_state("domcontentloaded", timeout=10_000)
            except PlaywrightTimeoutError:
                pass
            try:
                popup_target_id = _page_target_id(popup)
                popup_url = str(popup.url or "")[:2000]
                popup_info = _target_infos(session).get(popup_target_id, {})
                classified = bool(
                    popup_target_id
                    and _external_https_target({"url": popup_url})
                    and popup_target_id not in before
                    and str(
                        popup_info.get("openerId")
                        or popup_info.get("parentId")
                        or ""
                    )
                    == source_target_id
                )
                if not classified:
                    continue
                popup_lineage = popup_redirects.get(id(popup)) or [popup_url]
                popup_candidates.append(
                    {
                        "target_id": popup_target_id,
                        "mode": "new_popup_from_source",
                        "initial_url": popup_lineage[0],
                        "final_url": popup_url,
                        "redirect_lineage": popup_lineage[-12:],
                        "lineage_complete": len(popup_lineage) < 12,
                    }
                )
            except Exception:
                logger.debug(
                    "LinkedIn click popup detached before classification",
                    exc_info=True,
                )
                continue
        causal, causal_reason = _admit_linkedin_causal_events(
            corroborated,
            source_target_id=source_target_id,
            navigation_events=navigation_events,
            redirect_lineage=redirect_lineage,
            popup_event_count=len(popup_events),
            popup_candidates=popup_candidates,
        )
        fatal_signal, _ = _resolve_linkedin_click_epoch(
            causal,
            causal_reason,
            source_job_page_matches=False,
            login_surface=False,
            native_surface=False,
        )
        if fatal_signal in {
            "linkedin_apply_click:target_lost_or_unclassified",
            "linkedin_apply_click:ambiguous_causal_targets",
            "linkedin_apply_click:redirect_lineage_incomplete",
        }:
            return fatal_signal, {}

        source_page_matches = _linkedin_page_matches_job_id(page.url, source_job_id)
        authwall_matches = (
            _linkedin_authwall_redirect_job_id(page.url) == source_job_id
        )
        source_surface_matches = source_page_matches or authwall_matches
        state = _linkedin_click_page_state(page) if source_surface_matches else {}
        epoch_signal, disposition = _resolve_linkedin_click_epoch(
            causal,
            causal_reason,
            source_job_page_matches=source_surface_matches,
            login_surface=state.get("login") is True,
            native_surface=state.get("native_apply") is True,
        )
        if epoch_signal:
            return epoch_signal, {}

        if disposition == "linkedin_external_handoff" and causal is not None:
            attestation_id = uuid.uuid4().hex
            target_id = str(causal["target_id"])
            final_info = after.get(target_id, {})
            causal["final_url"] = str(final_info.get("url") or causal["final_url"])
            job["_linkedin_causal_apply_attestation"] = {
                "version": 1,
                "attestation_id": attestation_id,
                "source_job_id": source_job_id,
                "source_target_id": source_target_id,
                "target_id": target_id,
                "target_id_digest": _target_id_digest(target_id),
                "mode": causal["mode"],
                "initial_url": causal["initial_url"],
                "final_url": causal["final_url"],
                "redirect_lineage": list(causal.get("redirect_lineage") or [])[:12],
                "lineage_complete": causal.get("lineage_complete") is True,
                "before_target_ids": sorted(before),
            }
            return (
                None,
                {
                    "disposition": disposition,
                    "page_url": causal["final_url"],
                },
            )
        if disposition == "linkedin_login_required":
            job["_linkedin_login_baseline_target_ids"] = sorted(after)
            job["_linkedin_login_source_job_id"] = source_job_id
            job["_linkedin_login_entry_stage"] = (
                "post_apply_authwall"
                if authwall_matches
                else "post_apply_login_dialog"
            )
            return None, {
                "disposition": disposition,
                "stage": job["_linkedin_login_entry_stage"],
            }
        if disposition == "linkedin_native_apply_opened":
            return None, {"disposition": disposition}
        return reason, {}
    except Exception as exc:
        logger.exception("LinkedIn launcher-owned Apply click failed")
        return f"linkedin_apply_click:{type(exc).__name__}", {}
    finally:
        playwright.stop()


def _verify_linkedin_post_login_state(
    port: int, worker_id: int, job: dict
) -> tuple[bool, str]:
    """Admit a login turn only when it returns cleanly to the exact source job."""
    from playwright.sync_api import sync_playwright

    del worker_id
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        pages = [page for context in browser.contexts for page in context.pages]
        bound_pages = _bound_application_pages(browser, pages, job)
        if _linkedin_external_handoff_pages(bound_pages):
            return False, "linkedin_login_guard:external_target_created"
        source_job_id = str(job.get("_linkedin_login_source_job_id") or "")
        candidates = [
            page
            for page in bound_pages
            if _linkedin_page_matches_job_id(page.url, source_job_id)
        ]
        if len(candidates) != 1:
            return False, "linkedin_login_guard:exact_job_not_restored"
        session = browser.new_browser_cdp_session()
        current_ids = set(_target_infos(session))
        baseline_ids = set(job.get("_linkedin_login_baseline_target_ids") or [])
        unexpected = current_ids - baseline_ids
        if unexpected:
            return False, "linkedin_login_guard:unexpected_target_created"
        state = _linkedin_click_page_state(candidates[0])
        if state.get("login") is True:
            return False, "linkedin_login_guard:login_not_completed"
        if state.get("native_apply") is True:
            return False, "linkedin_login_guard:agent_opened_native_apply"
        _wait_for_linkedin_main_apply_control(candidates[0])
        return True, "linkedin_login_guard:verified"
    except Exception as exc:
        logger.exception("LinkedIn post-login guard failed")
        return False, f"linkedin_login_guard:{type(exc).__name__}"
    finally:
        playwright.stop()


def _linkedin_external_page_identity(page) -> dict[str, object]:
    """Read only bounded, non-form text that identifies the external posting."""
    raw = page.evaluate(
        r"""() => {
          const visible = (element) => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none'
              && rect.width > 0 && rect.height > 0;
          };
          const compact = (value) => String(value || '').replace(/\s+/g, ' ').trim();
          const headings = Array.from(document.querySelectorAll('h1, h2, [role="heading"]'))
            .filter(visible)
            .map((element) => compact(element.textContent))
            .filter(Boolean);
          return {
            page_title: compact(document.title),
            primary_headings: headings,
          };
        }"""
    )
    if not isinstance(raw, dict):
        raw = {}
    page_title = " ".join(str(raw.get("page_title") or "").split())[:300]
    raw_headings = raw.get("primary_headings")
    headings = (
        [" ".join(str(value).split())[:300] for value in raw_headings[:6]]
        if isinstance(raw_headings, list)
        else []
    )
    return {
        "version": 1,
        "page_title": page_title,
        "primary_headings": [value for value in headings if value],
    }


def _observe_linkedin_external_handoff_page(
    port: int, worker_id: int, job: dict
) -> tuple[str | None, dict]:
    """Observe only the launcher-attested causal Apply target."""
    from playwright.sync_api import sync_playwright

    del worker_id  # reserved for parity with the other observer ports
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        attestation = job.get("_linkedin_causal_apply_attestation")
        if not isinstance(attestation, Mapping) or attestation.get("version") != 1:
            return "linkedin_handoff_observer:causal_attestation_required", {}
        target_id = str(attestation.get("target_id") or "")
        if (
            not target_id
            or attestation.get("target_id_digest") != _target_id_digest(target_id)
        ):
            return "linkedin_handoff_observer:causal_attestation_invalid", {}
        pages = [page for context in browser.contexts for page in context.pages]
        page = next((page for page in pages if _page_target_id(page) == target_id), None)
        if page is None:
            return "linkedin_handoff_observer:attested_target_missing", {}
        parsed = urlparse(str(page.url or ""))
        if not _external_https_target({"url": parsed.geturl()}):
            return "linkedin_handoff_observer:attested_target_not_external", {}
        page.bring_to_front()
        page_identity = _linkedin_external_page_identity(page)
        final_url = str(page.url or "")[:2000]
        redirect_lineage = [
            str(value)[:2000]
            for value in list(attestation.get("redirect_lineage") or [])[:12]
        ]
        if final_url and (not redirect_lineage or redirect_lineage[-1] != final_url):
            if len(redirect_lineage) >= 12:
                return "linkedin_handoff_observer:redirect_lineage_overflow", {}
            redirect_lineage.append(final_url)
        attestation["final_url"] = final_url
        attestation["redirect_lineage"] = redirect_lineage
        attestation_evidence = {
            "version": 1,
            "verified": True,
            "attestation_id_digest": hashlib.sha256(
                str(attestation.get("attestation_id") or "").encode("utf-8")
            ).hexdigest(),
            "source_target_id_digest": _target_id_digest(
                attestation.get("source_target_id")
            ),
            "target_id_digest": _target_id_digest(target_id),
            "mode": str(attestation.get("mode") or ""),
            "initial_url": str(attestation.get("initial_url") or "")[:2000],
            "final_url": final_url,
            "redirect_lineage": redirect_lineage,
            "lineage_complete": attestation.get("lineage_complete") is True,
        }
        return (
            None,
            {
                "status": "attention",
                "disposition": "linkedin_external_handoff",
                "page_url": str(page.url or "").strip(),
                "page_identity": page_identity,
                "causal_apply_attestation": attestation_evidence,
                "submit_control_count": 0,
            },
        )
    except Exception as exc:
        logger.exception("LinkedIn external handoff observation failed")
        return f"linkedin_handoff_observer:{type(exc).__name__}", {}
    finally:
        playwright.stop()


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
            "form_fields": raw_fields,
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
                'li, fieldset, [data-qa*="field"], [class*="application-field"], [class*="question"]'
              ) || el.parentElement;
              const labelText = (el) => {
                const node = context(el);
                return ((node && node.innerText) || el.getAttribute('aria-label') || el.name || '')
                  .replace(/\s+/g, ' ').trim().slice(0, 500);
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
        _redact_protected_identifier_snapshot(snapshot)
        snapshot["document_url"] = snapshot.get("url", "")
        snapshot["url"] = page.url
        issues = _validate_pre_submit_snapshot(snapshot, profile, job)
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


_HISTORICAL_DUPLICATE_RE = re.compile(
    r"^(?:your application (?:was|has been) already submitted|"
    r"application already submitted|you have already applied"
    r"(?: for this (?:job|position|role))?)"
    r"(?:[.!]\s*)?(?:view (?:your )?application|check (?:your )?status)?$",
    re.IGNORECASE,
)


def _is_historical_duplicate_text(value: object) -> bool:
    """Recognize provider text describing a prior application, not this turn."""
    normalized = " ".join(str(value or "").split())
    return bool(_HISTORICAL_DUPLICATE_RE.fullmatch(normalized))


def _classify_post_submit_observation(observation: dict) -> str:
    """Classify the browser state after a final action without guessing success.

    A visible receipt is success. A visible verification gate or deterministic
    field validation rejection proves the application is not yet submitted and
    must not be collapsed into the retry-blocking ``submission_uncertain`` state.
    """
    # A provider can show a positive-looking status because this exact
    # application was submitted in an earlier session.  Keep that distinct
    # from a receipt produced by the current final action: it must not count
    # as a new application and must not enter the ordinary uncertain path.
    historical_duplicate = observation.get(
        "historical_duplicate"
    ) is True or _is_historical_duplicate_text(
        observation.get("historical_duplicate_text")
        or observation.get("confirmation_text")
    )
    if historical_duplicate and observation.get("confirmed") is True:
        return "conflicting_post_submit_status"
    if historical_duplicate:
        return "historical_duplicate"
    if observation.get("confirmed") is True:
        return "confirmed"
    if (
        observation.get("verification_visible") is True
        or observation.get("captcha_visible") is True
    ):
        return "verification_required"
    if observation.get("provider_submission_error_visible") is True:
        return "provider_submission_error"
    if int(observation.get("validation_error_count") or 0) > 0:
        if int(observation.get("manual_validation_error_count") or 0) > 0:
            return "validation_blocked_manual"
        if int(observation.get("repairable_validation_error_count") or 0) > 0:
            return "validation_blocked_repairable"
        return "validation_blocked_manual"
    return "uncertain"


def _observe_post_submit_page(
    port: int, worker_id: int, job: dict, attempt: int = 1
) -> dict:
    """Independently observe visible post-submit state through the existing CDP browser."""
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        pages = [page for context in browser.contexts for page in context.pages]
        pages = _bound_application_pages(browser, pages, job)
        if not pages:
            return {"confirmed": False, "reason": "post_submit_no_bound_application_page"}
        page, application_surface = _select_application_page_and_frame(pages)
        observed = application_surface.evaluate(
            r"""() => {
              const visible = (el) => {
                const style = getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden' &&
                  el.getClientRects().length > 0;
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
              const elementsById = new Map();
              for (const element of deepElements) {
                if (element.id && !elementsById.has(element.id)) {
                  elementsById.set(element.id, element);
                }
              }
              const byId = (id) => elementsById.get(id) || null;
              const historicalDuplicate = /^(?:your application (?:was|has been) already submitted|application already submitted|you have already applied(?: for this (?:job|position|role))?)(?:[.!]\s*)?(?:view (?:your )?application|check (?:your )?status)?$/i;
              const strongReceipt = /your application has been submitted|application (?:was |has been )?(?:successfully )?submitted|thank you for (?:applying|submitting your application)|we (have )?received your application|申请已提交|投递成功|申请成功/i;
              const providerSubmissionError = /there was an error (?:verifying|submitting|processing) (?:your )?application|error (?:verifying|submitting|processing) (?:your )?application|(?:unable|failed) to (?:verify|submit|process) (?:your )?application|(?:application|submission) (?:could not|couldn't|was not) (?:be )?(?:verified|submitted|processed)/i;
              const exactBadge = /^(applied|已申请|已投递)$/i;
              const submitLabel = /submit|send application|finish|complete application|提交申请|投递/i;
              const verificationText = /security code|verification code|one[- ]time (?:code|password)|\botp\b|verify (?:your )?email|email verification|验证码|校验码|验证邮箱/i;
              const unsafeRepairText = /video|audio|record(?:ing)?|camera|microphone|passport|national id|identity document|bank account|credit card|tax id|ssn|nric|身份证|护照|银行卡|录音|录像|摄像头|麦克风/i;
              const candidates = deepAll(
                '[role="status"],[aria-live],[data-qa*="confirm" i],[data-testid*="confirm" i],[class*="confirmation" i],[class*="success" i]'
              ).filter(visible).map((el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
              const lines = (document.body ? document.body.innerText : '').split(/\n+/)
                .map((line) => line.replace(/\s+/g, ' ').trim()).filter(Boolean);
              const structuredReceipt = candidates.find((text) => strongReceipt.test(text)) || '';
              const receiptText = structuredReceipt || lines.find((text) => strongReceipt.test(text)) || '';
              const historicalDuplicateText = candidates.find(
                (text) => historicalDuplicate.test(text)
              ) || '';
              const providerSubmissionErrorText = lines.find(
                (text) => providerSubmissionError.test(text)
              ) || '';
              const badgeText = deepAll('button,a,span,div')
                .filter(visible).map((el) => (el.innerText || '').replace(/\s+/g, ' ').trim())
                .find((text) => exactBadge.test(text)) || '';
              const context = (el) => el.closest(
                'li,fieldset,[data-qa*="field" i],[data-testid*="field" i],[class*="application-field" i],[class*="question" i],[class*="field" i]'
              ) || el.parentElement;
              const labelText = (el) => {
                const node = context(el);
                return ((node && node.innerText) || el.getAttribute('aria-label') || el.name || '')
                  .replace(/\s+/g, ' ').trim().slice(0, 500);
              };
              const controls = deepAll('input:not([type=hidden]),textarea,select')
                .filter(visible);
              const validationErrors = [];
              const seenErrors = new Set();
              const seenMessages = new Set();
              for (const el of controls) {
                let described = '';
                const describedBy = (el.getAttribute('aria-describedby') || '').trim().split(/\s+/).filter(Boolean);
                if (describedBy.length) {
                  described = describedBy.map((id) => {
                    const node = byId(id);
                    return node ? (node.innerText || node.textContent || '') : '';
                  }).join(' ').replace(/\s+/g, ' ').trim();
                }
                const nativeInvalid = Boolean(el.willValidate && !el.validity.valid);
                const ariaInvalid = el.getAttribute('aria-invalid') === 'true';
                const message = (el.validationMessage || described || '').replace(/\s+/g, ' ').trim();
                if (!nativeInvalid && !ariaInvalid && !message) continue;
                const label = labelText(el);
                const key = `${el.name || el.id || label}|${message}`;
                if (seenErrors.has(key)) continue;
                seenErrors.add(key);
                if (message) seenMessages.add(message);
                const type = el.tagName === 'SELECT' ? 'select' : (el.type || el.tagName.toLowerCase());
                const optionalClaimed = /\boptional\b|可选|非必填/i.test(label);
                const repairEvidence = `${label} ${message}`;
                const resumeFileRepair = type === 'file' &&
                  /\b(?:resume|curriculum vitae|cv)\b/i.test(repairEvidence);
                const repairable = !unsafeRepairText.test(repairEvidence) &&
                  type !== 'password' && (type !== 'file' || resumeFileRepair);
                validationErrors.push({
                  label: label.slice(0, 240),
                  message: message.slice(0, 240),
                  field_type: type,
                  optional_claimed: optionalClaimed,
                  repairable
                });
              }
              for (const alert of deepAll('[role="alert"],[aria-live="assertive"]').filter(visible)) {
                const message = (alert.innerText || alert.textContent || '').replace(/\s+/g, ' ').trim();
                if (!message || !/required|invalid|error|please (?:enter|select|complete|provide|upload)|必填|无效|错误|请选择|请填写/i.test(message)) continue;
                if (seenMessages.has(message)) continue;
                const key = `alert|${message}`;
                if (seenErrors.has(key)) continue;
                seenErrors.add(key);
                const resumeUploadAlert =
                  /\b(?:resume|curriculum vitae|cv)\b/i.test(message) &&
                  /\b(?:upload|attach|file|required|missing|invalid)\b/i.test(message) &&
                  !unsafeRepairText.test(message);
                validationErrors.push({
                  label: 'page validation alert',
                  message: message.slice(0, 240),
                  field_type: resumeUploadAlert ? 'file' : 'unknown',
                  optional_claimed: /\boptional\b|可选|非必填/i.test(message),
                  repairable: resumeUploadAlert
                });
              }
              const submitControls = deepAll('button,input[type=submit],input[type=button],[role="button"]')
                .filter((el) => visible(el) && submitLabel.test(
                  (el.innerText || el.value || el.getAttribute('aria-label') || '').trim()
                ));
              const captchaVisible = deepAll(
                'iframe,[class*="turnstile" i],[id*="turnstile" i],[class*="hcaptcha" i],[class*="recaptcha" i],[data-sitekey]'
              ).filter(visible).some((el) => {
                const rect = el.getBoundingClientRect();
                const marker = `${el.title || ''} ${el.src || ''} ${el.id || ''} ${el.className || ''}`.toLowerCase();
                return rect.width >= 80 && rect.height >= 40 && /captcha|turnstile|challenge/.test(marker);
              });
              const codeInputs = controls.filter((el) => {
                const maxLength = Number(el.maxLength || 0);
                return maxLength === 1 || /otp|verification|security.?code/i.test(`${el.name || ''} ${el.id || ''} ${el.autocomplete || ''}`);
              });
              const verificationVisible = captchaVisible ||
                (codeInputs.length >= 4 && verificationText.test(document.body ? document.body.innerText : '')) ||
                deepAll('form,section,dialog,[role="dialog"]')
                  .filter(visible).some((el) => verificationText.test(el.innerText || ''));
              const repairableCount = validationErrors.filter((item) => item.repairable).length;
              const manualCount = validationErrors.length - repairableCount;
              return {
                current_url: location.href,
                page_title: document.title || '',
                receipt_visible: Boolean(receiptText),
                receipt_structured: Boolean(structuredReceipt),
                historical_duplicate: Boolean(historicalDuplicateText),
                historical_duplicate_text: historicalDuplicateText.slice(0, 500),
                applied_badge_visible: Boolean(badgeText),
                confirmation_text: receiptText || historicalDuplicateText || badgeText,
                provider_submission_error_visible: Boolean(providerSubmissionErrorText),
                provider_submission_error_text: providerSubmissionErrorText.slice(0, 500),
                form_visible: deepAll('form').some(visible),
                submit_control_count: submitControls.length,
                validation_errors: validationErrors.slice(0, 12),
                validation_error_count: validationErrors.length,
                repairable_validation_error_count: repairableCount,
                manual_validation_error_count: manualCount,
                verification_visible: verificationVisible,
                captcha_visible: captchaVisible
              };
            }"""
        )
        observed["document_url"] = observed.get("current_url", "")
        observed["current_url"] = page.url
        screenshot = (
            config.APPLY_WORKER_DIR
            / f"worker-{worker_id}"
            / (
                "post-submit-observer.png"
                if attempt == 1
                else f"post-submit-observer-attempt-{attempt}.png"
            )
        )
        try:
            page.screenshot(path=str(screenshot), full_page=True)
            observed["screenshot_path"] = str(screenshot)
        except Exception:
            logger.exception("Post-submit screenshot capture failed")
            observed["screenshot_path"] = None
        receipt_is_decisive = bool(observed.get("receipt_visible")) and (
            bool(observed.get("receipt_structured"))
            or (
                observed.get("form_visible") is False
                and int(observed.get("submit_control_count") or 0) == 0
            )
        )
        observed["confirmed"] = bool(
            receipt_is_decisive or observed.get("applied_badge_visible")
        )
        observed["disposition"] = _classify_post_submit_observation(observed)
        observed["job_url"] = job.get("url")
        return observed
    except Exception as exc:
        logger.exception("Post-submit browser observation failed")
        return {
            "confirmed": False,
            "reason": f"post_submit_observer_error:{type(exc).__name__}",
        }
    finally:
        playwright.stop()


def _submission_evidence_consistent(model: dict | None, observer: dict) -> bool:
    """Require independent visible confirmation that agrees with the model claim."""
    if not model or observer.get("confirmed") is not True:
        return False
    if model.get("channel") == "direct_email":
        if observer.get("channel") != "direct_email":
            return False
        if model.get("send_accepted") is not True or model.get("sent_copy_verified") is not True:
            return False
        for key in ("recipient", "subject"):
            if str(model.get(key) or "").strip().casefold() != str(
                observer.get(key) or ""
            ).strip().casefold():
                return False
        model_attachments = {
            str(value).strip().casefold()
            for value in model.get("attachment_names", [])
            if str(value).strip()
        }
        observed_attachments = {
            str(value).strip().casefold()
            for value in observer.get("attachment_names", [])
            if str(value).strip()
        }
        return bool(model_attachments) and model_attachments == observed_attachments
    receipt_agrees = (
        model.get("receipt_visible") is True
        and observer.get("receipt_visible") is True
    )
    badge_agrees = (
        model.get("applied_badge_visible") is True
        and observer.get("applied_badge_visible") is True
    )
    if not (receipt_agrees or badge_agrees):
        return False

    model_text = " ".join(
        re.sub(
            r"[^\w]+", " ", str(model.get("confirmation_text") or "").casefold()
        ).split()
    )
    observed_text = " ".join(
        re.sub(
            r"[^\w]+", " ", str(observer.get("confirmation_text") or "").casefold()
        ).split()
    )
    text_agrees = bool(model_text and observed_text) and (
        model_text in observed_text
        or observed_text in model_text
        or (
            receipt_agrees
            and _looks_like_submission_receipt_text(model_text)
            and _looks_like_submission_receipt_text(observed_text)
        )
    )
    if not text_agrees:
        return False

    claimed_url = str(model.get("confirmation_url") or "").strip().rstrip("/")
    current_url = str(observer.get("current_url") or "").strip().rstrip("/")
    return not claimed_url or claimed_url == current_url


def _looks_like_submission_receipt_text(value: str) -> bool:
    """Recognize equivalent concise and verbose application receipt wording."""
    return bool(
        re.search(
            r"\b(?:your )?application (?:was |has been )?"
            r"(?:successfully )?(?:submitted|received)(?: successfully)?\b"
            r"|\bwe (?:have )?received your application\b"
            r"|\bthank you for (?:applying|submitting your application)\b"
            r"|申请已提交|投递成功|申请成功",
            value,
            flags=re.IGNORECASE,
        )
    )
