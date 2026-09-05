"""Bounded advancement of unverified radar leads into official-job evidence."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import socket
import ssl
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from applypilot.database import (
    finish_radar_fetch_run,
    ingest_radar_official_jobs,
    reconcile_radar_leads,
    start_radar_fetch_run,
)
from applypilot.discovery.ecosystem import _official_url
from applypilot.discovery.official import collect_jobposting_jsonld

Transport = Callable[..., Any]
_MAX_BODY_BYTES = 2_000_000
_MAX_REDIRECTS = 3
_TIMEOUT_SECONDS = 15.0
_REFRESH_AFTER = timedelta(hours=24)
_USER_AGENT = "ApplyPilotRadarAdvance/0.2 (+read-only-official-verification)"
_BLOCKED_STATUSES = {401, 403, 407, 429}
_PARSER_VERSION = "radar-advance-jsonld-v2"


def _validate_public_https_url(url: str, *, resolve_dns: bool) -> tuple[Any, list[str]]:
    parsed = urlsplit(url)
    if parsed.scheme.casefold() != "https":
        raise ValueError("official URL must use https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("official URL must not contain userinfo")
    hostname = (parsed.hostname or "").rstrip(".").casefold()
    if not hostname:
        raise ValueError("official URL is missing a hostname")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("official URL resolves to a local host")
    addresses: set[str] = set()
    try:
        addresses.add(str(ipaddress.ip_address(hostname)))
    except ValueError:
        if resolve_dns:
            try:
                addresses.update(
                    item[4][0]
                    for item in socket.getaddrinfo(
                        hostname, parsed.port or 443, type=socket.SOCK_STREAM
                    )
                )
            except OSError as exc:
                raise ValueError(f"official URL hostname could not be resolved: {exc}") from exc
    for address in addresses:
        if not ipaddress.ip_address(address).is_global:
            raise ValueError("official URL resolves to a non-public IP address")
    return parsed, sorted(addresses)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a validated IP while retaining hostname TLS verification/SNI."""

    def __init__(self, hostname: str, address: str, port: int, timeout: float):
        super().__init__(hostname, port=port, timeout=timeout, context=ssl.create_default_context())
        self._validated_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._validated_address, self.port), self.timeout, self.source_address
        )
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def _read_response_body(response, max_body_bytes: int) -> bytes:
    body = response.read(max_body_bytes + 1)
    if len(body) > max_body_bytes:
        raise ValueError("official response body exceeds size limit")
    return body


def _default_get_once(
    url: str,
    headers: Mapping[str, str],
    addresses: list[str],
    *,
    timeout_seconds: float,
    max_body_bytes: int,
) -> dict[str, Any]:
    parsed = urlsplit(url)
    target = parsed.path or "/"
    if parsed.query:
        target += f"?{parsed.query}"
    deadline = time.monotonic() + timeout_seconds
    last_error: OSError | None = None
    for address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        connection = _PinnedHTTPSConnection(
            str(parsed.hostname), address, parsed.port or 443, remaining
        )
        try:
            connection.request("GET", target, headers=dict(headers))
            response = connection.getresponse()
            return {
                "status_code": response.status,
                "body": _read_response_body(response, max_body_bytes),
                "headers": {
                    str(key).casefold(): str(value) for key, value in response.getheaders()
                },
            }
        except OSError as exc:
            last_error = exc
        finally:
            connection.close()
    if last_error is not None:
        raise last_error
    raise TimeoutError("official HTTPS request timed out")


def _normalise_transport_response(response: Any, max_body_bytes: int) -> dict[str, Any]:
    if isinstance(response, str):
        body, status, headers = response.encode(), 200, {}
    elif isinstance(response, bytes):
        body, status, headers = response, 200, {}
    elif isinstance(response, Mapping):
        status = int(response.get("status_code", response.get("status", 200)))
        raw_body = response.get("body", response.get("text", b""))
        body = raw_body if isinstance(raw_body, bytes) else str(raw_body).encode()
        headers = {
            str(key).casefold(): str(value)
            for key, value in dict(response.get("headers") or {}).items()
        }
    else:
        raise TypeError("transport must return text, bytes, or a mapping")
    if len(body) > max_body_bytes:
        raise ValueError("official response body exceeds size limit")
    return {"status_code": status, "body": body, "headers": headers}


