"""Deterministic resume-library routing and append-only provenance.

The application pipeline historically generated one file per job.  This
module adds a content-addressed library above that interface: identical resume
content is one artifact, validated artifacts gain fine-grained coverage cells,
and every routing/validation event remains auditable.  The legacy jobs columns
stay as a compatibility projection for the browser application runtime.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import yaml

from applypilot.apply.authorization import compute_file_binding, compute_job_fingerprint
from applypilot.config import CONFIG_DIR, TAILORED_DIR
from applypilot.radar import SUBTRACK_TO_TRACK, classify_job_subtracks
from applypilot.resume_versions import (
    changed_used_facts,
    ensure_version_schema,
    freeze_render,
    health_input_digest,
    library_root,
    profile_fact_snapshot,
    register_render,
    text_digest,
)
from applypilot.scoring.cover_letter import read_resume_source
from applypilot.scoring.validator import (
    current_profile_resume_fact_errors,
    validate_tailored_resume,
)
from applypilot.storage.transactions import execute_transactional_script

TAXONOMY_VERSION = "resume-library-v8"
POLICY_VERSION = "reuse-policy-v5"
HEALTH_POLICY_VERSION = "resume-health-v3"
RANKING_VERSION = "resume-ranking-v2"

REUSE_REQUIRED_COVERAGE = 0.90
REUSE_OVERALL_SCORE = 0.85
REUSE_MIN_MARGIN = 0.08
REORDER_OVERALL_SCORE = 0.70
PATCH_OVERALL_SCORE = 0.42
DEFAULT_CANDIDATE_LIMIT = 5

RESUME_RESOLUTIONS = {
    "reuse_as_is",
    "reuse_with_reorder",
    "patch_existing",
    "create_new",
}

_CONTENT_STOPWORDS = {
    "about", "across", "after", "also", "among", "and", "are", "based",
    "build", "candidate", "company", "develop", "experience", "for", "from",
    "have", "into", "intern", "internship", "job", "looking", "must", "our",
    "preferred", "required", "requirements", "role", "skills", "support", "team",
    "that", "the", "their", "this", "through", "using", "what", "will", "with",
    "work", "you", "your",
}

_PROFILE_TRACK_ALIASES = {
    "general_product_consulting": "general_product_consulting",
    "data_bi_decision": "data_bi_decision",
    "data_bi_decision_analysis": "data_bi_decision",
    "ai_implementation": "ai_implementation",
    "ai_implementation_automation": "ai_implementation",
    "spatial": "spatial",
    "spatial_data_urban_technology": "spatial",
}

_SENIOR_TITLE = re.compile(
    r"(?i)\b(?:senior|sr\.?|staff|principal|director|head|vice president|vp|chief)\b"
)
_EXPERIENCE_REQUIREMENT = re.compile(
    r"(?i)\b(?:at least|minimum(?:\s+of)?|min\.?)?\s*(\d{1,2})\s*\+?\s*years?"
)
_REQUIRED_MARKERS = re.compile(
    r"(?i)\b(?:must|required|requirements?|qualifications?|you have|proficien(?:t|cy)|experience with)\b"
)
_PREFERRED_MARKERS = re.compile(
    r"(?i)\b(?:preferred|optional|nice[- ]to[- ]have|good[- ]to[- ]have|bonus|plus|ideally)\b"
)

_KNOWN_SKILLS = {
    "cuda", "tensorrt", "kubernetes", "pytorch", "tensorflow", "spark",
    "python",
    "sql",
    "r",
    "javascript",
    "typescript",
    "react",
    "geopandas",
    "git",
    "postgresql",
    "arcgis",
    "qgis",
    "rest",
    "openapi",
    "rag",
    "llm",
    "machine learning",
    "deep learning",
    "power bi",
    "tableau",
    "excel",
    "aws",
    "azure",
    "gcp",
    "jira",
    "salesforce",
    "data visualization",
    "data analysis",
    "business intelligence",
    "project management",
    "product management",
    "stakeholder management",
}


def _requirement_marker_events(text: str) -> list[tuple[int, int, str]]:
    """Return non-negated requirement/preference markers in textual order."""
    events: list[tuple[int, int, str]] = []
    for marker, kind in (
        (_PREFERRED_MARKERS, "preferred"),
        (_REQUIRED_MARKERS, "required"),
    ):
        for match in marker.finditer(text):
            prefix = text[max(0, match.start() - 16):match.start()]
            if re.search(r"(?i)\b(?:not|no)\s+$", prefix):
                continue
            events.append((match.start(), match.end(), kind))
    return sorted(events)


def _marker_introduces_skill(text: str, event: tuple[int, int, str]) -> bool:
    """Whether a marker is a prefix for the immediately following skill."""
    marker_start, marker_end, _ = event
    suffix = text[marker_end:]
    if not any(
        re.match(rf"\s*(?:[:,\-]\s*)?{re.escape(skill)}(?!\w)", suffix)
        for skill in _KNOWN_SKILLS
    ):
        return False
    clause_start = max(text.rfind(";", 0, marker_start), text.rfind(",", 0, marker_start)) + 1
    return not _has_known_skill_between(text, clause_start, marker_start)


def _marker_is_broad_list_introducer(text: str, event: tuple[int, int, str]) -> bool:
    """Return whether a marker can legitimately govern a whole skill list."""
    return text[event[0]:event[1]].casefold() in {
        "experience with",
        "proficient",
        "proficiency",
    }


def _has_known_skill_between(text: str, start: int, end: int) -> bool:
    """Return whether another known skill separates two markers."""
    between = text[start:end]
    return any(
        re.search(rf"(?<!\w){re.escape(skill)}(?!\w)", between)
        for skill in _KNOWN_SKILLS
    )


def _skill_requirement_status(line: str, skill: str) -> str | None:
    """Classify one skill from its nearest explicit requirement marker.

    A generic ``experience with`` prefix must not turn a trailing
    ``preferred but not required`` list into hard requirements. Conversely,
    a marker that directly introduces a later skill must not override a
    preceding marker for this skill.
    """
    normalized = _normalise_text(line)
    match = re.search(rf"(?<!\w){re.escape(skill)}(?!\w)", normalized)
    if match is None:
        return None
    events = _requirement_marker_events(normalized)
    following = [event for event in events if event[0] >= match.end()]
    preceding = [event for event in events if event[1] <= match.start()]
    if following and not _marker_introduces_skill(normalized, following[0]):
        prior = preceding[-1] if preceding else None
        if (
            prior is not None
            and prior[2] != following[0][2]
            and _marker_introduces_skill(normalized, prior)
            and not _marker_is_broad_list_introducer(normalized, prior)
            and _has_known_skill_between(normalized, match.end(), following[0][0])
        ):
            return prior[2]
        return following[0][2]
    return preceding[-1][2] if preceding else None


def _requirement_sentences(description: str) -> list[tuple[str, str | None]]:
    """Keep section intent when a posting puts optional skills in bullet lists."""
    section: str | None = None
    sentences: list[tuple[str, str | None]] = []
    for line in description.splitlines():
        heading = _normalise_text(line).strip(" #:*-")
        if re.fullmatch(
            r"(?:preferred|optional)(?: qualifications| requirements| skills)?|"
            r"(?:good|nice)[- ]to[- ]have(?: skills)?|bonus(?: points)?|"
            r"advantageous(?:,? but not required)?", heading
        ):
            section = "preferred"
            continue
        if heading in {
            "requirements", "qualifications", "required skills", "minimum qualifications",
            "what you'll bring", "what you’ll bring", "what you bring",
            "what we're looking for", "what we’re looking for", "what we are looking for",
            "who you are", "who should apply", "what you need",
        }:
            section = "required"
            continue
        if heading in {
            "responsibilities", "about the role", "about your role", "benefits", "apply now",
            "what you'll do", "what you’ll do", "what you will do", "about us",
            "what we offer", "what you will gain", "application instructions",
        }:
            section = None
            continue
        sentences.extend(
            (part.strip(), section)
            for part in re.split(r"[.]+", line) if part.strip()
        )
    return sentences


def _section_skill_status(line: str, skill: str, section: str | None) -> str | None:
    status = _skill_requirement_status(line, skill)
    # Generic "experience with"/"proficiency" wording under Good to have is
    # still optional. An explicit must/required clause retains its hard gate.
    if section == "preferred" and not re.search(
        r"(?i)\b(?:must|required|mandatory)\b", line
    ):
        return "preferred"
    return status or section


def _explicit_skill_options(
    line: str, known_skills: set[str]
) -> tuple[list[list[str]], set[str]]:
    """Recognize explicit alternatives/examples, without inferring equivalence."""
    text = _normalise_text(line)
    skill_pattern = "(?:" + "|".join(
        re.escape(skill) for skill in sorted(known_skills, key=lambda value: (-len(value), value))
    ) + ")"
    groups: list[list[str]] = []
    # Restrict this to two named skills joined by explicit OR. Conjunctions,
    # slash notation and unknown skill names retain conservative treatment.
    pattern = (
        rf"(?<!\w)({skill_pattern})(?!\w)"
        rf"(?:\s*,?\s+or\s+({skill_pattern})(?!\w)|"
        rf"\s*,\s*({skill_pattern})(?!\w)\s*,?\s+or both\b)"
    )
    for match in re.finditer(pattern, text):
        # Do not silently carve a binary choice out of a longer OR list.
        # Longer lists remain conservative until parsed as one complete group.
        if re.search(
            rf"(?<!\w){skill_pattern}(?!\w)\s*(?:,|or)\s*$", text[:match.start()]
        ) or re.match(rf"\s*(?:,|or)\s*{skill_pattern}(?!\w)", text[match.end():]):
            continue
        group = sorted({match.group(1), match.group(2) or match.group(3)})
        outside = text[:match.start()] + text[match.end():]
        if not any(re.search(rf"(?<!\w){re.escape(skill)}(?!\w)", outside) for skill in group):
            groups.append(group)
    examples: set[str] = set()
    for match in re.finditer(r"\btools? such as (.*?)\bor similar(?: tools)?\b", text):
        examples.update(
            skill for skill in known_skills
            if re.search(rf"(?<!\w){re.escape(skill)}(?!\w)", match.group(1))
            and not re.search(
                rf"(?<!\w){re.escape(skill)}(?!\w)", text[:match.start()] + text[match.end():]
            )
        )
    return groups, examples


_DELIVERABLE_TERMS = {
    "dashboard",
    "reporting",
    "report",
    "analysis",
    "analytics",
    "data pipeline",
    "automation",
    "workflow",
    "prototype",
    "roadmap",
    "requirements",
    "implementation",
    "deployment",
    "integration",
    "presentation",
    "client",
    "stakeholder",
    "mapping",
    "spatial analysis",
    "forecast",
    "model",
}

# Resume reuse needs a finer job-nature taxonomy than discovery.  Discovery
# intentionally uses conservative title terms, while this layer must recognise
# validated material for adjacent technical work already present in the local
# application history.  Rules are ordered from specific to general.
_RESUME_SUBTYPE_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ai_research", "ai_implementation", ("ai research", "machine learning research", "model evaluation")),
    ("geospatial", "spatial", ("geospatial", "spatial analytics", "gis analyst")),
    ("data_analytics", "data_bi_decision", ("market data", "data services")),
    (
        "ai_solutions",
        "ai_implementation",
        ("machine learning engineer", "artificial intelligence engineer", "ai engineer",
         "ai agent engineer", "ai developer", "ai application", "ai engineering"),
    ),
    (
        "workflow_automation",
        "ai_implementation",
        ("operations automation", "automation engineer", "process automation", "workflow automation"),
    ),
    (
        "data_analytics",
        "data_bi_decision",
        ("data analysis", "data analyst", "analytics intern", "decision analysis"),
    ),
    (
        "map_data_operations",
        "spatial",
        ("map annotation", "map data", "geospatial annotation", "spatial annotation"),
    ),
    (
        "autonomous_vehicle_integration",
        "spatial",
        ("autonomous vehicle integration", "vehicle integration", "autonomous driving integration"),
    ),
    (
        "spatial_simulation",
        "spatial",
        ("map simulation", "spatial simulation", "traffic simulation", "mobility simulation"),
    ),
    (
        "software_quality_validation",
        "technical_engineering",
        ("qa engineer", "quality assurance", "software validation", "test engineer"),
    ),
    (
        "software_engineering",
        "technical_engineering",
        ("software engineer", "backend engineer", "full stack engineer", "site reliability engineer"),
    ),
    (
        "technology_analysis",
        "general_product_consulting",
        ("technology analyst", "business technology", "technology consulting"),
    ),
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalise_text(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _contains_phrase(text: str, phrase: str) -> bool:
    phrase = _normalise_text(phrase)
    if not phrase:
        return False
    if re.fullmatch(r"[a-z0-9+#.]+", phrase):
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text))
    return phrase in text


def _content_digest(text: str) -> str:
    canonical = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip()
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _taxonomy_config() -> dict:
    path = CONFIG_DIR / "linkedin_searches.yaml"
    if not path.is_file():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _profile_skills(profile: Mapping[str, object]) -> set[str]:
    result = set(_KNOWN_SKILLS)
    boundary = profile.get("skills_boundary", {})
    if isinstance(boundary, Mapping):
        for items in boundary.values():
            if isinstance(items, Iterable) and not isinstance(items, (str, bytes)):
                result.update(_normalise_text(item) for item in items if _normalise_text(item))
    return result


def _confirmed_experience_skill_facts(
    profile: Mapping[str, object],
) -> dict[str, dict[str, object]]:
    """Return narrowly admitted user-confirmed positive skill experience facts.

    These facts may prove that a hard gap is supported somewhere in the local
    factual record, allowing a new tailored variant to be attempted. They do
    not change the skill coverage of any existing resume artifact.
    """
    facts = profile.get("application_facts", [])
    if not isinstance(facts, list):
        return {}
    known_skills = _profile_skills(profile)
    admitted: dict[str, dict[str, object]] = {}
    for fact in facts:
        if not isinstance(fact, Mapping):
            continue
        if _normalise_text(fact.get("source")) != "user_confirmed":
            continue
        key = str(fact.get("key") or "").strip().casefold()
        match = re.fullmatch(r"([a-z0-9]+(?:_[a-z0-9]+)*)_experience_years", key)
        if match is None:
            continue
        skill = _normalise_text(match.group(1).replace("_", " "))
        value = fact.get("value")
        if (
            skill not in known_skills
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value <= 0
        ):
            continue
        admitted[skill] = {
            "skill": skill,
            "fact_key": key,
            "value": value,
            "source": "user_confirmed",
            "confirmed_at": str(fact.get("confirmed_at") or "").strip(),
        }
    return admitted


def _unsupported_required_skills(
    conn: sqlite3.Connection,
    profile: Mapping[str, object],
    hard_gaps: Iterable[str],
) -> tuple[list[str], list[dict[str, object]]]:
    """Classify hard gaps against base resumes and narrow confirmed facts."""
    all_source_text = "\n".join(
        _normalise_text(read_resume_source(Path(str(row["text_path"]))))
        for row in conn.execute(
            "SELECT text_path FROM resume_artifacts WHERE active=1 AND kind='base'"
        ).fetchall()
        if Path(str(row["text_path"])).is_file()
    )
    confirmed_facts = _confirmed_experience_skill_facts(profile)
    unsupported: list[str] = []
    fact_support: list[dict[str, object]] = []
    for gap in hard_gaps:
        if gap.startswith("one of: "):
            options = gap.removeprefix("one of: ").split(" | ")
            if any(re.search(rf"(?<!\w){re.escape(option)}(?!\w)", all_source_text) for option in options):
                continue
            fact = next((confirmed_facts[option] for option in options if option in confirmed_facts), None)
            if fact is not None:
                fact_support.append(fact)
                continue
        if _evidence_contains(all_source_text, gap):
            continue
        fact = confirmed_facts.get(_normalise_text(gap))
        if fact is None:
            unsupported.append(gap)
        else:
            fact_support.append(fact)
    return unsupported, fact_support


def _configured_source_paths_for_track(
    profile: Mapping[str, object], track: str | None
) -> set[str]:
    """Return explicitly configured source resumes for one canonical track."""
    if not track:
        return set()
    tailoring = profile.get("tailoring", {})
    if not isinstance(tailoring, Mapping):
        return set()
    variants = tailoring.get("resume_variants", [])
    if not isinstance(variants, list):
        return set()
    paths: set[str] = set()
    for variant in variants:
        if not isinstance(variant, Mapping):
            continue
        configured_track = _PROFILE_TRACK_ALIASES.get(
            _normalise_text(variant.get("track"))
        )
        configured_path = str(variant.get("path") or "").strip()
        if configured_track == track and configured_path:
            paths.add(str(Path(configured_path).resolve()).casefold())
    return paths


def _content_terms(title: str, description: str, *, limit: int = 28) -> list[str]:
    """Extract explainable JD terms for content-level resume comparison."""
    normalized = _normalise_text(f"{title}\n{description}")
    phrases = {
        phrase for phrase in (*_KNOWN_SKILLS, *_DELIVERABLE_TERMS)
        if " " in phrase and _contains_phrase(normalized, phrase)
    }
    counts: dict[str, int] = {}
    title_words = set(re.findall(r"[a-z][a-z0-9+#]{2,}", _normalise_text(title)))
    for token in re.findall(r"[a-z][a-z0-9+#]{2,}", normalized):
        if token in _CONTENT_STOPWORDS or token.isdigit():
            continue
        counts[token] = counts.get(token, 0) + 1
    ranked = sorted(
        counts,
        key=lambda token: (-(counts[token] + (2 if token in title_words else 0)), token),
    )
    return sorted(phrases) + [term for term in ranked if term not in phrases][:limit]


def _requested_resume_pages(description: str) -> int | None:
    """Only explicit resume/CV length language constrains artifact selection."""
    match = re.search(
        r"\b(one|two|1|2)[ -]page\s+(?:resume|cv)\b|"
        r"\b(?:resume|cv)\s+(?:must be|of)\s+(one|two|1|2)\s+pages?\b",
        description, re.IGNORECASE,
    )
    if not match:
        return None
    return {"one": 1, "two": 2, "1": 1, "2": 2}[(match[1] or match[2]).lower()]


def _maximum_resume_pages(description: str) -> int | None:
    match = re.search(
        r"\b(?:resume|cv)\s*:?\s*(?:(?:must be|should be|should|must|of)\s+)?"
        r"(?:limited to|limit is|no more than|at most|not exceed|maximum(?: of)?)\s+(one|two|1|2)\s+pages?\b",
        description, re.IGNORECASE,
    )
    return {"one": 1, "two": 2, "1": 1, "2": 2}[match[1].lower()] if match else None


_EVIDENCE_ALIASES = {
    "machine learning": ("ml", "machine-learning"),
    "llm": ("llms", "large language model", "large language models"),
    "rest": ("restful", "rest api", "rest apis"),
    "rag": ("retrieval-augmented generation", "retrieval augmented generation"),
    "javascript": ("js",), "typescript": ("ts",),
}


def _evidence_contains(text: str, term: str, *, inflections: bool = False) -> bool:
    if _contains_phrase(text, term) or any(
        _contains_phrase(text, alias) for alias in _EVIDENCE_ALIASES.get(term, ())
    ):
        return True
    # Limited noun plurals, never substring matches (e.g. R in research).
    if inflections and len(term) > 3:
        forms = [term + "s", term.removesuffix("s")]
        return any(_contains_phrase(text, form) for form in forms)
    return False


_CURATED_FAMILY_TRACKS = {
    "AI 工程与自动化": {"ai_implementation", "technical_engineering"},
    "AI 研究与评估": {"ai_implementation"},
    "数据分析与 BI": {"data_bi_decision"},
    "产品与业务运营": {"general_product_consulting"},
    "咨询与业务交付": {"general_product_consulting", "spatial"},
    "软件测试与质量": {"technical_engineering"},
}


def _artifact_metadata(artifact: Mapping[str, object]) -> dict:
    try:
        value = json.loads(str(artifact.get("metadata_json") or "{}"))
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _score_binding_error(job: Mapping[str, object], profile: Mapping[str, object] | None = None) -> str | None:
    """Legacy scores remain readable; new evidence must match its actual inputs."""
    raw = job.get("score_evidence_json")
    if not raw:
        return None
    try:
        evidence = json.loads(str(raw))
    except (ValueError, TypeError):
        return "Score evidence is malformed; rescore before routing."
    if not isinstance(evidence, dict) or "input_binding" not in evidence:
        return None
    binding = evidence["input_binding"]
    if not isinstance(binding, dict) or binding.get("job_fingerprint") != compute_job_fingerprint(dict(job)):
        return "The JD changed since scoring; rescore before routing."
    source = Path(str(binding.get("source_path") or ""))
    if not source.is_file() or text_digest(read_resume_source(source)) != binding.get("source_text_digest"):
        return "The scored resume source changed or is unavailable; rescore before routing."
    from applypilot.scoring.scorer import PROMPT_REVISION, _confirmed_scoring_facts

    if binding.get("prompt_revision") != PROMPT_REVISION:
        return "The scoring policy changed; rescore before routing."
    if binding.get("profile_facts_digest") and binding["profile_facts_digest"] != text_digest(json.dumps(
        _confirmed_scoring_facts(dict(profile or {})), sort_keys=True, ensure_ascii=False,
    )):
        return "The confirmed scoring facts changed; rescore before routing."
    if binding.get("context_mode") == "registered_evidence_sources":
        from applypilot.scoring.cover_letter import load_evidence_sources

        sources = load_evidence_sources(dict(profile or {}), source, read_resume_source(source))
        if text_digest("\n\n".join(item["text"] for item in sources)) != binding.get("context_text_digest"):
            return "The supplemental scoring evidence changed or is unavailable; rescore before routing."
    return None


def ensure_resume_library_schema(conn: sqlite3.Connection) -> None:
    """Create the additive resume-library schema without changing job rows."""
    execute_transactional_script(
        conn,
        """
        CREATE TABLE IF NOT EXISTS resume_artifacts (
            artifact_id             TEXT PRIMARY KEY,
            content_sha256          TEXT NOT NULL UNIQUE,
            kind                    TEXT NOT NULL,
            track                   TEXT,
            text_path               TEXT NOT NULL,
            pdf_path                TEXT,
            source_resume_path      TEXT,
            pdf_sha256              TEXT,
            pdf_size                INTEGER,
            validation_status       TEXT NOT NULL,
            validation_report_path  TEXT,
            validated_at            TEXT,
            active                  INTEGER NOT NULL DEFAULT 1,
            metadata_json           TEXT NOT NULL DEFAULT '{}',
            created_at              TEXT NOT NULL,
            updated_at              TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS resume_coverage_cells (
            artifact_id             TEXT NOT NULL,
            taxonomy_version        TEXT NOT NULL,
            track                   TEXT NOT NULL,
            subtype                 TEXT NOT NULL,
            evidence_job_url        TEXT NOT NULL,
            evidence_job_fingerprint TEXT NOT NULL,
            validated_at            TEXT NOT NULL,
            PRIMARY KEY (
                artifact_id, taxonomy_version, subtype,
                evidence_job_url, evidence_job_fingerprint
            ),
            FOREIGN KEY (artifact_id) REFERENCES resume_artifacts(artifact_id)
        );

        CREATE TABLE IF NOT EXISTS resume_artifact_aliases (
            artifact_id             TEXT NOT NULL,
            text_path               TEXT NOT NULL,
            pdf_path                TEXT,
            observed_at             TEXT NOT NULL,
            PRIMARY KEY (artifact_id, text_path),
            FOREIGN KEY (artifact_id) REFERENCES resume_artifacts(artifact_id)
        );

        CREATE TABLE IF NOT EXISTS job_resume_profiles (
            job_url                 TEXT NOT NULL,
            job_fingerprint         TEXT NOT NULL,
            taxonomy_version        TEXT NOT NULL,
            track                   TEXT,
            subtype                 TEXT,
            employment_type         TEXT,
            seniority               TEXT,
            required_skills_json    TEXT NOT NULL,
            preferred_skills_json   TEXT NOT NULL,
            deliverables_json       TEXT NOT NULL,
            features_json           TEXT NOT NULL,
            confidence              REAL NOT NULL,
            recorded_at             TEXT NOT NULL,
            PRIMARY KEY (job_url, job_fingerprint, taxonomy_version)
        );

        CREATE TABLE IF NOT EXISTS job_resume_assignments (
            assignment_id           TEXT PRIMARY KEY,
            job_url                 TEXT NOT NULL,
            job_fingerprint         TEXT NOT NULL,
            artifact_id             TEXT,
            decision                TEXT NOT NULL,
            required_coverage       REAL,
            overall_score           REAL,
            runner_up_margin        REAL,
            hard_gaps_json          TEXT NOT NULL,
            components_json         TEXT NOT NULL,
            reason                  TEXT NOT NULL,
            policy_version          TEXT NOT NULL,
            recorded_at             TEXT NOT NULL,
            FOREIGN KEY (artifact_id) REFERENCES resume_artifacts(artifact_id)
        );

        CREATE INDEX IF NOT EXISTS idx_job_resume_assignments_lookup
            ON job_resume_assignments(job_url, job_fingerprint, recorded_at);

        CREATE TABLE IF NOT EXISTS resume_validation_runs (
            validation_id           TEXT PRIMARY KEY,
            artifact_id             TEXT NOT NULL,
            job_url                 TEXT,
            job_fingerprint         TEXT,
            validation_kind         TEXT NOT NULL,
            status                  TEXT NOT NULL,
            evidence_json           TEXT NOT NULL,
            recorded_at             TEXT NOT NULL,
            FOREIGN KEY (artifact_id) REFERENCES resume_artifacts(artifact_id)
        );

        CREATE INDEX IF NOT EXISTS idx_resume_validation_runs_artifact
            ON resume_validation_runs(artifact_id, recorded_at);

        CREATE TABLE IF NOT EXISTS resume_route_outcomes (
            outcome_id              TEXT PRIMARY KEY,
            assignment_id           TEXT NOT NULL,
            resolution              TEXT NOT NULL,
            source_artifact_id      TEXT,
            output_artifact_id      TEXT,
            status                  TEXT NOT NULL,
            evidence_json           TEXT NOT NULL,
            recorded_at             TEXT NOT NULL,
            FOREIGN KEY (assignment_id) REFERENCES job_resume_assignments(assignment_id),
            FOREIGN KEY (source_artifact_id) REFERENCES resume_artifacts(artifact_id),
            FOREIGN KEY (output_artifact_id) REFERENCES resume_artifacts(artifact_id)
        );

        CREATE INDEX IF NOT EXISTS idx_resume_route_outcomes_assignment
            ON resume_route_outcomes(assignment_id, recorded_at);

        CREATE TABLE IF NOT EXISTS resume_artifact_health_checks (
            health_id               TEXT PRIMARY KEY,
            artifact_id             TEXT NOT NULL,
            policy_version          TEXT NOT NULL,
            status                  TEXT NOT NULL,
            reasons_json            TEXT NOT NULL,
            metrics_json            TEXT NOT NULL,
            checked_at              TEXT NOT NULL,
            FOREIGN KEY (artifact_id) REFERENCES resume_artifacts(artifact_id)
        );

        CREATE INDEX IF NOT EXISTS idx_resume_artifact_health_lookup
            ON resume_artifact_health_checks(artifact_id, policy_version, checked_at);
        """,
    )

    ensure_version_schema(conn)


def extract_job_profile(
    job: Mapping[str, object],
    profile: Mapping[str, object] | None = None,
    taxonomy_config: Mapping[str, object] | None = None,
) -> dict:
    """Extract a conservative, explainable fine-grained job fingerprint."""
    title = str(job.get("title") or "").strip()
    description = str(job.get("full_description") or "").strip()
    combined = _normalise_text(f"{title}\n{description}")
    config = dict(taxonomy_config or _taxonomy_config())

    title_matches = classify_job_subtracks(title, config)
    term_scores: dict[str, int] = {}
    tracks = config.get("tracks", {})
    if isinstance(tracks, Mapping):
        for raw_track, subtracks in tracks.items():
            if not isinstance(subtracks, Mapping):
                continue
            for subtype, terms in subtracks.items():
                if not isinstance(terms, list):
                    continue
                score = 0
                title_text = _normalise_text(title)
                for term in terms:
                    phrase = _normalise_text(term)
                    if _contains_phrase(title_text, phrase):
                        score += 5
                    if _contains_phrase(_normalise_text(description), phrase):
                        score += 1
                if score:
                    term_scores[str(subtype)] = score

    # The discovery classifier already proved these phrases against the title
    # with punctuation-normalized matching.  Give them title weight so a weak
    # description-only term cannot displace the title's subtype.
    for title_match in title_matches:
        term_scores[title_match] = max(term_scores.get(title_match, 0), 5)

    ordered = sorted(term_scores, key=lambda item: (-term_scores[item], item))
    subtype = ordered[0] if ordered else (title_matches[0] if title_matches else None)
    raw_track = SUBTRACK_TO_TRACK.get(subtype) if subtype else None
    track = raw_track.value if raw_track is not None else None
    top_score = term_scores.get(subtype or "", 0)
    second_score = term_scores.get(ordered[1], 0) if len(ordered) > 1 else 0
    confidence = 0.0
    if subtype:
        confidence = 0.70 if subtype in title_matches else 0.55
        confidence = min(1.0, confidence + min(top_score, 10) / 50)
        if second_score == top_score and second_score:
            confidence = min(confidence, 0.60)

    title_text = _normalise_text(title)
    matched_rule: tuple[str, str, bool] | None = None
    for rule_subtype, rule_track, phrases in _RESUME_SUBTYPE_RULES:
        if any(_contains_phrase(title_text, phrase) for phrase in phrases):
            matched_rule = (rule_subtype, rule_track, True)
            break
    if matched_rule is None and not subtype:
        description_text = _normalise_text(description)
        for rule_subtype, rule_track, phrases in _RESUME_SUBTYPE_RULES:
            if any(_contains_phrase(description_text, phrase) for phrase in phrases):
                matched_rule = (rule_subtype, rule_track, False)
                break
    if matched_rule is not None:
        rule_subtype, rule_track, title_hit = matched_rule
        # Fine-grained title rules intentionally refine the broader discovery
        # taxonomy.  Description-only rules remain a no-subtype fallback above.
        if title_hit or not subtype:
            subtype = rule_subtype
            track = rule_track
            confidence = 0.85 if title_hit else 0.60
            term_scores[subtype] = max(term_scores.get(subtype, 0), 5 if title_hit else 1)

    lowered_title = title.casefold()
    if re.search(r"\b(?:intern|internship|trainee|co-op|graduate programme)\b", lowered_title) or re.search(
        r"(?im)^\s*(?:full[- ]time\s+)?internship\s*$|"
        r"\b(?:as an?|we are (?:hiring|seeking|looking for) an?)\s+(?:[\w-]+\s+){0,5}intern\b|"
        r"\bthis (?:role|position|opportunity) is an? internship\b", description
    ):
        employment_type = "internship"
    elif re.search(r"\b(?:contract|temporary|freelance)\b", combined):
        employment_type = "contract"
    else:
        employment_type = "full_time_or_unspecified"

    years = [int(match.group(1)) for match in _EXPERIENCE_REQUIREMENT.finditer(description)]
    if _SENIOR_TITLE.search(title) or (years and max(years) >= 4):
        seniority = "senior_or_high_experience"
    elif employment_type == "internship" or re.search(r"\b(?:entry level|junior|graduate)\b", combined):
        seniority = "early_career"
    else:
        seniority = "unspecified"

    known_skills = _profile_skills(profile or {})
    # Narrow fallback for explicit lists of named tools outside the vocabulary.
    # Free-form duties/degree/eligibility clauses remain model-scoring evidence.
    for match in re.finditer(r"(?im)(?:^|[.;])\s*(?:required|mandatory)(?: skills| technologies| tools)?\s*:\s*([^\n.]+)", description):
        items = [item.strip() for item in re.split(r"[,;]|\s+(?:and|or)\s+", match[1])]
        if all(re.fullmatch(r"[A-Z][A-Za-z0-9+#/-]{1,35}(?: [A-Z][A-Za-z0-9+#/-]{1,35}){0,2}", item) for item in items):
            known_skills.update(item.casefold() for item in items)
    required: set[str] = set()
    preferred: set[str] = set()
    mentioned: set[str] = set()
    sentences = _requirement_sentences(description)
    required_groups: list[list[str]] = []
    statuses: dict[str, set[str | None]] = {skill: set() for skill in known_skills}
    for line, section in sentences:
        groups, examples = _explicit_skill_options(line, known_skills)
        active_groups = [
            group for group in groups
            if not examples.intersection(group)
            and all(_section_skill_status(line, skill, section) == "required" for skill in group)
        ]
        for group in active_groups:
            if group not in required_groups:
                required_groups.append(group)
        alternatives = {skill for group in active_groups for skill in group}
        for skill in known_skills:
            if not _contains_phrase(_normalise_text(line), skill):
                continue
            if skill in examples or skill in alternatives:
                continue
            statuses[skill].add(_section_skill_status(line, skill, section))
    for skill in known_skills:
        if not _contains_phrase(combined, skill):
            continue
        mentioned.add(skill)
        if "required" in statuses[skill]:
            required.add(skill)
        elif "preferred" in statuses[skill]:
            preferred.add(skill)

    deliverables = sorted(term for term in _DELIVERABLE_TERMS if _contains_phrase(combined, term))
    fingerprint = compute_job_fingerprint(dict(job))
    features = {
        "complete_description": bool(description),
        "requested_resume_pages": _requested_resume_pages(description),
        "maximum_resume_pages": _maximum_resume_pages(description),
        "mentioned_skills": sorted(mentioned),
        "required_skill_groups": required_groups,
        "title_matches": list(title_matches),
        "subtype_scores": term_scores,
        "max_required_years": max(years) if years else None,
        "location": str(job.get("location") or "").strip(),
        "content_terms": _content_terms(title, description),
    }
    return {
        "job_url": str(job.get("url") or ""),
        "job_fingerprint": fingerprint,
        "taxonomy_version": TAXONOMY_VERSION,
        "track": track,
        "subtype": subtype,
        "employment_type": employment_type,
        "seniority": seniority,
        "required_skills": sorted(required),
        "preferred_skills": sorted(preferred),
        "deliverables": deliverables,
        "features": features,
        "confidence": confidence,
    }


def persist_job_profile(conn: sqlite3.Connection, job_profile: Mapping[str, object]) -> None:
    ensure_resume_library_schema(conn)
    conn.execute(
        """
        INSERT OR IGNORE INTO job_resume_profiles (
            job_url, job_fingerprint, taxonomy_version, track, subtype,
            employment_type, seniority, required_skills_json,
            preferred_skills_json, deliverables_json, features_json,
            confidence, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_profile["job_url"],
            job_profile["job_fingerprint"],
            job_profile["taxonomy_version"],
            job_profile.get("track"),
            job_profile.get("subtype"),
            job_profile.get("employment_type"),
            job_profile.get("seniority"),
            _json(job_profile.get("required_skills", [])),
            _json(job_profile.get("preferred_skills", [])),
            _json(job_profile.get("deliverables", [])),
            _json(job_profile.get("features", {})),
            float(job_profile.get("confidence") or 0),
            _now(),
        ),
    )


