"""Resume and cover letter validation: banned words, fabrication detection, structural checks.

All validation is profile-driven -- no hardcoded personal data. The validator receives
a profile dict (from applypilot.config.load_profile()) and validates against the user's
actual skills, companies, projects, and school.

Validation modes
----------------
strict  -- banned words = hard errors that trigger retries (original behavior)
normal  -- banned words = warnings only; fabrication/structure = errors (default)
lenient -- banned words ignored; only fabrication and required structure checked
"""

import logging
import re
from difflib import SequenceMatcher

from applypilot.scoring.resume_plan import entry_recency_key, order_entries_by_recency

log = logging.getLogger(__name__)


# ── Universal Constants (not personal data) ───────────────────────────────

BANNED_WORDS: list[str] = [
    "passionate", "dedicated", "committed to",
    "utilizing", "utilize", "harnessing",
    "spearheaded", "spearhead", "orchestrated", "championed", "pioneered",
    "robust", "scalable solutions", "cutting-edge", "state-of-the-art", "best-in-class",
    "proven track record", "track record of success", "demonstrated ability",
    "strong communicator", "team player", "fast learner", "self-starter", "go-getter",
    "synergy", "cross-functional collaboration", "holistic",
    "transformative", "innovative solutions", "paradigm", "ecosystem",
    "proactive", "detail-oriented", "highly motivated",
    "seamless", "full lifecycle",
    "deep understanding", "extensive experience", "comprehensive knowledge",
    "thrives in", "excels at", "adept at", "well-versed in",
    "i am confident", "i believe", "i am excited",
    "plays a critical role", "instrumental in", "integral part of",
    "strong track record", "eager to", "eager",
    # Cover-letter-specific additions
    "this demonstrates", "this reflects", "i have experience with",
    "furthermore", "additionally", "moreover",
]

LLM_LEAK_PHRASES: list[str] = [
    "i am sorry", "i apologize", "i will try", "let me try",
    "i am at a loss", "i am truly sorry", "apologies for",
    "i keep fabricating", "i will have to admit", "one final attempt",
    "one last time", "if it fails again", "persistent errors",
    "i am having difficulty", "i made an error", "my mistake",
    "here is the corrected", "here is the revised", "here is the updated",
    "here is my", "below is the", "as requested",
    "note:", "disclaimer:", "important:",
    "i have rewritten", "i have removed", "i have fixed",
    "i have replaced", "i have updated", "i have corrected",
    "per your feedback", "based on your feedback", "as per the instructions",
    "the following resume", "the resume below",
    "the following cover letter", "the letter below",
]

# Known fabrication markers: completely unrelated tools/languages.
# Reasonable stretches (K8s, Terraform, Redis, Kafka etc.) are ALLOWED.
FABRICATION_WATCHLIST: set[str] = {
    # Languages with zero relation to the candidate's stack
    "c#", "c++", "golang", "rust", "ruby",
    "kotlin", "swift", "scala", "matlab",
    # Frameworks for wrong languages
    "spring", "django", "rails", "angular", "vue", "svelte",
    # Hard lies: certifications can't be stretched
    "certif", "certified", "pmp", "scrum master", "aws certified",
}

REQUIRED_SECTIONS: set[str] = {"SUMMARY", "TECHNICAL SKILLS", "EXPERIENCE", "PROJECTS", "EDUCATION"}


# ── Helpers ───────────────────────────────────────────────────────────────

def _build_skills_set(profile: dict) -> set[str]:
    """Build the set of allowed skills from the profile's skills_boundary."""
    boundary = profile.get("skills_boundary", {})
    allowed: set[str] = set()
    for category in boundary.values():
        if isinstance(category, (list, set)):
            allowed.update(s.lower().strip() for s in category)
    return allowed


def sanitize_text(text: str) -> str:
    """Auto-fix common LLM output issues instead of rejecting."""
    text = text.replace(" \u2014 ", ", ").replace("\u2014", ", ")   # em dash -> comma
    text = text.replace("\u2013", "-")    # en dash -> hyphen
    text = text.replace("\u201c", '"').replace("\u201d", '"')   # smart double quotes
    text = text.replace("\u2018", "'").replace("\u2019", "'")   # smart single quotes
    return text.strip()


# ── JSON Field Validation ─────────────────────────────────────────────────

