"""Execution bodies for the ApplyPilot radar command group."""

from __future__ import annotations

from types import ModuleType


def run_radar_main(runtime: ModuleType, values: dict[str, object]) -> None:
    """Restrict discovery-only execution to the explicit radar allowlist."""
    ctx = values["ctx"]
    _assert_discovery_only_command = runtime._assert_discovery_only_command

    _assert_discovery_only_command(
        ctx.invoked_subcommand,
        {"collect", "queries", "explore", "advance", "import-leads", "import-company-seeds", "report"},
    )


def run_radar_queries(runtime: ModuleType, values: dict[str, object]) -> None:
    """Generate candidate-operated LinkedIn Content Search URLs without browsing."""
    window = values["window"]
    track = values["track"]
    subtrack = values["subtrack"]
    json_output = values["json_output"]
    Table = runtime.Table
    console = runtime.console

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
        "[dim]Content URLs require visible review. Use radar explore for bounded LinkedIn/Indeed Jobs searches.[/dim]"
    )


def run_radar_explore(runtime: ModuleType, values: dict[str, object]) -> None:
    """Search both boards independently, keeping results as unverified leads."""
    from applypilot.database import get_connection
    from applypilot.discovery.explore import explore_job_boards

    runtime._radar_bootstrap()
    result = explore_job_boards(
        get_connection(), queries=values["query"],
        sites=values["site"] or ("linkedin", "indeed"),
        results_per_site=values["limit"],
        job_type=values.get("job_type"),
    )
    runtime.console.print_json(data=result)


def run_radar_advance(runtime: ModuleType, values: dict[str, object]) -> None:
    """Advance a small pending queue through public employer verification."""
    from applypilot.database import get_connection
    from applypilot.discovery.advance import advance_radar_queue

    runtime._radar_bootstrap()
    runtime.console.print_json(data=advance_radar_queue(get_connection(), limit=values["limit"]))


def run_radar_collect(runtime: ModuleType, values: dict[str, object]) -> None:
    """Collect verified official jobs using bounded public GET adapters."""
    company = values["company"]
    include_inactive = values["include_inactive"]
    dry_run = values["dry_run"]
    _official_source_config = runtime._official_source_config
    _radar_bootstrap = runtime._radar_bootstrap
    console = runtime.console
    typer = runtime.typer

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
            unclassified = 0
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
                    unclassified += 1
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
                    "track_filtered_count": 0,
                    "unclassified_count": unclassified,
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


def run_radar_import_leads(runtime: ModuleType, values: dict[str, object]) -> None:
    """Import a candidate-reviewed JSON/CSV lead file without creating jobs."""
    file = values["file"]
    source_id = values["source_id"]
    _assert_discovery_storage_path = runtime._assert_discovery_storage_path
    _radar_bootstrap = runtime._radar_bootstrap
    console = runtime.console
    os = runtime.os
    typer = runtime.typer

    import csv
    import json
    from datetime import UTC, datetime

    import yaml

    from applypilot.config import CONFIG_DIR, RADAR_IMPORT_DIR

    if os.environ.get("APPLYPILOT_ATTENDED_REVIEW") != "1":
        console.print(
            "[red]Lead import requires an explicit attended-review session.[/red]"
        )
        raise typer.Exit(code=2)
    _assert_discovery_storage_path(file, RADAR_IMPORT_DIR, "Lead import")
    from applypilot.discovery.ecosystem import (
        get_ecosystem_source,
        normalize_job_lead,
        radar_source_descriptor,
    )
    from applypilot.radar import classify_job_subtracks

    try:
        source_config = get_ecosystem_source(str(source_id))
        source = radar_source_descriptor(source_config, "job_lead")
    except (KeyError, TypeError, ValueError) as error:
        console.print(f"[red]Lead source is not enabled for P2 import: {error}[/red]")
        raise typer.Exit(code=2) from error

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

    query_config = yaml.safe_load(
        (CONFIG_DIR / "linkedin_searches.yaml").read_text(encoding="utf-8")
    ) or {}
    normalized = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            console.print(f"[red]Lead row {index} must be an object.[/red]")
            raise typer.Exit(code=1)
        candidate = dict(row)
        if not candidate.get("source_url") and candidate.get("url"):
            candidate["source_url"] = candidate["url"]
        try:
            lead = normalize_job_lead(candidate, source_config)
        except (TypeError, ValueError) as error:
            console.print(f"[red]Lead row {index} is invalid: {error}[/red]")
            raise typer.Exit(code=1) from error
        lead["subtracks"] = lead.get("subtracks") or list(
            classify_job_subtracks(lead.get("title"), query_config)
        )
        lead["reason"] = "requires fresh exact employer-official verification"
        if values.get("official_targets_reviewed") and lead.get("official_job_url"):
            # The normalized input drops self-reported trust fields. Only this
            # explicit attended source-review action can issue the attestation.
            lead["official_target_review"] = {
                "url": lead["official_job_url"],
                "observed_at": datetime.now(UTC).isoformat(),
                "method": "agent_visible_employer_review",
            }
        normalized.append(lead)

    _radar_bootstrap()
    from applypilot.database import (
        finish_radar_fetch_run,
        get_connection,
        ingest_radar_leads,
        start_radar_fetch_run,
    )

    conn = get_connection()
    run_id = start_radar_fetch_run(conn, source, parser_version="ecosystem-lead-import-v2")
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


