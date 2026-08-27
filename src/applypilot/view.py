"""Generate the local ApplyPilot opportunity workbench.

The database queries remain the read-only adapter for the existing product
contracts. Presentation and interaction live in the packaged frontend template
so the browser workspace can evolve without coupling UI markup to SQLite code.
"""

from __future__ import annotations

import json
import sqlite3
import webbrowser
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any

from rich.console import Console

from applypilot.apply.authorization import compute_job_fingerprint
from applypilot.config import APP_DIR, DB_PATH
from applypilot.eligibility import ELIGIBLE_SQL
from applypilot.frontend.contracts import build_prepare_job, build_prepare_summary

console = Console()
DATA_PLACEHOLDER = "__APPLYPILOT_DASHBOARD_DATA__"


def _system_state(
    state: str,
    title: str,
    message: str,
    *commands: tuple[str, str],
    detail: str = "",
) -> dict[str, Any]:
    return {
        "state": state,
        "title": title,
        "message": message,
        "actions": [{"label": label, "command": command} for label, command in commands],
        "detail": detail,
    }


def _empty_dashboard(system: dict[str, Any]) -> dict[str, Any]:
    prepare = build_prepare_summary([], library_available=False)
    if system.get("state") == "error":
        prepare["system"] = system
    return {
        "system": system,
        "stats": {"total": 0, "ready": 0, "scored": 0, "highFit": 0},
        "scoreDistribution": {},
        "sources": [],
        "jobs": [],
        "prepare": prepare,
    }


