"""Resume tailoring: LLM-powered ATS-optimized resume generation per job.

THIS IS THE HEAVIEST REFACTOR. Every piece of personal data -- name, email, phone,
skills, companies, projects, school -- is loaded at runtime from the user's profile.
Zero hardcoded personal information.

The LLM returns structured JSON, code assembles the final text. Header (name, contact)
is always code-injected, never LLM-generated. Each retry starts a fresh conversation
to avoid apologetic spirals.
"""

import json
import logging
import os
import re
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from applypilot.apply.application_facts import current_visible_fact_mappings
from applypilot.config import RESUME_PATH, TAILORED_DIR, load_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client
from applypilot.resume_versions import finish_resume_run, start_resume_run
from applypilot.scoring.cover_letter import read_resume_source
from applypilot.scoring.resume_plan import build_content_plan, format_content_plan
from applypilot.scoring.validator import (
    BANNED_WORDS,
    current_profile_resume_fact_errors,
    sanitize_text,
    validate_json_fields,
    validate_tailored_resume,
)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5  # max cross-run retries before giving up


def _keyword_hits(text: str, keyword: str) -> int:
    """Count a configured routing phrase without substring false positives."""
    keyword = keyword.strip().casefold()
    if not keyword:
        return 0
    if re.fullmatch(r"[a-z0-9+#.]+", keyword):
        return len(re.findall(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text))
    return text.count(keyword)


def _normalize_evidence_map_quotes(value: object, evidence_text: str) -> object:
    """Expand a short exact phrase to its shortest containing source line."""
    if not isinstance(value, list):
        return value
    source_lines = [
        re.sub(r"^\s*[-\u2022]\s*", "", line).strip()
        for line in evidence_text.splitlines()
        if line.strip()
    ]
    normalized: list[object] = []
    for raw_item in value:
        if not isinstance(raw_item, dict):
            normalized.append(raw_item)
            continue
        item = dict(raw_item)
        quote = str(item.get("source_quote") or "").strip()
        if quote and len(quote.split()) < 6:
            containing = [
                line
                for line in source_lines
                if quote.casefold() in line.casefold() and len(line.split()) >= 6
            ]
            if containing:
                item["source_quote"] = min(containing, key=lambda line: (len(line.split()), len(line)))
        normalized.append(item)
    return normalized


def select_resume_source(job: dict, profile: dict) -> tuple[Path, dict]:
    """Select a configured resume variant for one job, with an auditable score."""
    explicit = str(job.get("tailor_source_resume_path") or "").strip()
    if explicit:
        path = Path(explicit).resolve()
        if not path.exists():
            raise FileNotFoundError(f"Configured job-specific resume source is missing: {path}")
        return path, {"method": "job_override", "track": "explicit", "score": None}

    variants = profile.get("tailoring", {}).get("resume_variants", [])
    title = str(job.get("title") or "").casefold()
    description = str(job.get("full_description") or "").casefold()
    ranked: list[tuple[int, int, dict, Path]] = []
    for index, variant in enumerate(variants):
        if not isinstance(variant, dict) or not variant.get("path"):
            continue
        path = Path(str(variant["path"])).resolve()
        if not path.exists():
            log.warning("Configured tailoring resume variant is missing: %s", path)
            continue
        source_text = read_resume_source(path)
        if current_profile_resume_fact_errors(source_text, profile):
            log.warning(
                "Configured tailoring resume variant conflicts with current profile facts: %s",
                path,
            )
            continue
        keywords = [str(item) for item in variant.get("keywords", [])]
        title_score = sum(_keyword_hits(title, keyword) for keyword in keywords) * 4
        description_score = sum(_keyword_hits(description, keyword) for keyword in keywords)
        ranked.append((title_score + description_score, -index, variant, path))

    if ranked:
        score, _, variant, path = max(ranked, key=lambda item: (item[0], item[1]))
        if score > 0:
            return path, {
                "method": "configured_keyword_router",
                "track": str(variant.get("track") or path.stem),
                "score": score,
            }
        _, _, variant, path = max(ranked, key=lambda item: item[1])
        return path, {
            "method": "configured_default_source",
            "track": str(variant.get("track") or path.stem),
            "score": 0,
        }

    return RESUME_PATH.resolve(), {
        "method": "default_resume_fallback",
        "track": "default",
        "score": 0,
    }


# ── Prompt Builders (profile-driven) ──────────────────────────────────────

def _build_tailor_prompt(profile: dict, source_has_projects: bool = True) -> str:
    """Build the resume tailoring system prompt from the user's profile.

    All skills boundaries, preserved entities, and formatting rules are
    derived from the profile -- nothing is hardcoded.
    """
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Format skills boundary for the prompt
    skills_lines = []
    for category, items in boundary.items():
        if isinstance(items, list) and items:
            label = category.replace("_", " ").title()
            skills_lines.append(f"{label}: {', '.join(items)}")
    skills_block = "\n".join(skills_lines)

    # Preserved entities
    companies = resume_facts.get("preserved_companies", [])
    school = resume_facts.get("preserved_school", "")
    real_metrics = resume_facts.get("real_metrics", [])

    companies_str = ", ".join(companies) if companies else "N/A"
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"

    # Include ALL banned words from the validator so the LLM knows exactly
    # what will be rejected — the validator checks for these automatically.
    banned_str = ", ".join(BANNED_WORDS)

    multi_page_min_fill = float(
        profile.get("tailoring", {})
        .get("resume_layout", {})
        .get("multi_page_last_page_min_fill_ratio", 0.4)
        or 0.4
    )
    length_guidance = (
        "This selected source includes projects. Preserve only the projects that materially improve "
        "the match. For an internship or current-student application, prefer one readable page when "
        "the strongest evidence fits. Use two pages when multiple "
        "distinct evidence areas are needed, but the final page must contain enough role-relevant content "
        f"to occupy at least {multi_page_min_fill:.0%} of a normally filled page. Otherwise select more "
        "aggressively and use one page. Word counts are guidance, never a reason to pad, shrink fonts, "
        "or delete decisive evidence."
        if source_has_projects else
        "This selected source has no project section. Do not invent one. Prefer one readable page, "
        "but treat length as guidance rather than a strict word-count target."
    )

    return f"""You are a senior technical recruiter rewriting a resume to get this person an interview.

Take the base resume and job description. Return a tailored resume as a JSON object.

## RECRUITER SCAN (6 seconds):
1. Summary -- concise, role-specific, and source-proven
2. Education -- early for internships and current students
3. First 3 bullets of most recent role -- verbs and outcomes match?
4. Skills -- must-haves visible immediately?

## SKILLS BOUNDARY (real skills only):
{skills_block}

The selected source resume is the structural baseline. Any separately supplied SUPPLEMENTAL CANDIDATE EVIDENCE is also factual evidence, but use it only to complement a retained entry or add a clearly supported candidate fact. Do not blend employers, roles, projects, dates, metrics, or outcomes across entries. Do not add even a closely related or learnable tool unless that exact tool appears in the supplied candidate evidence.

## TAILORING RULES:

TARGET FUNCTION: Return the target function in `title` for routing and internship-layout selection only. It will not be printed beneath the candidate's name because that line is reserved for contact information.

EXPERIENCE IDENTITY: Never rename an employer or employment title. In every experience object, copy the exact company into `header` and preserve the exact source role in `subtitle`. Preserve dates and seniority. Target-job language belongs in the summary and bullets, not in past titles.

SUMMARY: Optional; use an empty string when it adds no distinct value. Source resumes may intentionally omit it; do not add generic filler. When present, tailor it to the role. Lead with the 1-2 source-proven skills that matter most for THIS role. Prefer one or two compact sentences, normally about 26-45 words total. Use the full line width efficiently: do not add a very short closing sentence, and ensure the final sentence contains at least 8 words. Every statement about work performed, data analyzed, experiments run, users served, domains covered, or outcomes achieved must be supported by the selected source. Do not turn a JD responsibility into candidate history. Target-role framing is allowed; invented experience is not. In particular, do not copy a JD-only action or domain term into the summary unless that term, or the same factual work, appears in the selected source. Never join two sector domains (for example, legal and planning) into one work claim unless a single source bullet explicitly contains both domains; split the facts into separate sentences or omit the weaker domain.

SECTION ORDER: For an internship, trainee, co-op, or current-student target, use Summary, Education, Technical Skills, Experience, Projects. For other roles, use Summary, Technical Skills, Experience, Projects, Education. The renderer applies this rule automatically; keep all education facts in `education`.

EDUCATION: Return a JSON array with exactly one complete institution/degree/date record per item, in source order. Never combine multiple schools into one string or paragraph. Preserve only source-supported GPA/status details.

TECHNICAL SKILLS LAYOUT: Keep each category compact enough to use the available line width efficiently. Avoid a wrapped row whose final rendered line contains only one to four words. Prefer removing lower-priority source skills, shortening a category label, or rebalancing source-grounded items across categories; never shrink typography or invent a skill to fill space.

SKILLS: Reorder each category so the job's must-haves appear first.

Order EXPERIENCE entries from current/most recent to oldest using their preserved dates. PROJECT entries may be freely reordered by JD relevance; project dates must remain accurate. Reorder bullets inside each entry by relevance. Rephrase only where the new wording is more useful and preserves exactly the same action, ownership, scope, tools, metric, and outcome. Verbatim source wording is allowed and preferred when rewriting would weaken factual precision.

PAGE LENGTH: One or two readable pages are both acceptable. Never sacrifice a relevant, supported project just to force one page. The source resume may omit projects; add a clearly sourced project from supplemental evidence when it materially helps this role.

EXPERIENCE AND PROJECT SELECTION: In EXPERIENCE, retire at most one low-relevance source entry and preserve the most recent experience. Choose PROJECTS by relevance; a newer project is not automatically more valuable. Keep at least one entry in each source-present section, and give every retained entry at least one substantive bullet. Allocate detail by JD relevance and the strength of sourced evidence. A relevant older experience may receive more bullets than a newer entry. Bullet counts and word targets are guidance, not reasons to invent, pad, or delete decisive evidence.

PROJECTS: {"Keep the most relevant source projects and preserve each project identity." if source_has_projects else "Return an empty projects list unless supplemental candidate evidence contains a clearly identified real project."}

BULLETS: Most relevant first. Write complete recruiter-readable statements, not keyword inventories. Each bullet should normally connect at least two and preferably three of: context/problem, owned action, concrete artifact/method, and result/user/decision impact. Preserve useful source detail about scope and constraints instead of over-compressing it into a tool list. Use only source-supported verbs and outcomes. Do not force every bullet into an impact formula, do not manufacture causal results, and do not add a number copied from the JD. Usually use 2-4 bullets per experience or project; preserve additional decisive evidence when useful.

JD EVIDENCE MAP: Before drafting, identify exactly 3 high-priority JD requirements. Classify each as `direct`, `transferable`, or `gap`. `direct` means the selected source proves substantially the same task, skill, or outcome. `transferable` means the source proves adjacent capability but you must not write the unsatisfied JD task as candidate history. `gap` means there is no honest support. For direct or transferable items, copy a source_quote of at least 6 words verbatim from the selected resume. For gaps, use an empty source_quote. At least 2 mappings must be direct or transferable. Do not place citations or gap labels in the visible resume.

## VOICE:
- Write like a real engineer. Short, direct.
- GOOD: "Automated financial reporting with Python + API integrations, cut processing time from 10 hours to 2"
- BAD: "Leveraged cutting-edge AI technologies to drive transformative operational efficiencies"
- BANNED WORDS (using ANY of these = validation failure — do not use them even once):
  {banned_str}
- No em dashes. Use commas, periods, or hyphens.

## HARD RULES:
- Do NOT invent work, companies, degrees, or certifications
- Do NOT add, round, merge, transfer, or change real numbers ({metrics_str})
- Preserved companies: {companies_str} -- names stay as-is
- Preserved school: {school}
- {length_guidance}

## OUTPUT: Return ONLY valid JSON. No markdown fences. No commentary. No "here is" preamble.

{{"title":"Target function used for routing only","summary":"1-2 compact tailored sentences.","skills":{{"Source category":"comma-separated source skills"}},"experience":[{{"header":"Exact Company","subtitle":"Exact Source Role | Exact Source Dates","bullets":["source-grounded complete narrative bullet"]}}],"projects":[{{"header":"Exact Source Project","subtitle":"Exact Source Role | Exact Source Dates","bullets":["source-grounded complete narrative bullet"]}}],"education":["Institution 1, degree, date","Institution 2, degree, date"],"evidence_map":[{{"requirement":"JD requirement","support_level":"direct or transferable or gap","source_quote":"verbatim source quote, or empty for gap"}}]}}"""