def run_radar_import_company_seeds(runtime: ModuleType, values: dict[str, object]) -> None:
    """Import candidate-reviewed ecosystem companies without creating jobs."""
    file = values["file"]
    source_id = values["source_id"]
    _assert_discovery_storage_path = runtime._assert_discovery_storage_path
    _radar_bootstrap = runtime._radar_bootstrap
    console = runtime.console
    os = runtime.os
    typer = runtime.typer

    import csv
    import json

    from applypilot.config import RADAR_IMPORT_DIR
    from applypilot.discovery.ecosystem import (
        get_ecosystem_source,
        normalize_company_seed,
        radar_source_descriptor,
    )

    if os.environ.get("APPLYPILOT_ATTENDED_REVIEW") != "1":
        console.print(
            "[red]Company-seed import requires an explicit attended-review session.[/red]"
        )
        raise typer.Exit(code=2)
    _assert_discovery_storage_path(file, RADAR_IMPORT_DIR, "Company-seed import")
    try:
        source_config = get_ecosystem_source(str(source_id))
        source = radar_source_descriptor(source_config, "company_seed")
    except (KeyError, TypeError, ValueError) as error:
        console.print(f"[red]Company-seed source is not enabled for P2 import: {error}[/red]")
        raise typer.Exit(code=2) from error

    if file.suffix.casefold() == ".json":
        payload = json.loads(file.read_text(encoding="utf-8"))
        rows = payload if isinstance(payload, list) else payload.get("companies", [])
    elif file.suffix.casefold() == ".csv":
        with file.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    else:
        console.print("[red]Company-seed import supports JSON or CSV.[/red]")
        raise typer.Exit(code=1)
    if not isinstance(rows, list):
        console.print("[red]Company-seed file must contain a list.[/red]")
        raise typer.Exit(code=1)

    normalized = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            console.print(f"[red]Company-seed row {index} must be an object.[/red]")
            raise typer.Exit(code=1)
        try:
            normalized.append(normalize_company_seed(row, source_config))
        except (TypeError, ValueError) as error:
            console.print(f"[red]Company-seed row {index} is invalid: {error}[/red]")
            raise typer.Exit(code=1) from error

    _radar_bootstrap()
    from applypilot.database import (
        finish_radar_fetch_run,
        get_connection,
        ingest_radar_company_seeds,
        start_radar_fetch_run,
    )

    conn = get_connection()
    run_id = start_radar_fetch_run(
        conn,
        source,
        parser_version="company-seed-import-v1",
    )
    counts = ingest_radar_company_seeds(conn, run_id, source, normalized)
    finish_radar_fetch_run(
        conn,
        run_id,
        status="partial",
        pagination_complete=False,
        raw_count=len(rows),
        normalized_count=len(normalized),
        new_count=counts["new"],
        existing_count=counts["existing"],
        metadata={"coverage_note": "company directories are non-exhaustive seed sources"},
    )
    console.print_json(
        data={
            "read_only": True,
            **counts,
            "jobs": 0,
            "leads": 0,
            "promotion_note": "company seeds require independent official-careers verification",
        }
    )


def run_radar_report(runtime: ModuleType, values: dict[str, object]) -> None:
    """Render the auditable daily radar report."""
    hours = values["hours"]
    output = values["output"]
    require_applied_snapshot = values["require_applied_snapshot"]
    _assert_discovery_storage_path = runtime._assert_discovery_storage_path
    _official_source_config = runtime._official_source_config
    _radar_bootstrap = runtime._radar_bootstrap
    console = runtime.console
    os = runtime.os
    typer = runtime.typer

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
        console.print(report, markup=False)
