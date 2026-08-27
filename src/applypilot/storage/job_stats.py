"""Read-only pipeline statistics over an explicit database connection."""

from __future__ import annotations

import sqlite3


def get_stats(conn: sqlite3.Connection) -> dict:
    """Return job counts by pipeline stage.

    Provides a snapshot of how many jobs are at each stage, useful for
    dashboard display and pipeline progress tracking.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        Dictionary with keys:
            total, by_site, pending_detail, with_description,
            scored, unscored, tailored, untailored_eligible,
            with_cover_letter, applied, score_distribution
    """
    from applypilot.eligibility import ELIGIBLE_SQL, refresh_job_eligibility
    refresh_job_eligibility(conn)

    stats: dict = {}

    # Total jobs
    stats["total"] = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE {ELIGIBLE_SQL}"
    ).fetchone()[0]
    stats["excluded_ineligible"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE eligibility_status = 'ineligible'"
    ).fetchone()[0]

    # By site breakdown
    rows = conn.execute(
        f"SELECT COALESCE(source_site, site), COUNT(*) as cnt FROM jobs WHERE {ELIGIBLE_SQL} "
        "GROUP BY COALESCE(source_site, site) ORDER BY cnt DESC"
    ).fetchall()
    stats["by_site"] = [(row[0], row[1]) for row in rows]

    # Enrichment stage
    stats["pending_detail"] = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL AND {ELIGIBLE_SQL}"
    ).fetchone()[0]

    stats["with_description"] = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL AND {ELIGIBLE_SQL}"
    ).fetchone()[0]

    stats["detail_errors"] = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE detail_error IS NOT NULL AND {ELIGIBLE_SQL}"
    ).fetchone()[0]

    # Scoring stage
    stats["scored"] = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL AND {ELIGIBLE_SQL}"
    ).fetchone()[0]

    stats["unscored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        f"WHERE full_description IS NOT NULL AND fit_score IS NULL AND {ELIGIBLE_SQL}"
    ).fetchone()[0]

    # Score distribution
    dist_rows = conn.execute(
        "SELECT fit_score, COUNT(*) as cnt FROM jobs "
        f"WHERE fit_score IS NOT NULL AND {ELIGIBLE_SQL} "
        "GROUP BY fit_score ORDER BY fit_score DESC"
    ).fetchall()
    stats["score_distribution"] = [(row[0], row[1]) for row in dist_rows]

    # Tailoring stage
    stats["tailored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL "
        f"AND tailor_status='machine_validated' AND {ELIGIBLE_SQL}"
    ).fetchone()[0]

    stats["untailored_eligible"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE fit_score >= 7 AND full_description IS NOT NULL "
        f"AND tailored_resume_path IS NULL AND {ELIGIBLE_SQL}"
    ).fetchone()[0]

    stats["tailor_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(tailor_attempts, 0) >= 5 "
        f"AND tailored_resume_path IS NULL AND {ELIGIBLE_SQL}"
    ).fetchone()[0]

    # Cover letter stage
    stats["with_cover_letter"] = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE cover_letter_path IS NOT NULL AND {ELIGIBLE_SQL}"
    ).fetchone()[0]

    stats["cover_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(cover_attempts, 0) >= 5 "
        f"AND (cover_letter_path IS NULL OR cover_letter_path = '') AND {ELIGIBLE_SQL}"
    ).fetchone()[0]

    # Application stage
    stats["applied"] = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL AND {ELIGIBLE_SQL}"
    ).fetchone()[0]

    stats["apply_errors"] = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE apply_error IS NOT NULL AND {ELIGIBLE_SQL}"
    ).fetchone()[0]

    stats["ready_to_apply"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE tailored_resume_path IS NOT NULL "
        "AND tailor_status = 'machine_validated' "
        "AND ((cover_letter_path IS NOT NULL AND cover_letter_status = 'human_approved') "
        "OR cover_letter_status = 'not_required') "
        "AND applied_at IS NULL "
        f"AND {ELIGIBLE_SQL}"
    ).fetchone()[0]

    return stats