def _build_judge_prompt(profile: dict, *, factual_only: bool = False) -> str:
    """Build the LLM judge prompt from the user's profile."""
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    profile_evidence, confirmed_profile_skills = _build_judge_profile_evidence(profile)

    # Flatten allowed skills for the judge
    all_skills: list[str] = []
    for items in boundary.values():
        if isinstance(items, list):
            all_skills.extend(items)
    all_skills.extend(sorted(confirmed_profile_skills))
    skills_str = ", ".join(all_skills) if all_skills else "N/A"

    real_metrics = resume_facts.get("real_metrics", [])
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"
    section_review_instruction = (
        "Separately inspect every visible section (SUMMARY, TECHNICAL SKILLS, EXPERIENCE, "
        "PROJECTS when present, and EDUCATION) for factual consistency, changed identities, "
        "unsupported claims, or contradictions. Do not score relevance, allocation, density, or "
        "style; an independent reviewer handles those. Return exactly one section_reviews item "
        "for each visible section."
        if factual_only else
        "Separately review every visible section (SUMMARY, TECHNICAL SKILLS, EXPERIENCE, PROJECTS "
        "when present, and EDUCATION). Fail a section when it is materially thin, generic, poorly "
        "allocated, internally inconsistent, or misses stronger supplied evidence for the target "
        "job. The leading relevant experience and project should normally have 2-4 substantive "
        "bullets, while every retained entry must have at least one. Return exactly one "
        "section_reviews item for each visible section."
    )
    usefulness_instruction = (
        "Do not judge usefulness or writing preference in this factual-only pass."
        if factual_only else
        "Also judge usefulness: the summary, skill order, leading bullets, and project order should "
        "emphasize the strongest source-supported direct or transferable matches without keyword "
        "stuffing. Missing JD requirements are honest gaps, not instructions to invent them. Do not "
        "fail merely because the candidate lacks A/B tests, DAU/MAU, interviews, a tool, or another "
        "responsibility; fail usefulness only when relevant source evidence exists but the tailored "
        "resume ignores it, or when the rewrite is so generic it could target an unrelated job."
    )

    return f"""You are a resume quality judge. A tailoring engine rewrote a resume to target a specific job. Your job is to catch unsupported claims and useless tailoring, not merely obvious lies.

Return only one JSON object matching this schema:
{{"verdict":"PASS or FAIL","issues":["specific verdict-changing issue"],"section_reviews":[{{"section":"SUMMARY","verdict":"PASS or FAIL","issues":[],"relevance":"high, medium, or low","density":"strong, adequate, or thin"}}],"claim_audits":[{{"section":"SUMMARY or EXPERIENCE or PROJECTS","claim":"exact complete summary sentence or bullet copied from the tailored resume","source_quotes":["one or more exact supporting quotes copied verbatim from the allowed candidate evidence"],"supported":true}}]}}

Audit every complete sentence in SUMMARY and every bullet in EXPERIENCE and PROJECTS. Copy each exactly into `claim`. For every factual action, experience, domain, user, experiment, data, ownership, tool, metric, or outcome statement, provide one or more exact quotes from the allowed candidate evidence that together support the whole claim. Allowed candidate evidence consists only of the ORIGINAL RESUME and the narrow ALLOWLISTED USER-CONFIRMED PROFILE EVIDENCE supplied with the request. Each quote must be verbatim; do not write an explanation in `source_quotes`. Use at most 4 non-repeated, shortest sufficient quotes per claim. Profile evidence may support only the explicitly labeled skill experience, internship availability, and education facts; it never supports a work action, metric, outcome, employer, project, or JD responsibility. A JD sentence is never candidate evidence. If the quote set does not support the whole claim, set `supported` false, explain it in `issues`, and FAIL.

{section_review_instruction}

The `issues` array must contain at most 8 concise, non-duplicated problems of at most 30 words each that actually require FAIL. Do not report allowed omissions or enumerate accurate reordering, faithful rewording, unchanged facts, or optional evidence that could have been retained anywhere in the response. If there is no verdict-changing problem, return an empty `issues` array.

## ALLOWLISTED USER-CONFIRMED PROFILE EVIDENCE
{profile_evidence or "None supplied."}

## CONTEXT -- what the tailoring engine was instructed to do (all of this is ALLOWED):
- Use the title field only to select the target function and layout; it is not printed in the header
- Rewrite the summary from scratch for the target job
- Reorder bullets and projects to put the most relevant first
- Reframe bullets to use the job's language
- Drop low-relevance bullets and replace with more relevant ones from other sections
- Reorder the skills section to put job-relevant skills first
- Change tone and wording extensively

## WHAT IS FABRICATION (FAIL for these):
1. Adding tools, languages, or frameworks anywhere that aren't in the selected source or the allowlisted user-confirmed skill facts. The combined upper boundary is: {skills_str}
2. Inventing NEW metrics or numbers not in the original. The real metrics are: {metrics_str}
3. Inventing work that has no basis in any original bullet (completely new achievements).
4. Adding companies, roles, or degrees that don't exist.
5. Changing real numbers (inflating 80% to 95%, 500 nodes to 1000 nodes).

## WHAT IS NOT FABRICATION (do NOT fail for these):
- Rewording any bullet, even heavily, as long as the underlying work is real
- Combining two original bullets into one
- Splitting one original bullet into two
- Describing the same work with different emphasis
- Dropping bullets entirely
- Reordering anything
- Changing the title or summary completely

## STRICT GROUNDING RULE:
There is no allowance for "minor stretches" or learnable-but-unlisted skills. A plausible claim is still unsupported if it is absent from the selected source and the narrow allowlisted profile evidence. Fail changed job titles, transferred metrics, stronger ownership, new causal outcomes, and JD facts copied into the candidate's history.

{usefulness_instruction}

Be strict about factual support and specific about each issue. Do not fail accurate reordering or faithful wording changes."""


