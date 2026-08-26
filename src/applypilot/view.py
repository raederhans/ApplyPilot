"""Generate the local ApplyPilot opportunity workbench.

The database queries remain the read-only adapter for the existing product
contracts. Presentation and interaction live in the packaged frontend template
so the browser workspace can evolve without coupling UI markup to SQLite code.
"""

from __future__ import annotations

import json
import webbrowser
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any

from rich.console import Console

from applypilot.config import APP_DIR
from applypilot.database import init_db
from applypilot.eligibility import ELIGIBLE_SQL, refresh_job_eligibility

console = Console()
DATA_PLACEHOLDER = "__APPLYPILOT_DASHBOARD_DATA__"


def collect_dashboard_data() -> dict[str, Any]:
    """Read the existing opportunity contracts into a frontend-safe model."""
    conn = init_db()
    refresh_job_eligibility(conn)

    total = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {ELIGIBLE_SQL}").fetchone()[0]
    ready = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        f"WHERE full_description IS NOT NULL AND application_url IS NOT NULL AND {ELIGIBLE_SQL}"
    ).fetchone()[0]
    scored = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL AND {ELIGIBLE_SQL}"
    ).fetchone()[0]
    high_fit = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE fit_score >= 7 AND {ELIGIBLE_SQL}"
    ).fetchone()[0]

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
               fit_score, score_reasoning, cover_letter_status
        FROM jobs
        WHERE fit_score >= 5 AND {ELIGIBLE_SQL}
        ORDER BY fit_score DESC, company_name, title
    """).fetchall()
    jobs: list[dict[str, Any]] = []
    for row in job_rows:
        reasoning_lines = (row["score_reasoning"] or "").splitlines()
        jobs.append(
            {
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
        )

    return {
        "stats": {"total": int(total), "ready": int(ready), "scored": int(scored), "highFit": int(high_fit)},
        "scoreDistribution": score_distribution,
        "sources": sources,
        "jobs": jobs,
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
    html = render_dashboard(collect_dashboard_data())
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
