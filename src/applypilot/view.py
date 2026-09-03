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
from applypilot.frontend.contracts import (
    build_discover_item,
    build_discover_summary,
    build_prepare_job,
    build_prepare_summary,
    build_verify_job,
    build_verify_summary,
)

console = Console()
DATA_PLACEHOLDER = "__APPLYPILOT_DASHBOARD_DATA__"
DASHBOARD_ASSET_DIRECTORY = Path("assets") / "capypilot"
DASHBOARD_ASSET_NAMES = (
    "capypilot-lockup-light.png",
    "capypilot-mark-compact-master.png",
    "capypilot-mascot-companion.png",
    "favicon.ico",
    "favicon-16.png",
    "favicon-32.png",
    "favicon-48.png",
    "app-icon-192.png",
    "app-icon-512.png",
)
ACTIVE_APPLICATION_SQL = (
    "(apply_status IS NULL OR apply_status NOT IN ('applied', 'submission_uncertain')) "
    "AND COALESCE(apply_retry_blocked, 0) = 0"
)


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
    discover = build_discover_summary([], [], lineage_available=False)
    prepare = build_prepare_summary([], library_available=False)
    verify = build_verify_summary([], [], ledger_available=False)
    if system.get("state") == "error":
        discover["system"] = system
        prepare["system"] = system
        verify["system"] = system
    return {
        "system": system,
        "discover": {"items": [], "sources": [], **discover},
        "stats": {"total": 0, "ready": 0, "scored": 0, "highFit": 0},
        "scoreDistribution": {},
        "sources": [],
        "jobs": [],
        "prepare": prepare,
        "verify": verify,
    }