def _register_artifact(
    conn: sqlite3.Connection,
    *,
    text_path: Path,
    kind: str,
    track: str | None,
    source_resume_path: str | None,
    validation_status: str,
    report_path: str | None = None,
    validated_at: str | None = None,
    metadata: Mapping[str, object] | None = None,
    promote: bool = True,
) -> tuple[str, bool]:
    text_path = text_path.expanduser().resolve()
    original_text_path = text_path
    original_pdf_path = text_path.with_suffix(".pdf")
    text = read_resume_source(text_path)
    digest = _content_digest(text)
    artifact_id = f"resume:{digest[:24]}"
    existing = conn.execute(
        "SELECT artifact_id, validation_status, metadata_json FROM resume_artifacts WHERE content_sha256=?",
        (digest,),
    ).fetchone()
    if existing and existing["validation_status"] in {"retired_profile_correction", "superseded_editorial"}:
        # Historical job projections may still reference these immutable bytes.
        # Sync must preserve the correction tombstone and its supersession data.
        return str(existing["artifact_id"]), False
    pdf_path = original_pdf_path
    render = None
    if (
        kind == "tailored" and validation_status == "machine_validated"
        and text_path.suffix.casefold() == ".txt" and original_pdf_path.is_file()
    ):
        # Separate a text identity from its immutable PDF editions. Never reuse
        # an older PDF merely because the normalized text is identical.
        render = freeze_render(original_text_path, text, artifact_id, report_path)
        text_path = Path(render["text_path"])
        pdf_path = Path(render["pdf_path"])
        report_path = render["validation_report_path"]
    pdf_sha256: str | None = None
    pdf_size: int | None = None
    if pdf_path.is_file():
        pdf_sha256, pdf_size = compute_file_binding(pdf_path)
    else:
        pdf_path = None
    now = _now()
    metadata = {**(json.loads(existing["metadata_json"] or "{}") if existing else {}), **dict(metadata or {})}
    source_file = Path(str(source_resume_path or ""))
    if promote and source_file.is_file():
        metadata["source_evidence_sha256"] = hashlib.sha256(source_file.read_bytes()).hexdigest()
    created = existing is None
    if created:
        conn.execute(
            """
            INSERT INTO resume_artifacts (
                artifact_id, content_sha256, kind, track, text_path, pdf_path,
                source_resume_path, pdf_sha256, pdf_size, validation_status,
                validation_report_path, validated_at, active, metadata_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                artifact_id,
                digest,
                kind,
                track,
                str(text_path),
                str(pdf_path) if pdf_path else None,
                source_resume_path,
                pdf_sha256,
                pdf_size,
                validation_status,
                report_path,
                validated_at,
                _json(metadata or {}),
                now,
                now,
            ),
        )
    elif promote and validation_status == "machine_validated" and pdf_path is not None:
        artifact_id = str(existing["artifact_id"])
        conn.execute(
            """
            UPDATE resume_artifacts
            SET kind='tailored', track=COALESCE(?, track), text_path=?, pdf_path=?,
                source_resume_path=COALESCE(?, source_resume_path), pdf_sha256=?,
                pdf_size=?, validation_status='machine_validated',
                validation_report_path=COALESCE(?, validation_report_path),
                validated_at=COALESCE(?, validated_at), active=1,
                metadata_json=?, updated_at=?
            WHERE artifact_id=?
            """,
            (
                track,
                str(text_path),
                str(pdf_path),
                source_resume_path,
                pdf_sha256,
                pdf_size,
                report_path,
                validated_at,
                _json(metadata or {}),
                now,
                artifact_id,
            ),
        )
    else:
        artifact_id = str(existing["artifact_id"])
    if render is not None:
        register_render(conn, render)
    conn.execute(
        """
        INSERT OR IGNORE INTO resume_artifact_aliases (
            artifact_id, text_path, pdf_path, observed_at
        ) VALUES (?, ?, ?, ?)
        """,
        (
            artifact_id,
            str(original_text_path),
            str(original_pdf_path) if original_pdf_path.is_file() else None,
            now,
        ),
    )
    return artifact_id, created


def _record_assignment(
    conn: sqlite3.Connection,
    *,
    job_profile: Mapping[str, object],
    artifact_id: str | None,
    decision: str,
    required_coverage: float | None,
    overall_score: float | None,
    margin: float | None,
    hard_gaps: list[str],
    components: Mapping[str, object],
    reason: str,
) -> str:
    existing = conn.execute(
        """
        SELECT assignment_id FROM job_resume_assignments
        WHERE job_url=? AND job_fingerprint=? AND COALESCE(artifact_id, '')=COALESCE(?, '')
          AND decision=? AND policy_version=? AND components_json=?
        ORDER BY recorded_at DESC LIMIT 1
        """,
        (
            job_profile["job_url"],
            job_profile["job_fingerprint"],
            artifact_id,
            decision,
            POLICY_VERSION,
            _json(components),
        ),
    ).fetchone()
    if existing:
        return str(existing["assignment_id"])
    assignment_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO job_resume_assignments (
            assignment_id, job_url, job_fingerprint, artifact_id, decision,
            required_coverage, overall_score, runner_up_margin, hard_gaps_json,
            components_json, reason, policy_version, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            assignment_id,
            job_profile["job_url"],
            job_profile["job_fingerprint"],
            artifact_id,
            decision,
            required_coverage,
            overall_score,
            margin,
            _json(hard_gaps),
            _json(components),
            reason,
            POLICY_VERSION,
            _now(),
        ),
    )
    return assignment_id


def _record_validation(
    conn: sqlite3.Connection,
    *,
    artifact_id: str,
    validation_kind: str,
    status: str,
    job_profile: Mapping[str, object] | None,
    evidence: Mapping[str, object],
) -> str:
    validation_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO resume_validation_runs (
            validation_id, artifact_id, job_url, job_fingerprint,
            validation_kind, status, evidence_json, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            validation_id,
            artifact_id,
            job_profile.get("job_url") if job_profile else None,
            job_profile.get("job_fingerprint") if job_profile else None,
            validation_kind,
            status,
            _json(evidence),
            _now(),
        ),
    )
    return validation_id


def record_route_outcome(
    conn: sqlite3.Connection,
    *,
    assignment_id: str,
    resolution: str,
    status: str,
    source_artifact_id: str | None = None,
    output_artifact_id: str | None = None,
    evidence: Mapping[str, object] | None = None,
) -> str:
    """Append the observed result of a four-tier routing decision."""
    ensure_resume_library_schema(conn)
    if resolution not in RESUME_RESOLUTIONS:
        raise ValueError(f"Unsupported resume resolution: {resolution}")
    outcome_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO resume_route_outcomes (
            outcome_id, assignment_id, resolution, source_artifact_id,
            output_artifact_id, status, evidence_json, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            outcome_id,
            assignment_id,
            resolution,
            source_artifact_id,
            output_artifact_id,
            status,
            _json(evidence or {}),
            _now(),
        ),
    )
    return outcome_id


def record_content_revalidation(
    conn: sqlite3.Connection,
    *,
    text: str,
    status: str,
    job: Mapping[str, object],
    evidence: Mapping[str, object] | None = None,
) -> bool:
    """Synchronize a job-specific verdict to the matching shared artifact.

    A failed verdict revokes reuse for identical normalized text bytes. A
    successful verdict may restore reuse only while the content-addressed
    artifact's own PDF binding is still current.
    """
    ensure_resume_library_schema(conn)
    artifact_row = conn.execute(
        "SELECT * FROM resume_artifacts WHERE content_sha256=?",
        (_content_digest(text),),
    ).fetchone()
    if artifact_row is None:
        return False

    artifact = dict(artifact_row)
    if artifact["validation_status"] in {"retired_profile_correction", "superseded_editorial"}:
        return False
    now = _now()
    effective_status = status
    active = 0
    if status == "machine_validated" and _artifact_is_current(artifact):
        active = 1
    elif status == "machine_validated":
        effective_status = "failed_artifact_binding"

    conn.execute(
        "UPDATE resume_artifacts SET validation_status=?, active=?, "
        "validated_at=CASE WHEN ?=1 THEN ? ELSE validated_at END, updated_at=? "
        "WHERE artifact_id=?",
        (
            effective_status,
            active,
            active,
            now,
            now,
            artifact["artifact_id"],
        ),
    )
    job_url = str(job.get("url") or "").strip()
    validation_evidence = {
        "requested_status": status,
        **dict(evidence or {}),
    }
    if (
        effective_status == "machine_validated"
        and validation_evidence.get("judge_review_mode")
        == "independent_factual_and_quality_cross_review"
    ):
        validation_evidence["quality_policy_version"] = POLICY_VERSION
    _record_validation(
        conn,
        artifact_id=str(artifact["artifact_id"]),
        validation_kind="job_specific_revalidation",
        status=effective_status,
        job_profile={
            "job_url": job_url,
            "job_fingerprint": compute_job_fingerprint(dict(job)),
        },
        evidence=validation_evidence,
    )
    return active == 1


def _add_coverage_cell(
    conn: sqlite3.Connection,
    artifact_id: str,
    job_profile: Mapping[str, object],
    validated_at: str | None,
) -> bool:
    if not job_profile.get("track") or not job_profile.get("subtype"):
        return False
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO resume_coverage_cells (
            artifact_id, taxonomy_version, track, subtype, evidence_job_url,
            evidence_job_fingerprint, validated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            artifact_id,
            TAXONOMY_VERSION,
            job_profile["track"],
            job_profile["subtype"],
            job_profile["job_url"],
            job_profile["job_fingerprint"],
            validated_at or _now(),
        ),
    )
    tracks = [
        str(row["track"])
        for row in conn.execute(
            "SELECT DISTINCT track FROM resume_coverage_cells "
            "WHERE artifact_id=? AND taxonomy_version=? ORDER BY track",
            (artifact_id, TAXONOMY_VERSION),
        ).fetchall()
    ]
    if tracks:
        conn.execute(
            "UPDATE resume_artifacts SET track=?, updated_at=? WHERE artifact_id=?",
            (tracks[0] if len(tracks) == 1 else "multi_track", _now(), artifact_id),
        )
    return cursor.rowcount == 1


def register_tailored_artifact(
    conn: sqlite3.Connection,
    *,
    job: Mapping[str, object],
    text_path: str | Path,
    source_resume_path: str | None,
    report_path: str | None,
    validation_kind: str = "generated_strict_validation",
    assignment_decision: str = "create_variant",
    profile: Mapping[str, object] | None = None,
) -> dict:
    """Register one successful tailored resume and its fine-grained coverage."""
    ensure_resume_library_schema(conn)
    job_profile = extract_job_profile(job, profile)
    persist_job_profile(conn, job_profile)
    path = Path(text_path)
    evidence_metadata = {}
    if report_path and Path(report_path).is_file():
        try:
            saved_report = json.loads(Path(report_path).read_text(encoding="utf-8"))
            generation_path = Path(str(saved_report.get("generation_record") or ""))
            supplemental_path = generation_path.with_name("supplemental.txt") if generation_path.name else None
            if supplemental_path is not None and supplemental_path.is_file():
                evidence_metadata["supplemental_evidence_path"] = str(supplemental_path)
        except (OSError, ValueError):
            pass
    artifact_id, created = _register_artifact(
        conn,
        text_path=path,
        kind="tailored",
        track=str(job_profile.get("track") or "") or None,
        source_resume_path=source_resume_path,
        validation_status="machine_validated",
        report_path=report_path,
        validated_at=str(job.get("tailored_at") or _now()),
        promote=validation_kind != "historical_machine_validation",
        metadata={"registered_from_job": job_profile["job_url"],
                  **evidence_metadata,
                  **({"fact_snapshot": profile_fact_snapshot(profile or {})}
                     if validation_kind != "historical_machine_validation" else {})},
    )
    stored_status = conn.execute(
        "SELECT validation_status FROM resume_artifacts WHERE artifact_id=?", (artifact_id,)
    ).fetchone()["validation_status"]
    if stored_status in {"retired_profile_correction", "superseded_editorial"}:
        raise ValueError("Resume artifact was retired after a profile correction; use its corrected successor")
    coverage_added = _add_coverage_cell(conn, artifact_id, job_profile, str(job.get("tailored_at") or ""))
    if created or coverage_added or validation_kind != "historical_machine_validation":
        artifact = conn.execute(
            "SELECT pdf_sha256, pdf_size, validation_report_path FROM resume_artifacts WHERE artifact_id=?",
            (artifact_id,),
        ).fetchone()
        # A historical import may observe an older render of current content.
        # Bind its validation to the observed PDF, not the convenience pointer.
        observed = dict(artifact) if artifact else {}
        if path.suffix.casefold() == ".txt" and path.with_suffix(".pdf").is_file():
            observed = freeze_render(path, read_resume_source(path), artifact_id, report_path)
        _record_validation(
            conn,
            artifact_id=artifact_id,
            validation_kind=validation_kind,
            status="machine_validated",
            job_profile=job_profile,
            evidence={
                "report_path": observed.get("validation_report_path", report_path),
                "pdf_sha256": observed.get("pdf_sha256"),
                "pdf_size": observed.get("pdf_size"),
                "quality_policy_version": (
                    POLICY_VERSION
                    if validation_kind in {"generated_strict_validation", "job_specific_revalidation"}
                    else None
                ),
            },
        )
    _record_assignment(
        conn,
        job_profile=job_profile,
        artifact_id=artifact_id,
        decision=assignment_decision,
        required_coverage=1.0,
        overall_score=1.0,
        margin=None,
        hard_gaps=[],
        components={"registration": validation_kind},
        reason="Machine-validated resume registered for this coverage cell.",
    )
    return {
        "artifact_id": artifact_id,
        "created": created,
        "coverage_added": coverage_added,
        "job_profile": job_profile,
    }


def sync_resume_library(
    conn: sqlite3.Connection,
    profile: Mapping[str, object],
    tailored_dir: Path | None = None,
) -> dict:
    """Idempotently import configured sources and validated generated resumes."""
    ensure_resume_library_schema(conn)
    stats = {"base_sources": 0, "validated_jobs": 0, "artifacts": 0, "coverage_cells": 0, "skipped": 0}
    variants = profile.get("tailoring", {})
    variants = variants.get("resume_variants", []) if isinstance(variants, Mapping) else []
    for variant in variants if isinstance(variants, list) else []:
        if not isinstance(variant, Mapping) or not variant.get("path"):
            continue
        path = Path(str(variant["path"])).expanduser().resolve()
        if not path.is_file():
            stats["skipped"] += 1
            continue
        track = _PROFILE_TRACK_ALIASES.get(str(variant.get("track") or ""))
        _, created = _register_artifact(
            conn,
            text_path=path,
            kind="base",
            track=track,
            source_resume_path=str(path),
            validation_status="source_only",
            metadata={"configured_track": variant.get("track"), "keywords": variant.get("keywords", [])},
        )
        stats["base_sources"] += 1
        stats["artifacts"] += int(created)

    cursor = conn.execute(
        """
        SELECT * FROM jobs
        WHERE tailor_status='machine_validated'
          AND tailored_resume_path IS NOT NULL
          AND TRIM(tailored_resume_path) != ''
        """
    )
    columns = [item[0] for item in cursor.description or ()]
    rows = cursor.fetchall()
    allowed_root = (tailored_dir or TAILORED_DIR).expanduser().resolve()
    for row in rows:
        job = dict(row) if isinstance(row, sqlite3.Row) else dict(zip(columns, row, strict=True))
        path = Path(str(job["tailored_resume_path"])).expanduser().resolve()
        try:
            path.relative_to(allowed_root)
        except ValueError:
            # Explicitly registered historical paths outside the default output
            # root remain valid; the containment check is only used to avoid
            # accidentally importing unrelated files discovered by scanning.
            pass
        if not path.is_file() or not path.with_suffix(".pdf").is_file():
            stats["skipped"] += 1
            continue
        try:
            result = register_tailored_artifact(
                conn,
                job=job,
                text_path=path,
                source_resume_path=str(job.get("tailor_source_resume_path") or "") or None,
                report_path=str(job.get("tailor_report_path") or "") or None,
                validation_kind="historical_machine_validation",
                assignment_decision="historical_validated",
                profile=profile,
            )
        except (OSError, ValueError):
            stats["skipped"] += 1
            continue
        stats["validated_jobs"] += 1
        stats["artifacts"] += int(result["created"])
        stats["coverage_cells"] += int(result["coverage_added"])
    conn.commit()
    return stats


def _artifact_is_current(artifact: Mapping[str, object]) -> bool:
    text_path = Path(str(artifact.get("text_path") or ""))
    pdf_path = Path(str(artifact.get("pdf_path") or ""))
    if not text_path.is_file() or not pdf_path.is_file():
        return False
    try:
        digest, size = compute_file_binding(pdf_path)
    except OSError:
        return False
    if digest != artifact.get("pdf_sha256") or size != artifact.get("pdf_size"):
        return False
    expected_text = artifact.get("content_sha256")
    if expected_text:
        try:
            return text_digest(read_resume_source(text_path)) == expected_text
        except (OSError, RuntimeError, ValueError):
            return False
    return True


_HEALTH_QUARANTINE_PREFIXES = (
    "Duplicate project entry:",
    "Experience entry '",
    "Project entry '",
    "Missing required section:",
    "Every retained experience entry",
    "Every retained project entry",
    "The leading experience entry",
    "The leading project entry",
    "Experience entries must remain",
    "Projects entries must remain",
    "The most recent experience entry",
    "The most recent project entry",
    "Experience bullet allocation",
    "Projects bullet allocation",
    "The oldest retained experience entry",
    "The oldest retained projects entry",
    "No experience entry may exceed",
    "No projects entry may exceed",
    "Resume header must",
    "Repeated or near-duplicate resume bullets",
    "Project resume is under-evidenced",
    "No-project resume is under-evidenced",
    "Education '",
)


def assess_resume_artifact_health(
    artifact: Mapping[str, object], profile: Mapping[str, object]
) -> dict[str, object]:
    """Identify only high-confidence defects that make an artifact unsafe to route.

    Historical bytes remain immutable. A quarantine result removes the artifact
    from candidate search under the current health policy, but never deletes it
    or breaks its application-history provenance.
    """
    text_path = Path(str(artifact.get("text_path") or ""))
    reasons: list[str] = []
    metrics: dict[str, object] = {"text_path": str(text_path), "input_digest": health_input_digest(artifact, profile)}
    if not text_path.is_file():
        reasons.append("Artifact text file is missing.")
        return {"status": "quarantined", "reasons": reasons, "metrics": metrics}
    try:
        text = read_resume_source(text_path)
    except (OSError, RuntimeError, ValueError) as exc:
        reasons.append(f"Artifact text is unreadable: {exc}")
        return {"status": "quarantined", "reasons": reasons, "metrics": metrics}

    metrics["word_count"] = len(text.split())
    metrics["has_projects"] = bool(
        re.search(r"(?im)^\s*(?:selected\s+)?projects\s*$", text)
    )
    source_text = text
    if artifact.get("kind") != "base":
        source = Path(str(artifact.get("source_resume_path") or ""))
        if source.is_file():
            source_text = read_resume_source(source)
            metrics["source_state"] = "available"
        else:
            metrics["source_state"] = "missing"
            reasons.append("Source evidence is unavailable; review before reuse.")
    metadata = json.loads(str(artifact.get("metadata_json") or "{}"))
    if (artifact.get("kind") != "base" and metadata.get("source_evidence_sha256") and source.is_file()
            and hashlib.sha256(source.read_bytes()).hexdigest() != metadata["source_evidence_sha256"]):
        reasons.append("Source evidence changed since this edition; review its affected claims before reuse.")
    if "fact_snapshot" in metadata:
        affected = changed_used_facts(metadata["fact_snapshot"], profile_fact_snapshot(profile), text)
        if affected:
            metrics["changed_used_facts"] = affected
            reasons.append("Used profile facts changed; review: " + ", ".join(affected))
    supplemental_path = Path(str(metadata.get("supplemental_evidence_path") or ""))
    evidence = source_text
    if supplemental_path.is_file():
        evidence += "\n\nSUPPLEMENTAL CANDIDATE EVIDENCE\n" + supplemental_path.read_text(encoding="utf-8")
    validation = validate_tailored_resume(text, dict(profile), original_text=evidence,
                                         selection_source_text=source_text)
    reasons.extend(
        str(error)
        for error in validation.get("errors", [])
        if str(error).startswith(_HEALTH_QUARANTINE_PREFIXES)
    )
    fact_errors = current_profile_resume_fact_errors(text, dict(profile))
    reasons.extend(str(error) for error in fact_errors if str(error) not in reasons)
    binding_invalid = (
        artifact.get("validation_status") == "machine_validated"
        and not _artifact_is_current(artifact)
    )
    if binding_invalid:
        reasons.append("Validated PDF binding is missing or no longer matches the artifact record.")
    return {
        "status": (
            "quarantined" if binding_invalid else "repair_required" if reasons else "eligible"
        ),
        "reasons": reasons,
        "metrics": metrics,
    }


def _record_artifact_health(
    conn: sqlite3.Connection,
    *,
    artifact_id: str,
    assessment: Mapping[str, object],
) -> None:
    conn.execute(
        """
        INSERT INTO resume_artifact_health_checks (
            health_id, artifact_id, policy_version, status,
            reasons_json, metrics_json, checked_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"health:{uuid.uuid4().hex}",
            artifact_id,
            HEALTH_POLICY_VERSION,
            str(assessment.get("status") or "quarantined"),
            _json(assessment.get("reasons", [])),
            _json(assessment.get("metrics", {})),
            _now(),
        ),
    )


