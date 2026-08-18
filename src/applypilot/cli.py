"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional
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
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
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


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from applypilot.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for tailor/cover stages."),
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

    stage_list = stages if stages else ["all"]

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


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(1, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum fit score for job selection."),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Browser-agent model override."),
    agent_backend: Optional[str] = typer.Option(
        None,
        "--agent-backend",
        help="Browser-agent CLI: codex or claude. Defaults to APPLYPILOT_APPLY_BACKEND.",
    ),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    manual_captcha_relay: bool = typer.Option(
        False,
        "--manual-captcha-relay",
        help="Pause visible Edge for applicant-completed CAPTCHA, then resume the agent.",
    ),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
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

    if dry_run:
        if not url:
            console.print("[red]--dry-run requires one explicit --url.[/red]")
            raise typer.Exit(code=1)
    if manual_captcha_relay and (
        dry_run or not url or continuous or headless or workers != 1 or limit not in (None, 1)
    ):
        console.print(
            "[red]Manual CAPTCHA relay requires submission mode, one exact URL, "
            "one visible worker, and limit 1.[/red]"
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
            ready = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL "
                "AND tailor_status = 'machine_validated' "
                "AND ((cover_letter_path IS NOT NULL AND cover_letter_status = 'human_approved') "
                "OR cover_letter_status = 'not_required') AND applied_at IS NULL"
            ).fetchone()[0]
        if ready == 0:
            if dry_run:
                console.print("[red]No machine-validated tailored resume is ready for preview.[/red]")
            else:
                console.print(
                    "[red]No submission-ready materials.[/red]\n"
                    "Prepare a cover letter, review it, then run [bold]approve-cover --url URL[/bold]."
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

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
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
    resume: Optional[str] = typer.Option(None, "--resume", help="Exact .txt or .docx resume source."),
) -> None:
    """Score and generate a cover letter for one exact, eligible job."""
    _bootstrap()
    from applypilot.single_job import prepare_cover_letter_for_url

    result = prepare_cover_letter_for_url(url, company, validation, resume)
    console.print_json(data=result)


@app.command("import-job")
def import_job(
    url: str = typer.Option(..., "--url", help="Exact HTTPS job URL."),
    title: str = typer.Option(..., "--title", help="Verified job title."),
    company: str = typer.Option(..., "--company", help="Verified employer name."),
    location: str = typer.Option("Singapore", "--location", help="Verified job location."),
    site: str = typer.Option("linkedin", "--site", help="Source job board."),
) -> None:
    """Register one exact job URL for enrichment and review."""
    _bootstrap()
    from applypilot.single_job import import_exact_job

    result = import_exact_job(url, title, company, location, site)
    console.print_json(data=result)


@app.command("import-listings")
def import_listings(
    file: Path = typer.Option(  # noqa: B008
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


@app.command("score-job")
def score_job_command(
    url: str = typer.Option(..., "--url", help="Exact enriched job URL."),
    resume: Optional[str] = typer.Option(None, "--resume", help="Exact .txt or .docx resume source."),
) -> None:
    """Score one exact eligible job against one explicit resume source."""
    _bootstrap()
    from applypilot.single_job import score_exact_job_for_url

    result = score_exact_job_for_url(url, resume)
    console.print_json(data=result)


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
def dashboard() -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from applypilot.view import open_dashboard

    open_dashboard()


@app.command()
def doctor() -> None:
    """Check your setup and diagnose missing requirements."""
    import shutil

    from applypilot.config import (
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

    # jobspy (discovery dep installed separately)
    try:
        import jobspy  # noqa: F401
        results.append(("python-jobspy", ok_mark, "Job board scraping available"))
    except ImportError:
        results.append(("python-jobspy", warn_mark,
                        "pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex"))

    # --- Tier 2 checks ---
    import os
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
    console.print("[bold]ApplyPilot Doctor[/bold]\n")

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