def _profile_evidence_value(value: object, *, limit: int = 240) -> str:
    """Return one bounded line from an allowlisted profile field."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit].rstrip()


def _display_skill_name(skill: str) -> str:
    """Render admitted normalized skill tokens without changing their meaning."""
    acronyms = {"ai", "api", "aws", "bi", "ci", "css", "gcp", "html", "sql", "ui", "ux"}
    return " ".join(
        part.upper() if part in acronyms else part.capitalize()
        for part in skill.split()
    )


def _build_judge_profile_evidence(profile: dict) -> tuple[str, set[str]]:
    """Build a non-secret, allowlisted supplement for judge grounding.

    The resume library already admits only positive, known-skill, user-confirmed
    ``*_experience_years`` facts. Reuse that gate rather than exposing arbitrary
    application facts. Availability and education use explicit field allowlists.
    """
    from applypilot.resume_library import _confirmed_experience_skill_facts

    lines: list[str] = []
    confirmed_skills = _confirmed_experience_skill_facts(profile)
    for skill, fact in sorted(confirmed_skills.items()):
        value = fact.get("value")
        years = f"{value:g}" if isinstance(value, (int, float)) else str(value)
        unit = "year" if value == 1 else "years"
        lines.append(
            f"User-confirmed skill experience: {_display_skill_name(skill)} ({years} {unit})."
        )

    facts = current_visible_fact_mappings(profile)
    if isinstance(facts, list):
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            if str(fact.get("source") or "").strip().casefold() != "user_confirmed":
                continue
            if str(fact.get("key") or "").strip().casefold() != "full_time_internship_availability":
                continue
            value = _profile_evidence_value(fact.get("value"))
            if value:
                lines.append(f"User-confirmed internship availability: {value}.")
            break

    education = profile.get("education", [])
    if isinstance(education, list):
        for item in education:
            if not isinstance(item, dict):
                continue
            institution = _profile_evidence_value(item.get("institution"))
            degree = _profile_evidence_value(item.get("degree"))
            if not institution or not degree:
                continue
            parts = [institution, degree]
            status = _profile_evidence_value(item.get("status"))
            if status:
                parts.append(status)
            expected = _profile_evidence_value(item.get("expected_graduation"))
            graduation = _profile_evidence_value(item.get("graduation"))
            if expected:
                parts.append(f"Expected graduation {expected}")
            elif graduation:
                parts.append(f"Graduated {graduation}")
            prefix = "Current education" if "current" in status.casefold() else "Education"
            lines.append(f"{prefix}: {' | '.join(parts)}.")

    return "\n".join(lines), set(confirmed_skills)


# ── JSON Extraction ───────────────────────────────────────────────────────

def extract_json(raw: str) -> dict:
    """Robustly extract JSON from LLM response (handles fences, preamble).

    Args:
        raw: Raw LLM response text.

    Returns:
        Parsed JSON dict.

    Raises:
        ValueError: If no valid JSON found.
    """
    raw = raw.strip()

    # Direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Markdown fences
    if "```" in raw:
        for part in raw.split("```")[1::2]:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            try:
                return json.loads(part)
            except json.JSONDecodeError:
                continue

    # Find outermost { ... }
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError("No valid JSON found in LLM response")


# ── Resume Assembly (profile-driven header) ──────────────────────────────

def assemble_resume_text(data: dict, profile: dict) -> str:
    """Convert JSON resume data to formatted plain text.

    Header (name, location, contact) is ALWAYS code-injected from the profile,
    never LLM-generated. All text fields are sanitized.

    Args:
        data: Parsed JSON resume from the LLM.
        profile: User profile dict from load_profile().

    Returns:
        Formatted resume text.
    """
    personal = profile.get("personal", {})
    lines: list[str] = []

    # Header -- always code-injected from profile
    display_name = (
        personal.get("preferred_display_name")
        or personal.get("preferred_name")
        or personal.get("full_name", "")
    )
    lines.append(display_name)

    # Location from search config or profile -- leave blank if not available
    # The location line is optional; the original used a hardcoded city.
    # We omit it here; the LLM prompt can include it if the user sets it.

    # Contact line
    contact_parts: list[str] = []
    if personal.get("email"):
        contact_parts.append(personal["email"])
    if personal.get("phone"):
        contact_parts.append(personal["phone"])
    if personal.get("github_url"):
        contact_parts.append(re.sub(r"^https?://", "", personal["github_url"]).rstrip("/"))
    if profile.get("tailoring", {}).get("include_linkedin", False) and personal.get("linkedin_url"):
        contact_parts.append(personal["linkedin_url"])
    if contact_parts:
        lines.append(" | ".join(contact_parts))
    lines.append("")

    def append_summary() -> None:
        if str(data.get("summary") or "").strip():
            lines.extend(["SUMMARY", sanitize_text(data["summary"]), ""])

    def append_skills() -> None:
        lines.append("TECHNICAL SKILLS")
        if isinstance(data["skills"], dict):
            for cat, val in data["skills"].items():
                lines.append(f"{cat}: {sanitize_text(str(val))}")
        lines.append("")

    def append_entries(section: str, entries: list[dict]) -> None:
        if section == "PROJECTS" and not entries:
            return
        lines.append(section)
        for entry in entries:
            lines.append(sanitize_text(entry.get("header", "")))
            if entry.get("subtitle"):
                lines.append(sanitize_text(entry["subtitle"]))
            for bullet in entry.get("bullets", []):
                lines.append(f"- {sanitize_text(bullet)}")
            lines.append("")

    def append_education() -> None:
        value = data.get("education", "")
        if isinstance(value, list):
            education_lines = [sanitize_text(str(item)) for item in value if str(item).strip()]
        else:
            raw = sanitize_text(str(value))
            education_lines = [line.strip() for line in raw.splitlines() if line.strip()]
            profile_schools = [
                str(item.get("institution") or "").strip()
                for item in profile.get("education", [])
                if isinstance(item, dict) and str(item.get("institution") or "").strip()
            ]
            if len(education_lines) == 1 and len(profile_schools) > 1:
                source_line = education_lines[0]
                positions = [
                    (source_line.casefold().find(school.casefold()), school)
                    for school in profile_schools
                ]
                if all(position >= 0 for position, _school in positions):
                    ordered = sorted(positions)
                    education_lines = [
                        source_line[start : ordered[index + 1][0] if index + 1 < len(ordered) else None]
                        .strip(" ;")
                        for index, (start, _school) in enumerate(ordered)
                    ]
        lines.extend(["EDUCATION", *education_lines, ""])

    title = str(data.get("title") or "")
    is_internship = bool(
        re.search(r"\b(?:intern|internship|trainee|co-op)\b", title, re.IGNORECASE)
    )
    layout = profile.get("tailoring", {}).get("resume_layout", {})
    default_internship_order = [
        "SUMMARY", "EDUCATION", "TECHNICAL SKILLS", "EXPERIENCE", "PROJECTS"
    ]
    default_general_order = [
        "SUMMARY", "TECHNICAL SKILLS", "EXPERIENCE", "PROJECTS", "EDUCATION"
    ]
    order = layout.get(
        "internship_section_order" if is_internship else "general_section_order",
        default_internship_order if is_internship else default_general_order,
    )
    appenders = {
        "SUMMARY": append_summary,
        "TECHNICAL SKILLS": append_skills,
        "EXPERIENCE": lambda: append_entries("EXPERIENCE", data.get("experience", [])),
        "PROJECTS": lambda: append_entries("PROJECTS", data.get("projects", [])),
        "EDUCATION": append_education,
    }
    for section in order:
        if section in appenders:
            appenders[section]()

    return "\n".join(lines).rstrip()


# ── LLM Judge ────────────────────────────────────────────────────────────

def _is_exact_source_quote(quote: str, normalized_source: str) -> bool:
    """Accept verbatim evidence, including a source clause closed by a semicolon.

    Judges sometimes quote an exact source clause but replace its trailing semicolon
    or colon with a period. That punctuation-only boundary change does not alter the
    evidence. No word, number, or internal punctuation differences are tolerated.
    """
    normalized_quote = re.sub(r"\s+", " ", quote).strip().casefold()
    if len(normalized_quote.split()) < 5:
        return False
    if normalized_quote in normalized_source:
        return True
    if not normalized_quote.endswith("."):
        return False
    clause = normalized_quote[:-1].rstrip()
    return any(f"{clause}{boundary}" in normalized_source for boundary in (";", ":"))


_QUALITY_DIMENSION_WEIGHTS = {
    "jd_alignment": 0.20,
    "section_allocation": 0.20,
    "content_density": 0.15,
    "narrative_completeness": 0.20,
    "specificity": 0.10,
    "redundancy_control": 0.05,
    "professional_style": 0.10,
}
_QUALITY_OVERALL_GATE = 72.0
_QUALITY_DIMENSION_FLOOR = 55.0


def _quality_judge_tailored_resume(
    original_text: str,
    tailored_text: str,
    job_title: str,
    job_description: str,
    content_plan: dict | None,
) -> dict:
    """Run one independent usefulness/density review with conservative gates."""
    prompt = """You are the independent resume usefulness reviewer. Do not audit factual truth; a
separate reviewer handles that. Score whether the supplied, fact-preserving resume uses its limited
space well for the target job. Treat the content plan as guidance, not a quota. Do not penalize an
honest skills gap, and do not demand invented metrics, tools, or responsibilities.

Return only JSON:
{"verdict":"PASS or FAIL","dimensions":{"jd_alignment":0,"section_allocation":0,
"content_density":0,"narrative_completeness":0,"specificity":0,
"redundancy_control":0,"professional_style":0},
"issues":[{"section":"SUMMARY or TECHNICAL SKILLS or EXPERIENCE or PROJECTS or EDUCATION",
"code":"short_code","severity":"blocking or advisory","message":"specific problem",
"repairable":true}],"section_reviews":[{"section":"SUMMARY","score":0,"comment":"brief"}]}

