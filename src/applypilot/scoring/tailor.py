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
import time
from datetime import datetime, timezone
from pathlib import Path

from applypilot.config import RESUME_PATH, TAILORED_DIR, load_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client
from applypilot.scoring.cover_letter import read_resume_source
from applypilot.scoring.validator import (
    BANNED_WORDS,
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
        "the strongest evidence fits. A genuinely high-threshold role may use two pages when multiple "
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

The selected source resume is the only factual source for this tailored version. Do not add even a closely related or learnable tool unless that exact tool already appears in the selected source.

## TAILORING RULES:

TARGET FUNCTION: Return the target function in `title` for routing and internship-layout selection only. It will not be printed beneath the candidate's name because that line is reserved for contact information.

EXPERIENCE IDENTITY: Never rename an employer or employment title. In every experience object, copy the exact company into `header` and preserve the exact source role in `subtitle`. Preserve dates and seniority. Target-job language belongs in the summary and bullets, not in past titles.

SUMMARY: Rewrite from scratch. Lead with the 1-2 source-proven skills that matter most for THIS role. Prefer one or two compact sentences, normally about 26-45 words total. Use the full line width efficiently: do not add a very short closing sentence, and ensure the final sentence contains at least 8 words. Every statement about work performed, data analyzed, experiments run, users served, domains covered, or outcomes achieved must be supported by the selected source. Do not turn a JD responsibility into candidate history. Target-role framing is allowed; invented experience is not. In particular, do not copy a JD-only action or domain term into the summary unless that term, or the same factual work, appears in the selected source. Never join two sector domains (for example, legal and planning) into one work claim unless a single source bullet explicitly contains both domains; split the facts into separate sentences or omit the weaker domain.

SECTION ORDER: For an internship, trainee, co-op, or current-student target, use Summary, Education, Technical Skills, Experience, Projects. For other roles, use Summary, Technical Skills, Experience, Projects, Education. The renderer applies this rule automatically; keep all education facts in `education`.

TECHNICAL SKILLS LAYOUT: Keep each category compact enough to use the available line width efficiently. Avoid a wrapped row whose final rendered line contains only one to four words. Prefer removing lower-priority source skills, shortening a category label, or rebalancing source-grounded items across categories; never shrink typography or invent a skill to fill space.

SKILLS: Reorder each category so the job's must-haves appear first.

Reorder bullets by relevance. Rephrase only where the new wording is more useful and preserves exactly the same action, ownership, scope, tools, metric, and outcome. Verbatim source wording is allowed and preferred when rewriting would weaken factual precision.

PROJECTS: {"Keep the most relevant source projects and preserve each project identity." if source_has_projects else "Return an empty projects list because the selected source has no project section."}

BULLETS: Most relevant first. Use only source-supported verbs and outcomes. Do not force every bullet into an impact formula, do not manufacture causal results, and do not add a number copied from the JD. Keep at most 4 bullets per experience or project.

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

{{"title":"Target function used for routing only","summary":"1-2 compact tailored sentences.","skills":{{"Source category":"comma-separated source skills"}},"experience":[{{"header":"Exact Company","subtitle":"Exact Source Role | Exact Source Dates","bullets":["source-grounded bullet"]}}],"projects":[{{"header":"Exact Source Project","subtitle":"Exact Source Role | Exact Source Dates","bullets":["source-grounded bullet"]}}],"education":"All source schools and degrees","evidence_map":[{{"requirement":"JD requirement","support_level":"direct or transferable or gap","source_quote":"verbatim source quote, or empty for gap"}}]}}"""


def _build_judge_prompt(profile: dict) -> str:
    """Build the LLM judge prompt from the user's profile."""
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Flatten allowed skills for the judge
    all_skills: list[str] = []
    for items in boundary.values():
        if isinstance(items, list):
            all_skills.extend(items)
    skills_str = ", ".join(all_skills) if all_skills else "N/A"

    real_metrics = resume_facts.get("real_metrics", [])
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"

    return f"""You are a resume quality judge. A tailoring engine rewrote a resume to target a specific job. Your job is to catch unsupported claims and useless tailoring, not merely obvious lies.

Return only one JSON object matching this schema:
{{"verdict":"PASS or FAIL","issues":["specific issue"],"summary_claims":[{{"claim":"exact complete sentence copied from the tailored SUMMARY","source_quotes":["one or more exact supporting quotes copied verbatim from the original resume"],"supported":true}}]}}

Audit every complete sentence in the tailored SUMMARY. Copy each sentence exactly into `claim`. For every factual action, experience, domain, user, experiment, data, ownership, tool, metric, or outcome statement, provide one or more exact original-resume quotes that together support the whole claim. Each quote must be verbatim; do not write an explanation in `source_quotes`. If a summary sentence names a sector or domain such as urban planning, legal, finance, transportation, or healthcare, at least one quoted source line must contain that same sector/domain word; an exact source role, degree, section line, or bullet is valid evidence. Do not omit an obvious sector quote. A JD sentence is never candidate evidence. If the quote set does not support the whole claim, set `supported` false, explain it in `issues`, and FAIL. Target-function labels such as "Data Analyst" may be supported by closely matching source work, but claims such as "analyzed engagement data", "ran experiments", or "turned user behavior into product improvements" require those facts in the original resume.

## CONTEXT -- what the tailoring engine was instructed to do (all of this is ALLOWED):
- Use the title field only to select the target function and layout; it is not printed in the header
- Rewrite the summary from scratch for the target job
- Reorder bullets and projects to put the most relevant first
- Reframe bullets to use the job's language
- Drop low-relevance bullets and replace with more relevant ones from other sections
- Reorder the skills section to put job-relevant skills first
- Change tone and wording extensively

## WHAT IS FABRICATION (FAIL for these):
1. Adding tools, languages, or frameworks anywhere that aren't in the selected source. The profile-level upper boundary is: {skills_str}
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
There is no allowance for "minor stretches" or learnable-but-unlisted skills. A plausible claim is still unsupported if it is absent from the selected source. Fail changed job titles, transferred metrics, stronger ownership, new causal outcomes, and JD facts copied into the candidate's history.

Also judge usefulness: the summary, skill order, leading bullets, and project order should emphasize the strongest source-supported direct or transferable matches without keyword stuffing. Missing JD requirements are honest gaps, not instructions to invent them. Do not fail merely because the candidate lacks A/B tests, DAU/MAU, interviews, a tool, or another responsibility; fail usefulness only when relevant source evidence exists but the tailored resume ignores it, or when the rewrite is so generic it could target an unrelated job.

Be strict about factual support and specific about each issue. Do not fail accurate reordering or faithful wording changes."""


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
        lines.extend(["EDUCATION", sanitize_text(str(data.get("education", ""))), ""])

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


def judge_tailored_resume(
    original_text: str,
    tailored_text: str,
    job_title: str,
    profile: dict,
    job_description: str = "",
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
    judge_prompt = _build_judge_prompt(profile)

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
    max_tokens = int(os.environ.get("APPLYPILOT_JUDGE_MAX_TOKENS", "1024"))
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
        audit = {"verdict": "FAIL", "issues": [f"Judge returned invalid JSON: {exc}"], "summary_claims": []}

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
    normalized_source = re.sub(r"\s+", " ", original_text).strip().casefold()
    audited_claims = audit.get("summary_claims", [])
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
    if not isinstance(audited_claims, list):
        issues_list.append("Judge summary_claims field was not a list.")
        audited_claims = []
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
        quotes_are_exact = bool(quotes) and all(
            _is_exact_source_quote(quote, normalized_source)
            for quote in quotes
        )
        combined_quotes = " ".join(quotes)
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

    missing_summary_evidence = [
        sentence for sentence in summary_sentences
        if sentence.casefold() not in grounded_claims
    ]
    if not summary_sentences:
        issues_list.append("Tailored SUMMARY could not be parsed for claim auditing.")
    elif missing_summary_evidence:
        issues_list.append(
            "Judge did not provide exact source evidence for every summary sentence: "
            + " | ".join(missing_summary_evidence[:3])
        )

    passed = (
        str(audit.get("verdict", "")).strip().upper() == "PASS"
        and not issues_list
        and bool(summary_sentences)
        and not missing_summary_evidence
    )
    issues = "none" if not issues_list else "; ".join(issues_list)

    return {
        "passed": passed,
        "verdict": "PASS" if passed else "FAIL",
        "issues": issues,
        "raw": response,
        "audit": audit,
        "summary_sentences": summary_sentences,
        "summary_evidence_complete": bool(summary_sentences) and not missing_summary_evidence,
        "response_meta": dict(getattr(client, "last_response_meta", {}) or {}),
    }


# ── Core Tailoring ───────────────────────────────────────────────────────

def tailor_resume(
    resume_text: str, job: dict, profile: dict,
    max_retries: int = 3, validation_mode: str = "normal",
    source_resume_path: str | None = None,
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
    }
    avoid_notes: list[str] = []
    if str(job.get("tailor_error") or "").strip():
        avoid_notes.append(str(job["tailor_error"]).strip())
    tailored = ""
    client = get_client()
    source_has_projects = bool(
        re.search(r"(?im)^\s*(?:selected\s+)?projects\s*$", resume_text)
    )
    tailor_prompt_base = _build_tailor_prompt(
        profile,
        source_has_projects=source_has_projects,
    )

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
                + f"ORIGINAL RESUME:\n{resume_text}\n\n---\n\n"
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
        validation = validate_json_fields(
            data,
            profile,
            mode=validation_mode,
            original_text=resume_text,
            job_description=job.get("full_description") or "",
            job_title=job.get("title") or "",
            target_company=job.get("company_name") or "",
        )
        report["validator"] = validation
        report["evidence_map"] = data.get("evidence_map", [])

        if not validation["passed"]:
            # Only retry if there are hard errors (warnings never block)
            avoid_notes.extend(validation["errors"])
            if attempt < max_retries:
                continue
            # Last attempt: return the rejected text for diagnostics only.
            tailored = assemble_resume_text(data, profile)
            report["status"] = "failed_validation"
            return tailored, report

        # Assemble text (header injected by code, em dashes auto-fixed)
        tailored = assemble_resume_text(data, profile)

        full_validation = validate_tailored_resume(
            tailored,
            profile,
            original_text=resume_text,
        )
        report["full_validator"] = full_validation
        if not full_validation["passed"]:
            avoid_notes.extend(full_validation["errors"])
            if attempt < max_retries:
                continue
            report["status"] = "failed_validation"
            return tailored, report

        # Layer 3: LLM judge catches semantic drift that deterministic checks
        # cannot prove. A skipped or failed judge is never a usable success.
        if validation_mode == "lenient":
            report["judge"] = {"verdict": "SKIPPED", "passed": True, "issues": "none"}
            report["status"] = "unreviewed_lenient"
            return tailored, report

        judge = judge_tailored_resume(
            resume_text,
            tailored,
            job.get("title", ""),
            profile,
            job_description=job.get("full_description") or "",
        )
        report["judge"] = judge

        if not judge["passed"]:
            avoid_notes.append(f"Judge rejected: {judge['issues']}")
            if attempt < max_retries:
                continue
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
              AND tailored_resume_path IS NULL
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
        try:
            library_route = (
                route_resume_for_job(conn, job, profile)
                if library_enabled
                else {
                    "decision": "create_variant",
                    "assignment_id": None,
                    "reason": "Resume library is not configured.",
                }
            )
            route_decision = library_route["decision"]
            if route_decision == "reuse_exact":
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

            source_path, routing = select_resume_source(job, profile)
            resume_text = read_resume_source(source_path)
            document_retries = max(0, int(os.environ.get("APPLYPILOT_DOCUMENT_MAX_RETRIES", "3")))
            tailored, report = tailor_resume(
                resume_text,
                job,
                profile,
                max_retries=document_retries,
                validation_mode=validation_mode,
                source_resume_path=str(source_path),
            )
            report["resume_routing"] = routing

            # Build safe filename prefix
            safe_title = re.sub(r"[^\w\s-]", "", job["title"])[:50].strip().replace(" ", "_")
            safe_company = re.sub(r"[^\w\s-]", "", job["company_name"])[:30].strip().replace(" ", "_")
            prefix = f"{safe_company}_{safe_title}"

            success = report["status"] == "machine_validated"
            validated_txt_path = TAILORED_DIR / f"{prefix}.txt"
            if success:
                txt_path = validated_txt_path
            else:
                rejected_dir = TAILORED_DIR / "rejected"
                rejected_dir.mkdir(parents=True, exist_ok=True)
                txt_path = rejected_dir / f"{prefix}_REJECTED.txt"
                if validated_txt_path.exists():
                    stale_path = rejected_dir / (
                        f"{prefix}_PREVIOUSLY_VALIDATED_"
                        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.txt"
                    )
                    validated_txt_path.replace(stale_path)
                    report["quarantined_previous_path"] = str(stale_path)
            if tailored:
                txt_path.write_text(tailored, encoding="utf-8")

            # Save job description for traceability
            job_path = TAILORED_DIR / f"{prefix}_JOB.txt"
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
                    rejected_dir = TAILORED_DIR / "rejected"
                    rejected_dir.mkdir(parents=True, exist_ok=True)
                    rejected_path = rejected_dir / f"{prefix}_REJECTED.txt"
                    txt_path.replace(rejected_path)
                    txt_path = rejected_path
                    success = False

            # Persist the render verdict together with the content verdict.
            report_path = TAILORED_DIR / f"{prefix}_REPORT.json"
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
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
                "resume_library_assignment_id": library_route["assignment_id"],
            }
        except Exception as e:
            result = {
                "url": job["url"], "title": job["title"],
                "company_name": job.get("company_name"),
                "source_site": job.get("source_site") or job.get("site"),
                "status": "error", "attempts": 0, "path": None, "pdf_path": None,
                "rejected_path": None, "report_path": None,
                "source_resume_path": None, "error": str(e),
                "resume_library_decision": "error",
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
    now = datetime.now(timezone.utc).isoformat()
    for r in results:
        if r["status"] == "machine_validated":
            attempt_increment = 0 if r.get("resume_library_decision") == "reuse_exact" else 1
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
            and r.get("resume_library_decision") == "create_variant"
        ):
            stored = conn.execute("SELECT * FROM jobs WHERE url=?", (r["url"],)).fetchone()
            if stored is not None:
                registration = register_tailored_artifact(
                    conn,
                    job=dict(stored),
                    text_path=r["path"],
                    source_resume_path=r.get("source_resume_path"),
                    report_path=r.get("report_path"),
                    profile=profile,
                )
                r["resume_artifact_id"] = registration["artifact_id"]
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
