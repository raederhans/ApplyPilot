"""Deterministic hard-eligibility screening for discovered jobs."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime

ELIGIBLE_SQL = "COALESCE(eligibility_status, 'eligible') != 'ineligible'"

_CITIZEN_OR_PR_PATTERNS = (
    re.compile(
        r"(?im)^\s*[-*•+]?\s*(?:eligibility\s*:\s*)?"
        r"singapore citizens?(?:\s+(?:or|and)\s+(?:singapore\s+)?"
        r"(?:permanent residents?|prs?))?\s*[.;]?$"
    ),
    re.compile(
        r"(?im)^\s*[-*•+]?\s*(?:eligibility\s*:\s*)?"
        r"(?:singapore\s+)?(?:permanent residents?|pr)\s*[.;]?$"
    ),
    re.compile(
        r"(?i)\b(?:applicants?|candidates?|you)\s+(?:must|need to)\s+be\s+"
        r"(?:a\s+)?singapore citizens?\b"
    ),
    re.compile(r"(?i)\bmust\s+be\s+(?:a\s+)?singapore citizens?\b"),
    re.compile(
        r"(?i)\b(?:only|exclusively)\s+(?:open|available)\s+to\s+"
        r"(?:singapore\s+)?(?:citizens?|permanent residents?|prs?)\b"
    ),
    re.compile(
        r"(?i)\b(?:singapore\s+)?(?:citizenship|permanent residency|pr status)\s+"
        r"(?:is\s+)?(?:mandatory|required)\b"
    ),
    re.compile(
        r"(?i)\b(?:singapore citizens?|singaporeans?|permanent residents?|prs?)\s+only\b"
    ),
    re.compile(
        r"(?i)\bonly\s+(?:singapore\s+)?(?:citizens?|singaporeans?|permanent residents?|prs?)"
        r"(?:\s+(?:may|can)\s+apply)?\b"
    ),
    re.compile(
        r"(?i)\b(?:open to|restricted to|eligible applicants? (?:are|must be))\s+"
        r"(?:singapore\s+)?(?:citizens?|permanent residents?|prs?)"
        r"(?:\s+(?:or|and)\s+(?:singapore\s+)?(?:citizens?|permanent residents?|prs?))?\b"
    ),
)


def evaluate_job_eligibility(job: dict) -> tuple[str, str | None]:
    """Return an auditable eligibility status without inferring soft criteria."""
    text = "\n".join(
        str(job.get(field) or "")
        for field in ("title", "description", "full_description")
    )
    for pattern in _CITIZEN_OR_PR_PATTERNS:
        match = pattern.search(text)
        if match:
            evidence = " ".join(match.group(0).split())[:240]
            return "ineligible", f"Hard Singapore citizen/PR requirement: {evidence}"
    return "eligible", None


def refresh_job_eligibility(conn: sqlite3.Connection) -> dict[str, int]:
    """Evaluate all jobs and persist only changed eligibility decisions."""
    rows = conn.execute(
        "SELECT url, title, description, full_description, eligibility_status, eligibility_reason "
        "FROM jobs"
    ).fetchall()
    now = datetime.now(UTC).isoformat()
    counts = {"eligible": 0, "ineligible": 0, "changed": 0}
    for row in rows:
        job = dict(row)
        status, reason = evaluate_job_eligibility(job)
        counts[status] += 1
        if job.get("eligibility_status") != status or job.get("eligibility_reason") != reason:
            conn.execute(
                "UPDATE jobs SET eligibility_status = ?, eligibility_reason = ?, "
                "eligibility_evaluated_at = ? WHERE url = ?",
                (status, reason, now, job["url"]),
            )
            counts["changed"] += 1
    if counts["changed"]:
        conn.commit()
    return counts
