"""Cover letter generation: LLM-powered, profile-driven, with validation.

Generates concise, engineering-voice cover letters tailored to specific job
postings. All personal data (name, skills, achievements) comes from the user's
profile at runtime. No hardcoded personal information.
"""

import json
import logging
import os
import re
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

from applypilot.config import COVER_LETTER_DIR, load_profile
from applypilot.database import get_connection
from applypilot.llm import get_client
from applypilot.scoring.validator import (
    BANNED_WORDS,
    LLM_LEAK_PHRASES,
    sanitize_text,
    validate_cover_letter,
)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5  # max cross-run retries before giving up


class CoverLetterValidationError(RuntimeError):
    """Raised when no generated attempt reaches the safe persistence gate."""

    def __init__(self, message: str, validation: dict | None = None) -> None:
        super().__init__(message)
        self.validation = validation or {}


# ── Prompt Builder (profile-driven) ──────────────────────────────────────

def _build_cover_letter_prompt(profile: dict, surface: str = "formal") -> str:
    """Build the cover letter system prompt from the user's profile.

    All personal data, skills, and sign-off name come from the profile.
    """
    personal = profile.get("personal", {})
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})
    current_employment = profile.get("current_employment", {})
    contact_preferences = profile.get("contact_preferences", {})

    # Preferred name for the sign-off (falls back to full name)
    sign_off_name = (
        personal.get("preferred_display_name")
        or personal.get("preferred_name")
        or personal.get("full_name", "")
    )

    # Flatten all allowed skills
    all_skills: list[str] = []
    for items in boundary.values():
        if isinstance(items, list):
            all_skills.extend(items)
    skills_str = ", ".join(all_skills) if all_skills else "the tools listed in the resume"

    # Real metrics from resume_facts
    real_metrics = resume_facts.get("real_metrics", [])
    preserved_projects = resume_facts.get("preserved_projects", [])

    # Build achievement examples for the prompt
    projects_hint = ""
    if preserved_projects:
        projects_hint = f"\nKnown projects to reference: {', '.join(preserved_projects)}"

    metrics_hint = ""
    if real_metrics:
        metrics_hint = f"\nReal metrics to use: {', '.join(real_metrics)}"

    # Build the full banned list from the validator so the prompt stays in sync
    # with what will actually be rejected — the validator checks all of these.
    all_banned = ", ".join(f'"{w}"' for w in BANNED_WORDS)
    leak_banned = ", ".join(f'"{p}"' for p in LLM_LEAK_PHRASES)

    length_guidance = {
        "formal": "Usually 300-450 words, but completeness and specificity matter more than the exact count.",
        "ats": "Usually 250-400 words for an ATS text area; obey any employer-provided limit.",
        "short_answer": "Usually 120-200 words for a why-this-role question; obey the form limit.",
        "linkedin": "Usually 60-120 words for a recruiter message.",
    }.get(surface, "Use the shortest length that fully answers the employer's request.")

    current_title = str(current_employment.get("title", "")).strip()
    current_company = str(current_employment.get("company", "")).strip()
    current_identity_rule = ""
    if current_title:
        current_identity_rule = (
            "\nCONFIRMED CURRENT EMPLOYMENT IDENTITY:\n"
            f"- Exact current title: {current_title}\n"
            f"- Exact current company: {current_company or 'not configured'}\n"
            "- If you mention the candidate's current role, reproduce the exact title above. "
            "Do not infer or paraphrase a current title from a resume heading, project role, "
            "or the target job. If you name the current employer, use the configured company.\n"
        )

    email_availability_policy = str(
        contact_preferences.get("email_application_availability_policy", "")
    ).strip()
    email_work_auth_statement = str(
        contact_preferences.get("email_application_work_authorization_statement", "")
    ).strip()
    email_application_rule = ""
    if email_availability_policy or email_work_auth_statement:
        email_application_rule = (
            "\nEMAIL APPLICATION AVAILABILITY RULE:\n"
            f"- Policy: {email_availability_policy or 'Do not state a specific start date.'}\n"
            "- Do not disclose or infer any exact internship start date in this letter.\n"
            "- If work authorization or sponsorship is relevant, use only this approved statement: "
            f"{email_work_auth_statement or 'Omit the topic.'}\n"
            "- Do not add a sentence offering, proposing, or negotiating a start date.\n"
        )

    return f"""Write a cover letter for {sign_off_name}. The goal is to get an interview.

TARGET SURFACE: {surface}. {length_guidance}
STRUCTURE: Use 3-5 focused body paragraphs when writing a formal letter. Do not pad or cut useful evidence merely to hit a fixed word count.

PARAGRAPH 1: State the exact role, the candidate's current degree or professional position, any confirmed intake availability that directly matches the job, and one specific reason the employer's technical problem is compelling. Avoid generic enthusiasm.

PARAGRAPH 2: Develop the strongest directly relevant experience as a short narrative. Explain ownership, the system or workflow built, how it worked, and the verified scope or result. Use only facts from the resume.{projects_hint}{metrics_hint}

PARAGRAPH 3: Use one second experience to prove a complementary requirement such as data pipelines, validation, collaboration, or stakeholder communication. Explain the transfer to the target role without pretending the past domain was identical.

PARAGRAPH 4: Explain why this specific employer and technical setting are the logical next step, identify what the candidate can contribute, thank the reader, and request an interview. Close professionally.

JD RESPONSE IS REQUIRED:
- Build the letter around the supplied JD EVIDENCE PLAN. Address at least two of its highest-priority requirements with verified evidence.
- The company-specific paragraph must fail a swap test: replacing this employer with a competitor should make the paragraph inaccurate or obviously incomplete.
- Prefer one core project or role plus one complementary example. Do not turn the letter into a resume inventory.
- You may combine explicit facts from multiple labelled resume sources and make a modest, clearly supportable statement about transferability. Never add a tool, metric, employer, ownership claim, or result that is absent from the sources.

BANNED WORDS AND PHRASES (automated validator rejects ANY of these — do not use even once):
{all_banned}

ALSO BANNED (meta-commentary the validator catches):
{leak_banned}

BANNED PUNCTUATION: No em dashes (—) or en dashes (–). Use commas or periods.

VOICE:
- Write as a thoughtful applicant addressing a hiring manager: professional, specific, and natural.
- NEVER narrate or explain what you're doing. BAD: "This demonstrates my commitment to X." GOOD: Just state the fact and move on.
- Do not overclaim transferability. Clearly distinguish verified past work from the employer's domain and planned learning.
- Read it out loud. If it sounds like a robot wrote it, rewrite it.

FABRICATION = INSTANT REJECTION:
The candidate's real tools are ONLY: {skills_str}.
Do NOT mention ANY tool not in this list. If the job asks for tools not listed, talk about the work you did, not the tools.
- Keep RESUME facts and TARGET JOB facts separate. Never copy a target feature, mode, domain, metric, or outcome into the candidate's past work unless the RESUME explicitly says the candidate already did it.
- A comparison such as "the same pattern" must not imply that the candidate's project had the employer's exact modes, domain, or outcomes.
- If the confirmed application facts explicitly match the target internship intake, state the matching availability once, without broadening any work-authorization condition.
{current_identity_rule}
{email_application_rule}

Sign off exactly as:
Sincerely,

{sign_off_name}

Output ONLY the letter text. No subject lines. No "Here is the cover letter:" preamble. No notes after the sign-off.
Start DIRECTLY with "Dear Hiring Manager," and end with the name."""