def _latest_artifact_health(
    conn: sqlite3.Connection, artifact_id: str
) -> dict[str, object] | None:
    row = conn.execute(
        """
        SELECT status, reasons_json, metrics_json, checked_at
        FROM resume_artifact_health_checks
        WHERE artifact_id=? AND policy_version=?
        ORDER BY checked_at DESC LIMIT 1
        """,
        (artifact_id, HEALTH_POLICY_VERSION),
    ).fetchone()
    if row is None:
        return None
    return {
        "status": row["status"],
        "reasons": json.loads(str(row["reasons_json"] or "[]")),
        "metrics": json.loads(str(row["metrics_json"] or "{}")),
        "checked_at": row["checked_at"],
    }


def audit_resume_library_health(
    conn: sqlite3.Connection, profile: Mapping[str, object]
) -> dict[str, object]:
    """Assess active candidates and persist reversible quarantine evidence."""
    ensure_resume_library_schema(conn)
    details: list[dict[str, object]] = []
    rows = conn.execute(
        """
        SELECT * FROM resume_artifacts
        WHERE active=1 AND (
            validation_status='machine_validated'
            OR (kind='base' AND validation_status='source_only')
        )
        ORDER BY artifact_id
        """
    ).fetchall()
    for row in rows:
        artifact = dict(row)
        assessment = assess_resume_artifact_health(artifact, profile)
        _record_artifact_health(
            conn,
            artifact_id=str(artifact["artifact_id"]),
            assessment=assessment,
        )
        details.append({"artifact_id": artifact["artifact_id"], **assessment})
    conn.commit()
    return {
        "policy_version": HEALTH_POLICY_VERSION,
        "checked": len(details),
        "eligible": sum(item["status"] == "eligible" for item in details),
        "repair_required": sum(item["status"] == "repair_required" for item in details),
        "quarantined": sum(item["status"] == "quarantined" for item in details),
        "details": details,
    }


