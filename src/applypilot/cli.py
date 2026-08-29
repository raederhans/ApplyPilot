"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import typer
from rich.console import Console
from rich.table import Table

from applypilot import __version__
from applypilot.commands import apply as apply_command_mod
from applypilot.commands import doctor as doctor_command_mod
from applypilot.commands import radar as radar_command_mod

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
    from applypilot.apply import authorization
    from applypilot.apply.submission_admission import evaluate_submission_admission

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
                f"Standing authorization expected one exact job for {target_url}, found {len(rows)}. "
                "Register it first with `applypilot import-job`, then run the exact-job "
                "enrichment, scoring, and material-readiness steps before retrying `apply --url`."
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
              AND COALESCE(eligibility_status, 'eligible') != 'ineligible'
              AND COALESCE(fit_score, -1) >= ?
            ORDER BY fit_score DESC, url
            LIMIT ?
            """,
            (minimum_fit_score, min(candidate_cap * 8, 80)),
        ).fetchall()

    selected: list[dict] = []
    exact_blocker: dict[str, str] | None = None
    for row in rows:
        job = dict(row)
        job["application_url"] = job.get("application_url") or job.get("url")
        decision_result = evaluate_submission_admission(
            job,
            profile,
            minimum_fit_score=minimum_fit_score,
            preview_only=False,
        )
        if not decision_result.get("admitted"):
            if target_url:
                reason = str(decision_result.get("reason") or "Readiness gate did not pass.")
                if not str(job.get("full_description") or "").strip():
                    next_step = (
                        f"applypilot import-job --url \"{target_url}\" "
                        "--description-file <official-job-description.txt>"
                    )
                elif job.get("fit_score") is None:
                    next_step = f"applypilot score-job --url \"{target_url}\""
                elif not str(job.get("application_readiness_status") or "").strip():
                    next_step = (
                        f"applypilot review-readiness --url \"{target_url}\" "
                        "--status confirmed --reason <evidence-summary> --reviewed-by agent"
                    )
                elif not str(job.get("tailored_resume_path") or "").strip() or str(
                    job.get("tailor_status") or ""
                ).casefold() != "machine_validated":
                    next_step = f"applypilot tailor-job --url \"{target_url}\""
                elif "cover-letter" in reason.casefold():
                    next_step = f"applypilot prepare-cover --url \"{target_url}\""
                else:
                    next_step = f"applypilot apply --dry-run --url \"{target_url}\""
                exact_blocker = {"reason": reason, "next_step": next_step}
            continue
        selected.append(job)
        if len(selected) == candidate_cap:
            break

    if not selected:
        if target_url and exact_blocker:
            raise ValueError(
                "Exact job is registered but not ready for application. "
                f"Gate: {exact_blocker['reason']} Next: {exact_blocker['next_step']}"
            )
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
    return radar_command_mod.run_radar_main(
        sys.modules[__name__],
        {
            "ctx": ctx,
        },
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


@app.command("prune-application-history")
def prune_application_history(
    days: int = typer.Option(
        180,
        "--days",
        min=30,
        help="Retain terminal runtime attempts for at least this many days.",
    ),
    execute: bool = typer.Option(
        False,
        "--execute",
        help="Delete eligible attempt rows; omission is a read-only preview.",
    ),
) -> None:
    """Preview or prune old attempts without deleting receipts or uncertainty."""
    if execute:
        _bootstrap()
    from applypilot.database import prune_application_runtime_history

    result = prune_application_runtime_history(retention_days=days, execute=execute)
    count_key = "deleted_attempts" if execute else "eligible_attempts"
    label = "Deleted" if execute else "Eligible"
    console.print(
        f"[green]{label}: {result[count_key]}[/green] terminal attempt rows "
        f"older than {days} days."
    )
    console.print(
        "[dim]Applied evidence, durable receipts, active attempts, and "
        "submission_uncertain rows are preserved.[/dim]"
    )


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
    return radar_command_mod.run_radar_queries(
        sys.modules[__name__],
        {
            "window": window,
            "track": track,
            "subtrack": subtrack,
            "json_output": json_output,
        },
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
    return radar_command_mod.run_radar_collect(
        sys.modules[__name__],
        {
            "company": company,
            "include_inactive": include_inactive,
            "dry_run": dry_run,
        },
    )


@radar_app.command("import-leads")
def radar_import_leads(
    file: Path = typer.Option(..., "--file", exists=True, dir_okay=False),
    source_id: str = typer.Option("linkedin-content-manual", "--source-id"),
) -> None:
    """Import a candidate-reviewed JSON/CSV lead file without creating jobs."""
    return radar_command_mod.run_radar_import_leads(
        sys.modules[__name__],
        {
            "file": file,
            "source_id": source_id,
        },
    )


@radar_app.command("import-company-seeds")
def radar_import_company_seeds(
    file: Path = typer.Option(..., "--file", exists=True, dir_okay=False),
    source_id: str = typer.Option(..., "--source-id"),
) -> None:
    """Import reviewed ecosystem companies without creating jobs or leads."""
    return radar_command_mod.run_radar_import_company_seeds(
        sys.modules[__name__],
        {
            "file": file,
            "source_id": source_id,
        },
    )


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
    return radar_command_mod.run_radar_report(
        sys.modules[__name__],
        {
            "hours": hours,
            "output": output,
            "require_applied_snapshot": require_applied_snapshot,
        },
    )


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
    from applypilot.apply import authorization
    from applypilot.apply.submission_admission import evaluate_submission_admission
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
        job_decision = evaluate_submission_admission(
            job,
            profile,
            minimum_fit_score=minimum_fit_score,
            preview_only=False,
        )
        if not job_decision.get("admitted"):
            decision_value = job_decision.get("decision")
            console.print(
                f"[red]Job decision is not ready_to_apply:[/red] {requested_url} "
                f"({decision_value}: {job_decision.get('reason')})"
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
    browser_backend: str | None = typer.Option(
        None,
        "--browser-backend",
        help="Browser runtime: edge, cloak, or auto. Defaults to APPLYPILOT_BROWSER_BACKEND.",
    ),
    interaction_mode: str | None = typer.Option(
        None,
        "--interaction-mode",
        help="Interaction policy: auto or playwright. Auto permits a structured Computer Use handoff request.",
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
    return apply_command_mod.run_apply(
        sys.modules[__name__],
        limit=limit,
        workers=workers,
        min_score=min_score,
        model=model,
        agent_backend=agent_backend,
        browser_backend=browser_backend,
        interaction_mode=interaction_mode,
        continuous=continuous,
        dry_run=dry_run,
        manual_captcha_relay=manual_captcha_relay,
        headless=headless,
        url=url,
        authorization_file=authorization_file,
        final_authorization_file=final_authorization_file,
        gen=gen,
        mark_applied=mark_applied,
        mark_failed=mark_failed,
        fail_reason=fail_reason,
        reset_failed=reset_failed,
    )


@app.command("browser-session")
def browser_session(
    url: str = typer.Option(
        "https://www.linkedin.com/login",
        "--url",
        help="HTTPS page to open in the dedicated persistent browser profile.",
    ),
    worker: int = typer.Option(0, "--worker", help="Browser worker profile number."),
    browser_backend: str | None = typer.Option(
        None,
        "--browser-backend",
        help="Browser runtime: edge or cloak. Defaults to APPLYPILOT_BROWSER_BACKEND.",
    ),
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

    from applypilot.apply.chrome import (
        allocate_cdp_port,
        cleanup_worker,
        get_browser_executable,
        launch_chrome,
        release_cdp_port,
        resolve_browser_backend,
    )

    try:
        effective_browser_backend = resolve_browser_backend(browser_backend, allow_auto=False)
        get_browser_executable(effective_browser_backend)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        console.print(f"[red]Browser backend unavailable:[/red] {exc}")
        raise typer.Exit(code=1) from None

    console.print("\n[bold blue]Opening persistent ApplyPilot browser session[/bold blue]")
    console.print(f"  Worker:  {worker}")
    console.print(f"  Backend: {effective_browser_backend}")
    console.print(f"  URL:     {url}")
    console.print("  Close this dedicated browser window after completing the login.")

    port = allocate_cdp_port(worker)
    process = None
    try:
        process = launch_chrome(
            worker,
            port=port,
            headless=False,
            start_url=url,
            browser_backend=effective_browser_backend,
        )
        while process.poll() is None:
            time.sleep(0.5)
    except KeyboardInterrupt:
        console.print("\n[yellow]Closing browser session...[/yellow]")
    finally:
        if process is not None:
            cleanup_worker(worker, process)
        release_cdp_port(worker)


@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from applypilot import config as app_config
    from applypilot.database import get_connection, get_stats
    from applypilot.services.application import count_submission_ready_jobs

    stats = get_stats()
    admission_ready = count_submission_ready_jobs(
        get_connection(),
        dry_run=False,
        profile=app_config.load_profile(),
    )

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
    summary.add_row("Prepared candidates (raw)", str(stats["ready_to_apply"]))
    summary.add_row("Admission-ready (pre-manifest)", str(admission_ready))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)
    console.print(
        "[dim]Admission-ready still requires an exact authorization manifest and "
        "runtime route verification before submission.[/dim]"
    )

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
    artifact_id: str | None = typer.Option(
        None,
        "--artifact-id",
        help="Explicitly select one current qualified candidate when automatic routing ties.",
    ),
    project_reuse: bool = typer.Option(
        False,
        "--project-reuse",
        help="Project a validated automatic or explicit reuse route into legacy material fields.",
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
    result = route_resume_for_job(conn, job, profile, artifact_id=artifact_id)
    if project_reuse:
        if result["decision"] not in {"reuse_exact", "manual_selection"}:
            console.print_json(data=result)
            console.print("[red]Only a validated reuse route can be projected.[/red]")
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
    return doctor_command_mod.run_doctor(sys.modules[__name__])


if __name__ == "__main__":
    app()