# ── Helpers ──────────────────────────────────────────────────────────────

def _strip_preamble(text: str) -> str:
    """Remove LLM preamble before 'Dear Hiring Manager,' if present.

    Gemini and other models sometimes output "Here is the cover letter:" or
    similar meta-commentary before the actual letter text. Strip everything
    before the first occurrence of "Dear" so the validator's start-check passes.
    """
    dear_idx = text.lower().find("dear")
    if dear_idx > 0:
        return text[dear_idx:]
    return text


def read_resume_source(path: Path) -> str:
    """Read a UTF-8 text or DOCX resume without mutating global state."""
    path = path.resolve()
    if path.suffix.lower() == ".txt":
        return path.read_text(encoding="utf-8")
    if path.suffix.lower() != ".docx":
        raise ValueError(f"Resume source must be .txt or .docx: {path}")

    word_ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    text_tag = f"{word_ns}t"
    tab_tag = f"{word_ns}tab"
    break_tags = {f"{word_ns}br", f"{word_ns}cr"}
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{word_ns}p"):
        parts: list[str] = []
        for node in paragraph.iter():
            if node.tag == text_tag:
                parts.append(node.text or "")
            elif node.tag == tab_tag:
                parts.append("\t")
            elif node.tag in break_tags:
                parts.append("\n")
        for line in "".join(parts).splitlines():
            if line.strip():
                paragraphs.append(line.strip())
    return "\n".join(paragraphs)


