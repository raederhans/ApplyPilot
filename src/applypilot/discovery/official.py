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
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from html import unescape
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlsplit

import yaml
from bs4 import BeautifulSoup

from applypilot.config import CONFIG_DIR

Transport = Callable[..., Any]
_BLOCKED_HTTP_CODES = {401, 403, 407, 429}
_USER_AGENT = "ApplyPilotOfficialRadar/0.1 (+read-only-job-discovery)"
_ACTIVE_PROVIDER_KEYS = {
    "greenhouse": "board",
    "lever": "site",
    "ashby": "board",
    "smartrecruiters": "company_id",
    "workable": "subdomain",
    "rss": "feed_url",
    "jobposting_jsonld": "career_url",
}


def load_company_watchlist(path: str | None = None) -> list[dict[str, Any]]:
    """Load the shipped company watchlist without enabling network activity."""
    watchlist_path = CONFIG_DIR / "company_watchlist.yaml" if path is None else path
    with open(watchlist_path, encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    companies = payload.get("companies", [])
    if not isinstance(companies, list):
        raise TypeError("company_watchlist.yaml must contain a companies list")
    normalized = [company for company in companies if isinstance(company, dict)]
    seen_ids: set[str] = set()
    for company in normalized:
        company_id = str(company.get("id") or "").strip()
        if not company_id:
            raise ValueError("company_watchlist company is missing id")
        if company_id.casefold() in seen_ids:
            raise ValueError(f"company_watchlist contains duplicate id: {company_id}")
        seen_ids.add(company_id.casefold())
        if not company.get("active", False):
            continue
        provider = str(company.get("provider") or "").casefold()
        required_key = _ACTIVE_PROVIDER_KEYS.get(provider)
        if required_key is None:
            raise ValueError(
                f"active company {company_id} has unsupported provider: {provider or 'missing'}"
            )
        if not str(company.get(required_key) or "").strip():
            raise ValueError(
                f"active {provider} company {company_id} requires canonical key {required_key}"
            )
        if company.get("cadence") != "daily":
            raise ValueError(
                f"active company {company_id} must use daily cadence until due-source scheduling exists"
            )
    return normalized


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
            "provider_identifier": (
                company.get("company_id")
                or company.get("subdomain")
                or company.get("identifier")
                or company.get("site")
                or company.get("board")
                or ""
            ),
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
    value = unescape(value)
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
        "provider_application_id": _plain_text(raw.get("provider_application_id")),
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


def _company_key(company: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(company.get(key) or "").strip()
        if value:
            return value
    return ""


def smartrecruiters_url(company: Mapping[str, Any], *, offset: int = 0) -> str:
    identifier = _company_key(company, "company_id", "identifier", "site", "board")
    limit = min(max(int(company.get("limit", 100)), 1), 100)
    parameters: list[tuple[str, str | int]] = [("limit", limit), ("offset", offset)]
    country = str(company.get("country") or "").strip()
    if country:
        parameters.append(("country", country))
    return (
        f"https://api.smartrecruiters.com/v1/companies/{quote(identifier, safe='')}/postings"
        f"?{urlencode(parameters)}"
    )


def workable_url(company: Mapping[str, Any]) -> str:
    account = _company_key(company, "subdomain", "account", "site")
    return f"https://www.workable.com/api/accounts/{quote(account, safe='')}?details=true"


def _joined_location(*values: Any) -> str:
    parts: list[str] = []
    for value in values:
        for part in str(value or "").split(","):
            cleaned = part.strip()
            if cleaned and cleaned.casefold() not in {item.casefold() for item in parts}:
                parts.append(cleaned)
    return ", ".join(parts)


def _smartrecruiters_record(summary: Mapping[str, Any], detail: Mapping[str, Any] | None = None) -> dict[str, Any]:
    merged = dict(summary)
    merged.update(detail or {})
    sections = _nested_mapping(_nested_mapping(merged.get("jobAd")).get("sections"))
    description_parts = []
    for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
        section = _nested_mapping(sections.get(key))
        text = _plain_text(section.get("text"))
        if text:
            description_parts.append(text)
    location = _nested_mapping(merged.get("location"))
    full_location = _joined_location(location.get("fullLocation")) or _joined_location(
        location.get("city"), location.get("region"), location.get("country")
    )
    employment_type = _nested_mapping(merged.get("typeOfEmployment"))
    function = _nested_mapping(merged.get("function"))
    public_url = merged.get("postingUrl") or merged.get("applyUrl")
    return {
        "id": merged.get("id") or merged.get("uuid"),
        "title": merged.get("name") or merged.get("title"),
        "url": public_url,
        "application_url": merged.get("applyUrl") or public_url,
        "location": full_location,
        "description": "\n\n".join(description_parts),
        "datePosted": merged.get("releasedDate"),
        "employmentType": employment_type.get("label") or merged.get("typeOfEmployment"),
        "requisition_id": merged.get("refNumber"),
        "provider_application_id": merged.get("uuid"),
        "function": function.get("label") or merged.get("function"),
    }


def collect_smartrecruiters(
    company: Mapping[str, Any], transport: Transport = _default_transport
) -> dict[str, Any]:
    """Read every SmartRecruiters posting page and enrich it from its official detail ref."""
    run = _empty_run(company)
    identifier = _company_key(company, "company_id", "identifier", "site", "board")
    if not identifier:
        run["status"] = "partial"
        run["errors"].append("SmartRecruiters company is missing company_id/identifier/site")
        return _finish_run(run)

    max_pages = min(max(int(company.get("max_pages", 50)), 1), 100)
    offset = 0
    expected_total: int | None = None
    seen_offsets: set[int] = set()
    seen_page_signatures: set[tuple[str, ...]] = set()
    seen_posting_ids: set[str] = set()
    summaries_seen = 0
    detail_concurrency = min(
        max(int(company.get("detail_concurrency", 1)), 1),
        8,
    )
    run["metadata"]["detail_concurrency"] = detail_concurrency

    while expected_total is None or summaries_seen < expected_total:
        if run["pages_scanned"] >= max_pages:
            run["status"] = "partial"
            run["errors"].append(f"pagination stopped at configured limit ({max_pages} pages)")
            break
        if offset in seen_offsets:
            run["status"] = "partial"
            run["errors"].append("pagination loop detected")
            break
        seen_offsets.add(offset)
        page_url = smartrecruiters_url(company, offset=offset)
        try:
            status_code, body, _headers = _call_transport(transport, page_url)
        except Exception as error:  # noqa: BLE001 - transports are an integration boundary
            run["status"] = "partial"
            run["errors"].append(str(error))
            break
        if status_code >= 400:
            run["status"] = _error_status(status_code) if run["pages_scanned"] == 0 else "partial"
            run["errors"].append(f"HTTP {status_code} for {page_url}")
            break
        try:
            payload = _parse_json(body)
        except (TypeError, ValueError) as error:
            run["status"] = "partial"
            run["errors"].append(f"invalid JSON: {error}")
            break
        if not isinstance(payload, Mapping) or not isinstance(payload.get("content"), list):
            run["status"] = "partial"
            run["errors"].append("SmartRecruiters response is missing content list")
            break
        try:
            page_total = int(payload["totalFound"])
        except (KeyError, TypeError, ValueError):
            run["status"] = "partial"
            run["errors"].append("SmartRecruiters response is missing valid totalFound")
            break
        if page_total < 0:
            run["status"] = "partial"
            run["errors"].append("SmartRecruiters totalFound must not be negative")
            break
        if expected_total is None:
            expected_total = page_total
        elif page_total != expected_total:
            run["status"] = "partial"
            run["errors"].append(
                f"SmartRecruiters totalFound changed during pagination ({expected_total} to {page_total})"
            )
            break

        try:
            response_offset = int(payload.get("offset", offset))
        except (TypeError, ValueError):
            run["status"] = "partial"
            run["errors"].append("SmartRecruiters response has invalid offset")
            break
        if response_offset != offset:
            run["status"] = "partial"
            run["errors"].append(
                f"SmartRecruiters offset mismatch (requested {offset}, received {response_offset})"
            )
            break

        raw_content = payload["content"]
        run["pages_scanned"] += 1
        run["raw_count"] += len(raw_content)
        if not raw_content and summaries_seen < expected_total:
            run["status"] = "partial"
            run["errors"].append("SmartRecruiters pagination returned an empty page before totalFound")
            break
        page_signature = tuple(
            str(item.get("ref") or item.get("id") or item.get("uuid") or "")
            if isinstance(item, Mapping)
            else ""
            for item in raw_content
        )
        if page_signature and page_signature in seen_page_signatures:
            run["status"] = "partial"
            run["errors"].append("SmartRecruiters pagination repeated a page")
            break
        seen_page_signatures.add(page_signature)

        detail_entries: list[tuple[Mapping[str, Any], str, str]] = []
        for summary in raw_content:
            summaries_seen += 1
            if not isinstance(summary, Mapping):
                run["status"] = "partial"
                run["errors"].append("skipped non-object SmartRecruiters record")
                continue
            posting_identity = str(summary.get("ref") or summary.get("id") or summary.get("uuid") or "").strip()
            if posting_identity and posting_identity in seen_posting_ids:
                run["status"] = "partial"
                run["errors"].append(f"SmartRecruiters pagination repeated posting {posting_identity}")
                continue
            if posting_identity:
                seen_posting_ids.add(posting_identity)
            detail_ref = str(summary.get("ref") or "").strip()
            detail_url = _safe_pagination_url(page_url, page_url, detail_ref) if detail_ref else None
            if detail_url is None:
                run["status"] = "partial"
                run["errors"].append("skipped SmartRecruiters record without a safe same-origin detail ref")
                continue
            detail_entries.append((summary, detail_ref, detail_url))

        def fetch_detail(
            entry: tuple[Mapping[str, Any], str, str],
        ) -> tuple[Mapping[str, Any] | None, list[str]]:
            _summary, detail_ref, detail_url = entry
            errors: list[str] = []
            try:
                detail_status, detail_body, _detail_headers = _call_transport(transport, detail_url)
                if detail_status >= 400:
                    errors.append(
                        f"HTTP {detail_status} for SmartRecruiters detail {detail_url}"
                    )
                    return None, errors
                parsed_detail = _parse_json(detail_body)
                if isinstance(parsed_detail, Mapping):
                    return parsed_detail, errors
                errors.append(f"invalid SmartRecruiters detail object for {detail_url}")
            except Exception as error:  # noqa: BLE001 - one detail must not discard other records
                errors.append(f"SmartRecruiters detail failed for {detail_ref}: {error}")
            return None, errors

        if detail_concurrency > 1 and len(detail_entries) > 1:
            with ThreadPoolExecutor(max_workers=detail_concurrency) as executor:
                detail_results = list(executor.map(fetch_detail, detail_entries))
        else:
            detail_results = [fetch_detail(entry) for entry in detail_entries]

        for (summary, _detail_ref, detail_url), (detail, detail_errors) in zip(
            detail_entries,
            detail_results,
            strict=True,
        ):
            if detail_errors:
                run["status"] = "partial"
                run["errors"].extend(detail_errors)
            job = normalise_job(
                _smartrecruiters_record(summary, detail),
                company,
                "smartrecruiters",
                source_url=detail_url,
            )
            if job is None:
                run["status"] = "partial"
                run["errors"].append("skipped SmartRecruiters record without title or public URL")
            else:
                run["jobs"].append(job)

        if summaries_seen > expected_total:
            run["status"] = "partial"
            run["errors"].append(
                f"SmartRecruiters returned more records ({summaries_seen}) than totalFound ({expected_total})"
            )
            break

        if summaries_seen >= expected_total:
            break
        next_offset = offset + len(raw_content)
        if next_offset <= offset:
            run["status"] = "partial"
            run["errors"].append("SmartRecruiters pagination did not advance")
            break
        offset = next_offset

    if expected_total is not None and len(seen_posting_ids) != expected_total:
        run["status"] = "partial"
        run["errors"].append(
            f"SmartRecruiters unique posting count ({len(seen_posting_ids)}) did not match totalFound ({expected_total})"
        )

    return _finish_run(run)


def _looks_like_application_url(value: Any) -> bool:
    path = urlsplit(str(value or "")).path.casefold().rstrip("/")
    return path.endswith("/apply") or "/candidates/new" in path


def _workable_urls(raw: Mapping[str, Any]) -> tuple[str, str]:
    """Resolve Workable's conflicting documented and live URL field shapes."""
    direct_url = _plain_text(raw.get("url"))
    application_url = _plain_text(raw.get("application_url"))
    shortlink = _plain_text(raw.get("shortlink"))
    job_url = next(
        (
            value
            for value in (direct_url, application_url, shortlink)
            if value and not _looks_like_application_url(value)
        ),
        direct_url or application_url or shortlink,
    )
    apply_url = next(
        (
            value
            for value in (application_url, direct_url, shortlink)
            if value and _looks_like_application_url(value)
        ),
        application_url or direct_url or job_url,
    )
    return job_url, apply_url


def _workable_record(raw: Mapping[str, Any]) -> dict[str, Any]:
    location = _joined_location(raw.get("city"), raw.get("state"), raw.get("country"))
    job_url, apply_url = _workable_urls(raw)
    return {
        "id": raw.get("shortcode") or raw.get("code"),
        "title": raw.get("title"),
        "url": job_url,
        "application_url": apply_url,
        "location": location,
        "description": raw.get("description"),
        "datePosted": raw.get("published_on") or raw.get("created_at"),
        "employment_type": raw.get("employment_type"),
    }


def collect_workable(company: Mapping[str, Any], transport: Transport = _default_transport) -> dict[str, Any]:
    """Read Workable's anonymous public account collection, which is not paginated."""
    run = _empty_run(company)
    account = _company_key(company, "subdomain", "account", "site")
    if not account:
        run["status"] = "partial"
        run["errors"].append("Workable company is missing subdomain/account/site")
        return _finish_run(run)
    url = workable_url(company)
    try:
        status_code, body, _headers = _call_transport(transport, url)
    except Exception as error:  # noqa: BLE001 - transports are an integration boundary
        run["status"] = "partial"
        run["errors"].append(str(error))
        return _finish_run(run)
    if status_code >= 400:
        run["status"] = _error_status(status_code)
        run["errors"].append(f"HTTP {status_code} for {url}")
        return _finish_run(run)
    try:
        payload = _parse_json(body)
    except (TypeError, ValueError) as error:
        run["status"] = "partial"
        run["errors"].append(f"invalid JSON: {error}")
        return _finish_run(run)
    if not isinstance(payload, Mapping) or not isinstance(payload.get("jobs"), list):
        run["status"] = "partial"
        run["errors"].append("Workable response is missing jobs list")
        return _finish_run(run)
    if payload.get("next") or payload.get("paging"):
        run["status"] = "partial"
        run["errors"].append("Workable public response unexpectedly indicated pagination")
    run["pages_scanned"] = 1
    run["raw_count"] = len(payload["jobs"])
    for raw in payload["jobs"]:
        if not isinstance(raw, Mapping):
            run["status"] = "partial"
            run["errors"].append("skipped non-object Workable record")
            continue
        job = normalise_job(_workable_record(raw), company, "workable", source_url=url)
        if job is None:
            run["status"] = "partial"
            run["errors"].append("skipped Workable record without title or public URL")
        else:
            run["jobs"].append(job)
    return _finish_run(run)


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
    "smartrecruiters": collect_smartrecruiters,
    "workable": collect_workable,
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