Use integer scores from 0 to 100. Judge section allocation as a whole: retained experience entries must stay current/most-recent first with preserved dates; project entries may follow relevance.
Detail allocation follows source strength and JD relevance, not recency. Summary is optional.
Relevance should select and order facts within that hierarchy, not make a distant entry dominate.
Judge narrative completeness by whether bullets connect an owned action to a concrete artifact or
method and a supported context/result/user impact. A long list of tools or noun phrases is not a
complete bullet. Education must show one institution per separate item/line. A blocking issue must
identify a visible section and a concrete problem that materially reduces interview usefulness.
Style preferences, optional additions, and minor wording improvements are advisory. FAIL only for
materially poor allocation, thin evidence, incomplete/fragmentary bullets, heavy repetition,
generic writing, or weak JD focus despite stronger supplied evidence."""
    messages = [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": (
                f"JOB TITLE: {job_title}\n\nJOB DESCRIPTION:\n{job_description[:8000]}\n\n"
                f"CONTENT PLAN:\n{json.dumps(content_plan or {}, ensure_ascii=False)}\n\n"
                f"CANDIDATE EVIDENCE:\n{original_text[:16000]}\n\n"
                f"TAILORED RESUME:\n{tailored_text}\n\nReview once and return JSON."
            ),
        },
    ]
    client = get_client()
    response = client.chat(
        messages,
        max_tokens=int(os.environ.get("APPLYPILOT_QUALITY_JUDGE_MAX_TOKENS", "2048")),
        temperature=0.15,
        response_format={"type": "json_object"},
        thinking={"type": os.environ.get("APPLYPILOT_JUDGE_THINKING", "disabled")},
    )
    try:
        audit = extract_json(response)
    except ValueError as exc:
        return {
            "passed": False,
            "verdict": "FAIL",
            "issues": [{
                "section": "",
                "code": "invalid_review_json",
                "severity": "blocking",
                "message": str(exc),
                "repairable": False,
            }],
            "dimensions": {},
            "overall_score": 0.0,
            "raw": response,
        }

    raw_dimensions = audit.get("dimensions", {})
    dimensions: dict[str, float] = {}
    for name in _QUALITY_DIMENSION_WEIGHTS:
        value = raw_dimensions.get(name) if isinstance(raw_dimensions, dict) else None
        try:
            score = float(value)
        except (TypeError, ValueError):
            score = -1.0
        dimensions[name] = max(0.0, min(100.0, score)) if score >= 0 else 0.0
    complete = all(
        isinstance(raw_dimensions, dict) and name in raw_dimensions
        for name in _QUALITY_DIMENSION_WEIGHTS
    )
    overall = sum(
        dimensions[name] * weight
        for name, weight in _QUALITY_DIMENSION_WEIGHTS.items()
    )
    raw_issues = audit.get("issues", [])
    issues = [item for item in raw_issues if isinstance(item, dict)] if isinstance(raw_issues, list) else []
    blocking = [
        item for item in issues
        if str(item.get("severity") or "").casefold() == "blocking"
        and str(item.get("section") or "").upper()
        in {"SUMMARY", "TECHNICAL SKILLS", "EXPERIENCE", "PROJECTS", "EDUCATION"}
    ]
    passed = (
        complete
        and overall >= _QUALITY_OVERALL_GATE
        and min(dimensions.values(), default=0.0) >= _QUALITY_DIMENSION_FLOOR
        and not blocking
    )
    return {
        "passed": passed,
        "verdict": "PASS" if passed else "FAIL",
        "issues": issues,
        "dimensions": dimensions,
        "overall_score": round(overall, 2),
        "section_reviews": audit.get("section_reviews", []),
        "thresholds": {
            "overall": _QUALITY_OVERALL_GATE,
            "dimension_floor": _QUALITY_DIMENSION_FLOOR,
        },
        "raw": response,
        "response_meta": dict(getattr(client, "last_response_meta", {}) or {}),
    }


def judge_tailored_resume(
    original_text: str,
    tailored_text: str,
    job_title: str,
    profile: dict,
    job_description: str = "",
    *,
    cross_review: bool = False,
    content_plan: dict | None = None,
) -> dict:
    """LLM judge layer: catches subtle fabrication that programmatic checks miss.

    Args:
        original_text: Base resume text.
        tailored_text: Tailored resume text.
        job_title: Target job title.
        profile: User profile for building the judge prompt.

    Returns:
        A structured verdict whose summary evidence is independently checked.
    """
    judge_prompt = _build_judge_prompt(profile, factual_only=cross_review)
    profile_evidence, confirmed_profile_skills = _build_judge_profile_evidence(profile)

    messages = [
        {"role": "system", "content": judge_prompt},
        {"role": "user", "content": (
            f"JOB TITLE: {job_title}\n\n"
            f"JOB DESCRIPTION:\n{job_description[:8000]}\n\n---\n\n"
            f"ORIGINAL RESUME:\n{original_text}\n\n---\n\n"
            f"TAILORED RESUME:\n{tailored_text}\n\n"
            "Judge this tailored resume:"
        )},
    ]

    client = get_client()
    max_tokens = int(os.environ.get("APPLYPILOT_JUDGE_MAX_TOKENS", "4096"))
    response = client.chat(
        messages,
        max_tokens=max_tokens,
        temperature=0.1,
        response_format={"type": "json_object"},
        thinking={"type": os.environ.get("APPLYPILOT_JUDGE_THINKING", "disabled")},
    )

    try:
        audit = extract_json(response)
    except ValueError as exc:
        audit = {
            "verdict": "FAIL",
            "issues": [f"Judge returned invalid JSON: {exc}"],
            "section_reviews": [],
            "claim_audits": [],
        }

    raw_issues = audit.get("issues", [])
    if isinstance(raw_issues, str):
        issues_list = [] if raw_issues.strip().casefold() == "none" else [raw_issues.strip()]
    elif isinstance(raw_issues, list):
        issues_list = [str(item).strip() for item in raw_issues if str(item).strip()]
    else:
        issues_list = ["Judge issues field was not a list."]

    summary_match = re.search(
        r"(?ims)^SUMMARY\s*$\s*(.*?)\s*^"
        r"(?:EDUCATION|TECHNICAL SKILLS|EXPERIENCE|PROJECTS)\s*$",
        tailored_text,
    )
    summary_text = summary_match.group(1).strip() if summary_match else ""
    summary_sentences = [
        re.sub(r"\s+", " ", sentence).strip()
        for sentence in re.split(r"(?<=[.!?])\s+", summary_text)
        if sentence.strip()
    ]
    bullet_claims = [
        re.sub(r"\s+", " ", line.strip()[2:]).strip()
        for line in tailored_text.splitlines()
        if line.strip().startswith("- ")
    ]
    expected_claims = [*summary_sentences, *bullet_claims]
    normalized_source = re.sub(
        r"\s+", " ", f"{original_text}\n{profile_evidence}"
    ).strip().casefold()
    audited_claims = audit.get("claim_audits", audit.get("summary_claims", []))
    grounded_claims: set[str] = set()
    sector_terms = {
        "banking", "energy", "finance", "financial", "healthcare", "hospitality",
        "industrial", "insurance", "legal", "logistics", "manufacturing", "medical",
        "planning", "property", "retail", "semiconductor", "transportation", "urban",
    }
    technical_terms = {
        str(skill).strip().casefold()
        for skills in profile.get("skills_boundary", {}).values()
        if isinstance(skills, (list, set, tuple))
        for skill in skills
        if len(str(skill).strip()) >= 2
    }
    technical_terms.update(confirmed_profile_skills)
    if not isinstance(audited_claims, list):
        issues_list.append("Judge claim_audits field was not a list.")
        audited_claims = []
    failed_claim_sections: set[str] = set()
    for item in audited_claims:
        if not isinstance(item, dict):
            continue
        claim = re.sub(r"\s+", " ", str(item.get("claim", ""))).strip()
        raw_quotes = item.get("source_quotes")
        if not isinstance(raw_quotes, list):
            raw_quotes = [item.get("source_quote", "")]
        quotes = [
            re.sub(r"\s+", " ", str(quote)).strip()
            for quote in raw_quotes
            if str(quote).strip()
        ]
        # Judges sometimes return a useful exact evidence set plus one
        # explanatory or slightly recomposed quote. Keep only the contiguous
        # source substrings for deterministic checks instead of discarding the
        # whole claim because one extra quote is invalid. A claim with no exact
        # quote still fails closed below.
        exact_quotes = [
            quote for quote in quotes
            if _is_exact_source_quote(quote, normalized_source)
        ]
        quotes_are_exact = bool(exact_quotes)
        combined_quotes = " ".join(exact_quotes)
        claim_sectors = {
            term for term in sector_terms
            if re.search(rf"\b{re.escape(term)}\b", claim, flags=re.IGNORECASE)
        }
        # "Urban planning" is one domain phrase, not two independent sector
        # claims. Exact evidence that says "City Planning" proves the planning
        # domain without requiring a second quote solely for the adjective.
        if {"urban", "planning"} <= claim_sectors:
            claim_sectors.discard("urban")
        quote_sectors = {
            term for term in sector_terms
            if re.search(rf"\b{re.escape(term)}\b", combined_quotes, flags=re.IGNORECASE)
        }
        missing_sector_evidence = sorted(claim_sectors - quote_sectors)
        source_units = [
            unit
            for unit in re.split(r"[\n.!?]+", original_text.casefold())
            if unit.strip()
        ]
        if missing_sector_evidence and any(
            all(re.search(rf"\b{re.escape(term)}\b", unit) for term in claim_sectors)
            for unit in source_units
        ):
            # A reviewer can omit one of several exact quotes even when a
            # single source bullet already proves the claimed domain pairing.
            # Deterministically verify co-occurrence before treating that
            # omission as candidate fabrication.
            missing_sector_evidence = []
        if missing_sector_evidence:
            issues_list.append(
                "Summary evidence quote does not support claimed sector(s): "
                + ", ".join(missing_sector_evidence)
            )

        claim_numbers = {
            token.replace(",", "").lstrip("~").rstrip(".").casefold()
            for token in re.findall(r"(?<![A-Za-z])~?\d[\d,.]*(?:%|\+)?", claim)
        }
        quote_numbers = {
            token.replace(",", "").lstrip("~").rstrip(".").casefold()
            for token in re.findall(
                r"(?<![A-Za-z])~?\d[\d,.]*(?:%|\+)?", combined_quotes
            )
        }
        missing_number_evidence = sorted(claim_numbers - quote_numbers)
        if missing_number_evidence:
            issues_list.append(
                "Summary evidence quote(s) do not support numeric claim(s): "
                + ", ".join(missing_number_evidence)
            )

        claim_lower = claim.casefold()
        claimed_technical_terms = {
            term for term in technical_terms if term in claim_lower
        }
        missing_technical_evidence = sorted(
            term for term in claimed_technical_terms if term not in normalized_source
        )
        if missing_technical_evidence:
            issues_list.append(
                "Selected source does not support summary technical term(s): "
                + ", ".join(missing_technical_evidence)
            )
        if (
            item.get("supported") is True
            and quotes_are_exact
            and not missing_sector_evidence
            and not missing_number_evidence
            and not missing_technical_evidence
        ):
            grounded_claims.add(claim.casefold())
        elif claim:
            section = str(item.get("section") or "").strip().upper()
            if section:
                failed_claim_sections.add(section)

    missing_claim_evidence = [
        claim for claim in expected_claims
        if claim.casefold() not in grounded_claims
    ]
    if summary_text and not summary_sentences:
        issues_list.append("Tailored SUMMARY could not be parsed for claim auditing.")
    elif missing_claim_evidence:
        issues_list.append(
            "Judge did not provide exact source evidence for every summary sentence and bullet: "
            + " | ".join(missing_claim_evidence[:3])
        )

    visible_sections = [
        section
        for section in ("SUMMARY", "TECHNICAL SKILLS", "EXPERIENCE", "PROJECTS", "EDUCATION")
        if re.search(rf"(?m)^\s*{re.escape(section)}\s*$", tailored_text)
    ]
    raw_section_reviews = audit.get("section_reviews", [])
    reviewed_sections: set[str] = set()
    if not isinstance(raw_section_reviews, list):
        issues_list.append("Judge section_reviews field was not a list.")
        raw_section_reviews = []
    for review in raw_section_reviews:
        if not isinstance(review, dict):
            continue
        section = str(review.get("section") or "").strip().upper()
        if section not in visible_sections or section in reviewed_sections:
            continue
        reviewed_sections.add(section)
        review_issues = review.get("issues", [])
        if str(review.get("verdict") or "").strip().upper() != "PASS" or review_issues:
            issues_list.append(f"Section review failed for {section}.")
    missing_section_reviews = [
        section for section in visible_sections if section not in reviewed_sections
    ]
    if missing_section_reviews:
        issues_list.append(
            "Judge omitted section review(s): " + ", ".join(missing_section_reviews)
        )

    passed = (
        str(audit.get("verdict", "")).strip().upper() == "PASS"
        and not issues_list
        and bool(expected_claims)
        and not missing_claim_evidence
        and not missing_section_reviews
    )
    issues = "none" if not issues_list else "; ".join(issues_list)

    factual_result = {
        "passed": passed,
        "verdict": "PASS" if passed else "FAIL",
        "issues": issues,
        "raw": response,
        "audit": audit,
        "summary_sentences": summary_sentences,
        "audited_claim_count": len(expected_claims),
        "claim_evidence_complete": bool(expected_claims) and not missing_claim_evidence,
        "summary_evidence_complete": not any(
            sentence in missing_claim_evidence for sentence in summary_sentences
        ),
        "section_reviews_complete": not missing_section_reviews,
        "failed_sections": sorted(failed_claim_sections),
        "response_meta": dict(getattr(client, "last_response_meta", {}) or {}),
    }
    if not cross_review:
        return factual_result

    quality_result = _quality_judge_tailored_resume(
        original_text,
        tailored_text,
        job_title,
        job_description,
        content_plan,
    )
    combined_passed = factual_result["passed"] and quality_result["passed"]
    failed_sections = {
        str(item.get("section") or "").strip().upper()
        for item in quality_result.get("issues", [])
        if isinstance(item, dict)
        and str(item.get("severity") or "").casefold() == "blocking"
    }
    failed_sections.update(factual_result.get("failed_sections", []))
    return {
        "passed": combined_passed,
        "verdict": "PASS" if combined_passed else "FAIL",
        "issues": {
            "factual": factual_result.get("issues"),
            "quality": quality_result.get("issues", []),
        },
        "failed_sections": sorted(section for section in failed_sections if section),
        "factual_review": factual_result,
        "quality_review": quality_result,
        "quality_dimensions": quality_result.get("dimensions", {}),
        "quality_overall_score": quality_result.get("overall_score"),
        "review_mode": "independent_factual_and_quality_cross_review",
    }


# ── Bounded local repair ─────────────────────────────────────────────────

_SECTION_DATA_KEYS = {
    "SUMMARY": "summary",
    "TECHNICAL SKILLS": "skills",
    "EXPERIENCE": "experience",
    "PROJECTS": "projects",
    "EDUCATION": "education",
    "EVIDENCE MAP": "evidence_map",
}


def _repair_sections_for_errors(errors: list[object]) -> set[str]:
    sections: set[str] = set()
    for raw_error in errors:
        error = str(raw_error).casefold()
        if "summary" in error:
            sections.add("SUMMARY")
        if "skill" in error or "tool" in error:
            sections.add("TECHNICAL SKILLS")
        if "experience" in error or "company" in error or "employer" in error:
            sections.add("EXPERIENCE")
        if "project" in error:
            sections.add("PROJECTS")
        if "education" in error or "school" in error or "degree" in error or "gpa" in error:
            sections.add("EDUCATION")
        if re.search(r"\bevidence map\b", error) or "grounded jd priorities" in error:
            sections.add("EVIDENCE MAP")
    return sections


def _repair_resume_sections(
    data: dict,
    *,
    allowed_sections: set[str],
    evidence_text: str,
    job_text: str,
    issues: object,
    content_plan: dict,
) -> tuple[dict, dict]:
    """Ask once for section-scoped repairs and enforce the scope in code."""
    allowed_keys = {
        _SECTION_DATA_KEYS[section]
        for section in allowed_sections
        if section in _SECTION_DATA_KEYS
    }
    if not allowed_keys:
        return data, {"attempted": False, "reason": "no_repairable_section"}
    prompt = """You are repairing a resume after independent review. Return the same JSON schema as