def load_evidence_sources(profile: dict, primary_path: Path, primary_text: str) -> list[dict]:
    """Load the selected resume plus explicitly registered supplemental resumes.

    Supplemental files are evidence-only. They may add a verified fact omitted
    from the selected variant, but they do not replace the selected resume as
    the primary positioning document.
    """
    primary_path = primary_path.resolve()
    sources = [{"label": "primary_selected_resume", "path": str(primary_path), "text": primary_text}]
    configured = profile.get("cover_letter", {}).get("evidence_sources", [])
    seen = {str(primary_path).lower()}
    for index, raw_path in enumerate(configured, start=1):
        path = Path(raw_path).resolve()
        key = str(path).lower()
        if key in seen:
            continue
        if not path.exists():
            log.warning("Configured cover-letter evidence source is missing: %s", path)
            continue
        text = read_resume_source(path)
        if not text.strip():
            continue
        sources.append({
            "label": f"supplemental_resume_{index}",
            "path": str(path),
            "text": text,
        })
        seen.add(key)
    work_auth = profile.get("work_authorization", {})
    current_employment = profile.get("current_employment", {})
    contact_preferences = profile.get("contact_preferences", {})
    email_policy = str(
        contact_preferences.get("email_application_availability_policy", "")
    ).strip()
    email_work_auth = str(
        contact_preferences.get("email_application_work_authorization_statement", "")
    ).strip()
    sources.append({
        "label": "confirmed_application_profile",
        "path": "profile.json",
        "text": (
            f"Internship sponsorship condition: {work_auth.get('require_sponsorship', '')}\n"
            f"Approved email work-authorization statement: {email_work_auth}\n"
            f"Email availability policy: {email_policy}\n"
            f"Current employer: {current_employment.get('company', '')}\n"
            f"Exact current title: {current_employment.get('title', '')}\n"
            "Do not disclose a specific internship start date in an email or cover letter. "
            "These are conditional application facts and must not be broadened."
        ),
    })
    return sources


def _parse_json_object(response: str) -> dict:
    text = response.strip()
    if not text:
        raise CoverLetterValidationError(
            "JD evidence planning returned an empty model response; no artifact may be persisted."
        )
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        text = fenced.group(1)
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object.")
    return payload


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _source_excerpt(source_text: str, job_description: str, max_lines: int = 10) -> str:
    """Select JD-relevant source lines while retaining exact quote text."""
    stopwords = {
        "about", "after", "also", "and", "are", "for", "from", "have", "into",
        "our", "that", "the", "their", "this", "with", "will", "you", "your",
        "intern", "internship", "role", "team", "work",
    }
    jd_tokens = {
        token
        for token in re.findall(r"[a-z0-9+#.]+", job_description.casefold())
        if len(token) >= 3 and token not in stopwords
    }
    lines = [line.strip() for line in source_text.splitlines() if len(line.split()) >= 4]
    ranked: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        line_tokens = set(re.findall(r"[a-z0-9+#.]+", line.casefold()))
        overlap = len(jd_tokens.intersection(line_tokens))
        metric_bonus = 1 if re.search(r"\d", line) else 0
        ranked.append((overlap * 3 + metric_bonus, index, line))
    selected = sorted(ranked, key=lambda item: (-item[0], item[1]))[:max_lines]
    return "\n".join(line for _, _, line in sorted(selected, key=lambda item: item[1]))


