"""Read-only adapters for official company career sources.

The module deliberately stops at discovery.  It never follows an application
link with a POST request, submits a form, writes to the local database, or
requires browser automation.  Callers receive a run ledger and normalised job
records which can later be passed through the radar's shared ingestion layer.

``transport`` is injectable to make the adapters deterministic in tests and to
let a caller provide its own rate-limited HTTP client.  A transport accepts a
URL and optional headers, returning text/bytes or a mapping with
``status_code``, ``text``/``body`` and optional ``headers``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import xml.etree.ElementTree as element_tree
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

import yaml
from bs4 import BeautifulSoup

from applypilot.config import CONFIG_DIR

Transport = Callable[..., Any]
_BLOCKED_HTTP_CODES = {401, 403, 407, 429}
_USER_AGENT = "ApplyPilotOfficialRadar/0.1 (+read-only-job-discovery)"


def load_company_watchlist(path: str | None = None) -> list[dict[str, Any]]:
    """Load the shipped company watchlist without enabling network activity."""
    watchlist_path = CONFIG_DIR / "company_watchlist.yaml" if path is None else path
    with open(watchlist_path, encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    companies = payload.get("companies", [])
    if not isinstance(companies, list):
        raise TypeError("company_watchlist.yaml must contain a companies list")
    return [company for company in companies if isinstance(company, dict)]


def _default_transport(url: str, headers: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Perform a conservative, read-only GET request."""
    request_headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json, application/xml, text/xml, text/html, */*;q=0.8",
    }
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return {
                "status_code": response.getcode(),
                "body": response.read(),
                "headers": dict(response.headers.items()),
            }
    except urllib.error.HTTPError as error:
        return {"status_code": error.code, "body": error.read(), "headers": dict(error.headers.items())}


def _call_transport(transport: Transport, url: str) -> tuple[int, str, dict[str, str]]:
    """Normalise the small transport contract used by all providers."""
    try:
        response = transport(url, headers={"User-Agent": _USER_AGENT})
    except TypeError:
        response = transport(url)

    if isinstance(response, (str, bytes)):
        body = response.decode("utf-8", errors="replace") if isinstance(response, bytes) else response
        return 200, body, {}
    if not isinstance(response, Mapping):
        raise TypeError("transport must return text, bytes, or a mapping")

    status = int(response.get("status_code", response.get("status", 200)))
    body = response.get("text", response.get("body", ""))
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    if not isinstance(body, str):
        body = str(body)
    headers = response.get("headers", {})
    return status, body, {str(key).lower(): str(value) for key, value in dict(headers).items()}


def _empty_run(company: Mapping[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        "source_id": f"official:{company.get('id', '')}:{company.get('provider', '')}",
        "company_id": company.get("id", ""),
        "company": company.get("name", ""),
        "provider": company.get("provider", ""),
        "status": "complete",
        "pagination_complete": True,
        "pages_scanned": 0,
        "raw_count": 0,
        "normalised_count": 0,
        "jobs": [],
        "errors": [],
        "error": None,
        "metadata": {
            "career_url": company.get("career_url", ""),
            "cadence": company.get("cadence", ""),
            "coverage_mode": company.get("coverage_mode", "full"),
            "read_only": True,
        },
        "started_at": now,
        "finished_at": now,
        "read_only": True,
    }


def _finish_run(run: dict[str, Any]) -> dict[str, Any]:
    run["normalised_count"] = len(run["jobs"])
    run["pagination_complete"] = run["status"] == "complete"
    run["error"] = "; ".join(run["errors"]) or None
    run["finished_at"] = datetime.now(UTC).isoformat()
    return run


def _error_status(status_code: int) -> str:
    return "blocked" if status_code in _BLOCKED_HTTP_CODES else "partial"


def _plain_text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    if "<" not in value or ">" not in value:
        return " ".join(value.split())
    return BeautifulSoup(value, "html.parser").get_text(" ", strip=True)


def _first(value: Any) -> str:
    if isinstance(value, list):
        return _first(value[0]) if value else ""
    if isinstance(value, Mapping):
        return _first(value.get("name") or value.get("value") or value.get("label"))
    return _plain_text(value)


def _location(value: Any) -> str:
    if isinstance(value, list):
        return "; ".join(part for part in (_location(item) for item in value) if part)
    if isinstance(value, Mapping):
        address = value.get("address")
        if isinstance(address, Mapping):
            pieces = [address.get(key) for key in ("addressLocality", "addressRegion", "addressCountry")]
            return ", ".join(str(piece) for piece in pieces if piece)
        return _first(value.get("name") or value.get("location") or value.get("label"))
    return _plain_text(value)


def _canonical_url(url: str) -> str:
    return url.split("#", 1)[0].rstrip("/")


def _nested_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _salary_text(raw: Mapping[str, Any]) -> str:
    compensation = _nested_mapping(raw.get("compensation"))
    direct = (
        compensation.get("scrapeableCompensationSalarySummary")
        or compensation.get("compensationTierSummary")
        or raw.get("salaryDescription")
    )
    if direct:
        return _plain_text(direct)
    salary_range = _nested_mapping(raw.get("salaryRange"))
    minimum = salary_range.get("min")
    maximum = salary_range.get("max")
    currency = str(salary_range.get("currency") or "").strip()
    interval = str(salary_range.get("interval") or "").strip()
    if minimum is None and maximum is None:
        return ""
    bounds = f"{minimum}-{maximum}" if minimum is not None and maximum is not None else str(minimum or maximum)
    return " ".join(part for part in (currency, bounds, interval) if part)


def normalise_job(raw: Mapping[str, Any], company: Mapping[str, Any], provider: str, *, source_url: str) -> dict[str, Any] | None:
    """Turn provider-specific data into a stable, source-attributed record.

    An entry without both a title and a public job URL is retained as a run
    error by the caller instead of being guessed into a listing.
    """
    title = _plain_text(raw.get("title") or raw.get("name") or raw.get("text"))
    urls = _nested_mapping(raw.get("urls"))
    url = _plain_text(
        raw.get("url")
        or raw.get("absolute_url")
        or raw.get("hostedUrl")
        or raw.get("jobUrl")
        or urls.get("show")
    )
    if url:
        url = urljoin(source_url, url)
    if not title or not url:
        return None

    identifier = raw.get("id") or raw.get("jobId") or raw.get("job_id") or raw.get("identifier")
    if isinstance(identifier, Mapping):
        identifier = identifier.get("value") or identifier.get("name")
    location_values = [
        raw.get("location"),
        raw.get("locations"),
        raw.get("secondaryLocations"),
        raw.get("jobLocation"),
        raw.get("categories", {}).get("location"),
    ]
    location = "; ".join(
        dict.fromkeys(
            part for value in location_values if (part := _location(value))
        )
    )
    description = _plain_text(
        raw.get("content")
        or raw.get("description")
        or raw.get("descriptionPlain")
        or raw.get("descriptionHtml")
    )
    published_at = _plain_text(raw.get("updated_at") or raw.get("createdAt") or raw.get("publishedAt") or raw.get("datePosted"))
    categories = _nested_mapping(raw.get("categories"))
    employment_type = _first(
        raw.get("employment_type") or raw.get("employmentType") or categories.get("commitment")
    )
    application_url = _plain_text(
        raw.get("application_url") or raw.get("apply_url") or raw.get("applyUrl") or urls.get("apply")
    )
    if application_url:
        application_url = urljoin(source_url, application_url)

    return {
        "title": title,
        "company": company.get("name", ""),
        "company_name": company.get("name", ""),
        "company_id": company.get("id", ""),
        "location": location,
        "url": url,
        "canonical_url": _canonical_url(url),
        "job_id": str(identifier or ""),
        "external_id": str(identifier or ""),
        "requisition_id": _plain_text(raw.get("requisitionId") or raw.get("requisition_id")),
        "description": description,
        "full_description": description,
        "salary": _salary_text(raw),
        "application_url": application_url or url,
        "published_at": published_at,
        "employment_type": employment_type,
        "provider": provider,
        "source_url": source_url,
        "verification_status": "verified_official",
        "track_tags": list(company.get("track_tags", [])),
    }


def _parse_json(body: str) -> Any:
    return json.loads(body.lstrip("\ufeff"))


def _records_and_next(payload: Any) -> tuple[list[Mapping[str, Any]], str | None]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)], None
    if not isinstance(payload, Mapping):
        return [], None
    records = payload.get("jobs") or payload.get("data") or payload.get("results") or payload.get("postings") or []
    if isinstance(records, Mapping):
        records = records.get("jobs") or records.get("data") or []
    next_url = payload.get("next") or payload.get("nextPage") or payload.get("next_page")
    return [item for item in records if isinstance(item, Mapping)] if isinstance(records, list) else [], str(next_url) if next_url else None


def _safe_pagination_url(origin_url: str, current_url: str, successor: str) -> str | None:
    """Resolve a provider cursor only when it remains HTTPS and same-origin."""
    candidate = urljoin(current_url, successor)
    origin = urlsplit(origin_url)
    resolved = urlsplit(candidate)
    if resolved.scheme.casefold() != "https":
        return None
    if not origin.netloc or resolved.netloc.casefold() != origin.netloc.casefold():
        return None
    return candidate


def _collect_json_pages(company: Mapping[str, Any], url: str, transport: Transport, provider: str) -> dict[str, Any]:
    run = _empty_run(company)
    next_url: str | None = url
    seen_urls: set[str] = set()
    max_pages = min(max(int(company.get("max_pages", 50)), 1), 100)
    while next_url:
        if run["pages_scanned"] >= max_pages:
            run["status"] = "partial"
            run["errors"].append(f"pagination stopped at configured limit ({max_pages} pages)")
            break
        if next_url in seen_urls:
            run["status"] = "partial"
            run["errors"].append("pagination loop detected")
            break
        seen_urls.add(next_url)
        try:
            status_code, body, _headers = _call_transport(transport, next_url)
        except Exception as error:  # noqa: BLE001 - injected transports may raise implementation-specific errors
            run["status"] = "partial"
            run["errors"].append(str(error))
            break
        if status_code >= 400:
            run["status"] = _error_status(status_code)
            run["errors"].append(f"HTTP {status_code} for {next_url}")
            break
        try:
            payload = _parse_json(body)
        except (TypeError, ValueError) as error:
            run["status"] = "partial"
            run["errors"].append(f"invalid JSON: {error}")
            break
        records, successor = _records_and_next(payload)
        run["pages_scanned"] += 1
        run["raw_count"] += len(records)
        for record in records:
            job = normalise_job(record, company, provider, source_url=next_url)
            if job is None:
                run["status"] = "partial"
                run["errors"].append("skipped record without title or public URL")
                continue
            run["jobs"].append(job)
        if successor:
            next_url = _safe_pagination_url(url, next_url, successor)
            if next_url is None:
                run["status"] = "partial"
                run["errors"].append("refused cross-origin or non-HTTPS pagination URL")
        else:
            next_url = None
    return _finish_run(run)


def greenhouse_url(company: Mapping[str, Any]) -> str:
    return f"https://boards-api.greenhouse.io/v1/boards/{company['board']}/jobs?content=true"


def lever_url(company: Mapping[str, Any]) -> str:
    return f"https://api.lever.co/v0/postings/{company['site']}?mode=json"


def ashby_url(company: Mapping[str, Any]) -> str:
    return (
        f"https://api.ashbyhq.com/posting-api/job-board/{company['board']}"
        "?includeCompensation=true"
    )


def collect_greenhouse(company: Mapping[str, Any], transport: Transport = _default_transport) -> dict[str, Any]:
    return _collect_json_pages(company, greenhouse_url(company), transport, "greenhouse")


def collect_lever(company: Mapping[str, Any], transport: Transport = _default_transport) -> dict[str, Any]:
    return _collect_json_pages(company, lever_url(company), transport, "lever")


def collect_ashby(company: Mapping[str, Any], transport: Transport = _default_transport) -> dict[str, Any]:
    return _collect_json_pages(company, ashby_url(company), transport, "ashby")


def _rss_text(node: element_tree.Element, name: str) -> str:
    for child in node:
        local_name = child.tag.rsplit("}", 1)[-1]
        if local_name == name:
            return "".join(child.itertext()).strip()
    return ""


def collect_rss(company: Mapping[str, Any], transport: Transport = _default_transport) -> dict[str, Any]:
    """Read a public company job RSS/Atom feed without attempting pagination."""
    run = _empty_run(company)
    url = str(company.get("feed_url") or company.get("career_url") or "")
    if not url:
        run["status"] = "partial"
        run["errors"].append("RSS company is missing feed_url")
        return _finish_run(run)
    try:
        status_code, body, _headers = _call_transport(transport, url)
    except Exception as error:  # noqa: BLE001 - injected transports may raise implementation-specific errors
        run["status"] = "partial"
        run["errors"].append(str(error))
        return _finish_run(run)
    if status_code >= 400:
        run["status"] = _error_status(status_code)
        run["errors"].append(f"HTTP {status_code} for {url}")
        return _finish_run(run)
    try:
        root = element_tree.fromstring(body)
    except element_tree.ParseError as error:
        run["status"] = "partial"
        run["errors"].append(f"invalid RSS/Atom XML: {error}")
        return _finish_run(run)
    entries = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] in {"item", "entry"}]
    if not entries:
        entries = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "job"]
    run["pages_scanned"] = 1
    run["raw_count"] = len(entries)
    for entry in entries:
        link = _rss_text(entry, "link") or _rss_text(entry, "url")
        if not link:
            link_node = next((child for child in entry if child.tag.rsplit("}", 1)[-1] == "link"), None)
            link = link_node.get("href", "") if link_node is not None else ""
        raw = {
            "id": (
                _rss_text(entry, "guid")
                or _rss_text(entry, "id")
                or _rss_text(entry, "requisitionid")
                or _rss_text(entry, "apijobid")
            ),
            "title": _rss_text(entry, "title"),
            "url": link,
            "location": ", ".join(
                part
                for part in (_rss_text(entry, "city"), _rss_text(entry, "country"))
                if part
            ) or _rss_text(entry, "location"),
            "description": _rss_text(entry, "description") or _rss_text(entry, "content"),
            "datePosted": (
                _rss_text(entry, "pubDate")
                or _rss_text(entry, "updated")
                or _rss_text(entry, "date")
            ),
        }
        job = normalise_job(raw, company, "rss", source_url=url)
        if job is None:
            run["status"] = "partial"
            run["errors"].append("skipped RSS entry without title or public URL")
        else:
            run["jobs"].append(job)
    if company.get("coverage_mode") == "latest_only" and run["status"] == "complete":
        run["status"] = "partial"
        run["errors"].append("source exposes latest items only; full pagination unavailable")
    return _finish_run(run)


def _jobposting_nodes(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, list):
        for item in value:
            yield from _jobposting_nodes(item)
    elif isinstance(value, Mapping):
        type_value = value.get("@type", "")
        types = type_value if isinstance(type_value, list) else [type_value]
        if any(str(item).casefold() == "jobposting" for item in types):
            yield value
        graph = value.get("@graph")
        if graph:
            yield from _jobposting_nodes(graph)


def extract_jobposting_jsonld(html: str, page_url: str, company: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Extract only explicit schema.org JobPosting nodes from a public page."""
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[dict[str, Any]] = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = _parse_json(script.get_text())
        except (TypeError, ValueError):
            continue
        for node in _jobposting_nodes(payload):
            org = node.get("hiringOrganization")
            raw = dict(node)
            if not raw.get("url"):
                raw["url"] = page_url
            if not raw.get("title"):
                raw["title"] = raw.get("name")
            if isinstance(org, Mapping) and not company.get("name"):
                raw["company"] = org.get("name", "")
            job = normalise_job(raw, company, "jobposting_jsonld", source_url=page_url)
            if job is not None:
                jobs.append(job)
    return jobs


def collect_jobposting_jsonld(company: Mapping[str, Any], transport: Transport = _default_transport) -> dict[str, Any]:
    run = _empty_run(company)
    url = str(company.get("career_url") or "")
    if not url:
        run["status"] = "partial"
        run["errors"].append("JSON-LD company is missing career_url")
        return _finish_run(run)
    try:
        status_code, body, _headers = _call_transport(transport, url)
    except Exception as error:  # noqa: BLE001 - injected transports may raise implementation-specific errors
        run["status"] = "partial"
        run["errors"].append(str(error))
        return _finish_run(run)
    if status_code >= 400:
        run["status"] = _error_status(status_code)
        run["errors"].append(f"HTTP {status_code} for {url}")
        return _finish_run(run)
    run["pages_scanned"] = 1
    run["jobs"] = extract_jobposting_jsonld(body, url, company)
    run["raw_count"] = len(run["jobs"])
    return _finish_run(run)


_COLLECTORS: dict[str, Callable[[Mapping[str, Any], Transport], dict[str, Any]]] = {
    "greenhouse": collect_greenhouse,
    "lever": collect_lever,
    "ashby": collect_ashby,
    "rss": collect_rss,
    "jobposting_jsonld": collect_jobposting_jsonld,
}


def collect_company(company: Mapping[str, Any], transport: Transport = _default_transport) -> dict[str, Any]:
    """Collect one configured company source and return an auditable ledger."""
    provider = str(company.get("provider") or "").casefold()
    collector = _COLLECTORS.get(provider)
    if collector is None:
        run = _empty_run(company)
        run["status"] = "blocked"
        run["errors"].append(f"unsupported official provider: {provider or 'missing'}")
        return _finish_run(run)
    return collector(company, transport)


def run_official_discovery(
    companies: Iterable[Mapping[str, Any]] | None = None,
    transport: Transport = _default_transport,
    *,
    active_only: bool = True,
) -> dict[str, Any]:
    """Collect configured official sources, returning source health and jobs only.

    This is intentionally a pure discovery boundary: it makes GET requests but
    never persists results or starts any application workflow.
    """
    selected = list(companies) if companies is not None else load_company_watchlist()
    runs: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for company in selected:
        if active_only and not company.get("active", False):
            skipped.append({"company_id": company.get("id", ""), "reason": "inactive"})
            continue
        runs.append(collect_company(company, transport))
    jobs = [job for run in runs for job in run["jobs"]]
    return {
        "runs": runs,
        "jobs": jobs,
        "skipped": skipped,
        "complete": sum(run["status"] == "complete" for run in runs),
        "partial": sum(run["status"] == "partial" for run in runs),
        "blocked": sum(run["status"] == "blocked" for run in runs),
        "read_only": True,
    }