def _write_reuse_route_report(
    job_profile: Mapping[str, object],
    artifact: Mapping[str, object],
    assignment_id: str,
    result: Mapping[str, object],
) -> Path:
    """Write a job-specific immutable report without mutating shared artifacts."""
    artifact_path = Path(str(artifact["text_path"])).resolve()
    route_root = library_root(artifact_path) / "routes"
    route_root.mkdir(parents=True, exist_ok=True)
    decision = str(result.get("decision") or "reuse_exact")
    decision_suffix = "-manual-selection" if decision == "manual_selection" else ""
    report_path = route_root / (
        f"{uuid.uuid4().hex}{decision_suffix}.json"
    )
    payload = {
        "status": "machine_validated",
        "decision": decision,
        "resolution": result.get("resolution") or "reuse_as_is",
        "policy_version": POLICY_VERSION,
        "ranking_version": RANKING_VERSION,
        "taxonomy_version": TAXONOMY_VERSION,
        "assignment_id": assignment_id,
        "job_url": job_profile["job_url"],
        "job_fingerprint": job_profile["job_fingerprint"],
        "artifact_id": artifact["artifact_id"],
        "artifact_text_path": artifact["text_path"],
        "artifact_pdf_path": artifact["pdf_path"],
        "artifact_pdf_sha256": artifact["pdf_sha256"],
        "artifact_pdf_size": artifact["pdf_size"],
        "required_coverage": result.get("required_coverage"),
        "overall_score": result.get("overall_score"),
        "runner_up_margin": result.get("runner_up_margin"),
        "score_components": next(
            (
                candidate.get("score_components")
                for candidate in result.get("candidates", [])
                if candidate.get("artifact_id") == artifact["artifact_id"]
            ),
            None,
        ),
        "reason": result.get("reason"),
        "recorded_at": _now(),
    }
    if report_path.exists():
        existing = json.loads(report_path.read_text(encoding="utf-8"))
        immutable_keys = {
            key: payload[key]
            for key in (
                "job_fingerprint",
                "artifact_id",
                "artifact_pdf_sha256",
                "artifact_pdf_size",
            )
        }
        if any(existing.get(key) != value for key, value in immutable_keys.items()):
            raise ValueError("Existing resume reuse report conflicts with current immutable bindings")
    else:
        report_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return report_path


