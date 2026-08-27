"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import typer
from rich.console import Console
from rich.table import Table

from applypilot import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="applypilot",
    help="Local-first, evidence-driven job application workspace.",
    no_args_is_help=True,
)
radar_app = typer.Typer(
    name="radar",
    help="Read-only multi-source job discovery, verification, and reporting.",
    no_args_is_help=True,
)
app.add_typer(radar_app, name="radar")
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from applypilot.config import ensure_dirs, load_env
    from applypilot.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _standing_auto_authorization_enabled(profile: dict) -> bool:
    """Return whether this workspace may bind ready jobs without a new prompt.

    The policy is intentionally narrower than a global "auto submit" switch:
    every job must still pass the readiness decision and receive a fresh,
    short-lived, byte-bound manifest before the browser can submit it.
    """
    policy = profile.get("submission_policy", {})
    if not isinstance(policy, dict):
        return False
    return bool(
        policy.get("authorization_granted", False)
        and policy.get("standing_auto_authorize_ready_jobs", False)
        and not policy.get("batch_authorization_required", False)
    )


def _build_standing_authorization_manifest(
    conn,
    *,
    profile: dict,
    target_url: str | None,
    requested_limit: int,
    min_score: int,
) -> dict:
    """Bind the next eligible jobs to a short-lived standing authorization."""
    from applypilot import config as app_config
    from applypilot.apply import authorization, decision

    policy = profile.get("submission_policy", {})
    if not isinstance(policy, dict) or not _standing_auto_authorization_enabled(profile):
        raise ValueError("Standing auto-authorization is not enabled for this workspace")

    configured_cap = int(policy.get("maximum_auto_authorized_submissions_per_run", 1))
    if configured_cap < 1:
        raise ValueError("Standing auto-authorization has no positive per-run cap")
    submission_cap = 1 if target_url else min(max(1, requested_limit), configured_cap)
    configured_candidate_cap = int(
        policy.get(
            "maximum_auto_authorized_candidates_per_run",
            max(configured_cap, configured_cap * 2),
        )
    )
    configured_candidate_cap = max(configured_candidate_cap, submission_cap)
    candidate_cap = 1 if target_url else min(submission_cap * 2, configured_candidate_cap)
    ttl_minutes = int(policy.get("standing_authorization_ttl_minutes", 120))
    minimum_fit_score = max(1, min(int(min_score), 10))

    if target_url:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE url = ? OR application_url = ?",
            (target_url, target_url),
        ).fetchall()
        if len(rows) != 1:
            raise ValueError(
                f"Standing authorization expected one exact job for {target_url}, found {len(rows)}"
            )
    else:
        # Scan a bounded superset so a portal/manual exclusion cannot consume
        # a standing-authorization slot.  The decision gate below remains the
        # single source of truth for final readiness.
        rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE (apply_status IS NULL OR apply_status IN ('failed', 'previewed'))
              AND COALESCE(apply_retry_blocked, 0) = 0
              AND eligibility_status != 'ineligible'
              AND COALESCE(fit_score, -1) >= ?
            ORDER BY fit_score DESC, url
            LIMIT ?
            """,
            (minimum_fit_score, min(candidate_cap * 8, 80)),
        ).fetchall()

    selected: list[dict] = []
    for row in rows:
        job = dict(row)
        job["application_url"] = job.get("application_url") or job.get("url")
        if decision.evaluate(
            job,
            minimum_fit_score=minimum_fit_score,
            allow_runtime_readiness=bool(policy.get("allow_runtime_readiness_review", False)),
            allow_runtime_cover_letter=bool(
                policy.get("allow_runtime_cover_letter_discovery", False)
            ),
        ).get("decision") != "ready_to_apply":
            continue
        if not str(job.get("company_name") or "").strip():
            continue
        if not str(job.get("full_description") or "").strip():
            continue
        application_url = str(job["application_url"])
        if app_config.portal_application_gate(
            application_url,
            source_site=job.get("source_site"),
            site=job.get("site"),
            preview_only=False,
        ):
            continue
        if app_config.is_manual_ats(application_url):
            continue
        selected.append(job)
        if len(selected) == candidate_cap:
            break

    if not selected:
        raise ValueError("No exact job currently satisfies standing authorization gates")
    return authorization.build_bound_manifest(
        selected,
        ttl_minutes=ttl_minutes,
        max_submissions=submission_cap,
    )


def _radar_bootstrap() -> None:
    """Initialize only discovery storage and the shared database."""
    from applypilot.config import ensure_radar_dirs
    from applypilot.database import init_db

    ensure_radar_dirs()
    init_db()


def _assert_discovery_only_command(
    command: str | None,
    allowed: set[str],
) -> None:
    """Fail closed when the discovery wrapper enters another command surface."""
    if os.environ.get("APPLYPILOT_DISCOVERY_ONLY") != "1":
        return
    if command not in allowed:
        console.print(
            "[red]Discovery-only mode blocks every non-radar/application command.[/red]"
        )
        raise typer.Exit(code=2)


def _assert_discovery_storage_path(path: Path, root: Path, label: str) -> None:
    """Keep discovery-only reads and writes inside their declared data lane."""
    if os.environ.get("APPLYPILOT_DISCOVERY_ONLY") != "1":
        return
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        console.print(f"[red]{label} must stay under {root}.[/red]")
        raise typer.Exit(code=2) from None


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]ApplyPilot Local[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot Local — evidence-driven discovery, preparation, and application."""
    _assert_discovery_only_command(ctx.invoked_subcommand, {"sync-linkedin-applied", "radar"})


@radar_app.callback()
def radar_main(ctx: typer.Context) -> None:
    """Restrict discovery-only execution to the explicit radar allowlist."""
    _assert_discovery_only_command(
        ctx.invoked_subcommand,
        {"collect", "queries", "import-leads", "report"},
    )


