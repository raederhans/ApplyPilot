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
import shutil
import sqlite3
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import yaml

from applypilot.apply.authorization import compute_file_binding, compute_job_fingerprint
from applypilot.config import CONFIG_DIR, TAILORED_DIR
from applypilot.radar import SUBTRACK_TO_TRACK, classify_job_subtracks
from applypilot.scoring.cover_letter import read_resume_source

TAXONOMY_VERSION = "resume-library-v5"
POLICY_VERSION = "reuse-policy-v2"

REUSE_REQUIRED_COVERAGE = 0.90
REUSE_OVERALL_SCORE = 0.85
REUSE_MIN_MARGIN = 0.08

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
    r"(?i)\b(?:preferred|nice to have|bonus|plus|ideally)\b"
)

_KNOWN_SKILLS = {
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
    (
        "ai_solutions",
        "ai_implementation",
        ("machine learning engineer", "artificial intelligence engineer", "ai engineer"),
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


def ensure_resume_library_schema(conn: sqlite3.Connection) -> None:
    """Create the additive resume-library schema without changing job rows."""
    conn.executescript(
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
        """
    )


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
    if re.search(r"\b(?:intern|internship|trainee|co-op|graduate programme)\b", lowered_title):
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
    required: set[str] = set()
    preferred: set[str] = set()
    mentioned: set[str] = set()
    sentences = [part.strip() for part in re.split(r"[\n\r.;]+", description) if part.strip()]
    for skill in known_skills:
        if not _contains_phrase(combined, skill):
            continue
        mentioned.add(skill)
        skill_lines = [line for line in sentences if _contains_phrase(_normalise_text(line), skill)]
        if any(_REQUIRED_MARKERS.search(line) for line in skill_lines):
            required.add(skill)
        elif any(_PREFERRED_MARKERS.search(line) for line in skill_lines):
            preferred.add(skill)

    deliverables = sorted(term for term in _DELIVERABLE_TERMS if _contains_phrase(combined, term))
    fingerprint = compute_job_fingerprint(dict(job))
    features = {
        "complete_description": bool(description),
        "mentioned_skills": sorted(mentioned),
        "title_matches": list(title_matches),
        "subtype_scores": term_scores,
        "max_required_years": max(years) if years else None,
        "location": str(job.get("location") or "").strip(),
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
) -> tuple[str, bool]:
    text_path = text_path.expanduser().resolve()
    original_text_path = text_path
    original_pdf_path = text_path.with_suffix(".pdf")
    text = read_resume_source(text_path)
    digest = _content_digest(text)
    artifact_id = f"resume:{digest[:24]}"
    pdf_path = original_pdf_path
    if (
        kind == "tailored"
        and validation_status == "machine_validated"
        and text_path.suffix.casefold() == ".txt"
        and original_pdf_path.is_file()
    ):
        parent = original_text_path.parent
        if (
            parent.name.casefold() == "artifacts"
            and parent.parent.name.casefold() == "resume-library"
        ):
            # A library sync may revisit jobs that already project to the
            # canonical content-addressed artifact. Keep that root stable
            # instead of creating resume-library/artifacts recursively.
            artifact_root = parent.resolve()
        else:
            storage_base = (
                parent.parent
                if parent.name.casefold() == "tailored_resumes"
                else parent
            )
            artifact_root = (storage_base / "resume-library" / "artifacts").resolve()
        artifact_root.mkdir(parents=True, exist_ok=True)
        neutral_stem = artifact_id.replace(":", "-")
        neutral_text = artifact_root / f"{neutral_stem}.txt"
        neutral_pdf = artifact_root / f"{neutral_stem}.pdf"
        if not neutral_text.exists():
            shutil.copyfile(original_text_path, neutral_text)
        if not neutral_pdf.exists():
            shutil.copyfile(original_pdf_path, neutral_pdf)
        text_path = neutral_text
        pdf_path = neutral_pdf
    pdf_sha256: str | None = None
    pdf_size: int | None = None
    if pdf_path.is_file():
        pdf_sha256, pdf_size = compute_file_binding(pdf_path)
    else:
        pdf_path = None
    now = _now()
    existing = conn.execute(
        "SELECT artifact_id, validation_status FROM resume_artifacts WHERE content_sha256=?",
        (digest,),
    ).fetchone()
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
    elif validation_status == "machine_validated" and pdf_path is not None:
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
    artifact_id, created = _register_artifact(
        conn,
        text_path=path,
        kind="tailored",
        track=str(job_profile.get("track") or "") or None,
        source_resume_path=source_resume_path,
        validation_status="machine_validated",
        report_path=report_path,
        validated_at=str(job.get("tailored_at") or _now()),
        metadata={"registered_from_job": job_profile["job_url"]},
    )
    coverage_added = _add_coverage_cell(conn, artifact_id, job_profile, str(job.get("tailored_at") or ""))
    if created or coverage_added:
        artifact = conn.execute(
            "SELECT pdf_sha256, pdf_size FROM resume_artifacts WHERE artifact_id=?",
            (artifact_id,),
        ).fetchone()
        _record_validation(
            conn,
            artifact_id=artifact_id,
            validation_kind=validation_kind,
            status="machine_validated",
            job_profile=job_profile,
            evidence={
                "report_path": report_path,
                "pdf_sha256": artifact["pdf_sha256"] if artifact else None,
                "pdf_size": artifact["pdf_size"] if artifact else None,
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
    return digest == artifact.get("pdf_sha256") and size == artifact.get("pdf_size")


def _write_reuse_route_report(
    job_profile: Mapping[str, object],
    artifact: Mapping[str, object],
    assignment_id: str,
    result: Mapping[str, object],
) -> Path:
    """Write a job-specific immutable report without mutating shared artifacts."""
    artifact_path = Path(str(artifact["text_path"])).resolve()
    route_root = (artifact_path.parent.parent / "routes").resolve()
    route_root.mkdir(parents=True, exist_ok=True)
    artifact_token = str(artifact["artifact_id"]).replace(":", "-")
    decision = str(result.get("decision") or "reuse_exact")
    decision_suffix = "-manual-selection" if decision == "manual_selection" else ""
    report_path = route_root / (
        f"{job_profile['job_fingerprint']}-{artifact_token}{decision_suffix}.json"
    )
    payload = {
        "status": "machine_validated",
        "decision": decision,
        "policy_version": POLICY_VERSION,
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
) -> dict:
    text = _normalise_text(read_resume_source(Path(str(artifact["text_path"]))))
    required = list(job_profile.get("required_skills", []))
    required_hits = [skill for skill in required if _contains_phrase(text, str(skill))]
    required_coverage = len(required_hits) / len(required) if required else 1.0
    preferred = list(job_profile.get("preferred_skills", []))
    deliverables = list(job_profile.get("deliverables", []))
    signals = [*preferred, *deliverables]
    signal_hits = [signal for signal in signals if _contains_phrase(text, str(signal))]
    signal_coverage = len(signal_hits) / len(signals) if signals else 1.0
    taxonomy_score = 1.0
    overall = 0.55 * taxonomy_score + 0.30 * required_coverage + 0.15 * signal_coverage
    if exact_job_validation:
        # A machine-validated artifact bound to this exact unchanged JD already
        # passed the stricter job-specific content and render gates. Generalised
        # cross-job similarity thresholds must not demote that exact evidence.
        overall = 1.0
        required_coverage = 1.0
    return {
        "artifact_id": artifact["artifact_id"],
        "required_coverage": round(required_coverage, 6),
        "signal_coverage": round(signal_coverage, 6),
        "overall_score": round(overall, 6),
        "exact_job_validation": exact_job_validation,
        "missing_required": sorted(set(required) - set(required_hits)),
        "matched_signals": sorted(signal_hits),
    }


def route_resume_for_job(
    conn: sqlite3.Connection,
    job: Mapping[str, object],
    profile: Mapping[str, object],
    *,
    artifact_id: str | None = None,
) -> dict:
    """Choose reuse/create/review/ignore for one exact job, without LLM calls."""
    ensure_resume_library_schema(conn)
    job_profile = extract_job_profile(job, profile)
    persist_job_profile(conn, job_profile)

    requested_artifact_id = str(artifact_id or "").strip() or None
    decision = "manual_review"
    artifact_id: str | None = None
    required_coverage: float | None = None
    overall_score: float | None = None
    margin: float | None = None
    hard_gaps: list[str] = []
    manual_selection_allowed = False
    candidates: list[dict] = []
    components: dict[str, object] = {"job_profile": job_profile, "candidates": []}

    if str(job.get("eligibility_status") or "").casefold() == "ineligible":
        decision = "ignore"
        reason = f"Job is ineligible: {job.get('eligibility_reason') or 'explicit eligibility failure'}"
    elif not str(job.get("full_description") or "").strip():
        reason = "Full job description is missing; subtype and hard requirements cannot be verified."
    elif not job_profile.get("subtype") or float(job_profile.get("confidence") or 0) < 0.55:
        reason = "No sufficiently confident fine-grained role subtype was found."
    else:
        configured_source_paths = _configured_source_paths_for_track(
            profile, str(job_profile.get("track") or "") or None
        )
        rows = conn.execute(
            """
            SELECT DISTINCT a.*
            FROM resume_artifacts AS a
            JOIN resume_coverage_cells AS c ON c.artifact_id=a.artifact_id
            WHERE a.active=1 AND a.validation_status='machine_validated'
              AND c.taxonomy_version=? AND c.subtype=?
            """,
            (TAXONOMY_VERSION, job_profile["subtype"]),
        ).fetchall()
        for row in rows:
            artifact = dict(row)
            if _artifact_is_current(artifact):
                exact_evidence = conn.execute(
                    """
                    SELECT 1 FROM resume_coverage_cells
                    WHERE artifact_id=? AND taxonomy_version=?
                      AND evidence_job_url=? AND evidence_job_fingerprint=?
                    LIMIT 1
                    """,
                    (
                        artifact["artifact_id"],
                        TAXONOMY_VERSION,
                        job_profile["job_url"],
                        job_profile["job_fingerprint"],
                    ),
                ).fetchone()
                scored = _candidate_score(
                    job_profile,
                    artifact,
                    exact_job_validation=exact_evidence is not None,
                )
                source_path = str(artifact.get("source_resume_path") or "").strip()
                source_is_configured = bool(source_path) and (
                    str(Path(source_path).resolve()).casefold()
                    in configured_source_paths
                )
                artifact_track_matches = (
                    str(artifact.get("track") or "")
                    == str(job_profile.get("track") or "")
                )
                scored["configured_source_preference"] = source_is_configured
                scored["artifact_track_matches"] = artifact_track_matches
                scored["route_preference_score"] = (
                    int(source_is_configured) + int(artifact_track_matches)
                )
                scored["artifact"] = artifact
                candidates.append(scored)
        candidates.sort(
            key=lambda item: (
                not item["exact_job_validation"],
                -item["overall_score"],
                -item["route_preference_score"],
                item["artifact_id"],
            )
        )
        components["candidates"] = [
            {key: value for key, value in candidate.items() if key != "artifact"}
            for candidate in candidates
        ]
        if not candidates:
            decision = "create_variant"
            reason = "This fine-grained role subtype has no current validated resume artifact."
        else:
            top = candidates[0]
            artifact_id = str(top["artifact_id"])
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
            if top["exact_job_validation"]:
                margin = 1.0
                decision = "reuse_exact"
                reason = (
                    "This current artifact was machine-validated for the exact unchanged job fingerprint."
                )
                components["unsupported_required_skills"] = []
            else:
                all_source_text = "\n".join(
                    _normalise_text(read_resume_source(Path(str(row["text_path"]))))
                    for row in conn.execute(
                        "SELECT text_path FROM resume_artifacts WHERE active=1 AND kind='base'"
                    ).fetchall()
                    if Path(str(row["text_path"])).is_file()
                )
                unsupported = [
                    gap for gap in hard_gaps if not _contains_phrase(all_source_text, gap)
                ]
                components["unsupported_required_skills"] = unsupported
            if not top["exact_job_validation"] and unsupported:
                decision = "manual_review"
                reason = "A required named skill is unsupported by every registered factual source."
            elif not top["exact_job_validation"] and required_coverage < REUSE_REQUIRED_COVERAGE:
                decision = "create_variant"
                reason = "The best artifact does not expose enough required skills for exact reuse."
            elif not top["exact_job_validation"] and overall_score < REUSE_OVERALL_SCORE:
                decision = "create_variant"
                reason = "The best artifact is below the conservative exact-reuse score."
            elif not top["exact_job_validation"] and margin < REUSE_MIN_MARGIN:
                decision = "manual_review"
                reason = "Two validated artifacts are too close to choose automatically."
                manual_selection_allowed = True
            elif not top["exact_job_validation"]:
                decision = "reuse_exact"
                if route_preference_resolved_tie:
                    reason = (
                        "A current validated artifact uses the explicitly configured source "
                        "for this track and clears all reuse gates."
                    )
                else:
                    reason = (
                        "A current validated artifact covers the same subtype and clears all "
                        "reuse gates."
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
        if decision != "manual_review" or not manual_selection_allowed:
            raise ValueError(
                "Manual selection cannot resolve this route decision; review remains required"
            )
        selected_required_coverage = float(selected["required_coverage"])
        selected_overall_score = float(selected["overall_score"])
        if (
            selected_required_coverage < REUSE_REQUIRED_COVERAGE
            or selected_overall_score < REUSE_OVERALL_SCORE
        ):
            raise ValueError("The requested candidate does not clear the existing reuse gates")
        selected_hard_gaps = list(selected["missing_required"])
        all_source_text = "\n".join(
            _normalise_text(read_resume_source(Path(str(row["text_path"]))))
            for row in conn.execute(
                "SELECT text_path FROM resume_artifacts WHERE active=1 AND kind='base'"
            ).fetchall()
            if Path(str(row["text_path"])).is_file()
        )
        unsupported = [
            gap
            for gap in selected_hard_gaps
            if not _contains_phrase(all_source_text, gap)
        ]
        if unsupported:
            raise ValueError(
                "Manual selection cannot resolve unsupported required skill review"
            )
        artifact_id = requested_artifact_id
        required_coverage = selected_required_coverage
        overall_score = selected_overall_score
        hard_gaps = selected_hard_gaps
        original_reason = reason
        decision = "manual_selection"
        reason = (
            "An explicit operator or agent selected one current qualified candidate "
            "from an otherwise unresolved tie."
        )
        components["manual_selection"] = {
            "artifact_id": artifact_id,
            "original_decision": "manual_review",
            "original_reason": original_reason,
        }

    assignment_id = _record_assignment(
        conn,
        job_profile=job_profile,
        artifact_id=artifact_id,
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
        "decision": decision,
        "reason": reason,
        "artifact_id": artifact_id,
        "required_coverage": required_coverage,
        "overall_score": overall_score,
        "runner_up_margin": margin,
        "hard_gaps": hard_gaps,
        "job_profile": job_profile,
        "candidates": components["candidates"],
    }
    if decision in {"reuse_exact", "manual_selection"} and artifact_id:
        artifact = dict(
            conn.execute(
                "SELECT * FROM resume_artifacts WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
        )
        result["artifact"] = artifact
        _record_validation(
            conn,
            artifact_id=artifact_id,
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
    }.items():
        where = " WHERE active=1 AND validation_status='machine_validated'" if key == "active_validated_artifacts" else ""
        counts[key] = conn.execute(f"SELECT COUNT(*) FROM {table}{where}").fetchone()[0]
    counts["decisions"] = {
        row["decision"]: row["count"]
        for row in conn.execute(
            "SELECT decision, COUNT(*) AS count FROM job_resume_assignments GROUP BY decision"
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
    return counts