def _candidate_score(
    job_profile: Mapping[str, object],
    artifact: Mapping[str, object],
    *,
    exact_job_validation: bool = False,
    covered_subtypes: Iterable[str] = (),
) -> dict:
    text = _normalise_text(read_resume_source(Path(str(artifact["text_path"]))))
    required = list(job_profile.get("required_skills", []))
    required_hits = [skill for skill in required if _evidence_contains(text, str(skill))]
    features = job_profile.get("features", {})
    groups = features.get("required_skill_groups", []) if isinstance(features, Mapping) else []
    group_hits = [
        group for group in groups
        if any(re.search(rf"(?<!\w){re.escape(str(skill))}(?!\w)", text) for skill in group)
    ]
    missing_groups = ["one of: " + " | ".join(group) for group in groups if group not in group_hits]
    required_count = len(required) + len(groups)
    required_coverage = (len(required_hits) + len(group_hits)) / required_count if required_count else 1.0
    preferred = list(job_profile.get("preferred_skills", []))
    preferred_hits = [skill for skill in preferred if _evidence_contains(text, str(skill))]
    preferred_coverage = len(preferred_hits) / len(preferred) if preferred else None
    deliverables = list(job_profile.get("deliverables", []))
    deliverable_hits = [item for item in deliverables if _evidence_contains(text, str(item), inflections=True)]
    deliverable_coverage = len(deliverable_hits) / len(deliverables) if deliverables else None
    features = job_profile.get("features", {})
    content_terms = list(features.get("content_terms", [])) if isinstance(features, Mapping) else []
    content_hits = [term for term in content_terms if _evidence_contains(text, str(term), inflections=True)]
    content_coverage = len(content_hits) / len(content_terms) if content_terms else None

    subtypes = set(covered_subtypes)
    target_subtype = str(job_profile.get("subtype") or "")
    target_track = str(job_profile.get("track") or "")
    artifact_track = str(artifact.get("track") or "")
    kind = str(artifact.get("kind") or "")
    metadata = _artifact_metadata(artifact)
    family = str(metadata.get("library_family") or "")
    curated_tracks = _CURATED_FAMILY_TRACKS.get(family)
    if target_subtype and target_subtype in subtypes:
        taxonomy_score = 1.0
        candidate_scope = "exact_subtype"
    elif target_track and artifact_track in {target_track, "multi_track"}:
        taxonomy_score = 0.78 if kind == "tailored" else 0.72
        candidate_scope = "same_track" if kind == "tailored" else "same_track_base"
    elif kind == "base":
        taxonomy_score = 0.45
        candidate_scope = "cross_track_base"
    else:
        taxonomy_score = 0.35
        candidate_scope = "cross_track"

    if curated_tracks:
        # Editorial families describe the current content; inherited job tracks
        # describe its ancestry and can be null or misleading after consolidation.
        taxonomy_score = 0.9 if target_track in curated_tracks else 0.35
        candidate_scope = "curated_family" if target_track in curated_tracks else "cross_family"
        if target_subtype == "product_management":
            taxonomy_score = 1.0 if family == "产品与业务运营" else 0.35
        elif target_subtype == "ai_research":
            taxonomy_score = 1.0 if family == "AI 研究与评估" else 0.35
        elif target_subtype == "software_quality_validation":
            taxonomy_score = 1.0 if family == "软件测试与质量" else 0.35
        elif target_track == "ai_implementation" and family == "AI 研究与评估":
            taxonomy_score = 0.55

    evidence_quality = 1.0 if artifact.get("validation_status") == "machine_validated" else 0.62
    dimensions: list[tuple[str, float, float | None]] = [
        ("required_coverage", 0.30, required_coverage if required_count else None),
        ("preferred_coverage", 0.10, preferred_coverage),
        ("deliverable_coverage", 0.15, deliverable_coverage),
        ("content_coverage", 0.10 if curated_tracks else 0.20, content_coverage),
        ("taxonomy_score", 0.25 if curated_tracks else 0.15, taxonomy_score),
        ("evidence_quality", 0.10, evidence_quality),
    ]
    active = [(name, weight, value) for name, weight, value in dimensions if value is not None]
    weight_total = sum(weight for _, weight, _ in active) or 1.0
    overall = sum(weight * float(value) for _, weight, value in active) / weight_total
    if exact_job_validation:
        # A machine-validated artifact bound to this exact unchanged JD already
        # passed the stricter job-specific content and render gates. Generalised
        # cross-job similarity thresholds must not demote that exact evidence.
        overall = 1.0
        required_coverage = 1.0
        candidate_scope = "exact_job"
    return {
        "artifact_id": artifact["artifact_id"],
        "artifact_kind": kind,
        "validation_status": artifact.get("validation_status"),
        "required_coverage": round(required_coverage, 6),
        "preferred_coverage": round(preferred_coverage, 6) if preferred_coverage is not None else None,
        "deliverable_coverage": round(deliverable_coverage, 6) if deliverable_coverage is not None else None,
        "content_coverage": round(content_coverage, 6) if content_coverage is not None else None,
        "signal_coverage": round(
            (len(preferred_hits) + len(deliverable_hits)) / (len(preferred) + len(deliverables)), 6
        ) if preferred or deliverables else 1.0,
        "taxonomy_score": taxonomy_score,
        "evidence_quality": evidence_quality,
        "candidate_scope": candidate_scope,
        "overall_score": round(overall, 6),
        "exact_job_validation": exact_job_validation,
        "missing_required": sorted(set(required) - set(required_hits)) + missing_groups,
        "matched_required_skill_groups": group_hits,
        "matched_preferred": sorted(preferred_hits),
        "matched_deliverables": sorted(deliverable_hits),
        "matched_content_terms": sorted(content_hits),
        "score_components": {
            name: {"weight": weight, "score": value}
            for name, weight, value in dimensions
        },
    }


