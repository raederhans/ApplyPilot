"""Read-only candidate selection, separate from admission and atomic claiming."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class JobCandidate:
    """Persisted snapshot plus a separate, advisory ranked projection."""

    stored: Mapping[str, object]
    job: dict

    def still_current(self, connection: sqlite3.Connection) -> bool:
        """Compare the exact stored row while the caller owns the write lock."""
        if not connection.in_transaction:
            raise RuntimeError("candidate revalidation requires a claim transaction")
        row = connection.execute(
            "SELECT * FROM jobs WHERE url=?", (self.stored["url"],)
        ).fetchone()
        return row is not None and dict(row) == dict(self.stored)


def select_candidates(
    connection: sqlite3.Connection,
    *,
    target_url: str | None,
    min_score: int,
    max_apply_attempts: int,
    preview_only: bool,
    allow_runtime_cover: bool,
    excluded: set[str],
    blocked_sites: list[str],
    blocked_patterns: list[str],
) -> list[JobCandidate]:
    """Keep the existing exact-job and broad-search predicates and ranking.

    Do not limit by raw fit before company-priority ranking. Ranking is a
    read-time advisory snapshot; it never grants authority to claim or submit.
    """
    from applypilot.eligibility import ELIGIBLE_SQL

    clauses = [
        "tailored_resume_path IS NOT NULL",
        "tailor_status = 'machine_validated'",
        "(apply_status IS NULL OR apply_status IN ('failed', 'previewed'))",
        "(apply_attempts IS NULL OR apply_attempts < ?)",
        "fit_score >= ?",
        ELIGIBLE_SQL,
    ]
    params: list[object] = [
        max_apply_attempts,
        max(1, min(int(min_score), 10)) if target_url else min_score,
    ]
    if not allow_runtime_cover and not (target_url and preview_only):
        clauses.append(
            "((cover_letter_path IS NOT NULL AND cover_letter_status IN "
            "('human_approved', 'agent_validated')) OR cover_letter_status = 'not_required')"
        )
    if target_url:
        clauses.append("(url = ? OR application_url = ?)")
        params.extend((target_url, target_url))
    else:
        if blocked_sites:
            placeholders = ",".join("?" for _ in blocked_sites)
            clauses.append(f"site NOT IN ({placeholders})")
            params.extend(blocked_sites)
        clauses.extend("url NOT LIKE ?" for _ in blocked_patterns)
        params.extend(blocked_patterns)
    if excluded:
        placeholders = ",".join("?" for _ in excluded)
        clauses.append(f"url NOT IN ({placeholders})")
        params.extend(sorted(excluded))
    order = "" if target_url else " ORDER BY fit_score DESC, url"
    rows = connection.execute(
        "SELECT * FROM jobs WHERE " + " AND ".join(f"({clause})" for clause in clauses) + order,
        params,
    ).fetchall()
    snapshots = {str(row["url"]): dict(row) for row in rows}
    jobs = [dict(row) for row in rows]
    if not target_url:
        from applypilot.discovery.company_priority import rank_with_company_priority
        from applypilot.discovery.diversity import recent_handled_companies

        jobs = rank_with_company_priority(
            connection, jobs, recent_companies=recent_handled_companies(connection)
        )
    candidates = []
    for job in jobs:
        job["application_url"] = job.get("application_url") or job.get("url")
        candidates.append(JobCandidate(MappingProxyType(snapshots[str(job["url"])]), job))
    return candidates