def _read_only_connection(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _discover_data(conn: sqlite3.Connection) -> dict[str, Any]:
    """Load bounded discovery lineage without initializing radar tables."""
    required_tables = {
        "radar_sources",
        "radar_fetch_runs",
        "radar_source_observations",
        "radar_leads",
        "radar_job_sources",
    }
    tables = {
        str(row["name"])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('radar_sources', 'radar_fetch_runs', "
            "'radar_source_observations', 'radar_leads', 'radar_job_sources')"
        ).fetchall()
    }
    lineage_available = tables == required_tables

    job_rows = conn.execute("""
        SELECT j.url, j.title, j.company_name, j.location,
               COALESCE(j.source_site, j.site) AS source,
               j.discovered_at AS first_seen_at,
               COALESCE(MAX(o.last_seen_at), j.last_seen_at, j.discovered_at) AS last_seen_at,
               MAX(o.published_at) AS published_at,
               j.fit_score, j.eligibility_status,
               COUNT(DISTINCT link.source_id) AS source_count,
               MAX(CASE WHEN o.verification_status IN
                    ('verified_official', 'official_target_open') THEN 1 ELSE 0 END)
                    AS verified_official
        FROM jobs AS j
        LEFT JOIN radar_job_sources AS link ON link.job_url = j.url
        LEFT JOIN radar_source_observations AS o
          ON o.observation_key = link.observation_key
        GROUP BY j.url
        ORDER BY COALESCE(MAX(o.published_at), MAX(o.last_seen_at),
                          j.last_seen_at, j.discovered_at) DESC,
                 j.company_name, j.title
    """).fetchall() if lineage_available else conn.execute("""
        SELECT url, title, company_name, location,
               COALESCE(source_site, site) AS source,
               discovered_at AS first_seen_at,
               COALESCE(last_seen_at, discovered_at) AS last_seen_at,
               NULL AS published_at, fit_score, eligibility_status,
               1 AS source_count, 0 AS verified_official
        FROM jobs
        ORDER BY COALESCE(last_seen_at, discovered_at) DESC, company_name, title
    """).fetchall()

    items = [build_discover_item({**dict(row), "kind": "listing", "url": row["url"]}) for row in job_rows]
    sources: list[dict[str, Any]] = []
    if lineage_available:
        lead_rows = conn.execute("""
            SELECT lead.title, COALESCE(source.company_name, observation.company_name) AS company_name,
                   lead.location, lead.source_url AS url,
                   COALESCE(source.company_name, source.provider, source.source_type) AS source,
                   source.provider, lead.first_seen_at, lead.last_seen_at,
                   observation.published_at, 1 AS source_count,
                   0 AS verified_official, NULL AS fit_score,
                   NULL AS eligibility_status
            FROM radar_leads AS lead
            JOIN radar_source_observations AS observation
              ON observation.observation_key = lead.observation_key
            LEFT JOIN radar_sources AS source ON source.source_id = observation.source_id
            WHERE COALESCE(lead.status, '') <> 'promoted'
            ORDER BY lead.last_seen_at DESC, lead.company_id, lead.title
        """).fetchall()
        items.extend(build_discover_item({**dict(row), "kind": "lead"}) for row in lead_rows)

        source_rows = conn.execute("""
            SELECT source.company_name, source.source_type, source.provider,
                   source.access_mode, source.priority_tier, source.active,
                   run.started_at, run.finished_at, run.status,
                   run.pagination_complete, run.normalized_count,
                   run.new_count, run.lead_count,
                   CASE WHEN COALESCE(run.error, '') = '' THEN 0 ELSE 1 END AS has_error
            FROM radar_sources AS source
            LEFT JOIN radar_fetch_runs AS run ON run.run_id = (
                SELECT latest.run_id FROM radar_fetch_runs AS latest
                WHERE latest.source_id = source.source_id
                ORDER BY latest.started_at DESC LIMIT 1
            )
            WHERE source.active = 1
            ORDER BY source.priority_tier, source.company_name, source.provider
        """).fetchall()
        sources = [
            {
                "name": row["company_name"] or row["provider"] or row["source_type"] or "Unnamed source",
                "type": row["source_type"] or "unknown",
                "provider": row["provider"] or "unknown",
                "accessMode": row["access_mode"] or "unknown",
                "priority": row["priority_tier"] or "",
                "status": row["status"] or "not_run",
                "paginationComplete": None
                if row["pagination_complete"] is None
                else bool(row["pagination_complete"]),
                "observed": int(row["normalized_count"] or 0),
                "new": int(row["new_count"] or 0),
                "leads": int(row["lead_count"] or 0),
                "lastRunAt": row["finished_at"] or row["started_at"] or "",
                "hasError": bool(row["has_error"]),
            }
            for row in source_rows
        ]

    items.sort(key=lambda item: str(item.get("publishedAt") or item.get("lastSeenAt") or ""), reverse=True)
    summary = build_discover_summary(items, sources, lineage_available=lineage_available)
    return {"items": items, "sources": sources, **summary}


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


def _verify_data(conn: sqlite3.Connection) -> dict[str, Any]:
    """Load persisted application outcomes without reading manifest or receipt files."""
    ledger_available = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_batch_consumptions'"
        ).fetchone()
        is not None
    )
    ledgers = (
        [
            dict(row)
            for row in conn.execute(
                "SELECT batch_id, job_url, reserved_at, status, updated_at "
                "FROM application_batch_consumptions "
                "ORDER BY updated_at DESC, batch_id, job_url"
            ).fetchall()
        ]
        if ledger_available
        else []
    )
    ledger_clause = " OR url IN (SELECT job_url FROM application_batch_consumptions)" if ledger_available else ""
    rows = conn.execute(f"""
        SELECT url, title, company_name, location, fit_score,
               COALESCE(source_site, site) AS source_site,
               apply_status, applied_at, apply_attempts, apply_retry_blocked,
               last_attempted_at, verification_confidence,
               application_recorded_at, submission_observation_json,
               submission_observed_at
        FROM jobs
        WHERE apply_status IS NOT NULL OR applied_at IS NOT NULL
           OR application_recorded_at IS NOT NULL
           OR submission_observed_at IS NOT NULL{ledger_clause}
        ORDER BY COALESCE(application_recorded_at, submission_observed_at,
                          last_attempted_at, applied_at) DESC,
                 company_name, title
    """).fetchall()
    ledgers_by_url: dict[str, list[dict[str, Any]]] = {}
    for ledger in ledgers:
        ledgers_by_url.setdefault(str(ledger["job_url"]), []).append(ledger)

    jobs: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        jobs.append(
            {
                "url": row["url"] or "",
                "title": row["title"] or "Untitled opportunity",
                "company": row["company_name"] or "Unknown employer",
                "location": row["location"] or "",
                "source": row["source_site"] or "Unknown source",
                "score": int(row["fit_score"] or 0),
                "verify": build_verify_job(record, ledgers_by_url.get(str(row["url"]), [])),
            }
        )
    summary = build_verify_summary(jobs, ledgers, ledger_available=ledger_available)
    return {"jobs": jobs, **summary}


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
        active_where = f"{ELIGIBLE_SQL} AND {ACTIVE_APPLICATION_SQL}"
        total = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {active_where}").fetchone()[0]
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs "
            f"WHERE full_description IS NOT NULL AND application_url IS NOT NULL AND {active_where}"
        ).fetchone()[0]
        scored = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL AND {active_where}").fetchone()[0]
        high_fit = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE fit_score >= 7 AND {active_where}").fetchone()[0]

        score_rows = conn.execute(
            "SELECT fit_score, COUNT(*) AS count FROM jobs "
            f"WHERE fit_score IS NOT NULL AND {active_where} "
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
            FROM jobs WHERE {active_where}
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
                   discovered_at, last_seen_at,
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
            WHERE fit_score >= 5 AND {active_where}
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
                "discoveredAt": row["discovered_at"] or "",
                "lastSeenAt": row["last_seen_at"] or "",
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
        verify = _verify_data(conn)
        discover = _discover_data(conn)
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
        "discover": discover,
        "stats": {"total": int(total), "ready": int(ready), "scored": int(scored), "highFit": int(high_fit)},
        "scoreDistribution": score_distribution,
        "sources": sources,
        "jobs": jobs,
        "prepare": prepare,
        "verify": verify,
    }