def safe_public_get(
    url: str,
    headers: Mapping[str, str] | None = None,
    *,
    transport: Transport | None = None,
    timeout_seconds: float = _TIMEOUT_SECONDS,
    max_body_bytes: int = _MAX_BODY_BYTES,
    max_redirects: int = _MAX_REDIRECTS,
) -> dict[str, Any]:
    """Read public HTTPS without proxies, DNS rebinding, or unbounded responses."""
    if timeout_seconds <= 0 or max_body_bytes < 1 or max_redirects < 0:
        raise ValueError("invalid public GET bounds")
    request_headers = {"User-Agent": _USER_AGENT, **dict(headers or {})}
    current_url = str(url).strip()
    for redirect_count in range(max_redirects + 1):
        _parsed, addresses = _validate_public_https_url(
            current_url, resolve_dns=transport is None
        )
        if transport is None:
            response = _default_get_once(
                current_url,
                request_headers,
                addresses,
                timeout_seconds=timeout_seconds,
                max_body_bytes=max_body_bytes,
            )
        else:
            try:
                raw = transport(current_url, headers=request_headers)
            except TypeError:
                raw = transport(current_url)
            response = _normalise_transport_response(raw, max_body_bytes)
        status = int(response["status_code"])
        if status not in {301, 302, 303, 307, 308}:
            response["url"] = current_url
            return response
        location = response["headers"].get("location")
        if not location:
            raise ValueError("official redirect is missing Location")
        if redirect_count >= max_redirects:
            raise ValueError("official redirect limit exceeded")
        current_url = urljoin(current_url, location)
    raise AssertionError("redirect loop bounds were not enforced")


def _jobposting_nodes(value: Any):
    if isinstance(value, list):
        for item in value:
            yield from _jobposting_nodes(item)
    elif isinstance(value, Mapping):
        raw_types = value.get("@type", "")
        types = raw_types if isinstance(raw_types, list) else [raw_types]
        if any(str(item).casefold() == "jobposting" for item in types):
            yield value
        if value.get("@graph") is not None:
            yield from _jobposting_nodes(value["@graph"])


def _company_key(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _valid_through_is_expired(value: Any) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        if "T" not in raw:
            return date.fromisoformat(raw) < datetime.now(UTC).date()
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC) < datetime.now(UTC)
    except ValueError:
        return True


def _host(value: str | None) -> str:
    return (urlsplit(value or "").hostname or "").rstrip(".").casefold()


def _verified_official_url(value: str, *, field: str) -> str:
    verified = _official_url(value, field=field)
    if verified is None:
        raise ValueError(f"{field} is missing")
    host = _host(verified)
    blocked_hosts = (
        "linkedin.com",
        "indeed.com",
        "indeed.sg",
        "mycareersfuture.gov.sg",
    )
    if any(host == item or host.endswith(f".{item}") for item in blocked_hosts):
        raise ValueError(f"{field} must not use an aggregator or ecosystem portal")
    return verified


def _trusted_seed_host(row, final_url: str) -> bool:
    if row["kind"] != "company_seed":
        return True
    final_host = _host(final_url)
    evidence = {
        _host(row["official_url"]),
        str(row["official_domain"] or "").strip().rstrip(".").casefold(),
    }
    evidence.discard("")
    return any(final_host == domain or final_host.endswith(f".{domain}") for domain in evidence)


def _lead_target_review_error(row, *, final_url: str | None = None) -> str | None:
    if row["kind"] != "lead":
        return None
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (TypeError, ValueError):
        payload = {}
    review = payload.get("official_target_review")
    if not isinstance(review, Mapping):
        return "official target lacks recent agent-visible employer review"
    target_url = str(row["url"] or "")
    reviewed_url = str(review.get("url") or "")
    if reviewed_url != target_url:
        return "official target review URL does not match the imported target"
    if review.get("method") != "agent_visible_employer_review":
        return "official target review method is invalid"
    observed_at = _parse_timestamp(review.get("observed_at"))
    now = datetime.now(UTC)
    if observed_at is None or observed_at.utcoffset() != timedelta(0):
        return "official target review timestamp must be ISO UTC"
    age = now - observed_at.astimezone(UTC)
    if age < timedelta(0) or age > _REFRESH_AFTER:
        return "official target review is stale or future-dated"
    if final_url is not None and _host(final_url) != _host(reviewed_url):
        return "final host does not match the reviewed official target host"
    return None