@app.command()
def init(
    resume: Path | None = typer.Option(
        None,
        "--resume",
        help="Import a .txt or .pdf resume without opening the interactive wizard.",
    ),
    profile: Path | None = typer.Option(
        None,
        "--profile",
        help="Import an existing profile JSON file.",
    ),
    searches: Path | None = typer.Option(
        None,
        "--searches",
        help="Import an existing search YAML file.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Replace existing onboarding files when importing all three inputs.",
    ),
) -> None:
    """Initialize interactively, or import profile, resume, and search files."""
    from applypilot.wizard.init import initialize_from_files, run_wizard

    inputs = (resume, profile, searches)
    if not any(inputs):
        run_wizard()
        return
    if not all(inputs):
        raise typer.BadParameter(
            "--resume, --profile, and --searches must be provided together"
        )
    try:
        initialize_from_files(
            resume=resume,
            profile=profile,
            searches=searches,
            force=force,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from None


@app.command("unanswered")
def unanswered() -> None:
    """Show unresolved form questions captured during application runs."""
    _bootstrap()
    from applypilot.database import get_unanswered_questions

    records = get_unanswered_questions()
    if not records:
        console.print("[green]No unresolved application questions.[/green]")
        return

    table = Table(title="Unresolved application questions")
    table.add_column("Job")
    table.add_column("Question")
    table.add_column("Context")
    table.add_column("Required")
    for record in records:
        job_label = f"{record.get('title') or 'Unknown'} @ {record.get('company_name') or 'Unknown'}"
        for question in record["questions"]:
            table.add_row(
                job_label,
                str(question.get("question", "")),
                str(question.get("proposed_context", "job-specific")),
                "Yes" if question.get("required") else "No",
            )
    console.print(table)
    console.print(
        "[dim]Answer these after the run; confirmed reusable facts can then be added "
        "to profile.json application_facts with an explicit context.[/dim]"
    )


@app.command("review-readiness")
def review_readiness(
    url: str = typer.Option(..., "--url", help="Exact job or application URL."),
    status: str = typer.Option(
        ...,
        "--status",
        help="Review result: confirmed or needs_review.",
    ),
    reason: str = typer.Option(
        ...,
        "--reason",
        help=(
            "Evidence-based review of work authorization, availability, location, "
            "and job hard requirements; do not guess."
        ),
    ),
    reviewed_by: str = typer.Option(
        ...,
        "--reviewed-by",
        help="Reviewer identity: user or agent.",
    ),
) -> None:
    """Record a local readiness review without opening or submitting an application."""
    _bootstrap()
    from applypilot.apply.authorization import compute_job_fingerprint
    from applypilot.database import get_connection

    normalized_status = status.strip().casefold()
    if normalized_status not in {"confirmed", "needs_review"}:
        console.print("[red]--status must be confirmed or needs_review.[/red]")
        raise typer.Exit(code=2)
    normalized_reviewer = reviewed_by.strip().casefold()
    if normalized_reviewer not in {"user", "agent"}:
        console.print("[red]--reviewed-by must be user or agent.[/red]")
        raise typer.Exit(code=2)
    normalized_reason = reason.strip()
    if not normalized_reason:
        console.print("[red]--reason must be non-empty and evidence-based.[/red]")
        raise typer.Exit(code=2)

    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE url = ? OR application_url = ?",
        (url, url),
    ).fetchall()
    if len(rows) != 1:
        console.print(
            f"[red]Expected one exact job for URL, found {len(rows)}:[/red] {url}"
        )
        raise typer.Exit(code=2)

    reviewed_job = dict(rows[0])
    job_url = reviewed_job["url"]
    readiness_fingerprint = compute_job_fingerprint(reviewed_job)
    reviewed_at = datetime.now(UTC).isoformat()
    conn.execute(
        """
        UPDATE jobs
        SET application_readiness_status = ?,
            application_readiness_reason = ?,
            application_readiness_reviewed_at = ?,
            application_readiness_reviewed_by = ?,
            application_readiness_fingerprint = ?
        WHERE url = ?
        """,
        (
            normalized_status,
            normalized_reason,
            reviewed_at,
            normalized_reviewer,
            readiness_fingerprint,
            job_url,
        ),
    )
    conn.commit()
    console.print(
        f"[green]Recorded {normalized_status} readiness review:[/green] {job_url}"
    )
    console.print(
        "[dim]confirmed means job-specific authorization, availability, location, and "
        "hard requirements were reviewed from evidence; it is not permission to guess.[/dim]"
    )


@app.command("sync-linkedin-applied")
def sync_linkedin_applied(
    file: Path = typer.Option(
        ...,
        "--file",
        exists=True,
        dir_okay=False,
        help="Lightweight LinkedIn Applied JSON or CSV export.",
    ),
) -> None:
    """Merge a browser/user export of LinkedIn Applied into local status."""
    from applypilot.config import RADAR_IMPORT_DIR

    if (
        os.environ.get("APPLYPILOT_DISCOVERY_ONLY") == "1"
        and file.suffix.casefold() not in {".json", ".csv"}
    ):
        console.print("[red]Applied export must be JSON or CSV.[/red]")
        raise typer.Exit(code=2)
    _assert_discovery_storage_path(file, RADAR_IMPORT_DIR, "Applied export")
    _radar_bootstrap()
    from applypilot.database import import_linkedin_applied_export

    result = import_linkedin_applied_export(file)
    console.print_json(data=result)


@app.command("reconcile-receipts")
def reconcile_receipts(
    file: Path = typer.Option(
        ...,
        "--file",
        exists=True,
        dir_okay=False,
        help="JSON receipt envelope or list of envelopes for uncertain submissions.",
    ),
) -> None:
    """Reconcile page, portal, or confirmation-email receipts idempotently."""
    _bootstrap()
    from applypilot.database import reconcile_submission_receipt

    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        console.print(f"[red]Invalid receipt JSON: {exc}[/red]")
        raise typer.Exit(code=2) from None
    items = payload if isinstance(payload, list) else [payload]
    if not items or len(items) > 500 or not all(isinstance(item, dict) for item in items):
        console.print("[red]Receipt file must contain one object or a list of 1-500 objects.[/red]")
        raise typer.Exit(code=2)

    results = [reconcile_submission_receipt(item) for item in items]
    console.print_json(data={
        "processed": len(results),
        "applied": sum(result.get("status") == "applied" for result in results),
        "changed": sum(result.get("changed") is True for result in results),
        "results": results,
    })


@app.command("fact-revision")
def fact_revision(
    key: str = typer.Option(..., "--key", help="Stable application fact key."),
    old: str = typer.Option("", "--old", help="Previous value, if known."),
    new: str = typer.Option(..., "--new", help="New user-confirmed value."),
    context: str = typer.Option("application", "--context", help="Where this fact applies."),
    source: str = typer.Option("user_confirmed", "--source", help="Fact provenance label."),
    confirmed_at: str | None = typer.Option(
        None, "--confirmed-at", help="Confirmation date/time; defaults to now."
    ),
    note: str = typer.Option("", "--note", help="Short revision note."),
) -> None:
    """Append one fact change to the local application knowledge base."""
    _bootstrap()
    from applypilot.database import record_application_fact_revision

    revision_id = record_application_fact_revision(
        key,
        old,
        new,
        context=context,
        source=source,
        confirmed_at=confirmed_at,
        note=note,
    )
    console.print(f"[green]Recorded fact revision {revision_id}.[/green]")


@app.command("fact-history")
def fact_history(
    key: str | None = typer.Option(None, "--key", help="Optional exact fact key."),
    limit: int = typer.Option(100, "--limit", min=1, max=500),
) -> None:
    """Show recent fact revisions used as agent guidance."""
    _bootstrap()
    from applypilot.database import get_application_fact_revisions

    rows = get_application_fact_revisions(key, limit)
    if not rows:
        console.print("[green]No application fact revisions recorded.[/green]")
        return
    table = Table(title="Application fact revision history")
    table.add_column("Key")
    table.add_column("Old")
    table.add_column("New")
    table.add_column("Context")
    table.add_column("Confirmed")
    for row in rows:
        table.add_row(
            str(row.get("fact_key", "")),
            str(row.get("old_value", "")),
            str(row.get("new_value", "")),
            str(row.get("context", "")),
            str(row.get("confirmed_at", "")),
        )
    console.print(table)


def _official_source_config(company: dict) -> dict:
    provider = str(company.get("provider") or "manual")
    return {
        "source_id": f"official:{company.get('id', '')}:{provider}",
        "company_id": company.get("id"),
        "company_name": company.get("name"),
        "source_type": "official_careers",
        "provider": provider,
        "access_mode": "public_read",
        "base_url": company.get("career_url") or company.get("feed_url"),
        "priority_tier": company.get("cadence"),
        "active": company.get("active", False),
    }