def render_dashboard(data: dict[str, Any]) -> str:
    """Render a self-contained workbench without allowing script injection."""
    template = resource_files("applypilot.frontend").joinpath("dashboard.html").read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    if template.count(DATA_PLACEHOLDER) != 1:
        raise RuntimeError("Dashboard template must contain exactly one data placeholder")
    return template.replace(DATA_PLACEHOLDER, payload)


def _publish_dashboard_assets(output: Path) -> None:
    """Publish approved package assets beside a generated Dashboard."""
    source = resource_files("applypilot.frontend").joinpath("assets", "capypilot")
    target = output.parent / DASHBOARD_ASSET_DIRECTORY
    target.mkdir(parents=True, exist_ok=True)
    for name in DASHBOARD_ASSET_NAMES:
        asset = source.joinpath(name)
        if not asset.is_file():
            raise RuntimeError(f"Packaged Dashboard asset is missing: {name}")
        (target / name).write_bytes(asset.read_bytes())


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
                "Your data was not changed. Run the read-only diagnostic; CapyPilot will not repair or replace the database automatically.",
                ("Run diagnostic", "applypilot doctor"),
                detail=f"{type(exc).__name__}: {exc}"[:240],
            )
        )
    html = render_dashboard(data)
    out.parent.mkdir(parents=True, exist_ok=True)
    _publish_dashboard_assets(out)
    out.write_text(html, encoding="utf-8")

    abs_path = str(out.resolve())
    console.print(f"[green]Opportunity workbench written to {abs_path}[/green]")
    return abs_path


def open_dashboard(output_path: str | None = None) -> None:
    """Generate the workbench and open it in the default browser."""
    path = generate_dashboard(output_path)
    console.print("[dim]Opening local workbench in browser...[/dim]")
    webbrowser.open(Path(path).as_uri())