def _flatten_tailored_output(data: dict) -> str:
    """Flatten only applicant-facing fields, excluding JD/evidence metadata."""
    values: list[str] = []
    for key in ("title", "summary"):
        values.append(str(data.get(key, "")))
    education = data.get("education", "")
    if isinstance(education, list):
        values.extend(str(item) for item in education)
    else:
        values.append(str(education))
    skills = data.get("skills", {})
    if isinstance(skills, dict):
        values.extend(str(value) for value in skills.values())
    for key in ("experience", "projects"):
        entries = data.get(key, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            values.extend(str(entry.get(field, "")) for field in ("header", "subtitle"))
            values.extend(str(item) for item in entry.get("bullets", []))
    return "\n".join(values)


def _numeric_claims(text: str) -> set[str]:
    """Return normalized numeric tokens used in factual claims."""
    return {
        token.replace(",", "").lstrip("~").rstrip(".").casefold()
        for token in re.findall(r"(?<![A-Za-z])~?\d[\d,.]*(?:%|\+)?", text)
    }


def _claim_stems(text: str) -> set[str]:
    """Return coarse lexical stems for conservative JD-contamination checks."""
    stems: set[str] = set()
    for token in re.findall(r"[a-z]+", text.casefold()):
        if len(token) < 5:
            continue
        if token.endswith("ies") and len(token) > 6:
            token = token[:-3] + "y"
        elif token.endswith("ing") and len(token) > 7:
            token = token[:-3]
        elif (token.endswith("ed") and len(token) > 6) or (token.endswith("es") and len(token) > 6):
            token = token[:-2]
        elif token.endswith("s") and len(token) > 5:
            token = token[:-1]
        stems.add(token)
    return stems


def _source_role_for_company(original_text: str, company: str) -> str:
    """Extract the role line immediately following a preserved company."""
    lines = [line.strip() for line in original_text.splitlines() if line.strip()]
    for index, line in enumerate(lines[:-1]):
        if company.casefold() not in line.casefold():
            continue
        role_line = lines[index + 1]
        month = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
        date_match = re.search(
            rf"(?={month}(?:\s+\d{{4}}|\s*[-–—]\s*{month}\s+\d{{4}}))",
            role_line,
            flags=re.IGNORECASE,
        )
        return (role_line[:date_match.start()] if date_match else role_line).strip(" |\t")
    return ""


def _section_pattern(section: str) -> str:
    """Return a regex pattern matching standard section names and their common variants."""
    sec = section.strip().upper()
    if sec == "EXPERIENCE":
        return r"(?:(?:WORK|PROFESSIONAL|RELEVANT|SELECTED|KEY|EMPLOYMENT)\s+)?(?:EXPERIENCE|HISTORY)"
    if sec == "PROJECTS":
        return r"(?:(?:SELECTED|KEY|PERSONAL|ACADEMIC|RELEVANT)\s+)?PROJECTS"
    if sec == "EDUCATION":
        return r"(?:ACADEMIC\s+BACKGROUND|EDUCATION(?:\s+BACKGROUND)?)"
    if sec in {"TECHNICAL SKILLS", "SKILLS"}:
        return r"(?:TECHNICAL\s+SKILLS|CORE\s+SKILLS|SKILLS|TECH\s+STACK|TECHNOLOGIES)"
    if sec == "SUMMARY":
        return r"(?:(?:PROFESSIONAL|CAREER|EXECUTIVE)\s+)?SUMMARY"
    return re.escape(section)


def _section_entries(text: str, section: str) -> list[dict]:
    """Parse one structured resume section without assuming its current order is correct."""
    from applypilot.scoring.pdf import canonical_section_header, parse_entries, parse_resume

    canonical = canonical_section_header(section) or section
    return parse_entries(parse_resume(text)["sections"].get(canonical, ""))


def _source_entry_headers(text: str, section: str) -> list[str]:
    """Return entry headers in canonical current/recent-first order."""
    return [
        str(entry.get("title") or "")
        for entry in order_entries_by_recency(_section_entries(text, section))
    ]


def _entry_matches_source(header: str, source_header: str) -> bool:
    left = set(re.findall(r"[a-z0-9]+", header.casefold()))
    right = set(re.findall(r"[a-z0-9]+", source_header.casefold()))
    return bool(left and right and (left <= right or right <= left or len(left & right) >= 2))


def _best_source_match_index(header: str, source_headers: list[str]) -> int | None:
    """Choose the closest header instead of the first loose token overlap."""
    normalized = header.strip().casefold()
    for index, source_header in enumerate(source_headers):
        if normalized == source_header.strip().casefold():
            return index
    left = set(re.findall(r"[a-z0-9]+", normalized))
    candidates: list[tuple[float, int]] = []
    for index, source_header in enumerate(source_headers):
        if not _entry_matches_source(header, source_header):
            continue
        right = set(re.findall(r"[a-z0-9]+", source_header.casefold()))
        union = left | right
        candidates.append((len(left & right) / len(union) if union else 0.0, index))
    return max(candidates, default=(0.0, -1))[1] if candidates else None


def _match_source_indices(
    output_headers: list[str], source_headers: list[str]
) -> list[int | None]:
    """Match output entries to distinct source entries, preferring exact names.

    A loose token match is useful when a model shortens a long organization or
    project name, but it must not let one output entry stand in for multiple
    source entries that happen to share generic words such as ``planning`` or
    ``design``.
    """
    matches: list[int | None] = [None] * len(output_headers)
    unused_source_indices = set(range(len(source_headers)))

    for output_index, header in enumerate(output_headers):
        normalized = header.strip().casefold()
        exact_index = next(
            (
                source_index
                for source_index in unused_source_indices
                if normalized == source_headers[source_index].strip().casefold()
            ),
            None,
        )
        if exact_index is not None:
            matches[output_index] = exact_index
            unused_source_indices.remove(exact_index)

    candidates: list[tuple[float, int, int]] = []
    for output_index, header in enumerate(output_headers):
        if matches[output_index] is not None:
            continue
        left = set(re.findall(r"[a-z0-9]+", header.casefold()))
        for source_index in unused_source_indices:
            source_header = source_headers[source_index]
            if not _entry_matches_source(header, source_header):
                continue
            right = set(re.findall(r"[a-z0-9]+", source_header.casefold()))
            union = left | right
            score = len(left & right) / len(union) if union else 0.0
            candidates.append((score, output_index, source_index))

    for _score, output_index, source_index in sorted(candidates, reverse=True):
        if matches[output_index] is not None or source_index not in unused_source_indices:
            continue
        matches[output_index] = source_index
        unused_source_indices.remove(source_index)

    return matches


def _section_entry_bullet_counts(text: str, section: str) -> list[tuple[str, int]]:
    return [
        (
            str(entry.get("title") or ""),
            len([item for item in entry.get("bullets", []) if str(item).strip()]),
        )
        for entry in _section_entries(text, section)
    ]


def _entry_has_supplemental_evidence(header: str, original_text: str, section: str) -> bool:
    """Check whether an entry header has evidence in supplemental original_text."""
    if not original_text or not header.strip():
        return False

    from applypilot.scoring.pdf import parse_entries

    matches = re.finditer(
        rf"(?ms)^\s*(?i:{_section_pattern(section)})\s*$\s*(.*?)(?=^\s*[A-Z][A-Z ]{{3,}}\s*$|\Z)",
        original_text,
    )
    for match in matches:
        for entry in parse_entries(match.group(1)):
            title = str(entry.get("title") or "").strip()
            if title and _entry_matches_source(header, title):
                return True

    supp_match = re.search(
        r"(?ms)^\s*SUPPLEMENTAL CANDIDATE EVIDENCE\s*$\s*(.*)",
        original_text,
        re.IGNORECASE,
    )
    if supp_match:
        supp_text = supp_match.group(1)
        for entry in _section_entries(supp_text, section):
            title = str(entry.get("title") or "").strip()
            if title and _entry_matches_source(header, title):
                return True
        for entry in parse_entries(supp_text):
            title = str(entry.get("title") or "").strip()
            if title and _entry_matches_source(header, title):
                return True

    return False


def _duplicate_project_errors(headers: list[str]) -> list[str]:
    seen: set[str] = set()
    errors = []
    for header in headers:
        identity = " ".join(header.casefold().split())
        if identity and identity in seen:
            errors.append(f"Duplicate project entry: '{header}'. Consolidate its supported evidence.")
        seen.add(identity)
    return errors


def _entry_allocation_warnings(
    entry_counts: list[tuple[str, int]],
    section: str,
    *,
    recent_minimum: int = 2,
) -> list[str]:
    """Flag advisory bullet count targets without blocking validation."""
    if not entry_counts:
        return []
    warnings: list[str] = []
    counts = [count for _header, count in entry_counts]
    if any(count > 4 for count in counts):
        warnings.append(
            f"No {section.lower()} entry should ideally exceed four bullets; got {counts}."
        )
    if counts and counts[0] < recent_minimum:
        warnings.append(
            f"The leading {section.lower()} entry should ideally contain at least {recent_minimum} substantive bullets."
        )
    return warnings


def _entry_allocation_errors(
    entry_counts: list[tuple[str, int]],
    source_headers: list[str],
    section: str,
    *,
    recent_minimum: int = 2,
) -> list[str]:
    """Validate entry constraints.

    Experience entries must remain in reverse chronological order (newest first),
    enforced by actual dates.
    Projects may freely reorder by relevance.
    Mere bullet count targets are advisory warnings; at least 1 substantive
    bullet per retained entry is enforced by callers.
    """
    return []


def _bullet_narrative_warnings(entries: list[dict], section: str) -> list[str]:
    """Flag obvious keyword fragments while leaving semantic quality to the Judge."""
    warnings: list[str] = []
    for entry in entries:
        header = str(entry.get("header") or "")
        for index, bullet in enumerate(entry.get("bullets", []), start=1):
            text = str(bullet).strip()
            word_count = len(re.findall(r"\b[\w+#./-]+\b", text))
            if word_count < 9:
                warnings.append(
                    f"{section.title()} '{header}' bullet {index} is a fragment "
                    f"({word_count} words); write a complete action, artifact/method, and result/context statement."
                )
    return warnings


def _education_items(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [line.strip() for line in str(value or "").splitlines() if line.strip()]


def validate_json_fields(
    data: dict,
    profile: dict,
    mode: str = "normal",
    original_text: str = "",
    job_description: str = "",
    job_title: str = "",
    target_company: str = "",
    selection_source_text: str | None = None,
) -> dict:
    """Validate individual JSON fields from an LLM-generated tailored resume.

    Args:
        data:    Parsed JSON from the LLM (title, summary, skills, experience, projects, education).
        profile: User profile dict from load_profile().
        mode:    Validation strictness — "strict", "normal", or "lenient".
                 strict  → banned words are errors (trigger retries)
                 normal  → banned words are warnings (no retry)
                 lenient → banned words ignored entirely

    Returns:
        {"passed": bool, "errors": list[str], "warnings": list[str]}
    """
    errors: list[str] = []
    warnings: list[str] = []

    # Required keys — always checked regardless of mode (summary is optional)
    for key in ("title", "skills", "experience", "education", "evidence_map"):
        if key not in data or not data[key]:
            errors.append(f"Missing required field: {key}")
    selection_source = selection_source_text if selection_source_text is not None else original_text
    source_has_projects = bool(
        re.search(
            r"(?im)^\s*(?:(?:selected|key|personal|academic|relevant)\s+)?projects\s*$",
            selection_source,
        )
    )
    if source_has_projects and not data.get("projects"):
        errors.append("Selected source contains projects, but the tailored output dropped all projects.")
    if "projects" not in data:
        data["projects"] = []
    if errors:
        return {"passed": False, "errors": errors, "warnings": warnings}

    # A target heading can describe the function, but it must not silently
    # promote the candidate beyond the seniority advertised by the JD.
    seniority_terms = {"senior", "lead", "principal", "manager", "director", "head"}
    output_title_terms = set(re.findall(r"[a-z]+", str(data.get("title", "")).casefold()))
    job_title_terms = set(re.findall(r"[a-z]+", job_title.casefold()))
    invented_seniority = sorted((output_title_terms & seniority_terms) - job_title_terms)
    if job_title and invented_seniority:
        errors.append(
            "Target heading adds seniority absent from the job title: "
            + ", ".join(invented_seniority)
        )

    # Collect all text for bulk checks (summary is optional)
    all_text_parts: list[str] = [str(data["summary"])] if data.get("summary") else []

    # Skills: check for fabrication (always enforced)
    if isinstance(data["skills"], dict):
        skills_text = " ".join(str(v) for v in data["skills"].values()).lower()
        for fake in FABRICATION_WATCHLIST:
            if len(fake) <= 2:
                continue
            if fake in skills_text:
                errors.append(f"Fabricated skill: '{fake}'")
        if original_text:
            source_tokens = set(re.findall(r"[a-z0-9+#.]+", original_text.casefold()))
            generic_skill_tokens = {
                "and", "analysis", "analytics", "data", "design", "engineering",
                "modeling", "models", "systems", "tools", "workflow", "workflows",
            }
            output_tokens = {
                token
                for token in re.findall(r"[a-z0-9+#.]+", skills_text.casefold())
                if len(token) >= 3 and token not in generic_skill_tokens
            }
            new_skill_tokens = sorted(output_tokens - source_tokens)
            if new_skill_tokens:
                errors.append(
                    "Skills section adds tokens absent from the selected source resume: "
                    + ", ".join(new_skill_tokens[:8])
                )

    # Experience/project selection budget: keep the newest source entry,
    # retire at most one entry per section, and retain meaningful evidence.
    resume_facts = profile.get("resume_facts", {})
    preserved_companies = resume_facts.get("preserved_companies", [])

    if isinstance(data["experience"], list):
        source_experience = _source_entry_headers(selection_source, "EXPERIENCE")
        output_headers = [str(entry.get("header", "")) for entry in data["experience"]]
        matched_experience = set(_match_source_indices(output_headers, source_experience))
        dropped_experience = [
            header for index, header in enumerate(source_experience)
            if index not in matched_experience
        ]
        if len(dropped_experience) > 1:
            errors.append(
                "Experience may retire at most one source entry; dropped: "
                + ", ".join(dropped_experience[:3])
            )
        if source_experience and source_experience[0] in dropped_experience:
            errors.append("The most recent experience entry cannot be retired.")

        # Grounding: reject experience entries without evidence in selection source or supplemental evidence
        for entry in data["experience"]:
            header = str(entry.get("header", "")).strip()
            if not header:
                continue
            is_in_source = any(_entry_matches_source(header, sh) for sh in source_experience)
            is_in_supplemental = False
            if original_text and (original_text != selection_source or "SUPPLEMENTAL CANDIDATE EVIDENCE" in original_text):
                is_in_supplemental = _entry_has_supplemental_evidence(header, original_text, "EXPERIENCE")
            if not is_in_source and not is_in_supplemental:
                errors.append(
                    f"Experience entry '{header}' has no evidence mapping to the selected source resume or supplemental evidence."
                )

        for company in preserved_companies:
            if not any(company.casefold() in header.casefold() for header in source_experience):
                continue
            has_company = any(
                company.lower() in str(e.get("header", "")).lower()
                for e in data["experience"]
            )
            company_was_retired = any(
                company.casefold() in header.casefold() for header in dropped_experience
            )
            if not has_company and not company_was_retired:
                errors.append(f"Company '{company}' missing from experience without an allowed retirement")
                continue
            if original_text:
                source_role = _source_role_for_company(original_text, company)
                matching_entries = [
                    entry
                    for entry in data["experience"]
                    if company.casefold() in str(entry.get("header", "")).casefold()
                ]
                matching_blob = " ".join(
                    f"{entry.get('header', '')} {entry.get('subtitle', '')}"
                    for entry in matching_entries
                ).casefold()
                if source_role and source_role.casefold() not in matching_blob:
                    errors.append(
                        f"Experience title for '{company}' does not preserve the selected "
                        f"source role exactly: {source_role}"
                    )
        for entry in data["experience"]:
            bullets = [str(item).strip() for item in entry.get("bullets", []) if str(item).strip()]
            if not bullets:
                errors.append(f"Experience '{entry.get('header', '')}' must retain at least one bullet.")
            all_text_parts.extend(bullets)

        # Experience entries must remain in reverse chronological order by actual date
        dated_entries = [
            (str(entry.get("header") or ""), entry_recency_key(entry))
            for entry in data["experience"]
            if isinstance(entry, dict) and entry_recency_key(entry) != (0, 0, 0)
        ]
        for i in range(len(dated_entries) - 1):
            if dated_entries[i][1] < dated_entries[i + 1][1]:
                errors.append(
                    f"Experience entries must remain in reverse chronological order; "
                    f"'{dated_entries[i][0]}' is older than '{dated_entries[i + 1][0]}'."
                )
                break

        experience_counts = [
            (
                str(entry.get("header") or ""),
                len([item for item in entry.get("bullets", []) if str(item).strip()]),
            )
            for entry in data["experience"]
        ]
        errors.extend(
            _entry_allocation_errors(
                experience_counts,
                source_experience,
                "EXPERIENCE",
            )
        )
        warnings.extend(
            _entry_allocation_warnings(
                experience_counts,
                "EXPERIENCE",
            )
        )
        warnings.extend(_bullet_narrative_warnings(data["experience"], "EXPERIENCE"))

    # Projects: collect bullets
    if isinstance(data["projects"], list):
        source_projects = _source_entry_headers(selection_source, "PROJECTS")
        output_project_headers = [str(entry.get("header", "")) for entry in data["projects"]]
        errors.extend(_duplicate_project_errors(output_project_headers))
        matched_projects = set(_match_source_indices(output_project_headers, source_projects))
        dropped_projects = [
            header for index, header in enumerate(source_projects)
            if index not in matched_projects
        ]
        if len(dropped_projects) > 1:
            warnings.append(
                "Review project selection for relevance; omitted: "
                + ", ".join(dropped_projects[:3])
            )

        # Grounding: reject project entries without evidence in selection source or supplemental evidence
        for entry in data["projects"]:
            header = str(entry.get("header", "")).strip()
            if not header:
                continue
            is_in_source = any(_entry_matches_source(header, sh) for sh in source_projects)
            is_in_supplemental = False
            if original_text and (original_text != selection_source or "SUPPLEMENTAL CANDIDATE EVIDENCE" in original_text):
                is_in_supplemental = _entry_has_supplemental_evidence(header, original_text, "PROJECTS")
            if not is_in_source and not is_in_supplemental:
                errors.append(
                    f"Project entry '{header}' has no evidence mapping to the selected source resume or supplemental evidence."
                )

        for entry in data["projects"]:
            bullets = [str(item).strip() for item in entry.get("bullets", []) if str(item).strip()]
            if not bullets:
                errors.append(f"Project '{entry.get('header', '')}' must retain at least one bullet.")
            all_text_parts.extend(bullets)
        project_counts = [
            (
                str(entry.get("header") or ""),
                len([item for item in entry.get("bullets", []) if str(item).strip()]),
            )
            for entry in data["projects"]
        ]
        errors.extend(
            _entry_allocation_errors(
                project_counts,
                source_projects,
                "PROJECTS",
            )
        )
        warnings.extend(
            _entry_allocation_warnings(
                project_counts,
                "PROJECTS",
            )
        )
        warnings.extend(_bullet_narrative_warnings(data["projects"], "PROJECTS"))

    # The target employer may be named in a target-facing summary, but it
    # cannot appear inside prior experience/project history unless the source
    # already contains it. This catches direct JD-to-history contamination.
    if target_company and target_company.casefold() not in original_text.casefold():
        history_blob = " ".join(
            str(value)
            for section in (data.get("experience", []), data.get("projects", []))
            if isinstance(section, list)
            for entry in section
            if isinstance(entry, dict)
            for value in (
                entry.get("header", ""),
                entry.get("subtitle", ""),
                " ".join(str(item) for item in entry.get("bullets", [])),
            )
        )
        if target_company.casefold() in history_blob.casefold():
            errors.append(
                f"Target company '{target_company}' was copied into candidate history."
            )

    # Education: preserved school must be present (always enforced)
    preserved_school = resume_facts.get("preserved_school", "")
    if preserved_school:
        education_items = _education_items(data.get("education", ""))
        edu = "\n".join(education_items)
        schools = [school.strip() for school in preserved_school.split(";") if school.strip()]
        for school in schools:
            if school.casefold() not in edu.casefold():
                errors.append(f"Education '{school}' missing")
        if len(schools) > 1:
            matched_item_indexes = [
                next(
                    (
                        index
                        for index, item in enumerate(education_items)
                        if school.casefold() in item.casefold()
                    ),
                    None,
                )
                for school in schools
            ]
            if None not in matched_item_indexes and len(set(matched_item_indexes)) < len(schools):
                errors.append(
                    "Education must use one separate item/line per institution; do not combine schools into one paragraph."
                )

    # Bulk text checks
    all_text = " ".join(all_text_parts).lower()

    # A summary is entirely candidate-facing, so JD-only action/domain words
    # are especially likely to be accidental JD-to-history contamination.
    # Target-title terms and ordinary connective/recruiting language are
    # excluded; specific claim terms must already be grounded in the source.
    if original_text and job_description:
        sensitive_claim_terms = {
            "adoption", "banking", "behavior", "campaign", "churn", "clinical",
            "conversion", "customer", "engagement", "experiment", "financial",
            "growth", "healthcare", "insurance", "interview", "logistic",
            "manufactur", "marketing", "medical", "monetization", "pharmaceutical",
            "retention", "revenue", "sale", "semiconductor",
        }
        source_stems = _claim_stems(original_text)
        jd_stems = _claim_stems(job_description)
        title_stems = _claim_stems(job_title)
        summary_stems = _claim_stems(str(data.get("summary", "")))
        jd_only_summary_claims = sorted(
            ((summary_stems & jd_stems) & sensitive_claim_terms)
            - source_stems
            - title_stems
        )
        if jd_only_summary_claims:
            errors.append(
                "Summary imports JD-only claim terms absent from the selected source: "
                + ", ".join(jd_only_summary_claims[:8])
            )

        # A single source trial/pilot/experiment must not become a track record
        # merely through pluralization in the summary.
        summary_lower = str(data.get("summary", "")).casefold()
        source_lower = original_text.casefold()
        inflated_plural_events = [
            noun
            for noun in ("trial", "pilot", "experiment")
            if re.search(rf"\b{noun}s\b", summary_lower)
            and not re.search(rf"\b{noun}s\b", source_lower)
            and re.search(rf"\b{noun}\b", source_lower)
        ]
        if inflated_plural_events:
            errors.append(
                "Summary pluralizes a single source event: "
                + ", ".join(inflated_plural_events)
            )

    # Evidence mapping is part of the generation contract. Quotes must be
    # copied verbatim from the selected resume, and at least two mapped JD
    # requirements must be reflected in the applicant-facing output.
    evidence_map = data.get("evidence_map", [])
    grounded_requirements: list[str] = []
    normalized_source = re.sub(r"\s+", " ", original_text).strip().casefold()
    jd_tokens = set(re.findall(r"[a-z0-9+#.]+", job_description.casefold()))
    if not isinstance(evidence_map, list):
        errors.append("evidence_map must be a list.")
    else:
        for item in evidence_map[:5]:
            if not isinstance(item, dict):
                continue
            requirement = str(item.get("requirement", "")).strip()
            quote = str(item.get("source_quote", "")).strip()
            support_level = str(item.get("support_level", "")).strip().casefold()
            if support_level not in {"direct", "transferable", "gap"}:
                errors.append(
                    "Each evidence_map item needs support_level direct, transferable, or gap."
                )
                continue
            if support_level == "gap":
                if quote:
                    errors.append("Gap evidence_map items must use an empty source_quote.")
                continue
            quote_grounded = (
                len(quote.split()) >= 6
                and re.sub(r"\s+", " ", quote).strip().casefold() in normalized_source
            )
            requirement_tokens = {
                token
                for token in re.findall(r"[a-z0-9+#.]+", requirement.casefold())
                if len(token) >= 3
            }
            requirement_in_jd = not job_description or bool(requirement_tokens & jd_tokens)
            if quote_grounded and requirement and requirement_in_jd:
                grounded_requirements.append(requirement)
        if len(grounded_requirements) < 2:
            errors.append(
                "Tailoring evidence map contains fewer than 2 JD-grounded, source-verified mappings."
            )
        else:
            output_tokens = set(
                re.findall(r"[a-z0-9+#.]+", _flatten_tailored_output(data).casefold())
            )
            covered = sum(
                bool(
                    {
                        token
                        for token in re.findall(r"[a-z0-9+#.]+", requirement.casefold())
                        if len(token) >= 4
                    }
                    & output_tokens
                )
                for requirement in grounded_requirements
            )
            if covered < 2:
                errors.append(
                    f"Tailored resume explicitly addresses only {covered} grounded JD priorities."
                )

    if original_text:
        new_numbers = sorted(
            _numeric_claims(_flatten_tailored_output(data)) - _numeric_claims(original_text)
        )
        if new_numbers:
            errors.append(
                "Tailored resume adds numeric claims absent from the selected source: "
                + ", ".join(new_numbers[:8])
            )

    # LLM self-talk is always an error regardless of mode (indicates broken output)
    found_leaks = [p for p in LLM_LEAK_PHRASES if p in all_text]
    if found_leaks:
        errors.append(f"LLM self-talk: '{found_leaks[0]}'")

    # Banned filler words — severity depends on mode
    if mode != "lenient":
        found_banned = [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", all_text)]
        if found_banned:
            msg = f"Banned words: {', '.join(found_banned[:5])}"

            if mode == "strict":
                errors.append(msg)
            else:  # normal
                warnings.append(msg)

    return {"passed": len(errors) == 0, "errors": errors, "warnings": warnings}


# ── Full Resume Text Validation ───────────────────────────────────────────

def current_profile_resume_fact_errors(text: str, profile: dict) -> list[str]:
    """Return explicit resume facts that conflict with the current profile.

    Omitted optional facts are allowed. Only an explicit GPA scoped to its
    institution is checked, so historical artifacts can remain immutable while
    stale factual claims are prevented from being reused.
    """
    education = [
        item
        for item in profile.get("education", [])
        if isinstance(item, dict) and str(item.get("institution") or "").strip()
    ]
    if not education:
        return []

    text_lower = text.casefold()
    institution_spans: list[tuple[int, int, dict]] = []
    for item in education:
        institution = str(item["institution"]).strip()
        for match in re.finditer(re.escape(institution.casefold()), text_lower):
            institution_spans.append((match.start(), match.end(), item))
    institution_spans.sort(key=lambda span: span[0])

    errors: list[str] = []
    for index, (_, end, item) in enumerate(institution_spans):
        expected_match = re.search(r"\b(\d+(?:\.\d+)?)\b", str(item.get("gpa") or ""))
        if not expected_match or not item.get("gpa_may_be_disclosed", True):
            continue
        segment_end = (
            institution_spans[index + 1][0]
            if index + 1 < len(institution_spans)
            else min(len(text), end + 500)
        )
        segment = text[end:segment_end]
        claimed = re.search(r"\bgpa\s*[:=]?\s*(\d+(?:\.\d+)?)\b", segment, re.IGNORECASE)
        if not claimed:
            continue
        expected = expected_match.group(1).rstrip("0").rstrip(".")
        actual = claimed.group(1).rstrip("0").rstrip(".")
        if actual != expected:
            errors.append(
                f"Education GPA for '{item['institution']}' is {actual}, "
                f"but the current profile records {expected}."
            )
    return errors


def validate_tailored_resume(text: str, profile: dict, original_text: str = "", *,
                             selection_source_text: str | None = None) -> dict:
    """Programmatic validation of a tailored resume against the user's profile.

    Args:
        text: The tailored resume text to validate.
        profile: User profile dict from load_profile().
        original_text: The original base resume text (for fabrication comparison).

    Returns:
        {"passed": bool, "errors": list[str], "warnings": list[str]}
    """
    errors: list[str] = []
    warnings: list[str] = []
    text_lower = text.lower()

    selection_source = original_text if selection_source_text is None else selection_source_text
    personal = profile.get("personal", {})
    resume_facts = profile.get("resume_facts", {})

    # 1. Check required sections exist (flexible matching, summary is optional)
    section_variants: dict[str, list[str]] = {
        "TECHNICAL SKILLS": ["technical skills", "skills", "tech stack", "core skills", "technologies"],
        "EXPERIENCE": ["experience", "work experience", "professional experience"],
        "EDUCATION": ["education", "academic background"],
    }
    if not selection_source or re.search(
        r"(?im)^\s*(?:(?:selected|key|personal|academic|relevant)\s+)?projects\s*$",
        selection_source,
    ):
        section_variants["PROJECTS"] = [
            "projects", "personal projects", "key projects", "selected projects", "academic projects", "relevant projects"
        ]
    for section, variants in section_variants.items():
        if not any(v in text_lower for v in variants):
            errors.append(f"Missing required section: {section} (or variant)")

    # 2. Check name preserved (warn, don't error -- we can inject it)
    display_name = (
        personal.get("preferred_display_name")
        or personal.get("preferred_name")
        or personal.get("full_name", "")
    )
    if display_name and display_name.casefold() not in text_lower:
        warnings.append(f"Name '{display_name}' missing -- will be injected")

    # 3-4. Apply the same bounded-retirement and density rules on revalidation.
    source_experience = _source_entry_headers(selection_source, "EXPERIENCE") if selection_source else []
    experience_entries = _section_entries(text, "EXPERIENCE")
    output_experience = [str(e.get("title") or "") for e in experience_entries]
    matched_experience = set(_match_source_indices(output_experience, source_experience))
    dropped_experience = [
        header for index, header in enumerate(source_experience)
        if index not in matched_experience
    ]
    if len(dropped_experience) > 1:
        errors.append("Experience retires more than one source entry.")
    if source_experience and source_experience[0] in dropped_experience:
        errors.append("The most recent experience entry cannot be retired.")

    # Grounding: reject experience entries without evidence in selection source or supplemental evidence
    if original_text:
        for entry in experience_entries:
            title = str(entry.get("title") or "").strip()
            if not title:
                continue
            is_in_source = any(_entry_matches_source(title, sh) for sh in source_experience)
            is_in_supplemental = False
            if "SUPPLEMENTAL CANDIDATE EVIDENCE" in original_text or original_text != text:
                is_in_supplemental = _entry_has_supplemental_evidence(title, original_text, "EXPERIENCE")
            if not is_in_source and not is_in_supplemental:
                errors.append(
                    f"Experience entry '{title}' has no evidence mapping to the selected source resume or supplemental evidence."
                )

    # Experience entries must remain in reverse chronological order by actual date
    dated_entries = [
        (str(entry.get("title") or ""), entry_recency_key(entry))
        for entry in experience_entries
        if entry_recency_key(entry) != (0, 0, 0)
    ]
    for i in range(len(dated_entries) - 1):
        if dated_entries[i][1] < dated_entries[i + 1][1]:
            errors.append(
                f"Experience entries must remain in reverse chronological order; "
                f"'{dated_entries[i][0]}' is older than '{dated_entries[i + 1][0]}'."
            )
            break

    experience_counts = _section_entry_bullet_counts(text, "EXPERIENCE")
    if any(count < 1 for _, count in experience_counts):
        errors.append("Every retained experience entry must contain at least one bullet.")
    errors.extend(
        _entry_allocation_errors(
            experience_counts,
            source_experience,
            "EXPERIENCE",
        )
    )
    warnings.extend(
        _entry_allocation_warnings(
            experience_counts,
            "EXPERIENCE",
        )
    )

    source_projects = _source_entry_headers(selection_source, "PROJECTS") if selection_source else []
    output_projects = [str(e.get("title") or "") for e in _section_entries(text, "PROJECTS")]
    errors.extend(_duplicate_project_errors(output_projects))
    matched_projects = set(_match_source_indices(output_projects, source_projects))
    dropped_projects = [
        header for index, header in enumerate(source_projects)
        if index not in matched_projects
    ]
    if len(dropped_projects) > 1:
        warnings.append("Review project selection for relevance; more than one source project omitted.")

    # Grounding: reject project entries without evidence in selection source or supplemental evidence
    if original_text:
        for entry in _section_entries(text, "PROJECTS"):
            title = str(entry.get("title") or "").strip()
            if not title:
                continue
            is_in_source = any(_entry_matches_source(title, sh) for sh in source_projects)
            is_in_supplemental = False
            if "SUPPLEMENTAL CANDIDATE EVIDENCE" in original_text or original_text != text:
                is_in_supplemental = _entry_has_supplemental_evidence(title, original_text, "PROJECTS")
            if not is_in_source and not is_in_supplemental:
                errors.append(
                    f"Project entry '{title}' has no evidence mapping to the selected source resume or supplemental evidence."
                )

    project_counts = _section_entry_bullet_counts(text, "PROJECTS")
    if any(count < 1 for _, count in project_counts):
        errors.append("Every retained project entry must contain at least one bullet.")
    errors.extend(
        _entry_allocation_errors(
            project_counts,
            source_projects,
            "PROJECTS",
        )
    )
    warnings.extend(
        _entry_allocation_warnings(
            project_counts,
            "PROJECTS",
        )
    )
    # 5. Check school preserved
    preserved_school = resume_facts.get("preserved_school", "")
    for school in [school.strip() for school in preserved_school.split(";") if school.strip()]:
        if school.casefold() not in text_lower:
            errors.append(f"Education '{school}' missing")
    schools = [school.strip() for school in preserved_school.split(";") if school.strip()]
    education_match = re.search(
        r"(?ims)^\s*EDUCATION\s*$\s*(.*?)(?=^\s*[A-Z][A-Z ]{3,}\s*$|\Z)",
        text,
    )
    if education_match and len(schools) > 1:
        education_lines = [
            line.strip() for line in education_match.group(1).splitlines() if line.strip()
        ]
        school_line_indexes = [
            next(
                (
                    index
                    for index, line in enumerate(education_lines)
                    if school.casefold() in line.casefold()
                ),
                None,
            )
            for school in schools
        ]
        if None not in school_line_indexes and len(set(school_line_indexes)) < len(schools):
            errors.append(
                "Education must place each institution on its own line, not in a combined paragraph."
            )

    # Optional GPA omission remains valid, but an explicit stale value cannot.
    errors.extend(current_profile_resume_fact_errors(text, profile))

    # 6. Check contact info preserved (warn, don't error -- we can inject)
    email = personal.get("email", "")
    phone = personal.get("phone", "")
    if email and email.lower() not in text_lower:
        warnings.append("Email missing -- will be injected")
    if phone and phone not in text:
        warnings.append("Phone missing -- will be injected")

    layout = profile.get("tailoring", {}).get("resume_layout", {})
    if layout.get("header_contact_immediately_after_name", False):
        from applypilot.scoring.pdf import canonical_section_header

        header_lines: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if canonical_section_header(stripped):
                break
            if stripped:
                header_lines.append(stripped)
        if len(header_lines) != 2 or "@" not in header_lines[1]:
            errors.append(
                "Resume header must place the contact line immediately after the name "
                "without a target-job-title line."
            )

    min_final_sentence_words = int(layout.get("summary_min_final_sentence_words", 0) or 0)
    summary_match = re.search(
        r"(?ims)^\s*SUMMARY\s*$\s*(.*?)(?=^\s*[A-Z][A-Z ]{3,}\s*$)", text
    )
    if min_final_sentence_words and summary_match:
        summary = re.sub(r"\s+", " ", summary_match.group(1)).strip()
        sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", summary) if part.strip()]
        final_sentence_words = len(re.findall(r"\b[\w+#./-]+\b", sentences[-1])) if sentences else 0
        if final_sentence_words < min_final_sentence_words:
            errors.append(
                "Summary ends with an undersized sentence "
                f"({final_sentence_words} words; minimum {min_final_sentence_words})."
            )

    # 7. Scan TECHNICAL SKILLS section for fabricated tools
    skills_start = text_lower.find("technical skills")
    skills_end = text_lower.find("experience", skills_start) if skills_start != -1 else -1
    if skills_start != -1 and skills_end != -1:
        skills_block = text_lower[skills_start:skills_end]
        for fake in FABRICATION_WATCHLIST:
            if len(fake) <= 2:
                continue
            if fake in skills_block:
                errors.append(f"FABRICATED SKILL in Technical Skills: '{fake}'")

    # 8. Scan full document for fabrication watchlist items not in original
    if original_text:
        original_lower = original_text.lower()
        for fake in FABRICATION_WATCHLIST:
            if len(fake) <= 2:
                continue
            if fake in text_lower and fake not in original_lower:
                errors.append(f"New tool/skill appeared: '{fake}' (not in original)")

    # 9. Em dashes (should be auto-fixed by sanitize_text, but safety net)
    if "\u2014" in text or "\u2013" in text:
        errors.append("Contains em dash or en dash.")

    # 10. Banned words (word-boundary matching)
    found_banned = [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", text_lower)]
    if found_banned:
        errors.append(f"Banned words: {', '.join(found_banned[:5])}")

    # 11. LLM self-talk leak detection
    found_leaks = [p for p in LLM_LEAK_PHRASES if p in text_lower]
    if found_leaks:
        errors.append(f"LLM self-talk: '{found_leaks[0]}'")

    # 12. Duplicate section detection
    for section_name in ["summary", "experience", "education", "projects"]:
        count = text_lower.count(f"\n{section_name}\n") + text_lower.count(f"\n{section_name} \n")
        if text_lower.startswith(f"{section_name}\n"):
            count += 1
        if count > 1:
            errors.append(f"Section '{section_name}' appears {count} times.")

    # 13. Repeated bullets are a hard failure; they usually indicate padding
    # or a retry artifact and waste scarce resume space.
    bullets = [
        re.sub(r"\s+", " ", line[2:]).strip()
        for line in text.splitlines()
        if line.strip().startswith("- ") and len(line.split()) >= 8
    ]
    repeated_pairs: list[tuple[int, int]] = []
    for left in range(len(bullets)):
        normalized_left = re.sub(r"[^a-z0-9 ]", "", bullets[left].casefold())
        for right in range(left + 1, len(bullets)):
            normalized_right = re.sub(r"[^a-z0-9 ]", "", bullets[right].casefold())
            if normalized_left == normalized_right or SequenceMatcher(
                None, normalized_left, normalized_right
            ).ratio() >= 0.9:
                repeated_pairs.append((left + 1, right + 1))
    if repeated_pairs:
        errors.append(f"Repeated or near-duplicate resume bullets: {repeated_pairs[:3]}")

    words = len(text.split())
    enforce_content_floor = bool(layout)
    if original_text and re.search(
        r"(?im)^\s*(?:(?:selected|key|personal|academic|relevant)\s+)?projects\s*$",
        original_text,
    ):
        if words > 950:
            warnings.append(f"Project resume is {words} words; review whether all content is decisive.")
        elif enforce_content_floor and words < int(layout.get("project_resume_min_words", 320) or 320):
            warnings.append(f"Project resume is under-evidenced ({words} words; minimum 320).")
        elif words < 320:
            warnings.append(f"Project resume is only {words} words; verify that decisive evidence was retained.")
    elif words > 700:
        warnings.append(f"No-project resume is {words} words; review whether it still fits one readable page.")
    elif enforce_content_floor and words < int(layout.get("no_project_resume_min_words", 300) or 300):
        warnings.append(f"No-project resume is under-evidenced ({words} words; minimum 300).")
    elif words < 300:
        warnings.append(f"No-project resume is only {words} words; verify that it is not under-evidenced.")

    return {
        "passed": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
    }


# ── Cover Letter Validation ──────────────────────────────────────────────

def validate_cover_letter(
    text: str,
    mode: str = "normal",
    expected_signoff: str | None = None,
    company_name: str | None = None,
    evidence_plan: dict | None = None,
    surface: str = "formal",
    expected_current_title: str | None = None,
    expected_current_company: str | None = None,
) -> dict:
    """Programmatic validation of a cover letter.

    Args:
        text: The cover letter text to validate.
        mode: Validation strictness — "strict", "normal", or "lenient".
              Strictness affects style checks, not a fixed word-count gate.

    Returns:
        {"passed": bool, "errors": list[str], "warnings": list[str]}
    """
    errors: list[str] = []
    warnings: list[str] = []
    text_lower = text.lower()

    # 1. Em dashes — always an error (sanitize_text should have caught these)
    if "\u2014" in text or "\u2013" in text:
        errors.append("Contains em dash or en dash.")

    # 2. Banned words — severity depends on mode
    if mode != "lenient":
        found = [w for w in BANNED_WORDS if re.search(r"\b" + re.escape(w) + r"\b", text_lower)]
        if found:
            msg = f"Banned words: {', '.join(found[:5])}"
            if mode == "strict":
                errors.append(msg)
            else:  # normal
                warnings.append(msg)

    # 3. Length guidance. Only obvious truncation blocks persistence; the
    # preferred range is advisory and varies by target surface.
    words = len(text.split())
    guidance = {
        "formal": (300, 450),
        "ats": (250, 400),
        "short_answer": (120, 200),
        "linkedin": (60, 120),
    }.get(surface)
    if words < 80:
        errors.append(f"Appears incomplete or truncated ({words} words).")
    elif guidance and (words < guidance[0] or words > guidance[1]):
        warnings.append(
            f"Length is {words} words; typical {surface} guidance is "
            f"{guidance[0]}-{guidance[1]}, not a hard limit."
        )

    # 4. LLM self-talk — always an error regardless of mode
    found_leaks = [p for p in LLM_LEAK_PHRASES if p in text_lower]
    if found_leaks:
        errors.append(f"LLM self-talk: '{found_leaks[0]}'")

    # 5. Must start with "Dear" — always checked (preamble should have been stripped)
    stripped = text.strip()
    if not stripped.lower().startswith("dear"):
        errors.append("Must start with 'Dear Hiring Manager,'")

    # 6. Formal letters need enough paragraph structure to carry a JD response,
    # but three versus four paragraphs is an editorial choice.
    blocks = [block.strip() for block in re.split(r"\n\s*\n", stripped) if block.strip()]
    body_blocks = [
        block for block in blocks
        if not block.lower().startswith("dear")
        and not block.lower().startswith(("sincerely", "regards", "best regards"))
        and (expected_signoff is None or block != expected_signoff)
        and len(block.split()) >= 10
    ]
    if surface in {"formal", "ats"} and len(body_blocks) < 3:
        errors.append(f"Needs at least 3 substantive body paragraphs; found {len(body_blocks)}.")

    # 7. When the caller knows the profile name, require the complete sign-off.
    if expected_signoff and not stripped.endswith(expected_signoff):
        errors.append(f"Must end with the configured sign-off: {expected_signoff}")

    # 8. Repetition checks catch accidental copy/paste and common LLM padding.
    sentences = [
        re.sub(r"\s+", " ", sentence).strip()
        for sentence in re.split(r"(?<=[.!?])\s+", " ".join(body_blocks))
        if len(sentence.split()) >= 7
    ]
    repeated_pairs: list[tuple[int, int]] = []
    for left in range(len(sentences)):
        normalized_left = re.sub(r"[^a-z0-9 ]", "", sentences[left].casefold())
        for right in range(left + 1, len(sentences)):
            normalized_right = re.sub(r"[^a-z0-9 ]", "", sentences[right].casefold())
            if normalized_left == normalized_right or SequenceMatcher(
                None, normalized_left, normalized_right
            ).ratio() >= 0.88:
                repeated_pairs.append((left + 1, right + 1))
    if repeated_pairs:
        errors.append(f"Repeated or near-duplicate sentences: {repeated_pairs[:3]}")

    openers = [
        " ".join(re.findall(r"[a-z0-9]+", block.casefold())[:4])
        for block in body_blocks
    ]
    repeated_openers = sorted({opener for opener in openers if opener and openers.count(opener) > 1})
    if repeated_openers:
        errors.append(f"Repeated paragraph openers: {', '.join(repeated_openers[:3])}")

    # 9. A statement about the candidate's current role is an identity fact, not
    # a stylistic paraphrase. If the letter chooses to mention it, require the
    # exact configured title in the same sentence. This catches plausible but
    # false titles inferred from a resume heading or target role.
    if expected_current_title:
        current_role_cue = re.compile(
            r"\b(?:i\s+)?currently\s+(?:hold|serve|work|am\s+employed)\b"
            r"|\bmy\s+current\s+(?:role|position)\b",
            flags=re.IGNORECASE,
        )
        current_role_segments = [
            segment.strip()
            for segment in re.split(r"(?<=[.!?])\s+|\n+", text)
            if current_role_cue.search(segment)
        ]
        expected_title_normalized = re.sub(
            r"\s+", " ", expected_current_title
        ).strip().casefold()
        for segment in current_role_segments:
            segment_normalized = re.sub(r"\s+", " ", segment).strip().casefold()
            if expected_title_normalized not in segment_normalized:
                errors.append(
                    "Current-role statement does not use the configured title exactly: "
                    f"{expected_current_title}."
                )
                break

            if expected_current_company and re.search(r"\bat\b", segment, flags=re.IGNORECASE):
                company_tokens = {
                    token
                    for token in re.findall(r"[a-z0-9]+", expected_current_company.casefold())
                    if len(token) > 3 and token not in {"company", "design", "planning"}
                }
                segment_tokens = set(re.findall(r"[a-z0-9]+", segment_normalized))
                if company_tokens and not company_tokens.intersection(segment_tokens):
                    errors.append(
                        "Current-role statement names an employer but does not match the "
                        f"configured current company: {expected_current_company}."
                    )
                    break

    # 10. Require a company anchor and at least two JD-priority responses. This
    # is deliberately a grounding gate, not a full semantic fact checker.
    if company_name and company_name.casefold() != "unknown employer":
        company_tokens = [
            token for token in re.findall(r"[a-z0-9]+", company_name.casefold())
            if len(token) > 2
        ]
        if company_tokens and not any(token in text_lower for token in company_tokens):
            errors.append(f"Missing company-specific reference to {company_name}.")

    if evidence_plan:
        grounded = [
            item for item in evidence_plan.get("requirements", [])
            if item.get("source_quote") and item.get("evidence_summary")
        ]
        letter_tokens = set(re.findall(r"[a-z0-9+#.]+", text_lower))
        covered = 0
        for item in grounded:
            requirement_tokens = {
                token
                for token in re.findall(r"[a-z0-9+#.]+", item.get("requirement", "").casefold())
                if len(token) >= 4
            }
            if requirement_tokens.intersection(letter_tokens):
                covered += 1
        if len(grounded) < 2:
            errors.append("JD evidence plan contains fewer than 2 grounded mappings.")
        elif covered < 2:
            errors.append(
                f"Letter explicitly addresses only {covered} grounded JD priorities; needs at least 2."
            )

    return {"passed": len(errors) == 0, "errors": errors, "warnings": warnings}
