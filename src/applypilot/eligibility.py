"""Deterministic hard-eligibility screening for discovered jobs."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime

ELIGIBLE_SQL = "COALESCE(eligibility_status, 'eligible') != 'ineligible'"
ELIGIBILITY_POLICY_REVISION = "explicit-do-not-apply-v2"

_EXPLICIT_DO_NOT_APPLY_PATTERNS = (
    re.compile(
        r"(?is)\bif\b.{0,160}?\b(?:please\s+)?(?:do\s+not|don't)\s+apply\b"
    ),
    re.compile(
        r"(?is)\b(?:please\s+)?(?:do\s+not|don't)\s+apply\b.{0,160}?\bif\b.{0,160}"
    ),
    re.compile(
        r"(?is)\b(?:applicants?|candidates?|applications?)\s+"
        r"(?:from|without|that|which|submitted\s+by)\b.{0,160}?"
        r"\bwill\s+not\s+be\s+considered\b"
    ),
    re.compile(
        r"(?is)\b(?:applicants?|candidates?)\s+requir(?:e|es|ed|ing)\s+"
        r"(?:visa\s+)?sponsorship\b.{0,100}?\bwill\s+not\s+be\s+considered\b"
    ),
    re.compile(
        r"(?is)\bif\s+you\s+(?:are\s+)?not\s+(?:a\s+)?"
        r"(?:singapore\s+citizen|singaporean|singapore\s+permanent\s+resident|"
        r"singapore\s+pr)\b.{0,100}?\b(?:do\s+not|don't)\s+apply\b"
    ),
    re.compile(
        r"(?is)\b(?:non[- ]?singapore\s+citizens?|non[- ]?singaporeans?|"
        r"applicants?\s+who\s+are\s+not\s+(?:singapore\s+)?"
        r"(?:citizens?|permanent\s+residents?|prs?))\b.{0,100}?"
        r"\b(?:should|must|may)\s+not\s+apply\b"
    ),
    re.compile(
        r"(?is)\bapplications?\s+from\s+(?:non[- ]?singapore\s+citizens?|"
        r"non[- ]?singaporeans?|applicants?\s+without\s+(?:singapore\s+)?"
        r"(?:citizenship|permanent\s+residency|pr\s+status))\b.{0,100}?"
        r"\bwill\s+not\s+be\s+considered\b"
    ),
)


def _confirmed_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().casefold()
    if normalized in {"yes", "true", "1"}:
        return True
    if normalized in {"no", "false", "0"}:
        return False
    return None


def _explicit_exclusion_conflicts(text: str, profile: Mapping[str, object]) -> bool:
    """Match only exclusions contradicted by a confirmed applicant fact."""
    normalized = " ".join(text.casefold().split())
    personal = profile.get("personal", {})
    if not isinstance(personal, Mapping):
        personal = {}
    work_auth = profile.get("work_authorization", {})
    if not isinstance(work_auth, Mapping):
        work_auth = {}

    citizen_term = bool(
        re.search(r"\bsingapore(?:an|\s+citizenship|\s+citizens?)\b", normalized)
    )
    permanent_resident_term = bool(
        re.search(
            r"\b(?:singapore\s+)?(?:permanent\s+residen(?:t|ts|cy)|"
            r"pr\s+status|prs?)\b",
            normalized,
        )
    )
    singapore_status_exclusion = bool(
        (citizen_term or permanent_resident_term)
        and re.search(
            r"\b(?:do\s+not|don't|should\s+not|must\s+not|may\s+not)\s+apply\b|"
            r"\bwill\s+not\s+be\s+considered\b",
            normalized,
        )
    )
    if singapore_status_exclusion:
        citizenship = str(
            personal.get("citizenship") or personal.get("nationality") or ""
        ).strip().casefold()
        singapore_citizen = _confirmed_bool(work_auth.get("singapore_citizen"))
        if singapore_citizen is None and citizenship:
            singapore_citizen = citizenship in {"singapore", "singaporean"}
        singapore_pr = None
        for key in (
            "singapore_permanent_resident",
            "singapore_pr",
            "permanent_resident",
        ):
            if key in work_auth:
                singapore_pr = _confirmed_bool(work_auth.get(key))
                break
        if citizen_term and permanent_resident_term:
            # "Citizen or PR" is an OR eligibility rule: hard-block only when
            # both alternatives are explicitly contradicted by confirmed facts.
            if singapore_citizen is False and singapore_pr is False:
                return True
        elif (
            citizen_term and singapore_citizen is False
        ) or (
            permanent_resident_term and singapore_pr is False
        ):
            return True

    if re.search(r"\bsponsor(?:ship|ed|ing)?\b", normalized):
        sponsorship = _confirmed_bool(
            work_auth.get("requires_sponsorship")
            if "requires_sponsorship" in work_auth
            else work_auth.get("sponsorship_required")
        )
        if sponsorship is True:
            return True

    if re.search(r"\b(?:work authori[sz]ation|right to work)\b", normalized):
        for key in ("authorized_to_work", "work_authorized", "legally_authorized_to_work"):
            authorized = _confirmed_bool(work_auth.get(key))
            if authorized is False:
                return True
            if authorized is True:
                break
    return False


def evaluate_job_eligibility(
    job: dict, profile: Mapping[str, object] | None = None
) -> tuple[str, str | None]:
    """Return an auditable eligibility status without inferring soft criteria."""
    text = "\n".join(
        str(job.get(field) or "")
        for field in ("title", "description", "full_description")
    )
    confirmed_profile = profile if isinstance(profile, Mapping) else {}
    for pattern in _EXPLICIT_DO_NOT_APPLY_PATTERNS:
        match = pattern.search(text)
        if match and _explicit_exclusion_conflicts(match.group(0), confirmed_profile):
            start = max(0, match.start() - 120)
            end = min(len(text), match.end() + 120)
            evidence = " ".join(text[start:end].split())[:240]
            return "ineligible", f"Explicit do-not-apply instruction: {evidence}"
    return "eligible", None


def refresh_job_eligibility(
    conn: sqlite3.Connection,
    profile: Mapping[str, object] | None = None,
    *,
    profile_revision: str | None = None,
    policy_revision: str = ELIGIBILITY_POLICY_REVISION,
) -> dict[str, int]:
    """Incrementally refresh eligibility using durable input revisions.

    Lightweight database triggers invalidate only rows whose eligibility input
    changed.  A profile or policy revision change deliberately re-evaluates all
    rows.  This keeps SQLite as the source of truth without rescanning every job
    before each application acquisition.
    """
    if profile is None:
        try:
            from applypilot.config import load_profile

            profile = load_profile()
        except (FileNotFoundError, TypeError, ValueError):
            profile = {}
    _required_revision = str(policy_revision or "").strip()
    if not _required_revision:
        raise ValueError("policy_revision is required")
    if profile_revision is None:
        profile_json = json.dumps(profile, sort_keys=True, separators=(",", ":"), default=str)
        profile_revision = hashlib.sha256(profile_json.encode("utf-8")).hexdigest()
    else:
        profile_revision = str(profile_revision).strip()
        if not profile_revision:
            raise ValueError("profile_revision is required")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS job_eligibility_cache (
            url                 TEXT PRIMARY KEY,
            input_fingerprint   TEXT NOT NULL,
            profile_revision    TEXT NOT NULL,
            policy_revision     TEXT NOT NULL,
            evaluated_at        TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS invalidate_job_eligibility_after_insert
        AFTER INSERT ON jobs
        BEGIN
            DELETE FROM job_eligibility_cache WHERE url = NEW.url;
        END;
        CREATE TRIGGER IF NOT EXISTS invalidate_job_eligibility_after_update
        AFTER UPDATE OF title, description, full_description ON jobs
        BEGIN
            DELETE FROM job_eligibility_cache WHERE url = NEW.url;
        END;
        CREATE TRIGGER IF NOT EXISTS invalidate_job_eligibility_after_delete
        AFTER DELETE ON jobs
        BEGIN
            DELETE FROM job_eligibility_cache WHERE url = OLD.url;
        END;
        """
    )
    rows = conn.execute(
        "SELECT j.url, j.title, j.description, j.full_description, "
        "j.eligibility_status, j.eligibility_reason "
        "FROM jobs AS j LEFT JOIN job_eligibility_cache AS c ON c.url = j.url "
        "WHERE c.url IS NULL OR c.profile_revision != ? OR c.policy_revision != ?",
        (profile_revision, _required_revision),
    ).fetchall()
    now = datetime.now(UTC).isoformat()
    counts = {"eligible": 0, "ineligible": 0, "changed": 0, "evaluated": 0, "skipped": 0}
    for row in rows:
        job = dict(row)
        status, reason = evaluate_job_eligibility(job, profile=profile)
        counts["evaluated"] += 1
        if job.get("eligibility_status") != status or job.get("eligibility_reason") != reason:
            conn.execute(
                "UPDATE jobs SET eligibility_status = ?, eligibility_reason = ?, "
                "eligibility_evaluated_at = ? WHERE url = ?",
                (status, reason, now, job["url"]),
            )
            counts["changed"] += 1
        content = "\0".join(
            str(job.get(field) or "") for field in ("title", "description", "full_description")
        )
        fingerprint = hashlib.sha256(content.encode("utf-8")).hexdigest()
        conn.execute(
            "INSERT INTO job_eligibility_cache "
            "(url, input_fingerprint, profile_revision, policy_revision, evaluated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(url) DO UPDATE SET input_fingerprint=excluded.input_fingerprint, "
            "profile_revision=excluded.profile_revision, policy_revision=excluded.policy_revision, "
            "evaluated_at=excluded.evaluated_at",
            (job["url"], fingerprint, profile_revision, _required_revision, now),
        )
    status_counts = dict(
        conn.execute(
            "SELECT COALESCE(eligibility_status, 'eligible'), COUNT(*) FROM jobs GROUP BY 1"
        ).fetchall()
    )
    counts["eligible"] = int(status_counts.get("eligible", 0))
    counts["ineligible"] = int(status_counts.get("ineligible", 0))
    counts["skipped"] = max(0, sum(status_counts.values()) - counts["evaluated"])
    conn.commit()
    return counts