@radar_app.command("queries")
def radar_queries(
    window: str | None = typer.Option(
        None,
        "--window",
        help="LinkedIn content window: past-24h, past-week, or past-month.",
    ),
    track: str | None = typer.Option(None, "--track", help="Optional stable top-level track."),
    subtrack: str | None = typer.Option(None, "--subtrack", help="Optional discovery subtrack."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Generate candidate-operated LinkedIn Content Search URLs without browsing."""
    import yaml

    from applypilot.config import CONFIG_DIR
    from applypilot.radar import build_linkedin_query_matrix

    path = CONFIG_DIR / "linkedin_searches.yaml"
    query_config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if window:
        query_config.setdefault("defaults", {})["window"] = window
    matrix = build_linkedin_query_matrix(query_config)
    if track:
        matrix = [item for item in matrix if item["track"] == track]
    if subtrack:
        matrix = [item for item in matrix if item["subtrack"] == subtrack]
    if json_output:
        console.print_json(data=matrix)
        return
    table = Table(title="LinkedIn read-only search queue")
    table.add_column("Track")
    table.add_column("Subtrack")
    table.add_column("Window")
    table.add_column("URL", overflow="fold")
    for item in matrix:
        table.add_row(item["track"], item["subtrack"], item["window"], item["url"])
    console.print(table)
    console.print(
        "[dim]URLs are for visible, candidate-operated review. ApplyPilot does not crawl LinkedIn.[/dim]"
    )


@radar_app.command("collect")
def radar_collect(
    company: list[str] | None = typer.Option(
        None,
        "--company",
        help="Company ID to collect; repeat to select multiple. Defaults to all active sources.",
    ),
    include_inactive: bool = typer.Option(
        False,
        "--include-inactive",
        help="Show inactive/manual sources in a dry run; live collection still requires --company.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show selected sources without HTTP or DB writes."),
) -> None:
    """Collect verified official jobs using bounded public GET adapters."""
    import yaml

    from applypilot import config as app_config
    from applypilot.database import (
        finish_radar_fetch_run,
        get_connection,
        ingest_radar_official_jobs,
        reconcile_radar_leads,
        start_radar_fetch_run,
    )
    from applypilot.discovery.official import collect_company, load_company_watchlist
    from applypilot.radar import classify_job_subtracks

    selected_ids = {value.casefold() for value in (company or [])}
    watchlist = load_company_watchlist()
    selected = [
        item for item in watchlist
        if (not selected_ids or str(item.get("id", "")).casefold() in selected_ids)
        and (item.get("active", False) or (include_inactive and dry_run))
    ]
    unknown = selected_ids - {str(item.get("id", "")).casefold() for item in watchlist}
    if unknown:
        console.print(f"[red]Unknown company IDs:[/red] {', '.join(sorted(unknown))}")
        raise typer.Exit(code=1)
    if include_inactive and not dry_run:
        console.print("[red]--include-inactive is inspection-only and requires --dry-run.[/red]")
        raise typer.Exit(code=2)
    if not selected:
        console.print("[yellow]No matching radar sources.[/yellow]")
        return

    if dry_run:
        console.print_json(data={
            "read_only": True,
            "selected": [
                {
                    "company_id": item.get("id"),
                    "provider": item.get("provider"),
                    "active": item.get("active", False),
                    "cadence": item.get("cadence"),
                }
                for item in selected
            ],
        })
        return

    _radar_bootstrap()
    conn = get_connection()
    radar_config = app_config.load_radar_config()
    accept_locations, reject_locations = app_config.get_location_filters(radar_config)
    excluded_titles = app_config.get_excluded_title_patterns(radar_config)
    query_config = yaml.safe_load(
        (app_config.CONFIG_DIR / "linkedin_searches.yaml").read_text(encoding="utf-8")
    ) or {}
    summaries: list[dict] = []

    for company_config in selected:
        source = _official_source_config(company_config)
        run_id = start_radar_fetch_run(conn, source, parser_version="official-adapters-v1")
        try:
            result = collect_company(company_config)
            accepted_jobs = []
            location_title_filtered = 0
            track_filtered = 0
            for job in result.get("jobs", []):
                if not app_config.radar_location_is_accepted(
                    job.get("location"),
                    accept_locations,
                    reject_locations,
                    allow_ambiguous_remote=bool(
                        radar_config.get("allow_ambiguous_remote", False)
                    ),
                ) or app_config.title_is_excluded(job.get("title"), excluded_titles):
                    location_title_filtered += 1
                    continue
                subtracks = classify_job_subtracks(job.get("title"), query_config)
                if not subtracks:
                    track_filtered += 1
                    continue
                accepted_job = dict(job)
                accepted_job["subtracks"] = list(subtracks)
                accepted_job["track_tags"] = list(subtracks)
                accepted_jobs.append(accepted_job)
            counts = ingest_radar_official_jobs(conn, run_id, source, accepted_jobs)
            finish_radar_fetch_run(
                conn,
                run_id,
                status=result.get("status", "partial"),
                pagination_complete=result.get("pagination_complete"),
                pages_fetched=result.get("pages_scanned"),
                raw_count=result.get("raw_count", 0),
                normalized_count=result.get("normalised_count", 0),
                new_count=counts["new"],
                existing_count=counts["existing"],
                error=result.get("error"),
                metadata={
                    **result.get("metadata", {}),
                    "accepted_count": len(accepted_jobs),
                    "filtered_count": len(result.get("jobs", [])) - len(accepted_jobs),
                    "location_title_filtered_count": location_title_filtered,
                    "track_filtered_count": track_filtered,
                },
            )
            reconciled = reconcile_radar_leads(conn, official_run_ids=[run_id])
            summaries.append({
                "company_id": company_config.get("id"),
                "status": result.get("status"),
                "raw": result.get("raw_count", 0),
                "normalised": result.get("normalised_count", 0),
                "accepted": len(accepted_jobs),
                **counts,
                "promoted_leads": reconciled["promoted"],
                "error": result.get("error"),
            })
        except Exception as error:  # noqa: BLE001 - provider failures must close the run ledger
            finish_radar_fetch_run(conn, run_id, status="partial", error=str(error))
            summaries.append({
                "company_id": company_config.get("id"),
                "status": "partial",
                "error": str(error),
            })
    console.print_json(data={"read_only": True, "sources": summaries})


@radar_app.command("import-leads")
def radar_import_leads(
    file: Path = typer.Option(..., "--file", exists=True, dir_okay=False),
    source_id: str = typer.Option("linkedin-content-manual", "--source-id"),
) -> None:
    """Import a candidate-reviewed JSON/CSV lead file without creating jobs."""
    import csv
    import json

    import yaml

    from applypilot.config import CONFIG_DIR, RADAR_IMPORT_DIR

    if (
        os.environ.get("APPLYPILOT_DISCOVERY_ONLY") == "1"
        and source_id != "linkedin-content-manual"
    ):
        console.print(
            "[red]Discovery-only lead imports require source-id "
            "linkedin-content-manual.[/red]"
        )
        raise typer.Exit(code=2)
    if os.environ.get("APPLYPILOT_ATTENDED_REVIEW") != "1":
        console.print(
            "[red]Lead import requires an explicit attended-review session.[/red]"
        )
        raise typer.Exit(code=2)
    _assert_discovery_storage_path(file, RADAR_IMPORT_DIR, "Lead import")
    _radar_bootstrap()
    from applypilot.database import (
        finish_radar_fetch_run,
        get_connection,
        ingest_radar_leads,
        start_radar_fetch_run,
    )
    from applypilot.radar import classify_job_subtracks

    if file.suffix.casefold() == ".json":
        payload = json.loads(file.read_text(encoding="utf-8"))
        rows = payload if isinstance(payload, list) else payload.get("leads", [])
    elif file.suffix.casefold() == ".csv":
        with file.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    else:
        console.print("[red]Lead import supports JSON or CSV.[/red]")
        raise typer.Exit(code=1)
    if not isinstance(rows, list):
        console.print("[red]Lead file must contain a list.[/red]")
        raise typer.Exit(code=1)

    source = {
        "source_id": source_id,
        "source_type": "social_lead",
        "provider": "candidate_reviewed_import",
        "access_mode": "authorised_local_import",
        "active": True,
    }
    conn = get_connection()
    query_config = yaml.safe_load(
        (CONFIG_DIR / "linkedin_searches.yaml").read_text(encoding="utf-8")
    ) or {}
    run_id = start_radar_fetch_run(conn, source, parser_version="lead-import-v1")
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        source_url = row.get("source_url") or row.get("url")
        if not str(source_url or "").strip():
            continue
        normalized.append({
            **row,
            "source_url": source_url,
            "subtracks": row.get("subtracks")
            or list(classify_job_subtracks(row.get("title"), query_config)),
            "status": "awaiting_official",
            "verification_status": "unverified",
            "reason": row.get("reason") or "requires official careers verification",
        })
    counts = ingest_radar_leads(conn, run_id, source, normalized)
    finish_radar_fetch_run(
        conn,
        run_id,
        status="partial",
        pagination_complete=False,
        raw_count=len(rows),
        normalized_count=len(normalized),
        lead_count=counts["leads"],
        metadata={"coverage_note": "candidate-reviewed import is not an exhaustive source scan"},
    )
    console.print_json(data={
        "read_only": True,
        **counts,
        "promoted": 0,
        "promotion_note": "awaiting a fresh official-source refresh",
    })


@radar_app.command("report")
def radar_report(
    hours: int = typer.Option(24, "--hours", min=1, max=24 * 31),
    output: Path | None = typer.Option(None, "--output", dir_okay=False),
    require_applied_snapshot: str | None = typer.Option(
        None,
        "--require-applied-snapshot",
        help="Exact complete Applied snapshot ID required for this report.",
    ),
) -> None:
    """Render the auditable daily radar report."""
    from datetime import UTC, datetime, timedelta

    from applypilot.config import RADAR_REPORT_DIR

    if os.environ.get("APPLYPILOT_DISCOVERY_ONLY") == "1" and not require_applied_snapshot:
        console.print(
            "[red]Discovery-only reports require this run's Applied snapshot ID.[/red]"
        )
        raise typer.Exit(code=2)
    if output:
        if (
            os.environ.get("APPLYPILOT_DISCOVERY_ONLY") == "1"
            and output.suffix.casefold() != ".md"
        ):
            console.print("[red]Discovery-only reports must use a .md output.[/red]")
            raise typer.Exit(code=2)
        _assert_discovery_storage_path(output, RADAR_REPORT_DIR, "Radar report")
    _radar_bootstrap()
    from applypilot.database import get_radar_daily_snapshot
    from applypilot.discovery.official import load_company_watchlist
    from applypilot.radar import render_daily_report

    since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    expected_sources = [
        _official_source_config(company)
        for company in load_company_watchlist()
        if company.get("active", False)
    ]
    snapshot = get_radar_daily_snapshot(
        since=since,
        expected_sources=expected_sources,
        applied_snapshot_id=require_applied_snapshot,
    )
    if require_applied_snapshot:
        applied_snapshot = snapshot["applied_snapshot"]
        snapshot_valid = (
            applied_snapshot.get("snapshot_id") == require_applied_snapshot
            and applied_snapshot.get("completeness") == "complete"
            and applied_snapshot.get("integrity_valid") is True
            and applied_snapshot.get("fresh") is True
        )
        if not snapshot_valid:
            console.print(
                "[red]Required LinkedIn Applied snapshot is missing, incomplete, "
                "invalid, or stale; report publication is blocked.[/red]"
            )
            raise typer.Exit(code=2)
    report = render_daily_report(**snapshot, report_date=datetime.now(UTC).date().isoformat())
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report, encoding="utf-8")
        console.print(f"[green]Wrote radar report:[/green] {output}")
    else:
        console.print(report)


@app.command()
def run(
    stages: list[str] | None = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(6, "--min-score", help="Minimum fit score for tailor/cover stages."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help=(
            "Validation strictness for tailor/cover stages. "
            "strict: banned words = errors, judge must pass. "
            "normal: banned words = warnings only (default, recommended for Gemini free tier). "
            "lenient: banned words ignored, LLM judge skipped (fastest, fewest API calls)."
        ),
    ),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf."""
    _bootstrap()

    from applypilot.pipeline import run_pipeline

    stage_list = stages or ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from applypilot.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    # Validate the --validation flag value
    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        validation_mode=validation,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command("authorize-batch")
def authorize_batch(
    urls: list[str] = typer.Option(
        ..., "--url", help="Exact prepared job URL; repeat for a batch."
    ),
    output: Path | None = typer.Option(None, "--output", dir_okay=False),
    expires_hours: int = typer.Option(24, "--expires-hours", min=1, max=168),
) -> None:
    """Record the initial approval for one exact, short-lived application batch."""
    _bootstrap()
    from applypilot import config as app_config
    from applypilot.apply import authorization, decision
    from applypilot.database import get_connection

    batch_root = (app_config.APP_DIR / "application-batches").resolve()
    batch_root.mkdir(parents=True, exist_ok=True)
    if output is None:
        output_path = batch_root / (
            "batch-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + ".json"
        )
    else:
        output_path = output if output.is_absolute() else batch_root / output
        output_path = output_path.resolve()
        try:
            output_path.relative_to(batch_root)
        except ValueError:
            console.print(f"[red]--output must stay under {batch_root}.[/red]")
            raise typer.Exit(code=2) from None
    if output_path.suffix.casefold() != ".json":
        console.print("[red]Authorization manifests must use a .json filename.[/red]")
        raise typer.Exit(code=2)

    conn = get_connection()
    profile = app_config.load_profile()
    submission_policy = profile.get("submission_policy", {})
    minimum_fit_score = int(
        submission_policy.get(
            "minimum_fit_score", app_config.DEFAULTS["min_score"]
        )
    )
    allow_runtime_readiness = bool(
        submission_policy.get("allow_runtime_readiness_review", False)
    )
    allow_runtime_cover = bool(
        submission_policy.get("allow_runtime_cover_letter_discovery", False)
    )
    manifest_jobs: list[dict] = []
    seen_urls: set[str] = set()
    for requested_url in urls:
        if requested_url in seen_urls:
            console.print(f"[red]Duplicate --url is not allowed:[/red] {requested_url}")
            raise typer.Exit(code=2)
        seen_urls.add(requested_url)
        rows = conn.execute(
            "SELECT * FROM jobs WHERE url = ? OR application_url = ?",
            (requested_url, requested_url),
        ).fetchall()
        if len(rows) != 1:
            console.print(
                f"[red]Expected one exact prepared job for URL, found {len(rows)}:[/red] "
                f"{requested_url}"
            )
            raise typer.Exit(code=2)
        job = dict(rows[0])
        job_decision = decision.evaluate(
            job,
            minimum_fit_score=minimum_fit_score,
            allow_runtime_readiness=allow_runtime_readiness,
            allow_runtime_cover_letter=allow_runtime_cover,
        )
        decision_value = (
            job_decision.get("decision")
            if isinstance(job_decision, dict)
            else getattr(job_decision, "decision", job_decision)
        )
        decision_value = getattr(decision_value, "value", decision_value)
        if str(decision_value).casefold() != "ready_to_apply":
            console.print(
                f"[red]Job decision is not ready_to_apply:[/red] {requested_url} "
                f"({decision_value})"
            )
            raise typer.Exit(code=2)
        if job.get("tailor_status") != "machine_validated" or not job.get(
            "tailored_resume_path"
        ):
            console.print(f"[red]Validated tailored resume missing:[/red] {requested_url}")
            raise typer.Exit(code=2)
        cover_status = str(job.get("cover_letter_status") or "").casefold()
        accepted_cover_statuses = {"human_approved", "agent_validated"}
        cover_ready = cover_status == "not_required" or (
            cover_status in accepted_cover_statuses and job.get("cover_letter_path")
        )
        if not cover_ready and not allow_runtime_cover:
            console.print(f"[red]Cover letter is not approved/not_required:[/red] {requested_url}")
            raise typer.Exit(code=2)
        application_url = str(job.get("application_url") or job["url"])
        target_host = (urlparse(application_url).hostname or "").casefold()
        if not target_host:
            console.print(f"[red]Application URL has no valid target host:[/red] {application_url}")
            raise typer.Exit(code=2)
        fingerprint_job = dict(job)
        fingerprint_job["application_url"] = application_url
        try:
            resume_path = authorization.resolve_resume_attachment(job)
        except (OSError, ValueError) as exc:
            console.print(f"[red]Validated tailored resume PDF is unavailable:[/red] {exc}")
            raise typer.Exit(code=2) from None
        try:
            resume_sha256, resume_size = authorization.compute_file_binding(resume_path)
        except OSError as exc:
            console.print(f"[red]Validated tailored resume is unreadable:[/red] {exc}")
            raise typer.Exit(code=2) from None
        manifest_jobs.append({
            "url": job["url"],
            "application_url": application_url,
            "target_host": target_host,
            "resume_path": str(resume_path),
            "resume_sha256": resume_sha256,
            "resume_size": resume_size,
            "job_fingerprint": authorization.compute_job_fingerprint(fingerprint_job),
        })

    authorized_at = datetime.now(UTC)
    manifest = {
        "version": 1,
        "batch_id": str(uuid.uuid4()),
        "authorized_at": authorized_at.isoformat(),
        "expires_at": (authorized_at + timedelta(hours=expires_hours)).isoformat(),
        "max_submissions": len(manifest_jobs),
        "jobs": manifest_jobs,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output_path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    except FileExistsError:
        console.print(f"[red]Refusing to overwrite existing authorization manifest:[/red] {output_path}")
        raise typer.Exit(code=2) from None
    console.print(
        f"[green]Prepared initial batch authorization for {len(manifest_jobs)} exact job(s):[/green] "
        f"{output_path}"
    )
    if submission_policy.get("batch_final_authorization_required", False):
        console.print(
            "[yellow]This workspace requires one grouped final authorization before submission.[/yellow]"
        )
    else:
        console.print(
            "[dim]Standing policy allows direct batch execution; create a final sidecar only when the active "
            "browser/platform requires grouped confirmation.[/dim]"
        )


@app.command("finalize-batch")
def finalize_batch(
    authorization_file: Path = typer.Option(
        ...,
        "--authorization-file",
        exists=True,
        dir_okay=False,
        help="Initial exact-job batch authorization manifest.",
    ),
    output: Path | None = typer.Option(None, "--output", dir_okay=False),
    ttl_minutes: int = typer.Option(120, "--ttl-minutes", min=1, max=240),
) -> None:
    """Record one final authorization covering every exact job in a prepared batch."""
    _bootstrap()
    from applypilot import config as app_config
    from applypilot.apply import authorization

    batch_root = (app_config.APP_DIR / "application-batches").resolve()
    batch_root.mkdir(parents=True, exist_ok=True)
    initial_path = authorization_file.resolve()
    if output is None:
        output_path = initial_path.with_name(initial_path.stem + ".final.json")
    else:
        output_path = output if output.is_absolute() else batch_root / output
        output_path = output_path.resolve()
    try:
        output_path.relative_to(batch_root)
    except ValueError:
        console.print(f"[red]--output must stay under {batch_root}.[/red]")
        raise typer.Exit(code=2) from None
    if output_path.suffix.casefold() != ".json":
        console.print("[red]Final batch authorizations must use a .json filename.[/red]")
        raise typer.Exit(code=2)

    try:
        final_authorization = authorization.build_final_authorization(
            initial_path,
            ttl_minutes=ttl_minutes,
        )
    except (OSError, TypeError, ValueError) as exc:
        console.print(f"[red]Initial batch authorization cannot be finalized:[/red] {exc}")
        raise typer.Exit(code=2) from None
    try:
        with output_path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(final_authorization, ensure_ascii=False, indent=2) + "\n")
    except FileExistsError:
        console.print(f"[red]Refusing to overwrite final batch authorization:[/red] {output_path}")
        raise typer.Exit(code=2) from None
    console.print(
        f"[green]Final authorization recorded for batch {final_authorization['batch_id']}:[/green] "
        f"{output_path}"
    )


@app.command()
def apply(
    limit: int | None = typer.Option(
        None,
        "--limit",
        "-l",
        help="Target number of independently confirmed submissions; pre-submit blockers use bound replacements.",
    ),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(6, "--min-score", help="Minimum fit score for job selection."),
    model: str | None = typer.Option(None, "--model", "-m", help="Browser-agent model override."),
    agent_backend: str | None = typer.Option(
        None,
        "--agent-backend",
        help="Browser-agent CLI: codex or claude. Defaults to APPLYPILOT_APPLY_BACKEND.",
    ),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    manual_captcha_relay: bool = typer.Option(
        False,
        "--manual-captcha-relay",
        help="Deprecated compatibility flag; visible CAPTCHA always hard-pauses for review.",
    ),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: str | None = typer.Option(None, "--url", help="Apply to a specific job URL."),
    authorization_file: Path | None = typer.Option(
        None,
        "--authorization-file",
        exists=True,
        dir_okay=False,
        help="Short-lived exact-job batch authorization manifest.",
    ),
    final_authorization_file: Path | None = typer.Option(
        None,
        "--final-authorization-file",
        exists=True,
        dir_okay=False,
        help="One final authorization bound to the exact initial batch manifest.",
    ),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: str | None = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: str | None = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: str | None = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
) -> None:
    """Prepare one application, or submit under workspace policy/one-off authorization."""
    _bootstrap()

    from applypilot.config import PROFILE_PATH as _profile_path
    from applypilot.config import get_chrome_path
    from applypilot.database import get_connection

    # --- Utility modes (no Chrome/Claude needed) ---

    if mark_applied:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from applypilot.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    if dry_run and not url:
        console.print("[red]--dry-run requires one explicit --url.[/red]")
        raise typer.Exit(code=1)
    try:
        profile = json.loads(_profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        profile = {}
    standing_auto_authorize = _standing_auto_authorization_enabled(profile)
    submission_policy = profile.get("submission_policy", {})
    final_batch_authorization_required = bool(
        isinstance(submission_policy, dict)
        and submission_policy.get("batch_final_authorization_required", False)
    )

    if final_authorization_file is not None and authorization_file is None:
        console.print(
            "[red]--final-authorization-file requires its bound --authorization-file.[/red]"
        )
        raise typer.Exit(code=2)
    if (
        not dry_run
        and final_batch_authorization_required
        and authorization_file is not None
        and final_authorization_file is None
    ):
        console.print(
            "[red]This workspace requires one final batch authorization before any browser submission. "
            "Run finalize-batch after the user approves the complete prepared batch, then pass both files.[/red]"
        )
        raise typer.Exit(code=2)

    if not dry_run and authorization_file is None and not url and not standing_auto_authorize:
        console.print(
            "[red]Submission without a manifest requires one exact --url.[/red]"
        )
        raise typer.Exit(code=2)
    if not dry_run and authorization_file is None:
        submission_policy = profile.get("submission_policy", {})
        manifest_required = bool(
            profile.get("batch_authorization_required", False)
            or (
                isinstance(submission_policy, dict)
                and submission_policy.get("batch_authorization_required", False)
            )
        )
        if manifest_required and not standing_auto_authorize:
            console.print(
                "[red]Profile policy requires --authorization-file for every submission.[/red]"
            )
            raise typer.Exit(code=2)
    requested_limit = limit if limit is not None else (0 if continuous else 1)
    if not dry_run and continuous and authorization_file is None:
        console.print(
            "[red]Continuous submission requires --authorization-file.[/red]"
        )
        raise typer.Exit(code=2)
    if (
        not dry_run
        and requested_limit > 1
        and authorization_file is None
        and not standing_auto_authorize
    ):
        console.print(
            "[red]Batch submission requires --authorization-file or standing auto-authorization.[/red]"
        )
        raise typer.Exit(code=2)
    if (
        not dry_run
        and url
        and authorization_file is None
        and os.environ.get("APPLYPILOT_AUTO_SUBMIT") != "1"
        and not standing_auto_authorize
    ):
        console.print(
            "[red]Single-URL submission requires APPLYPILOT_AUTO_SUBMIT=1 or "
            "--authorization-file.[/red]"
        )
        raise typer.Exit(code=2)
    if manual_captcha_relay and (dry_run or continuous or headless):
        console.print(
            "[red]Manual CAPTCHA relay requires bounded visible submission mode.[/red]"
        )
        raise typer.Exit(code=1)
        if continuous or headless or workers != 1 or (limit not in (None, 1)):
            console.print(
                "[red]Fill-only review requires a visible browser, one worker, one URL, and limit 1.[/red]"
            )
            raise typer.Exit(code=1)

    # --- Full apply mode ---

    backend = (agent_backend or os.environ.get("APPLYPILOT_APPLY_BACKEND", "claude")).strip().lower()
    if backend not in {"codex", "claude"}:
        console.print("[red]--agent-backend must be codex or claude.[/red]")
        raise typer.Exit(code=1)
    effective_model = model or (
        os.environ.get("APPLYPILOT_CODEX_MODEL", "gpt-5.6-sol")
        if backend == "codex"
        else os.environ.get("APPLYPILOT_CLAUDE_MODEL", "opus")
    )

    # Check 1: the selected browser-agent CLI and a visible browser are required.
    import shutil

    backend_binary = shutil.which("codex.exe") or shutil.which("codex") if backend == "codex" else shutil.which("claude")
    try:
        get_chrome_path()
        has_browser = True
    except FileNotFoundError:
        has_browser = False
    if not backend_binary or not has_browser:
        missing = []
        if not backend_binary:
            missing.append(f"{backend} CLI")
        if not has_browser:
            missing.append("Edge/Chrome/Chromium")
        console.print(f"[red]Browser apply is missing: {', '.join(missing)}.[/red]")
        raise typer.Exit(code=1)

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]applypilot init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: Submission needs an approved cover letter. A fill-only preview
    # can proceed with a validated resume and leave an optional letter blank.
    if not (gen and url):
        conn = get_connection()
        if dry_run:
            ready = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL "
                "AND tailor_status = 'machine_validated' AND applied_at IS NULL "
                "AND eligibility_status != 'ineligible'"
            ).fetchone()[0]
        else:
            allow_runtime_cover = bool(
                profile.get("submission_policy", {}).get(
                    "allow_runtime_cover_letter_discovery", False
                )
            )
            ready = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL "
                "AND tailor_status = 'machine_validated' "
                "AND applied_at IS NULL "
                + (
                    ""
                    if allow_runtime_cover
                    else "AND ((cover_letter_path IS NOT NULL AND cover_letter_status IN "
                    "('human_approved', 'agent_validated')) OR cover_letter_status = 'not_required')"
                )
            ).fetchone()[0]
        if ready == 0:
            if dry_run:
                console.print("[red]No machine-validated tailored resume is ready for preview.[/red]")
            else:
                console.print(
                    "[red]No submission-ready materials.[/red]\n"
                    "Prepare a validated resume and resolve any material hard requirements first."
                )
            raise typer.Exit(code=1)

    if gen:
        from applypilot.apply.launcher import gen_prompt
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        prompt_file = gen_prompt(target, min_score=min_score, model=effective_model)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        mcp_path = _profile_path.parent / ".mcp-apply-0.json"
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        if backend == "claude":
            console.print("\n[bold]Run manually:[/bold]")
            console.print(
                f"  claude --model {effective_model} "
                f"--mcp-config {mcp_path} "
                f"--permission-mode bypassPermissions < {prompt_file}"
            )
        else:
            console.print(
                "\n[dim]Codex MCP isolation is assembled by ApplyPilot at runtime; "
                "use apply --dry-run --url URL instead of copying a partial command.[/dim]"
            )
        return

    from applypilot.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else (0 if continuous else 1)
    authorization_manifest = None
    if authorization_file is not None:
        from applypilot.apply.authorization import (
            load_final_authorization,
            load_manifest,
        )

        try:
            if final_authorization_file is not None:
                authorization_manifest = load_final_authorization(
                    final_authorization_file,
                    authorization_file,
                )
            else:
                authorization_manifest = load_manifest(authorization_file)
        except (OSError, TypeError, ValueError) as exc:
            console.print(f"[red]Invalid authorization manifest:[/red] {exc}")
            raise typer.Exit(code=2) from None
    elif not dry_run and standing_auto_authorize:
        try:
            authorization_manifest = _build_standing_authorization_manifest(
                get_connection(),
                profile=profile,
                target_url=url,
                requested_limit=requested_limit,
                min_score=min_score,
            )
        except (TypeError, ValueError) as exc:
            console.print(f"[red]Standing auto-authorization could not bind a ready job:[/red] {exc}")
            raise typer.Exit(code=2) from None
        console.print(
            "[green]Standing authorization bound "
            f"{len(authorization_manifest['jobs'])} exact ready candidate(s) for up to "
            f"{authorization_manifest['max_submissions']} submissions.[/green]"
        )

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Success target: {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Backend:  {backend}")
    console.print(f"  Model:    {effective_model}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    console.print(f"  CAPTCHA:  {'manual relay' if manual_captcha_relay else 'stop on blocker'}")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=effective_model,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
        agent_backend=backend,
        manual_captcha_relay=manual_captcha_relay,
        authorization_manifest=authorization_manifest,
    )


@app.command("browser-session")
def browser_session(
    url: str = typer.Option(
        "https://www.linkedin.com/login",
        "--url",
        help="HTTPS page to open in the dedicated persistent browser profile.",
    ),
    worker: int = typer.Option(0, "--worker", help="Browser worker profile number."),
) -> None:
    """Open the dedicated visible browser for one-time interactive login."""
    _bootstrap()

    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        console.print("[red]--url must be an absolute HTTPS URL.[/red]")
        raise typer.Exit(code=1)
    if worker != 0:
        console.print("[red]Only worker 0 is supported by the local browser-session workflow.[/red]")
        raise typer.Exit(code=1)
    if os.environ.get("APPLYPILOT_BROWSER_PROFILE_MODE", "").lower() != "persistent":
        console.print(
            "[red]browser-session requires "
            "APPLYPILOT_BROWSER_PROFILE_MODE=persistent.[/red]"
        )
        raise typer.Exit(code=1)

    from applypilot.apply.chrome import cleanup_worker, launch_chrome

    console.print("\n[bold blue]Opening persistent ApplyPilot browser session[/bold blue]")
    console.print(f"  Worker:  {worker}")
    console.print(f"  URL:     {url}")
    console.print("  Close this dedicated browser window after completing the login.")

    process = launch_chrome(worker, headless=False, start_url=url)
    try:
        while process.poll() is None:
            time.sleep(0.5)
    except KeyboardInterrupt:
        console.print("\n[yellow]Closing browser session...[/yellow]")
    finally:
        cleanup_worker(worker, process)


@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from applypilot.database import get_stats

    stats = get_stats()

    console.print("\n[bold]ApplyPilot Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("Excluded: citizen/PR only", str(stats["excluded_ineligible"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        for site, count in stats["by_site"]:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)

    console.print()


@app.command("prepare-cover")
def prepare_cover(
    url: str = typer.Option(..., "--url", help="Exact discovered job URL."),
    company: str = typer.Option(..., "--company", help="Verified employer name."),
    validation: str = typer.Option("strict", "--validation", help="strict, normal, or lenient."),
    resume: str | None = typer.Option(None, "--resume", help="Exact .txt or .docx resume source."),
) -> None:
    """Score and generate a cover letter for one exact, eligible job."""
    _bootstrap()
    from applypilot.single_job import prepare_cover_letter_for_url

    result = prepare_cover_letter_for_url(url, company, validation, resume)
    console.print_json(data=result)


@app.command("import-job")
def import_job(
    url: str = typer.Option(..., "--url", help="Exact HTTPS job URL."),
    application_url: str | None = typer.Option(
        None,
        "--application-url",
        help="Exact official application form URL when it differs from the job detail URL.",
    ),
    title: str = typer.Option(..., "--title", help="Verified job title."),
    company: str = typer.Option(..., "--company", help="Verified employer name."),
    location: str = typer.Option("Singapore", "--location", help="Verified job location."),
    site: str = typer.Option("linkedin", "--site", help="Source job board."),
    description_file: Path | None = typer.Option(
        None,
        "--description-file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        help="UTF-8 file containing the verified job description.",
    ),
) -> None:
    """Register one exact job URL for enrichment and review."""
    _bootstrap()
    from applypilot.single_job import import_exact_job

    description = (
        description_file.read_text(encoding="utf-8") if description_file is not None else None
    )
    import_kwargs: dict[str, object] = {"description": description}
    if application_url is not None:
        import_kwargs["application_url"] = application_url
    result = import_exact_job(
        url,
        title,
        company,
        location,
        site,
        **import_kwargs,
    )
    console.print_json(data=result)


@app.command("import-listings")
def import_listings(
    file: Path = typer.Option(
        ..., "--file", exists=True, dir_okay=False, help="Candidate-provided listing CSV."
    ),
    portal: str | None = typer.Option(
        None,
        "--portal",
        help="JobStreet Singapore or InternSG when the CSV has no portal column.",
    ),
) -> None:
    """Import a local JobStreet or InternSG listing export without web access."""
    _bootstrap()
    from applypilot.single_job import import_portal_listings

    result = import_portal_listings(file, portal)
    console.print_json(data=result)


@app.command("rekey-email-job")
def rekey_email_job_command(
    url: str = typer.Option(..., "--url", help="Existing generic HTTPS careers URL."),
    reference: str = typer.Option(
        ...,
        "--reference",
        help="Stable lowercase role/date slug used only for ApplyPilot tracking.",
    ),
    title: str = typer.Option(..., "--title", help="Verified job title."),
    company: str = typer.Option(..., "--company", help="Verified employer name."),
    description_file: Path = typer.Option(
        ...,
        "--description-file",
        exists=True,
        dir_okay=False,
        help="UTF-8 file containing the candidate-provided job description.",
    ),
    location: str = typer.Option("Singapore", "--location", help="Verified location."),
) -> None:
    """Repair a generic careers-page key for one direct-email application."""
    _bootstrap()
    from applypilot.single_job import rekey_email_job

    result = rekey_email_job(
        url=url,
        reference=reference,
        title=title,
        company=company,
        description=description_file.read_text(encoding="utf-8"),
        location=location,
    )
    console.print_json(data=result)


@app.command("portal-list")
def portal_list(
    portal: str | None = typer.Option(None, "--portal", help="JobStreet Singapore or InternSG."),
    limit: int = typer.Option(100, "--limit", min=1, max=500, help="Maximum listings to show."),
) -> None:
    """Show JobStreet and InternSG listings already imported to the local database."""
    _bootstrap()
    from applypilot.single_job import list_portal_listings

    rows = list_portal_listings(portal, limit)
    table = Table(title="Imported portal listings", show_header=True, header_style="bold magenta")
    table.add_column("Portal")
    table.add_column("Title", max_width=42)
    table.add_column("Company", max_width=32)
    table.add_column("Location", max_width=22)
    table.add_column("Description")
    table.add_column("Eligibility")
    table.add_column("Apply status")
    for row in rows:
        table.add_row(
            str(row.get("source_site") or "?"),
            str(row.get("title") or "?"),
            str(row.get("company_name") or "?"),
            str(row.get("location") or "?"),
            "yes" if row.get("full_description") else "needs authorised text",
            str(row.get("eligibility_status") or "unknown"),
            str(row.get("apply_status") or "not started"),
        )
    console.print(table)
    console.print(f"[dim]{len(rows)} listing(s) shown.[/dim]")


@app.command("resume-library-sync")
def resume_library_sync_command() -> None:
    """Import configured sources and historical validated resumes by content."""
    _bootstrap()
    from applypilot.config import load_profile
    from applypilot.database import get_connection
    from applypilot.resume_library import library_status, sync_resume_library

    conn = get_connection()
    result = sync_resume_library(conn, load_profile())
    result["library"] = library_status(conn)
    console.print_json(data=result)


@app.command("resume-library-status")
def resume_library_status_command() -> None:
    """Show content-addressed artifacts, coverage, decisions, and validations."""
    _bootstrap()
    from applypilot.database import get_connection
    from applypilot.resume_library import library_status

    console.print_json(data=library_status(get_connection()))


@app.command("resume-route")
def resume_route_command(
    url: str = typer.Option(..., "--url", help="Exact job or application URL."),
    project_reuse: bool = typer.Option(
        False,
        "--project-reuse",
        help="If the decision is reuse_exact, project it into the legacy jobs material fields.",
    ),
) -> None:
    """Fingerprint one job and select reuse/create/review/ignore deterministically."""
    _bootstrap()
    from applypilot.config import load_profile
    from applypilot.database import get_connection
    from applypilot.resume_library import (
        project_reuse_to_job,
        route_resume_for_job,
        sync_resume_library,
    )

    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE url=? OR application_url=?", (url, url)
    ).fetchall()
    if len(rows) != 1:
        console.print(f"[red]Expected one exact job, found {len(rows)}:[/red] {url}")
        raise typer.Exit(code=2)
    profile = load_profile()
    sync_resume_library(conn, profile)
    job = dict(rows[0])
    result = route_resume_for_job(conn, job, profile)
    if project_reuse:
        if result["decision"] != "reuse_exact":
            console.print_json(data=result)
            console.print("[red]Only reuse_exact can be projected.[/red]")
            raise typer.Exit(code=2)
        result["legacy_projection"] = project_reuse_to_job(conn, job, result)
    console.print_json(data=result)


@app.command("score-job")
def score_job_command(
    url: str = typer.Option(..., "--url", help="Exact enriched job URL."),
    resume: str | None = typer.Option(None, "--resume", help="Exact .txt or .docx resume source."),
) -> None:
    """Score one exact eligible job against one explicit resume source."""
    _bootstrap()
    from applypilot.single_job import score_exact_job_for_url

    result = score_exact_job_for_url(url, resume)
    console.print_json(data=result)


@app.command("tailor-job")
def tailor_job_command(
    url: str = typer.Option(..., "--url", help="Exact scored job or application URL."),
    validation: str = typer.Option(
        "strict", "--validation", help="strict, normal, or lenient."
    ),
) -> None:
    """Tailor and machine-validate a resume for one exact job only."""
    _bootstrap()
    if validation not in {"strict", "normal", "lenient"}:
        console.print("[red]--validation must be strict, normal, or lenient.[/red]")
        raise typer.Exit(code=2)
    from applypilot.scoring.tailor import run_tailoring

    result = run_tailoring(
        min_score=0,
        limit=1,
        validation_mode=validation,
        target_url=url,
    )
    console.print_json(data=result)


@app.command("revalidate-tailored-job")
def revalidate_tailored_job_command(
    url: str = typer.Option(..., "--url", help="Exact job or application URL."),
) -> None:
    """Revalidate one locally edited tailored resume and render its upload PDF."""
    _bootstrap()
    from applypilot.single_job import revalidate_tailored_resume_for_url

    result = revalidate_tailored_resume_for_url(url)
    console.print_json(data=result)
    if result["status"] != "machine_validated":
        raise typer.Exit(code=2)


@app.command("approve-cover")
def approve_cover(
    url: str = typer.Option(..., "--url", help="Exact discovered job URL."),
    approved_by: str = typer.Option("user", "--approved-by", help="Human reviewer label."),
) -> None:
    """Mark the current machine-validated cover letter as human-approved."""
    _bootstrap()
    from applypilot.single_job import approve_cover_letter_for_url

    result = approve_cover_letter_for_url(url, approved_by=approved_by)
    console.print_json(data=result)


@app.command("mark-cover-not-required")
def mark_cover_not_required(
    url: str = typer.Option(..., "--url", help="Exact successfully previewed job URL."),
    verified_by: str = typer.Option(
        "browser_preview",
        "--verified-by",
        help="Audit label for the form inspection that found no cover-letter field.",
    ),
) -> None:
    """Mark an exact previewed form as not requiring a cover letter."""
    _bootstrap()
    from applypilot.single_job import mark_cover_letter_not_required_for_url

    result = mark_cover_letter_not_required_for_url(url, verified_by=verified_by)
    console.print_json(data=result)


@app.command()
def dashboard(
    output: Path | None = typer.Option(
        None,
        "--output",
        help="Write the generated workbench to this path.",
    ),
    open_browser: bool = typer.Option(
        True,
        "--open/--no-open",
        help="Open the generated workbench in the default browser.",
    ),
) -> None:
    """Generate the local HTML dashboard and optionally open it."""
    from applypilot.view import generate_dashboard, open_dashboard

    if open_browser:
        open_dashboard(str(output) if output else None)
    else:
        generate_dashboard(str(output) if output else None)


@app.command()
def doctor() -> None:
    """Check your setup and diagnose missing requirements."""
    import shutil
    import sqlite3

    from applypilot.config import (
        DB_PATH,
        PROFILE_PATH,
        RESUME_PATH,
        RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH,
        get_chrome_path,
        load_env,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)

    # --- Tier 1 checks ---
    # Profile
    if PROFILE_PATH.exists():
        results.append(("profile.json", ok_mark, str(PROFILE_PATH)))
    else:
        results.append(("profile.json", fail_mark, "Run 'applypilot init' to create"))

    # Resume
    if RESUME_PATH.exists():
        results.append(("resume.txt", ok_mark, str(RESUME_PATH)))
    elif RESUME_PDF_PATH.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found — plain-text needed for AI stages"))
    else:
        results.append(("resume.txt", fail_mark, "Run 'applypilot init' to add your resume"))

    # Search config
    if SEARCH_CONFIG_PATH.exists():
        results.append(("searches.yaml", ok_mark, str(SEARCH_CONFIG_PATH)))
    else:
        results.append(("searches.yaml", warn_mark, "Will use example config — run 'applypilot init'"))

    # Local database (read-only; never initializes, migrates, or repairs it).
    if not DB_PATH.is_file():
        results.append(("job database", warn_mark, "Not created yet — collect opportunities first"))
    else:
        from applypilot.view import collect_dashboard_data

        try:
            dashboard_data = collect_dashboard_data()
        except (OSError, sqlite3.DatabaseError) as exc:
            detail = f"{type(exc).__name__}: {exc}"[:120]
            results.append((
                "job database",
                fail_mark,
                f"{detail}; restore a known-good backup (automatic repair is disabled)",
            ))
        else:
            total = dashboard_data["stats"]["total"]
            results.append(("job database", ok_mark, f"Readable; {total} eligible roles"))

    # JobSpy is an optional capability rather than a core import dependency.
    try:
        from applypilot.optional_dependencies import require_jobboards

        require_jobboards()
        results.append(("python-jobspy", ok_mark, "Job board scraping available"))
    except RuntimeError as exc:
        results.append(("python-jobspy", warn_mark, str(exc)))

    # --- Tier 2 checks ---
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    has_deepseek = bool(os.environ.get("DEEPSEEK_API_KEY"))
    has_local = bool(os.environ.get("LLM_URL"))
    if has_gemini:
        model = os.environ.get("LLM_MODEL", "gemini-2.0-flash")
        results.append(("LLM API key", ok_mark, f"Gemini ({model})"))
    elif has_openai:
        model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
        results.append(("LLM API key", ok_mark, f"OpenAI ({model})"))
    elif has_deepseek:
        model = os.environ.get("LLM_MODEL", "deepseek-v4-pro")
        results.append(("LLM API key", ok_mark, f"DeepSeek ({model})"))
    elif has_local:
        results.append(("LLM API key", ok_mark, f"Local: {os.environ.get('LLM_URL')}"))
    else:
        results.append(("LLM API key", fail_mark,
                        "Set GEMINI_API_KEY, OPENAI_API_KEY, or DEEPSEEK_API_KEY in the local .env"))

    # --- Tier 3 checks ---
    # Claude Code CLI
    claude_bin = shutil.which("claude")
    if claude_bin:
        results.append(("Claude Code CLI", ok_mark, claude_bin))
    else:
        results.append(("Claude Code CLI", fail_mark,
                        "Install from https://claude.ai/code (needed for auto-apply)"))

    # Chromium browser
    try:
        chrome_path = get_chrome_path()
        results.append(("Edge/Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append(("Edge/Chrome/Chromium", fail_mark,
                        "Install Edge/Chrome or set CHROME_PATH (needed for auto-apply)"))

    # Node.js / npx (for Playwright MCP)
    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(("Node.js (npx)", ok_mark, npx_bin))
    else:
        results.append(("Node.js (npx)", fail_mark,
                        "Install Node.js 18+ from nodejs.org (needed for auto-apply)"))

    # CapSolver (optional)
    capsolver = os.environ.get("CAPSOLVER_API_KEY")
    if capsolver:
        captcha_mode = os.environ.get("APPLYPILOT_CAPTCHA_MODE", "manual_relay")
        results.append((
            "CapSolver API key",
            ok_mark,
            f"configured; mode={captcha_mode}; validity/balance not tested",
        ))
    else:
        results.append(("CapSolver API key", "[dim]optional[/dim]",
                        "Set CAPSOLVER_API_KEY in .env for CAPTCHA solving"))

    # --- Render results ---
    console.print()
    console.print("[bold]ApplyPilot Local Doctor[/bold]\n")

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        pad = " " * (col_w - len(check))
        console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

    console.print()

    # Tier summary
    from applypilot.config import TIER_LABELS, get_tier
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  → Tier 2 unlocks: scoring, tailoring, cover letters (needs LLM API key)[/dim]")
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")
    elif tier == 2:
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI + Chrome + Node.js)[/dim]")

    console.print()


if __name__ == "__main__":
    app()