def _parse_evidence_plan_response(response: str) -> dict:
    """Parse the compact line protocol used for evidence planning."""
    text = response.strip()
    if not text:
        raise CoverLetterValidationError(
            "JD evidence planning returned an empty model response; no artifact may be persisted."
        )
    if text.startswith("{") or text.startswith("```json"):
        return _parse_json_object(text)

    requirements: list[dict] = []
    company_reason = ""
    for line in text.splitlines():
        normalized = re.sub(r"^[#*\-\s]+", "", line).strip()
        if normalized.casefold().startswith("map:"):
            fields = [field.strip() for field in normalized.split(":", 1)[1].split("|||")]
            if len(fields) != 3:
                continue
            requirement, source_label, source_quote = fields
            if source_label.casefold() == "none":
                source_label = ""
            if source_quote.casefold() == "none":
                source_quote = ""
            requirements.append({
                "priority": len(requirements) + 1,
                "requirement": requirement,
                "evidence_summary": source_quote,
                "source_label": source_label,
                "source_quote": source_quote,
                "gap": "" if source_quote else "No source-verified quote",
            })
        elif normalized.casefold().startswith("company_reason:"):
            company_reason = normalized.split(":", 1)[1].strip()
    return {"requirements": requirements, "company_specific_reason": company_reason}