def _verified_jobposting_groups(
    body: str,
    page_url: str,
    queue_company: Any,
    *,
    resolve_dns: bool,
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    reasons: list[str] = []
    expected_company = _company_key(queue_company)
    page_host = _host(page_url)
    soup = BeautifulSoup(body, "html.parser")
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.get_text().lstrip("\ufeff"))
        except (TypeError, ValueError):
            continue
        for node in _jobposting_nodes(payload):
            organization = node.get("hiringOrganization")
            organization_name = (
                str(organization.get("name") or "").strip()
                if isinstance(organization, Mapping)
                else ""
            )
            if not organization_name:
                reasons.append("JobPosting is missing hiringOrganization.name")
                continue
            if not expected_company or _company_key(organization_name) != expected_company:
                reasons.append("hiringOrganization requires company alias confirmation")
                continue
            if _valid_through_is_expired(node.get("validThrough")):
                reasons.append("JobPosting validThrough is expired or invalid")
                continue
            candidate_url = urljoin(page_url, str(node.get("url") or page_url))
            try:
                _validate_public_https_url(candidate_url, resolve_dns=resolve_dns)
            except ValueError as exc:
                reasons.append(f"unsafe JobPosting URL: {exc}")
                continue
            if _host(candidate_url) != page_host:
                reasons.append("cross-site JobPosting URL requires manual review")
                continue
            accepted = dict(node)
            accepted["url"] = candidate_url
            grouped[organization_name].append(accepted)
    return dict(grouped), reasons


def _jsonld_html(nodes: list[dict[str, Any]]) -> str:
    payload = json.dumps(nodes, ensure_ascii=False).replace("</", "<\\/")
    return f'<script type="application/ld+json">{payload}</script>'


def _attempt_metadata(row, **extra) -> dict:
    return {
        "queue_kind": row["kind"],
        "queue_item_id": row["item_id"],
        "source_url": row["source_url"],
        "target_url": row["url"],
        "read_only": True,
        **extra,
    }


def _attempt_source(row) -> dict:
    identity = f"{row['kind']}:{row['item_id']}"
    digest = hashlib.sha256(identity.encode()).hexdigest()[:20]
    return {
        "source_id": f"official:queue:{row['kind']}:{digest}",
        "source_type": "official_careers",
        "provider": "jobposting_jsonld",
        "access_mode": "public_read",
        "base_url": row["url"],
    }


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _recent_attempts(conn) -> dict[tuple[str, str], datetime]:
    rows = conn.execute(
        "SELECT finished_at, started_at, metadata_json FROM radar_fetch_runs "
        "WHERE parser_version = ? ORDER BY started_at DESC",
        (_PARSER_VERSION,),
    ).fetchall()
    latest: dict[tuple[str, str], datetime] = {}
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, ValueError):
            continue
        key = (str(metadata.get("queue_kind") or ""), str(metadata.get("queue_item_id") or ""))
        attempted_at = _parse_timestamp(row["finished_at"] or row["started_at"])
        if all(key) and attempted_at and key not in latest:
            latest[key] = attempted_at
    return latest


def _queue_rows(conn, limit: int):
    rows = conn.execute(
        """
        SELECT 'lead' kind, l.lead_id item_id, l.official_job_url url, l.last_seen_at,
               l.company_id company, l.title, l.source_url, l.status,
               NULL official_domain, NULL official_url, o.payload_json
        FROM radar_leads l
        JOIN radar_source_observations o ON o.observation_key = l.observation_key
        WHERE l.status = 'awaiting_official'
          AND TRIM(COALESCE(l.official_job_url, '')) != ''
        UNION ALL
        SELECT 'company_seed', company_key, careers_url, last_seen_at,
               company_name, NULL,
               (SELECT source_url FROM radar_company_seed_sources s
                WHERE s.company_key = radar_company_seeds.company_key
                ORDER BY s.last_seen_at DESC, s.source_url LIMIT 1),
               status, official_domain, official_url, NULL
        FROM radar_company_seeds
        WHERE status IN ('awaiting_official_careers', 'official_careers_verified')
          AND TRIM(COALESCE(careers_url, '')) != ''
        """
    ).fetchall()
    latest, now = _recent_attempts(conn), datetime.now(UTC)
    eligible = []
    for row in rows:
        attempted = latest.get((row["kind"], row["item_id"]))
        if (
            row["status"] == "official_careers_verified"
            and attempted
            and now - attempted < _REFRESH_AFTER
        ):
            continue
        eligible.append((row, attempted))
    minimum = datetime.min.replace(tzinfo=UTC)
    eligible.sort(
        key=lambda item: (
            item[0]["status"] == "official_careers_verified",
            item[1] or minimum,
            item[0]["last_seen_at"],
            item[0]["kind"],
            item[0]["item_id"],
        )
    )
    return [row for row, _ in eligible[:limit]]


