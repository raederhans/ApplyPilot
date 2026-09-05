"""JobSpy-based job discovery: searches Indeed, LinkedIn, Glassdoor, ZipRecruiter.

Uses python-jobspy to scrape multiple job boards, deduplicates results,
parses salary ranges, and stores everything in the ApplyPilot database.

Search queries, locations, and filtering rules are loaded from the user's
search configuration YAML (searches.yaml) rather than being hardcoded.
"""

import logging
import multiprocessing
import sqlite3
import tempfile
import time
import warnings
from datetime import UTC, datetime
from pathlib import Path

from applypilot import config
from applypilot.database import get_connection, init_db, store_jobs
from applypilot.optional_dependencies import require_jobboards

log = logging.getLogger(__name__)


# -- Proxy parsing -----------------------------------------------------------

def parse_proxy(proxy_str: str) -> dict:
    """Parse host:port:user:pass into components."""
    parts = proxy_str.split(":")
    if len(parts) == 4:
        host, port, user, passwd = parts
        return {
            "host": host,
            "port": port,
            "user": user,
            "pass": passwd,
            "jobspy": f"{user}:{passwd}@{host}:{port}",
            "playwright": {
                "server": f"http://{host}:{port}",
                "username": user,
                "password": passwd,
            },
        }
    if len(parts) == 2:
        host, port = parts
        return {
            "host": host,
            "port": port,
            "user": None,
            "pass": None,
            "jobspy": f"{host}:{port}",
            "playwright": {"server": f"http://{host}:{port}"},
        }
    raise ValueError(
        f"Proxy format not recognized: {proxy_str}. "
        f"Expected: host:port:user:pass or host:port"
    )


# -- Retry wrapper -----------------------------------------------------------

def _scrape_to_files(kwargs: dict, result_path: str, error_path: str) -> None:
    """Child-process target used to make third-party scraping terminable."""
    try:
        scrape_jobs = require_jobboards().scrape_jobs
        scrape_jobs(**kwargs).to_pickle(result_path)
    except BaseException as exc:  # Child must report every failure to the parent.
        Path(error_path).write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")


def _scrape_once_with_timeout(kwargs: dict, timeout_seconds: float):
    """Run one JobSpy request in a process that can be terminated on timeout."""
    with tempfile.TemporaryDirectory(prefix="applypilot-jobspy-") as temp_dir:
        result_path = Path(temp_dir) / "result.pkl"
        error_path = Path(temp_dir) / "error.txt"
        context = multiprocessing.get_context("spawn")
        process = context.Process(
            target=_scrape_to_files,
            args=(kwargs, str(result_path), str(error_path)),
            daemon=True,
        )
        process.start()
        process.join(timeout_seconds)
        if process.is_alive():
            process.terminate()
            process.join(5)
            if process.is_alive():
                process.kill()
                process.join(5)
            raise TimeoutError(f"JobSpy query exceeded {timeout_seconds:.0f}s wall-clock timeout")
        if error_path.exists():
            raise RuntimeError(error_path.read_text(encoding="utf-8"))
        if not result_path.exists():
            raise RuntimeError(f"JobSpy subprocess exited with code {process.exitcode} without a result")

        import pandas as pd

        return pd.read_pickle(result_path)


def _scrape_with_retry(
    kwargs: dict,
    max_retries: int = 2,
    backoff: float = 5.0,
    timeout_seconds: float = 150.0,
):
    """Call scrape_jobs with bounded retries and a per-attempt wall-clock timeout."""
    for attempt in range(max_retries + 1):
        try:
            return _scrape_once_with_timeout(kwargs, timeout_seconds)
        except Exception as e:
            err = str(e).lower()
            transient = any(k in err for k in ("timeout", "429", "proxy", "connection", "reset", "refused"))
            if transient and attempt < max_retries:
                wait = backoff * (attempt + 1)
                log.warning("Retry %d/%d in %.0fs: %s", attempt + 1, max_retries, wait, e)
                time.sleep(wait)
            else:
                raise


# -- Location filtering ------------------------------------------------------

def _load_location_config(search_cfg: dict) -> tuple[list[str], list[str]]:
    """Extract accept/reject location lists from search config.

    Falls back to sensible defaults if not defined in the YAML.
    """
    return config.get_location_filters(search_cfg)