def build_evidence_plan(job: dict, evidence_sources: list[dict]) -> dict:
    """Map the JD's priorities to source-verified resume evidence before drafting."""
    company = job.get("company_name") or "Unknown employer"
    job_description = (job.get("full_description") or "")[:12000]
    source_text = "\n\n".join(
        f"SOURCE LABEL: {source['label']}\nPATH: {source['path']}\nRELEVANT EXCERPT:\n"
        f"{_source_excerpt(source['text'], job_description, max_lines=12 if index == 0 else 6)}"
        for index, source in enumerate(evidence_sources)
    )
    messages = [
        {
            "role": "system",
            "content": (
                "You are an evidence planner for a cover letter. Extract exactly 3 important "
                "requirements from the job description and map them to verified resume evidence. "
                "A source_quote must be copied verbatim from the named verified source and should be "
                "at least six words. If there is no evidence, leave source_label and source_quote "
                "as NONE. You may connect facts across resume variants, but do not invent tools, "
                "metrics, ownership, employers, or outcomes. Return exactly four lines and no JSON: "
                "three lines formatted MAP: requirement ||| source_label ||| source_quote, then one "
                "line COMPANY_REASON: employer-specific technical reason from the JD."
            ),
        },
        {
            "role": "user",
            "content": (
                f"TARGET ROLE: {job.get('title', '')}\nCOMPANY: {company}\n"
                f"JOB DESCRIPTION:\n{job_description}\n\nVERIFIED CANDIDATE SOURCES:\n{source_text}"
            ),
        },
    ]
    plan_tokens = int(os.environ.get("APPLYPILOT_EVIDENCE_PLAN_MAX_TOKENS", "4096"))
    plan_retries = max(0, int(os.environ.get("APPLYPILOT_PLAN_MAX_RETRIES", "2")))
    payload: dict | None = None
    last_error: Exception | None = None
    for attempt in range(plan_retries + 1):
        response = get_client().chat(messages, max_tokens=plan_tokens, temperature=0.1)
        try:
            payload = _parse_evidence_plan_response(response)
            break
        except (CoverLetterValidationError, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            if attempt >= plan_retries:
                raise
            log.warning("JD evidence plan attempt %d returned no parseable plan; retrying.", attempt + 1)
    if payload is None:
        raise CoverLetterValidationError(f"JD evidence planning failed: {last_error}")
    raw_requirements = payload.get("requirements")
    if not isinstance(raw_requirements, list):
        raise CoverLetterValidationError("JD evidence planning returned no requirements.")

    source_lookup = {source["label"]: _normalized(source["text"]) for source in evidence_sources}
    requirements: list[dict] = []
    grounded_count = 0
    for item in raw_requirements[:5]:
        if not isinstance(item, dict) or not str(item.get("requirement", "")).strip():
            continue
        source_label = str(item.get("source_label", "")).strip()
        source_quote = str(item.get("source_quote", "")).strip()
        quote_is_grounded = (
            source_label in source_lookup
            and len(source_quote.split()) >= 6
            and _normalized(source_quote) in source_lookup[source_label]
        )
        normalized_item = {
            "priority": item.get("priority", len(requirements) + 1),
            "requirement": str(item.get("requirement", "")).strip(),
            "evidence_summary": str(item.get("evidence_summary", "")).strip() if quote_is_grounded else "",
            "source_label": source_label if quote_is_grounded else "",
            "source_quote": source_quote if quote_is_grounded else "",
            "gap": str(item.get("gap", "")).strip() or ("No source-verified quote" if not quote_is_grounded else ""),
        }
        grounded_count += int(quote_is_grounded)
        requirements.append(normalized_item)

    if len(requirements) < 2 or grounded_count < 2:
        raise CoverLetterValidationError(
            f"JD evidence plan is insufficient: {len(requirements)} requirements, "
            f"{grounded_count} source-grounded mappings."
        )
    return {
        "requirements": requirements,
        "company_specific_reason": str(payload.get("company_specific_reason", "")).strip(),
    }


# ── Core Generation ──────────────────────────────────────────────────────

def generate_cover_letter_document(
    resume_text: str,
    job: dict,
    profile: dict,
    evidence_sources: list[dict] | None = None,
    max_retries: int = 3,
    validation_mode: str = "normal",
    surface: str = "formal",
) -> dict:
    """Plan, draft, and validate a grounded cover letter.

    No text is returned as successful unless it passes the final validator.
    Callers can therefore persist every returned document safely.
    """
    company = job.get("company_name") or "Unknown employer"
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {company}\n"
        f"SOURCE BOARD: {job.get('source_site') or job.get('site') or 'Unknown'}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{(job.get('full_description') or '')[:12000]}"
    )
    if evidence_sources is None:
        evidence_sources = [{
            "label": "primary_selected_resume",
            "path": "runtime_primary_resume",
            "text": resume_text,
        }]
    evidence_plan = build_evidence_plan(job, evidence_sources)
    job_description = job.get("full_description") or ""
    evidence_text = "\n\n".join(
        f"SOURCE LABEL: {source['label']}\nRELEVANT EXCERPT:\n"
        f"{_source_excerpt(source['text'], job_description, max_lines=12 if index == 0 else 6)}"
        for index, source in enumerate(evidence_sources)
    )

    avoid_notes: list[str] = []
    last_validation: dict = {}
    client = get_client()
    cl_prompt_base = _build_cover_letter_prompt(profile, surface=surface)
    personal = profile.get("personal", {})
    expected_signoff = (
        personal.get("preferred_display_name")
        or personal.get("preferred_name")
        or personal.get("full_name", "")
    )
    current_employment = profile.get("current_employment", {})
    expected_current_title = str(current_employment.get("title", "")).strip() or None
    expected_current_company = str(current_employment.get("company", "")).strip() or None

    for attempt in range(max_retries + 1):
        prompt = cl_prompt_base
        if avoid_notes:
            prompt += "\n\n## AVOID THESE ISSUES:\n" + "\n".join(
                f"- {note}" for note in avoid_notes[-8:]
            )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": (
                f"VERIFIED CANDIDATE SOURCES:\n{evidence_text}\n\n---\n\n"
                f"JD EVIDENCE PLAN:\n{json.dumps(evidence_plan, ensure_ascii=False, indent=2)}\n\n---\n\n"
                f"TARGET JOB:\n{job_text}\n\nWrite the cover letter:"
            )},
        ]

        letter_tokens = int(os.environ.get("APPLYPILOT_COVER_MAX_TOKENS", "6144"))
        letter = client.chat(messages, max_tokens=letter_tokens, temperature=0.55)
        letter = _strip_preamble(sanitize_text(letter))
        last_validation = validate_cover_letter(
            letter,
            mode=validation_mode,
            expected_signoff=expected_signoff,
            company_name=company,
            evidence_plan=evidence_plan,
            surface=surface,
            expected_current_title=expected_current_title,
            expected_current_company=expected_current_company,
        )
        if last_validation["passed"]:
            return {
                "text": letter,
                "validation": last_validation,
                "evidence_plan": evidence_plan,
                "surface": surface,
            }

        avoid_notes.extend(last_validation["errors"])
        log.debug(
            "Cover letter attempt %d/%d failed: %s",
            attempt + 1,
            max_retries + 1,
            last_validation["errors"],
        )

    raise CoverLetterValidationError(
        "Cover letter failed validation after all generation attempts: "
        + "; ".join(last_validation.get("errors", ["unknown validation failure"])),
        validation=last_validation,
    )