def _has_current_exact_quality_validation(
    conn: sqlite3.Connection,
    *,
    artifact_id: str,
    job_url: str,
    job_fingerprint: str,
) -> bool:
    """Return whether this exact binding passed the current unified quality gate."""
    rows = conn.execute(
        """
        SELECT validation_kind, evidence_json
        FROM resume_validation_runs
        WHERE artifact_id=? AND job_url=? AND job_fingerprint=?
          AND status='machine_validated'
        ORDER BY recorded_at DESC
        """,
        (artifact_id, job_url, job_fingerprint),
    ).fetchall()
    for row in rows:
        if row["validation_kind"] not in {
            "generated_strict_validation",
            "job_specific_revalidation",
        }:
            continue
        try:
            evidence = json.loads(str(row["evidence_json"] or "{}"))
        except json.JSONDecodeError:
            continue
        if evidence.get("quality_policy_version") == POLICY_VERSION:
            return True
    return False


def route_resume_for_job(
    conn: sqlite3.Connection,
    job: Mapping[str, object],
    profile: Mapping[str, object],
    *,
    artifact_id: str | None = None,
    minimum_fit_score: int | None = None,
    top_k: int = DEFAULT_CANDIDATE_LIMIT,
) -> dict:
    """Rank a broad artifact pool and choose one of four editing resolutions.

    ``decision`` remains a compatibility projection for older callers. The
    canonical decision is ``resolution``: reuse_as_is, reuse_with_reorder,
    patch_existing, or create_new.
    """
    ensure_resume_library_schema(conn)
    job_profile = extract_job_profile(job, profile)
    persist_job_profile(conn, job_profile)

    requested_artifact_id = str(artifact_id or "").strip() or None
    decision = "manual_review"
    resolution: str | None = None
    selected_artifact_id: str | None = None
    required_coverage: float | None = None
    overall_score: float | None = None
    margin: float | None = None
    hard_gaps: list[str] = []
    manual_selection_allowed = False
    candidates: list[dict] = []
    profile_fact_rejections: list[dict[str, object]] = []
    health_rejections: list[dict[str, object]] = []
    health_enforced = bool(
        profile.get("tailoring", {}).get("resume_layout", {})
        if isinstance(profile.get("tailoring", {}), Mapping)
        else False
    )
    library_policy = profile.get("tailoring", {}).get("library_policy", {})
    ceiling = max(1, int(library_policy.get("max_variants", 50)))
    active_count = conn.execute(
        "SELECT COUNT(*) FROM resume_artifacts WHERE active=1 AND kind='tailored' "
        "AND validation_status='machine_validated'"
    ).fetchone()[0]
    components: dict[str, object] = {
        "library_policy": {"mode": "reuse_first", "max_variants": ceiling,
                           "active_variants": active_count, "at_capacity": active_count >= ceiling},
        "job_profile": job_profile,
        "candidates": [],
        "profile_fact_rejections": profile_fact_rejections,
        "health_rejections": health_rejections,
        "health_policy_version": HEALTH_POLICY_VERSION,
        "ranking_version": RANKING_VERSION,
        "health_enforced": health_enforced,
        "reuse_thresholds": {
            "required_coverage": REUSE_REQUIRED_COVERAGE,
            "overall_score": REUSE_OVERALL_SCORE,
            "runner_up_margin_is_gate": False,
        },
        "resolution_thresholds": {
            "reuse_as_is": REUSE_OVERALL_SCORE,
            "reuse_with_reorder": REORDER_OVERALL_SCORE,
            "patch_existing": PATCH_OVERALL_SCORE,
        },
        "candidate_limit": max(1, int(top_k)),
    }

    fit_score = job.get("fit_score")
    score_binding_error = _score_binding_error(job, profile)
    score_status = str(job.get("score_status") or "").casefold()
    numeric_fit = isinstance(fit_score, (int, float)) and not isinstance(fit_score, bool)
    fit_gate_passed = (
        minimum_fit_score is not None
        and numeric_fit
        and 1 <= fit_score <= 10
        and score_status in {"", "scored"}
        and not score_binding_error
        and fit_score >= minimum_fit_score
    )
    components["fit_gate"] = {
        "fit_score": fit_score,
        "minimum_fit_score": minimum_fit_score,
        "passed": fit_gate_passed,
    }

    if str(job.get("eligibility_status") or "").casefold() == "ineligible":
        decision = "ignore"
        reason = f"Job is ineligible: {job.get('eligibility_reason') or 'explicit eligibility failure'}"
    elif not str(job.get("full_description") or "").strip():
        reason = "Full job description is missing; subtype and hard requirements cannot be verified."
    elif score_binding_error:
        reason = score_binding_error
    elif score_status not in {"", "scored"} or (
        minimum_fit_score is not None and (fit_score is not None or score_status == "scored")
        and not fit_gate_passed
    ):
        reason = "The available fit score is invalid, failed or below the configured admission threshold; review or rescore before routing."
    else:
        configured_source_paths = _configured_source_paths_for_track(
            profile, str(job_profile.get("track") or "") or None
        )
        rows = conn.execute(
            """
            SELECT a.*
            FROM resume_artifacts AS a
            WHERE a.active=1
              AND (
                a.validation_status='machine_validated'
                OR (a.kind='base' AND a.validation_status='source_only')
              )
            ORDER BY a.updated_at DESC, a.artifact_id
            """,
        ).fetchall()
        for row in rows:
            artifact = dict(row)
            requested_pages = job_profile["features"].get("requested_resume_pages")
            maximum_pages = job_profile["features"].get("maximum_resume_pages")
            page_count = _artifact_metadata(artifact).get("page_count")
            if (requested_pages and page_count != requested_pages) or (
                maximum_pages and (not isinstance(page_count, int) or page_count > maximum_pages)
            ):
                components.setdefault("page_rejections", []).append(artifact["artifact_id"])
                continue
            artifact_health_status = "eligible"
            text_path = Path(str(artifact.get("text_path") or ""))
            if not text_path.is_file():
                continue
            if health_enforced:
                health = _latest_artifact_health(conn, str(artifact["artifact_id"]))
                if health is None or health["metrics"].get("input_digest") != health_input_digest(artifact, profile):
                    health = assess_resume_artifact_health(artifact, profile)
                    _record_artifact_health(
                        conn,
                        artifact_id=str(artifact["artifact_id"]),
                        assessment=health,
                    )
                artifact_health_status = str(health.get("status") or "quarantined")
                if artifact_health_status != "eligible":
                    health_rejections.append(
                        {
                            "artifact_id": artifact["artifact_id"],
                            "status": health.get("status"),
                            "reasons": health.get("reasons", []),
                        }
                    )
                    if artifact_health_status == "quarantined":
                        continue
            if artifact["validation_status"] == "machine_validated" and not _artifact_is_current(artifact):
                continue
            artifact_text = read_resume_source(text_path)
            fact_errors = current_profile_resume_fact_errors(artifact_text, dict(profile))
            if fact_errors:
                profile_fact_rejections.append(
                    {"artifact_id": artifact["artifact_id"], "errors": fact_errors}
                )
                continue
            coverage_rows = conn.execute(
                "SELECT subtype, evidence_job_url, evidence_job_fingerprint "
                "FROM resume_coverage_cells WHERE artifact_id=? AND taxonomy_version=?",
                (artifact["artifact_id"], TAXONOMY_VERSION),
            ).fetchall()
            covered_subtypes = [str(item["subtype"]) for item in coverage_rows]
            exact_coverage = any(
                item["evidence_job_url"] == job_profile["job_url"]
                and item["evidence_job_fingerprint"] == job_profile["job_fingerprint"]
                for item in coverage_rows
            )
            exact_evidence = exact_coverage and _has_current_exact_quality_validation(
                conn,
                artifact_id=str(artifact["artifact_id"]),
                job_url=str(job_profile["job_url"]),
                job_fingerprint=str(job_profile["job_fingerprint"]),
            )
            scored = _candidate_score(
                job_profile,
                artifact,
                exact_job_validation=exact_evidence,
                covered_subtypes=covered_subtypes,
            )
            source_path = str(artifact.get("source_resume_path") or artifact.get("text_path") or "").strip()
            source_is_configured = bool(source_path) and (
                str(Path(source_path).resolve()).casefold() in configured_source_paths
            )
            artifact_track_matches = str(artifact.get("track") or "") in {
                str(job_profile.get("track") or ""), "multi_track"
            }
            scored["configured_source_preference"] = source_is_configured
            scored["artifact_track_matches"] = artifact_track_matches
            scored["artifact_health_status"] = artifact_health_status
            scored["route_preference_score"] = int(source_is_configured) + int(artifact_track_matches)
            unsupported, confirmed_support = _unsupported_required_skills(
                conn, profile, scored["missing_required"]
            )
            scored["unsupported_required_skills"] = unsupported
            scored["confirmed_required_skill_facts"] = confirmed_support
            validated = artifact["validation_status"] == "machine_validated"
            if artifact_health_status == "repair_required":
                recommended_resolution = "patch_existing"
            elif exact_evidence or (
                validated
                and scored["required_coverage"] >= REUSE_REQUIRED_COVERAGE
                and scored["overall_score"] >= REUSE_OVERALL_SCORE
                and not scored["missing_required"]
            ):
                recommended_resolution = "reuse_as_is"
            elif (
                validated
                and scored["required_coverage"] >= REUSE_REQUIRED_COVERAGE
                and scored["overall_score"] >= REORDER_OVERALL_SCORE
                and not unsupported
            ):
                recommended_resolution = "reuse_with_reorder"
            elif scored["overall_score"] >= PATCH_OVERALL_SCORE or artifact_track_matches:
                recommended_resolution = "patch_existing"
            else:
                recommended_resolution = "create_new"
            scored["recommended_resolution"] = recommended_resolution
            scored["reuse_qualified"] = recommended_resolution == "reuse_as_is"
            scored["artifact"] = artifact
            candidates.append(scored)
        candidates.sort(
            key=lambda item: (
                not item["exact_job_validation"],
                item["recommended_resolution"] != "reuse_as_is",
                bool(item["unsupported_required_skills"]) and not fit_gate_passed,
                -item["overall_score"],
                -item["route_preference_score"],
                item["artifact_id"],
            )
        )
        total_candidates = len(candidates)
        candidates = candidates[:max(1, int(top_k))]
        components["candidate_count_total"] = total_candidates
        components["usable_base_sources"] = [
            {
                "artifact_id": candidate["artifact_id"],
                "text_path": candidate["artifact"]["text_path"],
            }
            for candidate in candidates
            if candidate["artifact"].get("kind") == "base"
        ]
        components["candidates"] = [
            {key: value for key, value in candidate.items() if key != "artifact"}
            for candidate in candidates
        ]
        if not candidates:
            if (
                (not job_profile.get("subtype") or float(job_profile.get("confidence") or 0) < 0.55)
                and not fit_gate_passed
            ):
                decision = "manual_review"
                resolution = None
                reason = (
                    "No factual candidate was found, and subtype confidence and the configured "
                    "fit-score gate are both insufficient for automatic generation."
                )
            elif profile_fact_rejections:
                decision = "create_variant"
                resolution = "create_new"
                reason = (
                    "Available artifacts conflict with current profile facts; create a corrected resume."
                )
            else:
                decision = "create_variant"
                resolution = "create_new"
                reason = "No current factual resume artifact is available; create a new validated resume."
        else:
            top = candidates[0]
            selected_artifact_id = str(top["artifact_id"])
            required_coverage = float(top["required_coverage"])
            overall_score = float(top["overall_score"])
            route_preference_resolved_tie = False
            if len(candidates) > 1:
                runner_up = candidates[1]
                route_preference_resolved_tie = (
                    overall_score == float(runner_up["overall_score"])
                    and int(top["route_preference_score"])
                    > int(runner_up["route_preference_score"])
                )
                margin = (
                    1.0
                    if route_preference_resolved_tie
                    else round(overall_score - float(runner_up["overall_score"]), 6)
                )
            else:
                margin = 1.0
            components["route_preference_resolved_tie"] = route_preference_resolved_tie
            hard_gaps = list(top["missing_required"])
            components["unsupported_required_skills"] = top["unsupported_required_skills"]
            components["confirmed_required_skill_facts"] = top["confirmed_required_skill_facts"]
            resolution = str(top["recommended_resolution"])
            if (
                (not job_profile.get("subtype") or float(job_profile.get("confidence") or 0) < 0.55)
                and not fit_gate_passed
            ):
                decision = "manual_review"
                resolution = None
                reason = (
                    "The Top-K search found candidates, but subtype confidence and the configured "
                    "fit-score gate are both insufficient for automatic editing."
                )
            elif top["unsupported_required_skills"] and not fit_gate_passed:
                decision = "manual_review"
                resolution = None
                reason = (
                    "Named required skills are unsupported and the fit-score gate was not proven to pass."
                )
            elif resolution == "reuse_as_is":
                decision = "reuse_exact"
                margin = 1.0 if top["exact_job_validation"] else margin
                reason = (
                    "The exact unchanged job already validated this artifact."
                    if top["exact_job_validation"] else
                    "The best current validated artifact clears the content and factual reuse gates."
                )
                if route_preference_resolved_tie:
                    reason += " An explicitly configured source resolved the score tie."
            elif resolution == "reuse_with_reorder":
                decision = "create_variant"
                reason = "The best validated artifact has strong content coverage; reorder it and revalidate."
            elif resolution == "patch_existing":
                decision = "create_variant"
                reason = "The best artifact is a credible base but needs targeted content additions or strengthening."
            else:
                decision = "create_variant"
                selected_artifact_id = None
                reason = "No artifact is sufficiently close for bounded editing; create a new validated resume."
            if profile_fact_rejections:
                reason += " Higher-ranked artifacts were rejected because they conflict with current profile facts."
            manual_selection_allowed = (
                resolution is not None and margin is not None and margin < REUSE_MIN_MARGIN
            )

    if requested_artifact_id:
        selected = next(
            (
                candidate
                for candidate in candidates
                if candidate["artifact_id"] == requested_artifact_id
            ),
            None,
        )
        if selected is None:
            raise ValueError(
                "The requested resume artifact is not a current candidate for this exact job"
            )
        if decision not in {"manual_review", "create_variant", "reuse_exact"} or not manual_selection_allowed:
            raise ValueError(
                "Manual selection cannot resolve this route decision"
            )
        selected_required_coverage = float(selected["required_coverage"])
        selected_overall_score = float(selected["overall_score"])
        selected_hard_gaps = list(selected["missing_required"])
        unsupported = list(selected["unsupported_required_skills"])
        confirmed_fact_support = list(selected["confirmed_required_skill_facts"])
        components["unsupported_required_skills"] = unsupported
        components["confirmed_required_skill_facts"] = confirmed_fact_support
        selected_resolution = str(selected["recommended_resolution"])
        if unsupported and selected_resolution == "reuse_as_is":
            raise ValueError(
                "Manual selection cannot resolve unsupported required skill review"
            )
        selected_artifact_id = requested_artifact_id
        required_coverage = selected_required_coverage
        overall_score = selected_overall_score
        hard_gaps = selected_hard_gaps
        original_decision = decision
        original_reason = reason
        resolution = selected_resolution
        decision = "manual_selection" if resolution == "reuse_as_is" else "create_variant"
        reason = "An explicit operator or agent selected a Top-K candidate for the bounded resolution."
        components["manual_selection"] = {
            "artifact_id": selected_artifact_id,
            "original_decision": original_decision,
            "original_reason": original_reason,
            "resolution": resolution,
        }

    if active_count >= ceiling and resolution and resolution != "reuse_as_is":
        components["capacity_proposed_resolution"] = resolution
        decision = "manual_review"
        resolution = None
        reason = "The active library is at its size ceiling; consolidate or replace an existing variant before adding another."
    components["new_variant_reason"] = reason if resolution in {"create_new", "patch_existing"} else None
    components["resolution"] = resolution
    components["selected_source_artifact_id"] = selected_artifact_id

    assignment_id = _record_assignment(
        conn,
        job_profile=job_profile,
        artifact_id=selected_artifact_id,
        decision=decision,
        required_coverage=required_coverage,
        overall_score=overall_score,
        margin=margin,
        hard_gaps=hard_gaps,
        components=components,
        reason=reason,
    )
    result = {
        "assignment_id": assignment_id,
        "ranking_version": RANKING_VERSION,
        "decision": decision,
        "resolution": resolution,
        "reason": reason,
        "artifact_id": selected_artifact_id,
        "required_coverage": required_coverage,
        "overall_score": overall_score,
        "runner_up_margin": margin,
        "hard_gaps": hard_gaps,
        "job_profile": job_profile,
        "candidates": components["candidates"],
        "profile_fact_rejections": profile_fact_rejections,
        "health_rejections": health_rejections,
        "reuse_thresholds": components["reuse_thresholds"],
        "resolution_thresholds": components["resolution_thresholds"],
    }
    if selected_artifact_id:
        artifact = dict(
            conn.execute(
                "SELECT * FROM resume_artifacts WHERE artifact_id=?", (selected_artifact_id,)
            ).fetchone()
        )
        result["artifact"] = artifact
        raw_score_evidence = job.get("score_evidence_json")
        score_evidence = json.loads(str(raw_score_evidence)) if raw_score_evidence and not score_binding_error else {}
        binding = score_evidence.get("input_binding", {}) if isinstance(score_evidence, dict) else {}
        if binding:
            result["score_alignment"] = {
                "scored_source_path": binding.get("source_path"),
                "selected_text_path": artifact.get("text_path"),
                "same_content": binding.get("source_text_digest") == text_digest(
                    read_resume_source(Path(str(artifact["text_path"])))
                ),
                "meaning": "Candidate fit is based on the scored source; routing score measures the selected resume's JD coverage.",
            }
    if decision in {"reuse_exact", "manual_selection"} and selected_artifact_id:
        _record_validation(
            conn,
            artifact_id=selected_artifact_id,
            validation_kind=(
                "manual_selection_route_binding"
                if decision == "manual_selection"
                else "reuse_route_binding"
            ),
            status="machine_validated",
            job_profile=job_profile,
            evidence={
                "assignment_id": assignment_id,
                "decision": decision,
                "pdf_sha256": artifact.get("pdf_sha256"),
                "pdf_size": artifact.get("pdf_size"),
                "policy_version": POLICY_VERSION,
            },
        )
        result["reuse_report_path"] = str(
            _write_reuse_route_report(job_profile, artifact, assignment_id, result)
        )
        record_route_outcome(
            conn,
            assignment_id=assignment_id,
            resolution="reuse_as_is",
            status="machine_validated",
            source_artifact_id=selected_artifact_id,
            output_artifact_id=selected_artifact_id,
            evidence={"reuse_report_path": result["reuse_report_path"]},
        )
    conn.commit()
    return result