the supplied resume data. Change only the explicitly allowed fields. Preserve every other field
byte-for-byte in meaning and structure. Resolve only the listed issues, obey the content plan,
and use candidate evidence only. Do not add employers, projects, titles, dates, tools, metrics,
ownership, or outcomes. This is one bounded repair pass, not a complete rewrite."""
    client = get_client()
    response = client.chat(
        [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": (
                    f"ALLOWED SECTIONS: {', '.join(sorted(allowed_sections))}\n\n"
                    f"REVIEW ISSUES:\n{json.dumps(issues, ensure_ascii=False)}\n\n"
                    f"CONTENT PLAN:\n{json.dumps(content_plan, ensure_ascii=False)}\n\n"
                    f"CURRENT RESUME DATA:\n{json.dumps(data, ensure_ascii=False)}\n\n"
                    f"CANDIDATE EVIDENCE:\n{evidence_text[:18000]}\n\n"
                    f"TARGET JOB:\n{job_text}\n\nReturn repaired JSON."
                ),
            },
        ],
        max_tokens=int(os.environ.get("APPLYPILOT_REPAIR_MAX_TOKENS", "3072")),
        temperature=0.2,
        response_format={"type": "json_object"},
        thinking={"type": os.environ.get("APPLYPILOT_TAILOR_THINKING", "disabled")},
    )
    try:
        proposed = extract_json(response)
    except ValueError as exc:
        return data, {
            "attempted": True,
            "applied": False,
            "sections": sorted(allowed_sections),
            "error": f"Repair returned invalid JSON: {exc}",
        }
    repaired = json.loads(json.dumps(data))
    changed: list[str] = []
    for key in allowed_keys:
        if key in proposed and proposed[key] != repaired.get(key):
            repaired[key] = proposed[key]
            changed.append(key)
    return repaired, {
        "attempted": True,
        "applied": bool(changed),
        "sections": sorted(allowed_sections),
        "changed_fields": sorted(changed),
        "response_meta": dict(getattr(client, "last_response_meta", {}) or {}),
    }


# ── Core Tailoring ───────────────────────────────────────────────────────

def tailor_resume(
    resume_text: str, job: dict, profile: dict,
    max_retries: int = 1, validation_mode: str = "normal",
    source_resume_path: str | None = None,
    route_context: dict | None = None,
    supplemental_evidence: str = "",
    validate_layout: bool = False,
) -> tuple[str, dict]:
    """Generate a tailored resume via JSON output + fresh context on each retry.

    Key design choices:
    - LLM returns structured JSON, code assembles the text (no header leaks)
    - Each retry starts a FRESH conversation (no apologetic spiral)
    - Issues from previous attempts are noted in the system prompt
    - Em dashes and smart quotes are auto-fixed, not rejected

    Args:
        resume_text:      Base resume text.
        job:              Job dict with title, site, location, full_description.
        profile:          User profile dict.
        max_retries:      Maximum retry attempts.
        validation_mode:  "strict", "normal", or "lenient".
                          strict  -- banned words trigger retries; judge must pass
                          normal  -- banned words = warnings only; judge can fail on last retry
                          lenient -- banned words ignored; LLM judge skipped

    Returns:
        (tailored_text, report) where report contains validation details.
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job.get('company_name') or 'Unknown employer'}\n"
        f"SOURCE BOARD: {job.get('source_site') or job.get('site') or 'Unknown'}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:6000]}"
    )

    report: dict = {
        "attempts": 0, "validator": None, "full_validator": None, "judge": None,
        "status": "pending", "validation_mode": validation_mode,
        "source_resume_path": source_resume_path or "runtime_source",
        "generation_diagnostics": [],
        "supplemental_evidence_used": bool(supplemental_evidence.strip()),
        "layout_validation": None,
        "local_repair": None,
    }
    avoid_notes: list[str] = []
    if str(job.get("tailor_error") or "").strip():
        avoid_notes.append(str(job["tailor_error"]).strip())
    tailored = ""
    client = get_client()
    repair_limit = max(
        0, int(os.environ.get("APPLYPILOT_LOCAL_REPAIR_LIMIT", "1"))
    )
    local_repairs_used = 0
    source_has_projects = bool(
        re.search(r"(?im)^\s*(?:selected\s+)?projects\s*$", resume_text)
        or re.search(r"(?im)^\s*(?:selected\s+)?projects\s*$", supplemental_evidence)
    )
    route_context = dict(route_context or {})
    job_profile = route_context.get("job_profile")
    if not isinstance(job_profile, dict):
        from applypilot.resume_library import extract_job_profile

        job_profile = extract_job_profile(job, profile)
    content_plan = build_content_plan(resume_text, job_profile, route_context)
    report["content_plan"] = content_plan
    tailor_prompt_base = _build_tailor_prompt(
        profile,
        source_has_projects=source_has_projects,
    )
    tailor_prompt_base += (
        "\n\n## CONTENT ALLOCATION PLAN\n" + format_content_plan(content_plan)
    )
    resolution = str(route_context.get("resolution") or "create_new")
    strategy_guidance = {
        "reuse_with_reorder": (
            "ROUTING RESOLUTION: REORDER ONLY. Preserve the selected resume's factual content and "
            "bullet substance. Improve section, skill, and bullet ordering for the target job; make only "
            "minor wording edits needed for coherence. The result must still pass every normal gate."
        ),
        "patch_existing": (
            "ROUTING RESOLUTION: PATCH THE SELECTED RESUME. Keep its strong relevant evidence, then "
            "strengthen thin high-priority sections using only supplied candidate evidence. Prefer focused "
            "bullet improvements over rebuilding the document from scratch."
        ),
        "create_new": (
            "ROUTING RESOLUTION: CREATE A NEW COMPOSITION. Select the strongest supplied evidence for "
            "this job, while preserving identities, dates, metrics, and factual scope."
        ),
    }.get(resolution)
    if strategy_guidance:
        tailor_prompt_base += "\n\n## ROUTING STRATEGY\n" + strategy_guidance

    for attempt in range(max_retries + 1):
        report["attempts"] = attempt + 1

        # Fresh conversation every attempt
        prompt = tailor_prompt_base
        if avoid_notes:
            prompt += "\n\n## AVOID THESE ISSUES (from previous attempt):\n" + "\n".join(
                f"- {n}" for n in avoid_notes[-5:]
            )
        retry_feedback = ""
        if avoid_notes:
            retry_feedback = (
                "CRITICAL RETRY: The prior draft was rejected. You MUST materially rewrite "
                "the affected summary or bullets and must not repeat the rejected claim. "
                "Use only source wording that resolves each issue below:\n- "
                + "\n- ".join(avoid_notes[-5:])
                + "\n\n"
            )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": (
                retry_feedback
                + f"SELECTED SOURCE RESUME:\n{resume_text}\n\n---\n\n"
                + (
                    f"SUPPLEMENTAL CANDIDATE EVIDENCE:\n{supplemental_evidence[:16000]}\n\n---\n\n"
                    if supplemental_evidence.strip() else ""
                )
                + f"TARGET JOB:\n{job_text}\n\nReturn the JSON:"
            )},
        ]

        max_tokens = int(os.environ.get("APPLYPILOT_TAILOR_MAX_TOKENS", "4096"))
        raw = client.chat(
            messages,
            max_tokens=max_tokens,
            temperature=0.35,
            response_format={"type": "json_object"},
            thinking={"type": os.environ.get("APPLYPILOT_TAILOR_THINKING", "disabled")},
        )
        diagnostic = {
            "attempt": attempt + 1,
            "response_chars": len(raw or ""),
            "response_meta": dict(getattr(client, "last_response_meta", {}) or {}),
            "response_excerpt": (raw or "")[:1200],
        }
        report["generation_diagnostics"].append(diagnostic)

        # Parse JSON from response
        try:
            data = extract_json(raw)
        except ValueError as exc:
            diagnostic["parse_error"] = str(exc)
            avoid_notes.append("Output was not valid JSON. Return ONLY a JSON object, nothing else.")
            continue

        # Layer 1: Validate JSON fields
        combined_evidence = resume_text + (
            "\n\nSUPPLEMENTAL CANDIDATE EVIDENCE\n" + supplemental_evidence
            if supplemental_evidence.strip() else ""
        )
        data["evidence_map"] = _normalize_evidence_map_quotes(
            data.get("evidence_map"), combined_evidence
        )
        validation = validate_json_fields(
            data,
            profile,
            mode=validation_mode,
            original_text=combined_evidence,
            selection_source_text=resume_text,
            job_description=job.get("full_description") or "",
            job_title=job.get("title") or "",
            target_company=job.get("company_name") or "",
        )
        report["validator"] = validation
        report["evidence_map"] = data.get("evidence_map", [])

        if not validation["passed"]:
            repair_sections = _repair_sections_for_errors(validation["errors"])
            if local_repairs_used < repair_limit and repair_sections:
                repaired_data, repair_report = _repair_resume_sections(
                    data,
                    allowed_sections=repair_sections,
                    evidence_text=combined_evidence,
                    job_text=job_text,
                    issues=validation["errors"],
                    content_plan=content_plan,
                )
                local_repairs_used += 1
                report["local_repair"] = repair_report
                if repair_report.get("applied"):
                    repaired_validation = validate_json_fields(
                        repaired_data,
                        profile,
                        mode=validation_mode,
                        original_text=combined_evidence,
                        selection_source_text=resume_text,
                        job_description=job.get("full_description") or "",
                        job_title=job.get("title") or "",
                        target_company=job.get("company_name") or "",
                    )
                    report["repair_validation"] = repaired_validation
                    if repaired_validation["passed"]:
                        data = repaired_data
                        validation = repaired_validation
                        report["validator"] = validation
                        report["evidence_map"] = data.get("evidence_map", [])
            if not validation["passed"]:
                avoid_notes.extend(validation["errors"])
                if attempt < max_retries and not repair_sections:
                    continue
                tailored = assemble_resume_text(data, profile)
                report["status"] = "failed_validation"
                return tailored, report

        # Assemble text (header injected by code, em dashes auto-fixed)
        tailored = assemble_resume_text(data, profile)

        full_validation = validate_tailored_resume(
            tailored,
            profile,
            original_text=combined_evidence,
            selection_source_text=resume_text,
        )
        report["full_validator"] = full_validation
        if not full_validation["passed"]:
            avoid_notes.extend(full_validation["errors"])
            if attempt < max_retries:
                continue
            report["status"] = "failed_validation"
            return tailored, report

        # Production generation must close the loop on actual pagination, not
        # merely word counts. Render to an isolated temporary directory so a
        # sparse one-page draft or a barely-started second page becomes retry
        # feedback before the semantic judge and before any live artifact is
        # registered.
        if validate_layout:
            try:
                from applypilot.scoring.pdf import convert_to_pdf

                with tempfile.TemporaryDirectory(prefix="applypilot-resume-layout-") as temp_dir:
                    text_path = Path(temp_dir) / "resume.txt"
                    text_path.write_text(tailored, encoding="utf-8")
                    convert_to_pdf(
                        text_path,
                        output_path=text_path.with_suffix(".pdf"),
                        layout_override=(
                            profile.get("tailoring", {}).get("resume_layout", {})
                        ),
                    )
                report["layout_validation"] = {"passed": True, "error": None}
            except Exception as exc:
                layout_error = str(exc)
                report["layout_validation"] = {
                    "passed": False,
                    "error": layout_error,
                }
                avoid_notes.append(
                    "Rendered layout failed: "
                    + layout_error
                    + " Adjust content allocation, not facts or typography. If a second page is "
                    "only partly used, retain more distinct relevant evidence; otherwise make the "
                    "strongest evidence concise enough for one full page."
                )
                if attempt < max_retries:
                    continue
                report["status"] = "failed_layout"
                return tailored, report

        # Layer 3: LLM judge catches semantic drift that deterministic checks
        # cannot prove. A skipped or failed judge is never a usable success.
        if validation_mode == "lenient":
            report["judge"] = {"verdict": "SKIPPED", "passed": True, "issues": "none"}
            report["status"] = "unreviewed_lenient"
            return tailored, report

        judge = judge_tailored_resume(
            combined_evidence,
            tailored,
            job.get("title", ""),
            profile,
            job_description=job.get("full_description") or "",
            cross_review=True,
            content_plan=content_plan,
        )
        report["judge"] = judge

        if not judge["passed"]:
            repair_sections = {
                str(section).strip().upper()
                for section in judge.get("failed_sections", [])
                if str(section).strip().upper() in _SECTION_DATA_KEYS
            }
            if local_repairs_used < repair_limit and repair_sections:
                repaired_data, repair_report = _repair_resume_sections(
                    data,
                    allowed_sections=repair_sections,
                    evidence_text=combined_evidence,
                    job_text=job_text,
                    issues=judge.get("issues"),
                    content_plan=content_plan,
                )
                local_repairs_used += 1
                report["local_repair"] = repair_report
                if repair_report.get("applied"):
                    repaired_validation = validate_json_fields(
                        repaired_data,
                        profile,
                        mode=validation_mode,
                        original_text=combined_evidence,
                        selection_source_text=resume_text,
                        job_description=job.get("full_description") or "",
                        job_title=job.get("title") or "",
                        target_company=job.get("company_name") or "",
                    )
                    repaired_text = assemble_resume_text(repaired_data, profile)
                    repaired_full_validation = validate_tailored_resume(
                        repaired_text,
                        profile,
                        original_text=combined_evidence,
                        selection_source_text=resume_text,
                    )
                    repaired_layout = {"passed": True, "error": None}
                    if repaired_validation["passed"] and repaired_full_validation["passed"] and validate_layout:
                        try:
                            from applypilot.scoring.pdf import convert_to_pdf

                            with tempfile.TemporaryDirectory(
                                prefix="applypilot-resume-repair-layout-"
                            ) as temp_dir:
                                text_path = Path(temp_dir) / "resume.txt"
                                text_path.write_text(repaired_text, encoding="utf-8")
                                convert_to_pdf(
                                    text_path,
                                    output_path=text_path.with_suffix(".pdf"),
                                    layout_override=(
                                        profile.get("tailoring", {}).get("resume_layout", {})
                                    ),
                                )
                        except Exception as exc:
                            repaired_layout = {"passed": False, "error": str(exc)}
                    report["repair_validation"] = repaired_validation
                    report["repair_full_validator"] = repaired_full_validation
                    report["repair_layout_validation"] = repaired_layout
                    if (
                        repaired_validation["passed"]
                        and repaired_full_validation["passed"]
                        and repaired_layout["passed"]
                    ):
                        repaired_judge = judge_tailored_resume(
                            combined_evidence,
                            repaired_text,
                            job.get("title", ""),
                            profile,
                            job_description=job.get("full_description") or "",
                            cross_review=True,
                            content_plan=content_plan,
                        )
                        report["judge_after_repair"] = repaired_judge
                        if repaired_judge["passed"]:
                            report["validator"] = repaired_validation
                            report["full_validator"] = repaired_full_validation
                            report["layout_validation"] = repaired_layout
                            report["judge"] = repaired_judge
                            report["status"] = "machine_validated"
                            return repaired_text, report
            report["status"] = "failed_judge"
            return tailored, report

        # Both passed
        report["status"] = "machine_validated"
        return tailored, report

    report["status"] = "exhausted_retries"
    return tailored, report