def generate_cover_letter(
    resume_text: str,
    job: dict,
    profile: dict,
    max_retries: int = 3,
    validation_mode: str = "normal",
) -> str:
    """Backward-compatible text-only wrapper around the safe document API."""
    return generate_cover_letter_document(
        resume_text,
        job,
        profile,
        max_retries=max_retries,
        validation_mode=validation_mode,
    )["text"]


# ── Batch Entry Point ────────────────────────────────────────────────────

def run_cover_letters(min_score: int = 7, limit: int = 20,
                      validation_mode: str = "normal") -> dict:
    """Generate cover letters for high-scoring jobs that have tailored resumes.

    Args:
        min_score:       Minimum fit_score threshold.
        limit:           Maximum jobs to process.
        validation_mode: "strict", "normal", or "lenient".

    Returns:
        {"generated": int, "errors": int, "elapsed": float}
    """
    profile = load_profile()
    conn = get_connection()
    from applypilot.eligibility import ELIGIBLE_SQL, refresh_job_eligibility
    refresh_job_eligibility(conn)

    # Fetch jobs that have tailored resumes but no cover letter yet
    jobs = conn.execute(
        "SELECT * FROM jobs "
        "WHERE fit_score >= ? AND tailored_resume_path IS NOT NULL "
        "AND tailor_status='machine_validated' "
        "AND company_name IS NOT NULL AND company_name != '' "
        "AND full_description IS NOT NULL "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '') "
        f"AND COALESCE(cover_attempts, 0) < ? AND {ELIGIBLE_SQL} "
        "ORDER BY fit_score DESC LIMIT ?",
        (min_score, MAX_ATTEMPTS, limit),
    ).fetchall()

    if not jobs:
        log.info("No jobs needing cover letters (score >= %d).", min_score)
        return {"generated": 0, "errors": 0, "elapsed": 0.0}

    # Convert rows to dicts
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    COVER_LETTER_DIR.mkdir(parents=True, exist_ok=True)
    log.info(
        "Generating cover letters for %d jobs (score >= %d)...",
        len(jobs), min_score,
    )
    t0 = time.time()
    completed = 0
    results: list[dict] = []
    error_count = 0

    for job in jobs:
        completed += 1
        try:
            primary_path = Path(job["tailored_resume_path"]).resolve()
            if not primary_path.exists():
                raise FileNotFoundError(f"Selected tailored resume not found: {primary_path}")
            resume_text = read_resume_source(primary_path)
            evidence_sources = load_evidence_sources(profile, primary_path, resume_text)
            document = generate_cover_letter_document(
                resume_text,
                job,
                profile,
                evidence_sources=evidence_sources,
                max_retries=max(0, int(os.environ.get("APPLYPILOT_DOCUMENT_MAX_RETRIES", "3"))),
                validation_mode=validation_mode,
            )
            letter = document["text"]

            # Build safe filename prefix
            safe_title = re.sub(r"[^\w\s-]", "", job["title"])[:50].strip().replace(" ", "_")
            safe_company = re.sub(r"[^\w\s-]", "", job["company_name"])[:30].strip().replace(" ", "_")
            prefix = f"{safe_company}_{safe_title}"

            cl_path = COVER_LETTER_DIR / f"{prefix}_CL.txt"
            report_path = COVER_LETTER_DIR / f"{prefix}_CL.report.json"
            cl_path.write_text(letter, encoding="utf-8")
            report = {
                "url": job["url"],
                "title": job["title"],
                "company": job["company_name"],
                "source_site": job.get("source_site") or job.get("site"),
                "resume_source": str(primary_path),
                "evidence_sources": [source["path"] for source in evidence_sources],
                "status": "machine_validated",
                "validation": document["validation"],
                "evidence_plan": document["evidence_plan"],
                "surface": document["surface"],
                "human_approval_required": True,
            }
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

            result = {
                "url": job["url"],
                "path": str(cl_path),
                "report_path": str(report_path),
                "title": job["title"],
                "company_name": job["company_name"],
                "source_site": job.get("source_site") or job.get("site"),
                "resume_source": str(primary_path),
                "evidence_sources": [source["path"] for source in evidence_sources],
            }
            results.append(result)

            elapsed = time.time() - t0
            rate = completed / elapsed if elapsed > 0 else 0
            log.info(
                "%d/%d [OK] | %.1f jobs/min | %s",
                completed, len(jobs), rate * 60, result["title"][:40],
            )
        except Exception as e:
            result = {
                "url": job["url"], "title": job["title"],
                "company_name": job.get("company_name"),
                "source_site": job.get("source_site") or job.get("site"),
                "path": None, "error": str(e),
            }
            error_count += 1
            results.append(result)
            log.error("%d/%d [ERROR] %s -- %s", completed, len(jobs), job["title"][:40], e)

    # Persist to DB: increment attempt counter for ALL, save path only for successes
    now = datetime.now(timezone.utc).isoformat()
    saved = 0
    for r in results:
        if r.get("path"):
            conn.execute(
                "UPDATE jobs SET cover_letter_path=?, cover_letter_at=?, "
                "cover_letter_status='machine_validated', cover_letter_error=NULL, "
                "cover_letter_approved_at=NULL, cover_letter_approved_by=NULL, "
                "cover_letter_source_resume_path=?, cover_letter_evidence_sources=?, "
                "cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
                (
                    r["path"],
                    now,
                    r["resume_source"],
                    json.dumps(r["evidence_sources"], ensure_ascii=False),
                    r["url"],
                ),
            )
            saved += 1
        else:
            conn.execute(
                "UPDATE jobs SET cover_letter_status='failed_validation', cover_letter_error=?, "
                "cover_letter_path=NULL, cover_letter_at=NULL, "
                "cover_attempts=COALESCE(cover_attempts,0)+1 WHERE url=?",
                (r.get("error", "unknown cover-letter error"), r["url"]),
            )
    conn.commit()

    elapsed = time.time() - t0
    log.info("Cover letters done in %.1fs: %d generated, %d errors", elapsed, saved, error_count)

    return {
        "generated": saved,
        "errors": error_count,
        "elapsed": elapsed,
    }