def _unresolved_rows(conn, limit: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT 'lead' kind, lead_id item_id, company_id company, title, source_url, last_seen_at
        FROM radar_leads WHERE status = 'awaiting_official'
          AND TRIM(COALESCE(official_job_url, '')) = ''
        UNION ALL
        SELECT 'company_seed', company_key, company_name, NULL,
               (SELECT source_url FROM radar_company_seed_sources s
                WHERE s.company_key = radar_company_seeds.company_key
                ORDER BY s.last_seen_at DESC, s.source_url LIMIT 1), last_seen_at
        FROM radar_company_seeds WHERE status = 'awaiting_official_careers'
          AND TRIM(COALESCE(careers_url, '')) = ''
        ORDER BY last_seen_at, kind, item_id LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "kind": row["kind"], "item_id": row["item_id"], "company": row["company"],
            "title": row["title"], "source_url": row["source_url"], "target_url": None,
            "status": "pending",
            "reason": "missing official_job_url" if row["kind"] == "lead" else "missing careers_url",
            "next_action": "find_official_job_url" if row["kind"] == "lead" else "find_official_careers_url",
        }
        for row in rows
    ]


def _pending_attempt(row, reason: str, run_id: str, next_action: str | None = None) -> dict:
    result = {
        "kind": row["kind"], "item_id": row["item_id"], "url": row["url"],
        "source_url": row["source_url"], "target_url": row["url"], "status": "pending",
        "reason": reason, "run_id": run_id, "newjobs": 0, "promoted": 0,
    }
    if next_action:
        result["next_action"] = next_action
    return result


def _finish_pending(conn, run_id: str, row, reason: str, blocked: bool = False) -> None:
    if row["kind"] == "lead":
        conn.execute("UPDATE radar_leads SET reason = ? WHERE lead_id = ?", (reason, row["item_id"]))
        conn.commit()
    finish_radar_fetch_run(
        conn, run_id, status="blocked" if blocked else "partial", error=reason,
        metadata=_attempt_metadata(row, attempt_status="pending", reason=reason),
    )