def _location_ok(location: str | None, accept: list[str], reject: list[str]) -> bool:
    """Check if a job location passes the user's location filter.

    Remote jobs are always accepted. Non-remote jobs must match an accept
    pattern and not match a reject pattern.
    """
    return config.location_is_accepted(location, accept, reject, keep_unknown=True)


# -- Bounded job-board search -----------------------------------------------

def _clean_jobspy_value(value):
    """Return a JSON-friendly scalar, treating pandas/numpy missing values as absent."""
    if value is None:
        return None
    try:
        import pandas as pd

        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _normalize_job_board_rows(df, limit: int) -> list[dict]:
    """Normalize JobSpy rows without persisting them."""
    jobs: list[dict] = []
    for raw in df.to_dict(orient="records"):
        url = _clean_jobspy_value(raw.get("job_url"))
        title = _clean_jobspy_value(raw.get("title"))
        if not url or not str(url).strip() or not title or not str(title).strip():
            continue

        company = _clean_jobspy_value(raw.get("company"))
        description = _clean_jobspy_value(raw.get("description"))
        job = {
            "url": str(url),
            "title": str(title),
            "company_name": str(company) if company else None,
            "location": (
                str(clean_location)
                if (clean_location := _clean_jobspy_value(raw.get("location"))) is not None
                else None
            ),
            "full_description": str(description) if description else None,
            "application_url": (
                str(clean_apply_url)
                if (clean_apply_url := _clean_jobspy_value(raw.get("job_url_direct"))) is not None
                else None
            ),
        }
        if not company:
            job["quality_issues"] = ["missing_company_name"]
        jobs.append(job)
        if len(jobs) >= limit:
            break
    return jobs


def search_job_board(
    query: str,
    site: str,
    *,
    location: str = "Singapore",
    country: str = "Singapore",
    results_per_site: int = 10,
    hours_old: int = 168,
    timeout_seconds: float = 30,
    job_type: str | None = None,
) -> dict:
    """Run one bounded LinkedIn or Indeed search without writing to the database."""
    normalized_site = site.strip().lower()
    if normalized_site not in {"linkedin", "indeed"}:
        raise ValueError("site must be 'linkedin' or 'indeed'")
    if results_per_site < 1:
        raise ValueError("results_per_site must be at least 1")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than 0")
    if job_type not in {None, "internship", "fulltime", "parttime", "contract"}:
        raise ValueError("unsupported job_type")

    kwargs = {
        "site_name": [normalized_site],
        "search_term": query,
        "location": location,
        "results_wanted": results_per_site,
        "hours_old": hours_old,
        "description_format": "markdown",
        "verbose": 0,
    }
    if normalized_site == "linkedin":
        kwargs["linkedin_fetch_description"] = True
    else:
        kwargs["country_indeed"] = country
    if job_type:
        kwargs["job_type"] = job_type
        # JobSpy's Indeed adapter ignores job_type when hours_old is supplied.
        # An explicit caller-selected type takes precedence over the time filter.
        if normalized_site == "indeed":
            kwargs.pop("hours_old")

    response = {
        "site": normalized_site,
        "query": query,
        "status": "error",
        "jobs": [],
        "raw_count": 0,
        "error": None,
        "coverage": "non_exhaustive",
    }
    try:
        df = _scrape_once_with_timeout(kwargs, float(timeout_seconds))
    except Exception as exc:
        response["error"] = f"{type(exc).__name__}: {exc}"
        return response

    response["raw_count"] = len(df)
    response["jobs"] = _normalize_job_board_rows(df, results_per_site)
    response["status"] = "partial" if response["jobs"] else "empty"
    return response


# -- DB storage (JobSpy DataFrame -> SQLite) ---------------------------------