def _read_only_connection(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _prepare_assignments(
    conn: sqlite3.Connection, jobs: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Load current persisted resume routes in one bounded, read-only query."""
    tables = {
        str(row["name"])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('job_resume_assignments', 'resume_artifacts')"
        ).fetchall()
    }
    if tables != {"job_resume_assignments", "resume_artifacts"}:
        return {}, False

    rows = conn.execute(f"""
        SELECT assignment.job_url, assignment.job_fingerprint,
               assignment.decision, assignment.required_coverage,
               assignment.overall_score, assignment.runner_up_margin,
               assignment.hard_gaps_json, assignment.reason, assignment.recorded_at,
               artifact.kind AS artifact_kind, artifact.track AS artifact_track,
               artifact.pdf_path AS artifact_pdf_path,
               artifact.pdf_sha256 AS artifact_pdf_sha256,
               artifact.pdf_size AS artifact_pdf_size,
               artifact.validation_status AS artifact_validation_status,
               artifact.validation_report_path AS artifact_validation_report_path,
               artifact.validated_at AS artifact_validated_at
        FROM job_resume_assignments AS assignment
        LEFT JOIN resume_artifacts AS artifact
          ON artifact.artifact_id = assignment.artifact_id
        WHERE assignment.job_url IN (
            SELECT url FROM jobs WHERE fit_score >= 5 AND {ELIGIBLE_SQL}
        )
        ORDER BY assignment.recorded_at DESC, assignment.assignment_id DESC
    """).fetchall()
    expected = {job["url"]: compute_job_fingerprint(job) for job in jobs}
    latest: dict[str, dict[str, Any]] = {}
    current: dict[str, dict[str, Any]] = {}
    for row in rows:
        record = dict(row)
        url = str(record["job_url"])
        latest.setdefault(url, record)
        if url not in current and record["job_fingerprint"] == expected.get(url):
            current[url] = record
    return {url: current.get(url, record) for url, record in latest.items()}, True


def collect_dashboard_data(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Read existing opportunity contracts without changing workspace state."""
    owns_connection = conn is None
    if conn is None:
        if not DB_PATH.is_file():
            return _empty_dashboard(
                _system_state(
                    "empty",
                    "No opportunities yet",
                    "Collect current openings, then enrich and score them to build your shortlist.",
                    ("Collect openings", "applypilot radar collect"),
                    ("Build shortlist", "applypilot run discover enrich score"),
                )
            )
        conn = _read_only_connection(DB_PATH)
    else:
        conn.row_factory = sqlite3.Row

    try:
        total = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {ELIGIBLE_SQL}").fetchone()[0]
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs "
            f"WHERE full_description IS NOT NULL AND application_url IS NOT NULL AND {ELIGIBLE_SQL}"
        ).fetchone()[0]
        scored = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL AND {ELIGIBLE_SQL}").fetchone()[0]
        high_fit = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE fit_score >= 7 AND {ELIGIBLE_SQL}").fetchone()[0]

        score_rows = conn.execute(
            "SELECT fit_score, COUNT(*) AS count FROM jobs "
            f"WHERE fit_score IS NOT NULL AND {ELIGIBLE_SQL} "
            "GROUP BY fit_score ORDER BY fit_score DESC"
        ).fetchall()
        score_distribution = {str(int(row["fit_score"])): int(row["count"]) for row in score_rows}

        source_rows = conn.execute(f"""
            SELECT COALESCE(source_site, site) AS source_site,
                   COUNT(*) AS total,
                   SUM(CASE WHEN fit_score >= 7 THEN 1 ELSE 0 END) AS high_fit,
                   SUM(CASE WHEN fit_score BETWEEN 5 AND 6 THEN 1 ELSE 0 END) AS mid_fit,
                   SUM(CASE WHEN fit_score < 5 AND fit_score IS NOT NULL THEN 1 ELSE 0 END) AS low_fit,
                   SUM(CASE WHEN fit_score IS NULL THEN 1 ELSE 0 END) AS unscored,
                   ROUND(AVG(fit_score), 1) AS avg_score
            FROM jobs WHERE {ELIGIBLE_SQL}
            GROUP BY COALESCE(source_site, site)
            ORDER BY high_fit DESC, total DESC
        """).fetchall()
        sources = [
            {
                "name": row["source_site"] or "Unknown source",
                "total": int(row["total"] or 0),
                "highFit": int(row["high_fit"] or 0),
                "midFit": int(row["mid_fit"] or 0),
                "lowFit": int(row["low_fit"] or 0),
                "unscored": int(row["unscored"] or 0),
                "averageScore": float(row["avg_score"] or 0),
            }
            for row in source_rows
        ]

        job_rows = conn.execute(f"""
            SELECT url, title, salary, description, location, company_name,
                   COALESCE(source_site, site) AS source_site, strategy,
                   full_description, application_url, detail_error,
                   fit_score, score_reasoning, eligibility_status, eligibility_reason,
                   dedupe_status, possible_repost_of,
                   tailored_resume_path, tailored_at, tailor_status,
                   cover_letter_path, cover_letter_at, cover_letter_status,
                   cover_letter_approved_at,
                   applied_at, apply_status, apply_error, apply_retry_blocked,
                   apply_retry_reason, unanswered_questions_json,
                   application_readiness_status, application_readiness_reason,
                   application_readiness_reviewed_at, application_readiness_fingerprint
            FROM jobs
            WHERE fit_score >= 5 AND {ELIGIBLE_SQL}
            ORDER BY fit_score DESC, company_name, title
        """).fetchall()
        jobs: list[dict[str, Any]] = []
        contract_jobs: list[dict[str, Any]] = []
        for row in job_rows:
            reasoning_lines = (row["score_reasoning"] or "").splitlines()
            contract_job = dict(row)
            contract_jobs.append(contract_job)
            job = {
                "url": row["url"] or "",
                "title": row["title"] or "Untitled opportunity",
                "salary": row["salary"] or "",
                "location": row["location"] or "",
                "company": row["company_name"] or "Unknown employer",
                "source": row["source_site"] or "Unknown source",
                "strategy": row["strategy"] or "",
                "description": row["full_description"] or "",
                "applicationUrl": row["application_url"] or "",
                "detailError": row["detail_error"] or "",
                "score": int(row["fit_score"] or 0),
                "keywords": reasoning_lines[0][:160] if reasoning_lines else "",
                "reasoning": reasoning_lines[1][:320] if len(reasoning_lines) > 1 else "",
                "coverLetterStatus": row["cover_letter_status"] or "",
            }
            jobs.append(job)
        assignments, library_available = _prepare_assignments(conn, contract_jobs)
        for job, contract_job in zip(jobs, contract_jobs, strict=True):
            job["prepare"] = build_prepare_job(contract_job, assignments.get(contract_job["url"]))
    finally:
        if owns_connection:
            conn.close()

    if total == 0:
        system = _system_state(
            "empty",
            "No opportunities yet",
            "Collect current openings, then enrich and score them to build your shortlist.",
            ("Collect openings", "applypilot radar collect"),
            ("Build shortlist", "applypilot run discover enrich score"),
        )
    elif scored < total:
        system = _system_state(
            "needs_scoring",
            f"{total - scored} opportunities still need fit evidence",
            "Continue enrichment and scoring while reviewing the evidence already available.",
            ("Enrich and score", "applypilot run enrich score"),
        )
    elif not jobs:
        system = _system_state(
            "no_shortlist",
            "No role currently meets the 5+ shortlist threshold",
            "The evidence is available, but none of the scored roles belongs in the ranked queue yet.",
        )
    else:
        system = _system_state(
            "ready",
            "Workspace ready",
            "The ranked queue reflects the latest persisted local evidence.",
        )

    prepare = build_prepare_summary(jobs, library_available=library_available)
    return {
        "system": system,
        "stats": {"total": int(total), "ready": int(ready), "scored": int(scored), "highFit": int(high_fit)},
        "scoreDistribution": score_distribution,
        "sources": sources,
        "jobs": jobs,
        "prepare": prepare,
    }


def render_dashboard(data: dict[str, Any]) -> str:
    """Render a self-contained workbench without allowing script injection."""
    template = resource_files("applypilot.frontend").joinpath("dashboard.html").read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    if template.count(DATA_PLACEHOLDER) != 1:
        raise RuntimeError("Dashboard template must contain exactly one data placeholder")
    return template.replace(DATA_PLACEHOLDER, payload)


def generate_dashboard(output_path: str | None = None) -> str:
    """Generate the local HTML workbench and return its absolute path."""
    out = Path(output_path) if output_path else APP_DIR / "dashboard.html"
    try:
        data = collect_dashboard_data()
    except (OSError, sqlite3.DatabaseError) as exc:
        console.print("[yellow]Workspace data could not be read:[/yellow]", str(exc))
        data = _empty_dashboard(
            _system_state(
                "error",
                "Unable to read the local workspace",
                "Your data was not changed. Run the read-only diagnostic; ApplyPilot will not repair or replace the database automatically.",
                ("Run diagnostic", "applypilot doctor"),
                detail=f"{type(exc).__name__}: {exc}"[:240],
            )
        )
    html = render_dashboard(data)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    abs_path = str(out.resolve())
    console.print(f"[green]Opportunity workbench written to {abs_path}[/green]")
    return abs_path


def open_dashboard(output_path: str | None = None) -> None:
    """Generate the workbench and open it in the default browser."""
    path = generate_dashboard(output_path)
    console.print("[dim]Opening local workbench in browser...[/dim]")
    webbrowser.open(Path(path).as_uri())