def _tailor_report_error(report: dict) -> str:
    """Create a compact persisted failure reason from layered validation."""
    issues: list[str] = []
    for key in ("validator", "full_validator"):
        validation = report.get(key) or {}
        issues.extend(str(item) for item in validation.get("errors", []))
    judge = report.get("judge") or {}
    if judge and not judge.get("passed", False):
        issues.append(f"Judge: {judge.get('issues') or judge.get('raw') or 'no PASS verdict'}")
    if report.get("render_error"):
        issues.append(f"Render: {report['render_error']}")
    if not issues:
        issues.append(str(report.get("status") or "unknown tailoring failure"))
    return "; ".join(issues[:8])


# ── Batch Entry Point ────────────────────────────────────────────────────

def run_tailoring(min_score: int = 7, limit: int = 20,
                  validation_mode: str = "normal",
                  target_url: str | None = None) -> dict:
    """Generate tailored resumes for high-scoring jobs.

    Args:
        min_score:       Minimum fit_score to tailor for.
        limit:           Maximum jobs to process.
        validation_mode: "strict", "normal", or "lenient".
        target_url:      Optional exact job/application URL. When set, no other
                         database row can be tailored.

    Returns:
        {"approved": int, "failed": int, "errors": int, "elapsed": float}
    """
    profile = load_profile()
    conn = get_connection()
    from applypilot.resume_library import (
        RESUME_RESOLUTIONS,
        record_route_outcome,
        register_tailored_artifact,
        route_resume_for_job,
        sync_resume_library,
    )

    configured_variants = profile.get("tailoring", {}).get("resume_variants", [])
    library_enabled = bool(configured_variants)
    # Import historical validated material before routing. Synthetic/minimal
    # profiles without a configured source library retain the legacy path.
    if library_enabled:
        sync_resume_library(conn, profile)

    if target_url:
        from applypilot.eligibility import ELIGIBLE_SQL, refresh_job_eligibility

        refresh_job_eligibility(conn)
        rows = conn.execute(
            f"""
            SELECT * FROM jobs
            WHERE (url = ? OR application_url = ?)
              AND fit_score >= ?
              AND full_description IS NOT NULL
              AND COALESCE(tailor_attempts, 0) < 5
              AND {ELIGIBLE_SQL}
            """,
            (target_url, target_url, min_score),
        ).fetchall()
        if len(rows) > 1:
            raise ValueError("Exact URL matched more than one pending job.")
        jobs = [dict(rows[0])] if rows else []
    else:
        jobs = get_jobs_by_stage(
            conn=conn,
            stage="pending_tailor",
            min_score=min_score,
            limit=limit,
        )
    missing_company = [job for job in jobs if not job.get("company_name")]
    jobs = [job for job in jobs if job.get("company_name")]

    if missing_company:
        log.warning(
            "Skipping %d untailored job(s) with missing company metadata: %s",
            len(missing_company),
            ", ".join(str(job.get("title") or job.get("url") or "unknown") for job in missing_company[:5]),
        )

    if not jobs:
        if missing_company:
            log.warning(
                "No tailorable jobs with score >= %d; repair company metadata before retrying.",
                min_score,
            )
        else:
            log.info("No untailored jobs with score >= %d.", min_score)
        return {"approved": 0, "failed": 0, "errors": 0, "elapsed": 0.0}

    TAILORED_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Tailoring resumes for %d jobs (score >= %d)...", len(jobs), min_score)
    t0 = time.time()
    completed = 0
    results: list[dict] = []
    stats: dict[str, int] = {
        "machine_validated": 0,
        "failed_validation": 0,
        "failed_judge": 0,
        "unreviewed_lenient": 0,
        "exhausted_retries": 0,
        "error": 0,
    }

    for job in jobs:
        completed += 1
        route_resolution = None
        library_route: dict = {}
        run_dir: Path | None = None
        try:
            library_route = (
                route_resume_for_job(
                    conn,
                    job,
                    profile,
                    minimum_fit_score=min_score,
                    top_k=max(1, int(os.environ.get("APPLYPILOT_RESUME_TOP_K", "5"))),
                )
                if library_enabled
                else {
                    "decision": "create_variant",
                    "assignment_id": None,
                    "reason": "Resume library is not configured.",
                }
            )
            route_decision = library_route["decision"]
            route_resolution = library_route.get("resolution") or (
                "reuse_as_is" if route_decision == "reuse_exact" else "create_new"
            )
            if route_resolution == "reuse_as_is":
                artifact = library_route["artifact"]
                result = {
                    "url": job["url"],
                    "path": artifact["text_path"],
                    "rejected_path": None,
                    "report_path": library_route["reuse_report_path"],
                    "source_resume_path": (
                        artifact.get("source_resume_path") or artifact["text_path"]
                    ),
                    "error": None,
                    "pdf_path": artifact["pdf_path"],
                    "title": job["title"],
                    "company_name": job["company_name"],
                    "source_site": job.get("source_site") or job.get("site"),
                    "status": "machine_validated",
                    "attempts": 0,
                    "resume_library_decision": "reuse_exact",
                    "resume_library_resolution": "reuse_as_is",
                    "resume_artifact_id": artifact["artifact_id"],
                    "resume_library_assignment_id": library_route["assignment_id"],
                }
                results.append(result)
                stats["machine_validated"] += 1
                log.info(
                    "%d/%d [REUSED] artifact=%s | %s",
                    completed,
                    len(jobs),
                    artifact["artifact_id"],
                    result["title"][:40],
                )
                continue
            if route_decision in {"manual_review", "ignore"}:
                result = {
                    "url": job["url"],
                    "path": None,
                    "rejected_path": None,
                    "report_path": None,
                    "source_resume_path": None,
                    "error": library_route["reason"],
                    "pdf_path": None,
                    "title": job["title"],
                    "company_name": job["company_name"],
                    "source_site": job.get("source_site") or job.get("site"),
                    "status": f"routing_{route_decision}",
                    "attempts": 0,
                    "resume_library_decision": route_decision,
                    "resume_library_assignment_id": library_route["assignment_id"],
                }
                results.append(result)
                stats[result["status"]] = stats.get(result["status"], 0) + 1
                log.info(
                    "%d/%d [%s] %s | %s",
                    completed,
                    len(jobs),
                    route_decision.upper(),
                    library_route["reason"],
                    result["title"][:40],
                )
                continue

            selected_artifact = library_route.get("artifact")
            if (
                route_resolution in {"reuse_with_reorder", "patch_existing"}
                and isinstance(selected_artifact, dict)
                and selected_artifact.get("text_path")
            ):
                source_path = Path(str(selected_artifact["text_path"])).expanduser().resolve()
                routing = {
                    "method": "resume_library_top_k",
                    "track": library_route.get("job_profile", {}).get("track"),
                    "score": library_route.get("overall_score"),
                    "artifact_id": selected_artifact.get("artifact_id"),
                    "resolution": route_resolution,
                }
            else:
                source_path, routing = select_resume_source(job, profile)
            resume_text = read_resume_source(source_path)
            run_dir = start_resume_run(TAILORED_DIR.parent, job, kind="tailoring")
            supplemental_parts: list[str] = []
            # A short selected source may omit projects. Supply the configured
            # factual sources so page length does not silently erase that evidence.
            for variant in profile.get("tailoring", {}).get("resume_variants", []):
                evidence_path = Path(str(variant.get("path") or "")).expanduser().resolve()
                if evidence_path == source_path or not evidence_path.is_file():
                    continue
                supplemental_parts.append(
                    f"SOURCE DOCUMENT {evidence_path.name}\n{read_resume_source(evidence_path)}"
                )
            for candidate in library_route.get("candidates", [])[:4]:
                candidate_id = str(candidate.get("artifact_id") or "")
                if not candidate_id or candidate_id == library_route.get("artifact_id"):
                    continue
                row = conn.execute(
                    "SELECT text_path FROM resume_artifacts WHERE artifact_id=? AND active=1",
                    (candidate_id,),
                ).fetchone()
                if row is None:
                    continue
                candidate_path = Path(str(row["text_path"])).expanduser().resolve()
                if not candidate_path.is_file() or candidate_path == source_path:
                    continue
                candidate_text = read_resume_source(candidate_path).strip()
                if candidate_text:
                    supplemental_parts.append(
                        f"SOURCE ARTIFACT {candidate_id}\n{candidate_text[:5000]}"
                    )
                if sum(len(part) for part in supplemental_parts) >= 12000:
                    break
            supplemental_evidence = "\n\n".join(supplemental_parts)
            document_retries = max(0, int(os.environ.get("APPLYPILOT_DOCUMENT_MAX_RETRIES", "1")))
            tailored, report = tailor_resume(
                resume_text,
                job,
                profile,
                max_retries=document_retries,
                validation_mode=validation_mode,
                source_resume_path=str(source_path),
                route_context={
                    "resolution": route_resolution,
                    "assignment_id": library_route.get("assignment_id"),
                    "source_artifact_id": library_route.get("artifact_id"),
                    "candidates": library_route.get("candidates", []),
                    "job_profile": library_route.get("job_profile", {}),
                },
                supplemental_evidence=supplemental_evidence,
                validate_layout=True,
            )
            report["resume_routing"] = routing

            # Build safe filename prefix
            safe_title = re.sub(r"[^\w\s-]", "", job["title"])[:50].strip().replace(" ", "_")
            safe_company = re.sub(r"[^\w\s-]", "", job["company_name"])[:30].strip().replace(" ", "_")
            prefix = f"{safe_company}_{safe_title}"

            success = report["status"] == "machine_validated"
            validated_txt_path = run_dir / f"{prefix}.txt"
            if success:
                txt_path = validated_txt_path
            else:
                rejected_dir = run_dir / "rejected"
                rejected_dir.mkdir(parents=True, exist_ok=True)
                txt_path = rejected_dir / f"{prefix}_REJECTED.txt"
                if validated_txt_path.exists():
                    stale_path = rejected_dir / (
                        f"{prefix}_PREVIOUSLY_VALIDATED_"
                        f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.txt"
                    )
                    validated_txt_path.replace(stale_path)
                    report["quarantined_previous_path"] = str(stale_path)
            if tailored:
                txt_path.write_text(tailored, encoding="utf-8")

            # Save job description for traceability
            job_path = run_dir / f"{prefix}_JOB.txt"
            job_desc = (
                f"Title: {job['title']}\n"
                f"Company: {job['company_name']}\n"
                f"Source: {job.get('source_site') or job.get('site') or 'Unknown'}\n"
                f"Location: {job.get('location', 'N/A')}\n"
                f"Score: {job.get('fit_score', 'N/A')}\n"
                f"URL: {job['url']}\n\n"
                f"{job.get('full_description', '')}"
            )
            job_path.write_text(job_desc, encoding="utf-8")

            # Generate PDF only after deterministic checks and the strict judge pass.
            pdf_path = None
            if success:
                try:
                    from applypilot.scoring.pdf import convert_to_pdf
                    pdf_path = str(convert_to_pdf(txt_path))
                except Exception as exc:
                    log.warning("PDF generation failed for %s: %s", txt_path, exc)
                    report["render_error"] = str(exc)
                    report["status"] = "failed_render"
                    rejected_dir = run_dir / "rejected"
                    rejected_dir.mkdir(parents=True, exist_ok=True)
                    rejected_path = rejected_dir / f"{prefix}_REJECTED.txt"
                    txt_path.replace(rejected_path)
                    txt_path = rejected_path
                    success = False

            # Persist the render verdict together with the content verdict.
            report["tailored_resume_path"] = str(txt_path) if success else None
            report["rejected_path"] = str(txt_path) if tailored and not success else None
            report_path = finish_resume_run(
                run_dir, report, source_text=resume_text,
                supplemental_evidence=supplemental_evidence,
            )

            result = {
                "url": job["url"],
                "path": str(txt_path) if success else None,
                "rejected_path": str(txt_path) if tailored and not success else None,
                "report_path": str(report_path),
                "source_resume_path": str(source_path),
                "error": None if success else _tailor_report_error(report),
                "pdf_path": pdf_path,
                "title": job["title"],
                "company_name": job["company_name"],
                "source_site": job.get("source_site") or job.get("site"),
                "status": report["status"],
                "attempts": report["attempts"],
                "resume_library_decision": "create_variant",
                "resume_library_resolution": route_resolution,
                "resume_source_artifact_id": library_route.get("artifact_id"),
                "resume_library_assignment_id": library_route["assignment_id"],
            }
        except Exception as e:
            failed_report = None
            if run_dir is not None and not (run_dir / "validation.json").exists():
                failed_report = str(finish_resume_run(
                    run_dir, {"status": "error", "error": str(e)}, source_text=resume_text,
                ))
            result = {
                "url": job["url"], "title": job["title"],
                "company_name": job.get("company_name"),
                "source_site": job.get("source_site") or job.get("site"),
                "status": "error", "attempts": 0, "path": None, "pdf_path": None,
                "rejected_path": None, "report_path": failed_report,
                "source_resume_path": None, "error": str(e),
                "resume_library_decision": "error",
                "resume_library_resolution": route_resolution,
                "resume_source_artifact_id": library_route.get("artifact_id"),
                "resume_library_assignment_id": library_route.get("assignment_id"),
            }
            log.error("%d/%d [ERROR] %s -- %s", completed, len(jobs), job["title"][:40], e)

        results.append(result)
        stats[result.get("status", "error")] = stats.get(result.get("status", "error"), 0) + 1

        elapsed = time.time() - t0
        rate = completed / elapsed if elapsed > 0 else 0
        log.info(
            "%d/%d [%s] attempts=%s | %.1f jobs/min | %s",
            completed, len(jobs),
            result["status"].upper(),
            result.get("attempts", "?"),
            rate * 60,
            result["title"][:40],
        )

    # Persist to DB: only fully machine-validated outputs become downstream inputs.
    now = datetime.now(UTC).isoformat()
    for r in results:
        if r["status"] == "machine_validated":
            attempt_increment = 0 if r.get("resume_library_resolution") == "reuse_as_is" else 1
            conn.execute(
                "UPDATE jobs SET tailored_resume_path=?, tailored_at=?, "
                "tailor_status='machine_validated', tailor_error=NULL, "
                "tailor_source_resume_path=?, tailor_report_path=?, "
                "tailor_attempts=COALESCE(tailor_attempts,0)+? WHERE url=?",
                (
                    r["path"],
                    now,
                    r["source_resume_path"],
                    r["report_path"],
                    attempt_increment,
                    r["url"],
                ),
            )
        else:
            conn.execute(
                "UPDATE jobs SET tailored_resume_path=NULL, tailored_at=NULL, "
                "tailor_status=?, tailor_error=?, tailor_source_resume_path=?, "
                "tailor_report_path=?, tailor_attempts=COALESCE(tailor_attempts,0)+1 "
                "WHERE url=?",
                (
                    r["status"],
                    r.get("error"),
                    r.get("source_resume_path"),
                    r.get("report_path"),
                    r["url"],
                ),
            )
    conn.commit()

    # Newly generated material becomes reusable only after the normal strict
    # content and render gates have already promoted it to machine_validated.
    for r in results:
        if (
            library_enabled
            and
            r["status"] == "machine_validated"
            and r.get("resume_library_resolution") in {
                "reuse_with_reorder", "patch_existing", "create_new"
            }
        ):
            stored = conn.execute("SELECT * FROM jobs WHERE url=?", (r["url"],)).fetchone()
            if stored is not None:
                registration = register_tailored_artifact(
                    conn,
                    job=dict(stored),
                    text_path=r["path"],
                    source_resume_path=r.get("source_resume_path"),
                    report_path=r.get("report_path"),
                    assignment_decision=f"{r.get('resume_library_resolution')}_validated",
                    profile=profile,
                )
                r["resume_artifact_id"] = registration["artifact_id"]
                record_route_outcome(
                    conn,
                    assignment_id=r["resume_library_assignment_id"],
                    resolution=r["resume_library_resolution"],
                    status="machine_validated",
                    source_artifact_id=r.get("resume_source_artifact_id"),
                    output_artifact_id=registration["artifact_id"],
                    evidence={
                        "report_path": r.get("report_path"),
                        "attempts": r.get("attempts"),
                    },
                )
        elif (
            library_enabled
            and r.get("resume_library_assignment_id")
            and r.get("resume_library_resolution") in RESUME_RESOLUTIONS
        ):
            record_route_outcome(
                conn,
                assignment_id=r["resume_library_assignment_id"],
                resolution=r["resume_library_resolution"],
                status=r.get("status") or "error",
                source_artifact_id=r.get("resume_source_artifact_id"),
                evidence={"error": r.get("error"), "report_path": r.get("report_path")},
            )
    conn.commit()

    elapsed = time.time() - t0
    log.info(
        "Tailoring done in %.1fs: %d machine_validated, %d failed_validation, "
        "%d failed_judge, %d errors",
        elapsed,
        stats.get("machine_validated", 0),
        stats.get("failed_validation", 0),
        stats.get("failed_judge", 0),
        stats.get("error", 0),
    )

    return {
        "approved": stats.get("machine_validated", 0),
        "machine_validated": stats.get("machine_validated", 0),
        "failed": sum(
            stats.get(status, 0)
            for status in (
                "failed_validation", "failed_judge", "failed_render",
                "unreviewed_lenient", "exhausted_retries"
            )
        ),
        "errors": stats.get("error", 0),
        "elapsed": elapsed,
        "results": results,
    }