def store_jobspy_results(
    conn: sqlite3.Connection,
    df,
    source_label: str,
    excluded_titles: list[str] | None = None,
) -> tuple[int, int]:
    """Store JobSpy DataFrame results into the DB. Returns (new, existing)."""
    now = datetime.now(UTC).isoformat()
    excluded = 0
    excluded_titles = excluded_titles or []
    grouped: dict[str, list[dict]] = {}

    # DataFrame.iterrows constructs one Series per row and dominates ingestion
    # time for larger result sets. A record conversion preserves the existing
    # dynamic-column behavior while doing the column work in pandas once.
    for row in df.to_dict(orient="records"):
        url = str(row.get("job_url", ""))
        if not url or url == "nan":
            continue

        title = str(row.get("title", "")) if str(row.get("title", "")) != "nan" else None
        if config.title_is_excluded(title, excluded_titles):
            excluded += 1
            continue
        company = str(row.get("company", "")) if str(row.get("company", "")) != "nan" else None
        location_str = str(row.get("location", "")) if str(row.get("location", "")) != "nan" else None

        # Build salary string from min/max
        salary = None
        min_amt = row.get("min_amount")
        max_amt = row.get("max_amount")
        interval = str(row.get("interval", "")) if str(row.get("interval", "")) != "nan" else ""
        currency = str(row.get("currency", "")) if str(row.get("currency", "")) != "nan" else ""
        if min_amt and str(min_amt) != "nan":
            if max_amt and str(max_amt) != "nan":
                salary = f"{currency}{int(float(min_amt)):,}-{currency}{int(float(max_amt)):,}"
            else:
                salary = f"{currency}{int(float(min_amt)):,}"
            if interval:
                salary += f"/{interval}"

        description = str(row.get("description", "")) if str(row.get("description", "")) != "nan" else None
        site_name = str(row.get("site", source_label))
        is_remote = row.get("is_remote", False)

        site_label = f"{site_name}"
        if is_remote:
            location_str = f"{location_str} (Remote)" if location_str else "Remote"

        # If JobSpy gave us a full description, promote it directly
        full_description = None
        detail_scraped_at = None
        if description and len(description) > 200:
            full_description = description
            detail_scraped_at = now

        # Extract apply URL if JobSpy provided it
        apply_url = str(row.get("job_url_direct", "")) if str(row.get("job_url_direct", "")) != "nan" else None
        grouped.setdefault(site_label, []).append({
            "url": url,
            "title": title,
            "salary": salary,
            "description": description,
            "location": location_str,
            "company_name": company,
            "full_description": full_description,
            "application_url": apply_url,
            "detail_scraped_at": detail_scraped_at,
        })

    new = 0
    existing = 0
    for site_label, prepared_jobs in grouped.items():
        added, duplicates = store_jobs(conn, prepared_jobs, site_label, "jobspy")
        new += added
        existing += duplicates
    if excluded:
        log.info("Filtered %d jobs by excluded title", excluded)
    return new, existing


# -- Single search execution -------------------------------------------------

