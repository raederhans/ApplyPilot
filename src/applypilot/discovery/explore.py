"""Bounded cross-company discovery; board results remain unverified leads."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

from applypilot.discovery.diversity import rank_company_diverse, recent_handled_companies
from applypilot.discovery.ecosystem import normalize_job_lead, radar_source_descriptor
from applypilot.discovery.jobspy import search_job_board
from applypilot.storage.radar import (
    finish_radar_fetch_run,
    ingest_radar_leads,
    start_radar_fetch_run,
)

# Rotate coverage rather than always starting from the same employer or field.
# Callers can choose any query; these are discovery prompts, never fit gates.
_QUERY_PAIRS = (
    ("product intern", "business analyst intern"),
    ("data analyst intern", "business intelligence intern"),
    ("AI solutions intern", "automation intern"),
    ("urban planning intern", "geospatial intern"),
)


def default_exploration_queries(today: date | None = None) -> list[str]:
    local_date = today or datetime.now(timezone(timedelta(hours=8))).date()
    return list(_QUERY_PAIRS[local_date.toordinal() % len(_QUERY_PAIRS)])


def board_search_url(site: str, query: str, location: str = "Singapore", job_type=None) -> str:
    if site == "linkedin":
        params = {
            "keywords": query, "location": location, "sortBy": "DD", "f_TPR": "r604800",
        }
        if job_type:
            params["f_JT"] = {"internship": "I", "fulltime": "F", "parttime": "P", "contract": "C"}[job_type]
        return "https://www.linkedin.com/jobs/search/?" + urlencode(params)
    if site == "indeed":
        params = {
            "q": query, "l": location, "sort": "date", "fromage": "7",
        }
        if job_type:
            params.pop("fromage")
            params["jt"] = job_type
        return "https://sg.indeed.com/jobs?" + urlencode(params)
    raise ValueError("site must be linkedin or indeed")


def explore_job_boards(
    conn, *, queries=None, sites=("linkedin", "indeed"),
    results_per_site: int = 5, job_type: str | None = None, search=None,
) -> dict:
    """Run at most six searches; retain provenance without creating job rows."""
    queries = list(dict.fromkeys(q.strip() for q in (
        default_exploration_queries() if queries is None else queries
    ) if q.strip()))
    sites = list(dict.fromkeys(sites))
    if not 1 <= len(queries) <= 3 or any(len(q) > 160 for q in queries):
        raise ValueError("choose 1 to 3 short role queries")
    if not sites or any(site not in {"linkedin", "indeed"} for site in sites):
        raise ValueError("sites must be linkedin and/or indeed")
    if not 1 <= results_per_site <= 10:
        raise ValueError("results_per_site must be between 1 and 10")
    if job_type not in {None, "internship", "fulltime", "parttime", "contract"}:
        raise ValueError("unsupported job_type")
    search = search or search_job_board
    recent_companies = set(recent_handled_companies(conn))
    fetch_limit = min(results_per_site * 2, 10)
    runs, review = [], []
    for query in queries:
        for site in sites:
            source_id = f"{site}-jobs"
            source = radar_source_descriptor(source_id, "job_lead")
            run_id = start_radar_fetch_run(conn, source, parser_version="board-explore-v1")
            try:
                result = search(query, site, results_per_site=fetch_limit, job_type=job_type)
            except Exception as error:  # noqa: BLE001 - isolate providers and close ledger
                result = {"status": "error", "jobs": [], "raw_count": 0, "error": str(error)}
            leads = []
            invalid_count = 0
            for job in rank_company_diverse(
                result.get("jobs", []), recent_companies=recent_companies,
            )[:results_per_site]:
                candidate = {**job, "source_url": job.get("url")}
                # An external application URL is only a target for verification.
                candidate["official_job_url"] = job.get("application_url")
                try:
                    lead = normalize_job_lead(candidate, source_id)
                except ValueError:
                    candidate["official_job_url"] = None
                    try:
                        lead = normalize_job_lead(candidate, source_id)
                    except ValueError as error:
                        invalid_count += 1
                        review.append({"site": site, "url": job.get("url"),
                                       "title": job.get("title"), "reason": str(error)})
                        continue
                lead["full_description"] = str(job.get("full_description") or "")[:20000]
                lead["discovery_query"] = query
                lead["reason"] = "requires fresh employer verification; fit not yet assessed"
                leads.append(lead)
            recent_companies.update(lead["company_name"] for lead in leads)
            counts = ingest_radar_leads(conn, run_id, source, leads)
            status = "failed" if result["status"] == "error" else "partial"
            search_url = board_search_url(site, query, job_type=job_type)
            metadata = {
                "query": query, "search_status": result["status"],
                "coverage": "non_exhaustive", "invalid_count": invalid_count,
                "job_type": job_type,
                "fetch_limit": fetch_limit,
                "filters_verified": False,
                "time_filter_hours": None if site == "indeed" and job_type else 168,
                "search_url": search_url,
                "next_action": "verify visible selected filters, duties and employer links; stop at access challenges",
            }
            finish_radar_fetch_run(
                conn, run_id, status=status, pagination_complete=False,
                raw_count=result.get("raw_count", 0), normalized_count=len(leads),
                lead_count=counts["leads"], error=result.get("error"), metadata=metadata,
            )
            runs.append({"site": site, **metadata, "leads": counts["leads"],
                         "error": result.get("error")})
    return {"read_only": True, "jobs_created": 0, "sources": runs,
            "needs_metadata_review": review,
            "next_action": "radar advance; review unresolved employer URLs in the visible browser"}
