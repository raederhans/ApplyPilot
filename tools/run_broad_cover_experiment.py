"""Run an isolated real-network ApplyPilot cover-letter experiment.

The caller must point APPLYPILOT_DIR at a disposable directory containing a
copied profile/resume/search configuration. This script never launches the
application browser and never submits a job application.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import UTC, datetime

from applypilot import config
from applypilot.database import get_connection, init_db
from applypilot.discovery.jobspy import run_discovery
from applypilot.eligibility import ELIGIBLE_SQL, refresh_job_eligibility
from applypilot.scoring.cover_letter import run_cover_letters
from applypilot.scoring.scorer import score_job
from applypilot.scoring.tailor import run_tailoring

AI_INTERN_TITLE = re.compile(
    r"\b(intern|internship|trainee|co-?op)\b.*"
    r"(ai|artificial intelligence|machine learning|data science|llm|nlp|"
    r"computer vision|generative|automation)|"
    r"(ai|artificial intelligence|machine learning|data science|llm|nlp|"
    r"computer vision|generative|automation).*\b(intern|internship|trainee|co-?op)\b",
    re.IGNORECASE,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours-old", type=int, default=168)
    parser.add_argument("--results-per-site", type=int, default=50)
    parser.add_argument("--score-limit", type=int, default=30)
    parser.add_argument("--tailor-limit", type=int, default=6)
    parser.add_argument("--cover-limit", type=int, default=6)
    parser.add_argument("--min-score", type=int, default=7)
    parser.add_argument("--skip-discovery", action="store_true")
    parser.add_argument("--skip-materials", action="store_true")
    parser.add_argument("--seed-selected-resume-for-cover", action="store_true")
    parser.add_argument("--skip-tailor", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if "data-experiments" not in str(config.APP_DIR).casefold():
        raise RuntimeError(
            "Refusing broad experiment because APPLYPILOT_DIR is not inside a data-experiments directory."
        )
    config.load_env()
    config.ensure_dirs()
    init_db()
    started = datetime.now(UTC)
    started_perf = time.perf_counter()

    queries = [
        "AI intern",
        "artificial intelligence intern",
        "AI engineer intern",
        "AI research intern",
        "AI solutions intern",
        "AI product intern",
        "generative AI intern",
        "LLM intern",
        "machine learning intern",
        "data science intern",
        "NLP intern",
        "computer vision intern",
        "automation intern",
    ]
    scan_config = {
        "queries": [
            {"query": query, "tier": 1 if index < 6 else 2}
            for index, query in enumerate(queries)
        ],
        "locations": [{"location": "Singapore", "remote": False}],
        "country": "Singapore",
        # Google Jobs timed out in the first real run. Keep the retry bounded
        # and continue the broad workflow on the two sources that returned.
        "boards": ["indeed", "linkedin"],
        "defaults": {
            "results_per_site": args.results_per_site,
            "hours_old": args.hours_old,
            "query_timeout_seconds": 90,
            "max_retries": 0,
        },
    }
    discovery = {"skipped": True, "reason": "reusing an isolated completed scan"}
    if not args.skip_discovery:
        discovery = run_discovery(scan_config)

    conn = get_connection()
    refresh_job_eligibility(conn)
    candidates = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM jobs WHERE full_description IS NOT NULL "
            "AND company_name IS NOT NULL AND company_name != '' "
            "AND fit_score IS NULL AND COALESCE(score_attempts, 0) < 2 "
            f"AND {ELIGIBLE_SQL} ORDER BY discovered_at DESC, url"
        ).fetchall()
        if AI_INTERN_TITLE.search(row["title"] or "")
    ][: args.score_limit]

    resume_text = config.RESUME_PATH.read_text(encoding="utf-8")
    score_errors: list[dict] = []
    score_rows: list[dict] = []
    for job in candidates:
        result = score_job(resume_text, job)
        now = datetime.now(UTC).isoformat()
        if result["score"] == 0:
            score_errors.append({"url": job["url"], "title": job["title"], "error": result["reasoning"]})
            conn.execute(
                "UPDATE jobs SET fit_score=NULL, scored_at=NULL, score_status='failed', "
                "score_error=?, score_attempts=COALESCE(score_attempts,0)+1 WHERE url=?",
                (result["reasoning"], job["url"]),
            )
        else:
            score_rows.append({
                "url": job["url"],
                "title": job["title"],
                "company": job["company_name"],
                **result,
            })
            conn.execute(
                "UPDATE jobs SET fit_score=?, score_reasoning=?, scored_at=?, "
                "score_status='scored', score_error=NULL, "
                "score_attempts=COALESCE(score_attempts,0)+1 WHERE url=?",
                (
                    result["score"],
                    f"{result['keywords']}\n{result['reasoning']}",
                    now,
                    job["url"],
                ),
            )
    conn.commit()

    tailoring = {"skipped": True}
    covers = {"skipped": True}
    if not args.skip_materials:
        if not args.skip_tailor:
            tailoring = run_tailoring(
                min_score=args.min_score,
                limit=args.tailor_limit,
                validation_mode="strict",
            )
        if args.seed_selected_resume_for_cover:
            seed_rows = conn.execute(
                "SELECT url FROM jobs WHERE fit_score >= ? AND company_name IS NOT NULL "
                "AND company_name != '' AND tailored_resume_path IS NULL "
                f"AND {ELIGIBLE_SQL} ORDER BY fit_score DESC, company_name, title LIMIT ?",
                (args.min_score, args.cover_limit),
            ).fetchall()
            for row in seed_rows:
                conn.execute(
                    "UPDATE jobs SET tailored_resume_path=?, tailored_at=? WHERE url=?",
                    (str(config.RESUME_PATH.resolve()), datetime.now(UTC).isoformat(), row["url"]),
                )
            conn.commit()
            tailoring["selected_resume_seeded_for_cover"] = len(seed_rows)
            tailoring["seed_note"] = (
                "These jobs use the explicit selected AI resume as the cover source; "
                "they are not counted as successful LLM tailoring."
            )
        covers = run_cover_letters(
            min_score=args.min_score,
            limit=args.cover_limit,
            validation_mode="strict",
        )

    final_rows = [
        dict(row)
        for row in conn.execute(
            "SELECT url, title, company_name, source_site, fit_score, score_status, score_error, "
            "tailored_resume_path, cover_letter_path, cover_letter_status, cover_letter_error, "
            "cover_letter_source_resume_path FROM jobs "
            "WHERE fit_score IS NOT NULL OR score_status='failed' "
            "ORDER BY fit_score DESC, company_name, title"
        ).fetchall()
    ]
    source_counts = {
        str(row["source"] or "unknown"): int(row["count"])
        for row in conn.execute(
            "SELECT COALESCE(source_site, site) AS source, COUNT(*) AS count "
            "FROM jobs GROUP BY COALESCE(source_site, site) ORDER BY count DESC"
        ).fetchall()
    }
    eligible_ai_titles = [
        row["title"] or ""
        for row in conn.execute(
            "SELECT title FROM jobs WHERE full_description IS NOT NULL "
            f"AND {ELIGIBLE_SQL}"
        ).fetchall()
        if AI_INTERN_TITLE.search(row["title"] or "")
    ]
    database_summary = {
        "raw_jobs": int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]),
        "eligible_jobs": int(
            conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {ELIGIBLE_SQL}").fetchone()[0]
        ),
        "excluded_ineligible": int(
            conn.execute("SELECT COUNT(*) FROM jobs WHERE eligibility_status='ineligible'").fetchone()[0]
        ),
        "eligible_ai_intern_titles": len(eligible_ai_titles),
        "sources": source_counts,
        "scored": int(conn.execute("SELECT COUNT(*) FROM jobs WHERE score_status='scored'").fetchone()[0]),
        "score_failed": int(conn.execute("SELECT COUNT(*) FROM jobs WHERE score_status='failed'").fetchone()[0]),
        "cover_machine_validated": int(
            conn.execute("SELECT COUNT(*) FROM jobs WHERE cover_letter_status='machine_validated'").fetchone()[0]
        ),
        "cover_failed_validation": int(
            conn.execute("SELECT COUNT(*) FROM jobs WHERE cover_letter_status='failed_validation'").fetchone()[0]
        ),
        "cover_human_approved": int(
            conn.execute("SELECT COUNT(*) FROM jobs WHERE cover_letter_status='human_approved'").fetchone()[0]
        ),
        "applied": int(conn.execute("SELECT COUNT(*) FROM jobs WHERE apply_status='applied'").fetchone()[0]),
    }
    quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
    report = {
        "scope": {
            "location": "Singapore",
            "hours_old": args.hours_old,
            "boards": scan_config["boards"],
            "queries": queries,
            "results_per_site": args.results_per_site,
            "score_limit": args.score_limit,
            "tailor_limit": args.tailor_limit,
            "cover_limit": args.cover_limit,
            "min_score": args.min_score,
        },
        "safety": {
            "isolated_app_dir": str(config.APP_DIR),
            "browser_apply_launched": False,
            "submission_attempted": False,
            "human_approval_required": True,
        },
        "started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "elapsed_seconds": round(time.perf_counter() - started_perf, 2),
        "discovery": discovery,
        "title_confirmed_candidates": len(candidates),
        "scored_successfully": len(score_rows),
        "score_errors": score_errors,
        "score_results": score_rows,
        "tailoring": tailoring,
        "covers": covers,
        "database_summary": database_summary,
        "final_rows": final_rows,
        "database_quick_check": quick_check,
    }
    report_path = config.LOG_DIR / f"broad-cover-experiment-{started.strftime('%Y%m%d-%H%M%S')}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "report_path": str(report_path),
        "discovery": discovery,
        "title_confirmed_candidates": len(candidates),
        "scored_successfully": len(score_rows),
        "score_errors": len(score_errors),
        "tailoring": tailoring,
        "covers": covers,
        "database_quick_check": quick_check,
    }, ensure_ascii=False))
    return 0 if quick_check == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