def _run_one_search(
    search: dict,
    sites: list[str],
    results_per_site: int,
    hours_old: int,
    proxy_config: dict | None,
    defaults: dict,
    max_retries: int,
    accept_locs: list[str],
    reject_locs: list[str],
    glassdoor_map: dict,
    excluded_titles: list[str],
) -> dict:
    """Run a single search query and store results in DB."""
    s = search
    label = f"\"{s['query']}\" in {s['location']} {'(remote)' if s.get('remote') else ''}"
    if "tier" in s:
        label += f" [tier {s['tier']}]"

    # Glassdoor needs a simplified location; every board still gets its own
    # request so one platform failure cannot hide another platform's results.
    gd_location = glassdoor_map.get(s["location"], s["location"].split(",")[0])
    all_dfs = []
    site_statuses: dict[str, dict] = {}
    timeout_seconds = float(defaults.get("query_timeout_seconds", 150))

    for site in sites:
        site_location = gd_location if site == "glassdoor" else s["location"]
        kwargs = {
            "site_name": [site],
            "search_term": s["query"],
            "location": site_location,
            "results_wanted": results_per_site,
            "hours_old": hours_old,
            "description_format": "markdown",
            "verbose": 0,
        }
        if site == "indeed":
            kwargs["country_indeed"] = defaults.get("country_indeed", "usa")
        if s.get("remote"):
            kwargs["is_remote"] = True
        if proxy_config:
            kwargs["proxies"] = [proxy_config["jobspy"]]
        if site == "linkedin":
            kwargs["linkedin_fetch_description"] = True
        try:
            df = _scrape_with_retry(
                kwargs,
                max_retries=max_retries,
                timeout_seconds=timeout_seconds,
            )
            all_dfs.append(df)
            count = len(df)
            site_statuses[site] = {
                "status": "partial" if count else "empty",
                "total": count,
                "error": None,
                "coverage": "non_exhaustive",
            }
        except Exception as e:
            site_statuses[site] = {
                "status": "error",
                "total": 0,
                "error": f"{type(e).__name__}: {e}",
                "coverage": "non_exhaustive",
            }
            log.error("[%s] (%s): %s", label, site, e)

    if not all_dfs:
        log.error("[%s]: all sites failed", label)
        return {
            "new": 0, "existing": 0, "errors": len(site_statuses),
            "filtered": 0, "total": 0, "label": label, "sites": site_statuses,
        }

    import pandas as pd

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        df = pd.concat(all_dfs, ignore_index=True) if len(all_dfs) > 1 else all_dfs[0]

    if len(df) == 0:
        log.info("[%s] 0 results", label)
        return {
            "new": 0, "existing": 0,
            "errors": sum(item["status"] == "error" for item in site_statuses.values()),
            "filtered": 0, "total": 0, "label": label, "sites": site_statuses,
        }

    # Filter by location before storing
    before = len(df)
    locations = df["location"].tolist() if "location" in df.columns else [""] * len(df)
    location_mask = [
        _location_ok(
            str(location) if str(location) != "nan" else None,
            accept_locs,
            reject_locs,
        )
        for location in locations
    ]
    df = df.loc[location_mask]
    filtered = before - len(df)

    conn = get_connection()
    new, existing = store_jobspy_results(conn, df, s["query"], excluded_titles)

    msg = f"[{label}] {before} results -> {new} new, {existing} dupes"
    if filtered:
        msg += f", {filtered} filtered (location)"
    log.info(msg)

    return {
        "new": new, "existing": existing,
        "errors": sum(item["status"] == "error" for item in site_statuses.values()),
        "filtered": filtered, "total": before, "label": label, "sites": site_statuses,
    }


# -- Single query search -----------------------------------------------------

def search_jobs(
    query: str,
    location: str,
    sites: list[str] | None = None,
    remote_only: bool = False,
    results_per_site: int = 50,
    hours_old: int = 72,
    proxy: str | None = None,
    country_indeed: str = "usa",
) -> dict:
    """Run a single job search via JobSpy and store results in DB."""
    if sites is None:
        sites = ["indeed", "linkedin", "zip_recruiter"]

    proxy_config = parse_proxy(proxy) if proxy else None

    log.info('Search: "%s" in %s | sites=%s | remote=%s', query, location, sites, remote_only)

    kwargs = {
        "site_name": sites,
        "search_term": query,
        "location": location,
        "results_wanted": results_per_site,
        "hours_old": hours_old,
        "description_format": "markdown",
        "country_indeed": country_indeed,
        "verbose": 2,
    }

    if remote_only:
        kwargs["is_remote"] = True

    if proxy_config:
        kwargs["proxies"] = [proxy_config["jobspy"]]

    if "linkedin" in sites:
        kwargs["linkedin_fetch_description"] = True

    try:
        df = _scrape_with_retry(kwargs, max_retries=2, timeout_seconds=150)
    except Exception as e:
        log.error("JobSpy search failed: %s", e)
        return {"error": str(e), "total": 0, "new": 0, "existing": 0}

    total = len(df)
    log.info("JobSpy returned %d results", total)

    if total == 0:
        return {"total": 0, "new": 0, "existing": 0}

    if "site" in df.columns:
        site_counts = df["site"].value_counts()
        for site, count in site_counts.items():
            log.info("  %s: %d", site, count)

    conn = init_db()
    new, existing = store_jobspy_results(conn, df, query)
    log.info("Stored: %d new, %d already in DB", new, existing)

    db_total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL").fetchone()[0]
    log.info("DB total: %d jobs, %d pending detail scrape", db_total, pending)

    return {"total": total, "new": new, "existing": existing}


# -- Full crawl (all queries x all locations) --------------------------------