def project_reuse_to_job(
    conn: sqlite3.Connection,
    job: Mapping[str, object],
    route: Mapping[str, object],
) -> dict:
    """Project a validated automatic or explicit reuse route atomically."""
    if route.get("decision") not in {"reuse_exact", "manual_selection"} or not route.get(
        "artifact"
    ):
        raise ValueError("Only a validated reuse route can be projected")
    artifact = route["artifact"]
    if not _artifact_is_current(artifact):
        raise ValueError("Resume artifact bytes changed before compatibility projection")
    report_path = str(route.get("reuse_report_path") or "").strip()
    if not report_path or not Path(report_path).is_file():
        raise ValueError("Job-specific resume reuse report is missing")
    now = _now()
    cursor = conn.execute(
        """
        UPDATE jobs SET tailored_resume_path=?, tailored_at=?,
            tailor_status='machine_validated', tailor_error=NULL,
            tailor_source_resume_path=?, tailor_report_path=?
        WHERE url=?
        """,
        (
            artifact["text_path"],
            now,
            artifact.get("source_resume_path") or artifact["text_path"],
            report_path,
            job["url"],
        ),
    )
    if cursor.rowcount != 1:
        conn.rollback()
        raise ValueError("Exact job disappeared before resume reuse projection")
    conn.commit()
    return {
        "job_url": job["url"],
        "artifact_id": artifact["artifact_id"],
        "tailored_resume_path": artifact["text_path"],
        "tailor_report_path": report_path,
        "projected_at": now,
    }