def advance_radar_queue(conn, *, limit: int = 5, transport: Transport | None = None) -> dict:
    """Advance a fair queue slice using fresh, exact official JSON-LD evidence."""
    if not 1 <= limit <= 10:
        raise ValueError("limit must be between 1 and 10")
    attempts, unresolved = [], _unresolved_rows(conn, limit)
    totals = {"newjobs": 0, "existing": 0, "promoted": 0}
    for row in _queue_rows(conn, limit):
        source = _attempt_source(row)
        run_id = start_radar_fetch_run(
            conn, source, parser_version=_PARSER_VERSION,
            metadata=_attempt_metadata(row, attempt_status="running"),
        )
        try:
            _verified_official_url(str(row["url"]), field="target_url")
        except ValueError as exc:
            reason = str(exc)
            _finish_pending(conn, run_id, row, reason, blocked=True)
            attempts.append(_pending_attempt(row, reason, run_id))
            continue
        review_error = _lead_target_review_error(row)
        if review_error:
            _finish_pending(conn, run_id, row, review_error)
            attempts.append(
                _pending_attempt(
                    row,
                    review_error,
                    run_id,
                    "review_employer_page_then_import_reviewed_target",
                )
            )
            continue
        try:
            response = safe_public_get(str(row["url"]).strip(), transport=transport)
        except Exception as exc:  # noqa: BLE001
            reason = f"{type(exc).__name__}: {exc}"
            _finish_pending(conn, run_id, row, reason, blocked=True)
            attempts.append(_pending_attempt(row, reason, run_id))
            continue
        status_code, final_url = int(response["status_code"]), str(response["url"])
        try:
            _verified_official_url(final_url, field="final_url")
        except ValueError as exc:
            reason = str(exc)
            _finish_pending(conn, run_id, row, reason, blocked=True)
            attempts.append(_pending_attempt(row, reason, run_id))
            continue
        if status_code >= 400:
            blocked = status_code in _BLOCKED_STATUSES
            reason = f"{'access blocked' if blocked else 'HTTP error'}: HTTP {status_code}"
            _finish_pending(conn, run_id, row, reason, blocked)
            attempts.append(_pending_attempt(row, reason, run_id))
            continue
        if not _trusted_seed_host(row, final_url):
            reason = "final careers host lacks official_domain or official_url evidence"
            _finish_pending(conn, run_id, row, reason)
            attempts.append(_pending_attempt(row, reason, run_id, "confirm_official_domain"))
            continue
        review_error = _lead_target_review_error(row, final_url=final_url)
        if review_error:
            _finish_pending(conn, run_id, row, review_error)
            attempts.append(
                _pending_attempt(
                    row,
                    review_error,
                    run_id,
                    "review_employer_page_then_import_reviewed_target",
                )
            )
            continue
        groups, rejected = _verified_jobposting_groups(
            response["body"].decode("utf-8", errors="replace"), final_url, row["company"],
            resolve_dns=transport is None,
        )
        if not groups:
            reason = rejected[0] if rejected else "no public JobPosting JSON-LD was verified"
            _finish_pending(conn, run_id, row, reason)
            action = "confirm_company_alias" if "alias" in reason else "manual_review_or_configure_supported_official_provider"
            attempts.append(_pending_attempt(row, reason, run_id, action))
            continue
        jobs = []
        for organization_name, nodes in groups.items():
            company = {"id": "", "name": organization_name, "provider": "jobposting_jsonld", "career_url": final_url}
            cached = lambda *_a, _nodes=nodes, **_k: {"status_code": 200, "text": _jsonld_html(_nodes)}
            for job in collect_jobposting_jsonld(company, cached).get("jobs", []):
                try:
                    _validate_public_https_url(job["url"], resolve_dns=transport is None)
                    _validate_public_https_url(job.get("application_url") or job["url"], resolve_dns=transport is None)
                except (KeyError, ValueError):
                    continue
                if _host(job["url"]) == _host(final_url) == _host(job.get("application_url") or job["url"]):
                    jobs.append(job)
        if not jobs:
            reason = "JobPosting URLs require manual review"
            _finish_pending(conn, run_id, row, reason)
            attempts.append(_pending_attempt(row, reason, run_id, "manual_review"))
            continue
        try:
            counts = ingest_radar_official_jobs(conn, run_id, source, jobs)
            finish_radar_fetch_run(
                conn, run_id, status="complete", pagination_complete=True, pages_fetched=1,
                raw_count=len(jobs) + len(rejected), normalized_count=len(jobs),
                new_count=counts["new"], existing_count=counts["existing"],
                metadata=_attempt_metadata(row, attempt_status="verified", final_url=final_url),
            )
            reconciled = reconcile_radar_leads(conn, official_run_ids=[run_id])
        except Exception as exc:  # noqa: BLE001
            reason = f"official ingest failed: {exc}"
            finish_radar_fetch_run(
                conn, run_id, status="failed", error=reason,
                metadata=_attempt_metadata(row, attempt_status="storage_failed", reason=reason),
            )
            attempts.append(_pending_attempt(row, reason, run_id))
            continue
        if row["kind"] == "company_seed":
            conn.execute(
                "UPDATE radar_company_seeds SET status = 'official_careers_verified', "
                "verification_status = 'official_careers_verified' WHERE company_key = ?",
                (row["item_id"],),
            )
            conn.commit()
            queue_status = "official_careers_verified"
        else:
            current = conn.execute("SELECT status FROM radar_leads WHERE lead_id = ?", (row["item_id"],)).fetchone()
            queue_status = str(current["status"]) if current else "missing"
        totals["newjobs"] += int(counts["new"])
        totals["existing"] += int(counts["existing"])
        totals["promoted"] += int(reconciled["promoted"])
        attempts.append({
            "kind": row["kind"], "item_id": row["item_id"], "url": row["url"],
            "source_url": row["source_url"], "target_url": row["url"], "final_url": final_url,
            "status": "verified", "reason": None, "run_id": run_id,
            "newjobs": int(counts["new"]), "existing": int(counts["existing"]),
            "promoted": int(reconciled["promoted"]), "queue_status": queue_status,
        })
    return {
        "attempted": len(attempts), **totals,
        "pending": sum(item["status"] == "pending" for item in attempts),
        "attempts": attempts, "unresolved": unresolved,
    }