def _full_crawl(
    search_cfg: dict,
    tiers: list[int] | None = None,
    locations: list[str] | None = None,
    sites: list[str] | None = None,
    results_per_site: int = 100,
    hours_old: int = 72,
    proxy: str | None = None,
    max_retries: int = 2,
) -> dict:
    """Run all search queries from search config across all locations."""
    if sites is None:
        sites = ["indeed", "linkedin", "zip_recruiter"]

    # Build search combinations from config
    queries = search_cfg.get("queries", [])
    locs = search_cfg.get("locations", [])
    defaults = search_cfg.get("defaults", {})
    max_retries = int(defaults.get("max_retries", max_retries))
    glassdoor_map = search_cfg.get("glassdoor_location_map", {})
    accept_locs, reject_locs = _load_location_config(search_cfg)
    excluded_titles = config.get_excluded_title_patterns(search_cfg)

    if tiers:
        queries = [q for q in queries if q.get("tier") in tiers]
    if locations:
        locs = [loc for loc in locs if loc.get("label") in locations]

    searches = []
    for q in queries:
        for loc in locs:
            searches.append({
                "query": q["query"],
                "location": loc["location"],
                "remote": loc.get("remote", False),
                "tier": q.get("tier", 0),
            })

    proxy_config = parse_proxy(proxy) if proxy else None

    log.info("Full crawl: %d search combinations", len(searches))
    log.info("Sites: %s | Results/site: %d | Hours old: %d",
             ", ".join(sites), results_per_site, hours_old)

    # Ensure DB schema is ready
    init_db()

    total_new = 0
    total_existing = 0
    total_errors = 0
    for completed, s in enumerate(searches, start=1):
        result = _run_one_search(
            s, sites, results_per_site, hours_old,
            proxy_config, defaults, max_retries,
            accept_locs, reject_locs, glassdoor_map, excluded_titles,
        )
        total_new += result["new"]
        total_existing += result["existing"]
        total_errors += result["errors"]

        if completed % 5 == 0 or completed == len(searches):
            log.info("Progress: %d/%d queries done (%d new, %d dupes, %d errors)",
                     completed, len(searches), total_new, total_existing, total_errors)

    # Final stats
    conn = get_connection()
    db_total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    log.info("Full crawl complete: %d new | %d dupes | %d errors | %d total in DB",
             total_new, total_existing, total_errors, db_total)

    return {
        "new": total_new,
        "existing": total_existing,
        "errors": total_errors,
        "db_total": db_total,
        "queries": len(searches),
    }


# -- Public entry point ------------------------------------------------------

def run_discovery(cfg: dict | None = None) -> dict:
    """Main entry point for JobSpy-based job discovery.

    Loads search queries and locations from the user's search config YAML,
    then runs a full crawl across all configured job boards.

    Args:
        cfg: Override the search configuration dict. If None, loads from
             the user's searches.yaml file.

    Returns:
        Dict with stats: new, existing, errors, db_total, queries.
    """
    if cfg is None:
        cfg = config.load_search_config()

    if not cfg:
        log.warning("No search configuration found. Run `applypilot init` to create one.")
        return {"new": 0, "existing": 0, "errors": 0, "db_total": 0, "queries": 0}

    # Validate the optional capability once before expanding the search matrix.
    # Without this boundary, one missing dependency would spawn and fail a
    # separate child process for every configured query/location pair.
    require_jobboards()

    # The shipped search schema and onboarding wizard use ``boards`` plus a
    # top-level ``country``. Keep the older spellings working for existing
    # installations, but make the current schema authoritative.
    normalized_cfg = dict(cfg)
    defaults = dict(cfg.get("defaults", {}))
    if cfg.get("country") and not defaults.get("country_indeed"):
        defaults["country_indeed"] = cfg["country"]
    normalized_cfg["defaults"] = defaults

    proxy = cfg.get("proxy")
    sites = cfg.get("boards") or cfg.get("sites")
    results_per_site = defaults.get("results_per_site", 100)
    hours_old = defaults.get("hours_old", 72)
    tiers = cfg.get("tiers")
    locations = cfg.get("location_labels")

    return _full_crawl(
        search_cfg=normalized_cfg,
        tiers=tiers,
        locations=locations,
        sites=sites,
        results_per_site=results_per_site,
        hours_old=hours_old,
        proxy=proxy,
    )