def library_status(conn: sqlite3.Connection) -> dict:
    ensure_resume_library_schema(conn)
    counts = {}
    for key, table in {
        "artifacts": "resume_artifacts",
        "active_validated_artifacts": "resume_artifacts",
        "coverage_cells": "resume_coverage_cells",
        "job_profiles": "job_resume_profiles",
        "assignments": "job_resume_assignments",
        "validation_runs": "resume_validation_runs",
        "route_outcomes": "resume_route_outcomes",
        "health_checks": "resume_artifact_health_checks",
        "render_versions": "resume_render_versions",
    }.items():
        where = " WHERE active=1 AND validation_status='machine_validated'" if key == "active_validated_artifacts" else ""
        counts[key] = conn.execute(f"SELECT COUNT(*) FROM {table}{where}").fetchone()[0]
    counts["decisions"] = {
        row["decision"]: row["count"]
        for row in conn.execute(
            "SELECT decision, COUNT(*) AS count FROM job_resume_assignments GROUP BY decision"
        ).fetchall()
    }
    counts["resolutions"] = {
        row["resolution"]: row["count"]
        for row in conn.execute(
            "SELECT resolution, COUNT(*) AS count FROM resume_route_outcomes GROUP BY resolution"
        ).fetchall()
    }
    counts["outcome_statuses"] = {
        row["status"]: row["count"]
        for row in conn.execute(
            "SELECT status, COUNT(*) AS count FROM resume_route_outcomes GROUP BY status"
        ).fetchall()
    }
    counts["latest_health_statuses"] = {
        row["status"]: row["count"]
        for row in conn.execute(
            """
            SELECT checks.status, COUNT(*) AS count
            FROM resume_artifact_health_checks AS checks
            JOIN (
                SELECT artifact_id, MAX(checked_at) AS checked_at
                FROM resume_artifact_health_checks
                WHERE policy_version=? GROUP BY artifact_id
            ) AS latest
              ON latest.artifact_id=checks.artifact_id
             AND latest.checked_at=checks.checked_at
            WHERE checks.policy_version=?
            GROUP BY checks.status
            """,
            (HEALTH_POLICY_VERSION, HEALTH_POLICY_VERSION),
        ).fetchall()
    }
    counts["covered_subtypes"] = [
        row["subtype"]
        for row in conn.execute(
            "SELECT DISTINCT subtype FROM resume_coverage_cells "
            "WHERE taxonomy_version=? ORDER BY subtype",
            (TAXONOMY_VERSION,),
        ).fetchall()
    ]
    counts["taxonomy_version"] = TAXONOMY_VERSION
    counts["policy_version"] = POLICY_VERSION
    counts["health_policy_version"] = HEALTH_POLICY_VERSION
    return counts
