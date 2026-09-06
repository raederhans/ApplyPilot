"""Fail-closed SuccessFactors job, authentication, and final-route bindings."""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from html.parser import HTMLParser
from urllib.parse import (
    ParseResult,
    parse_qsl,
    unquote,
    urlencode,
    urljoin,
    urlparse,
    urlunparse,
)

SUCCESSFACTORS_CREDENTIAL_HOST_SUFFIXES = (
    "successfactors.com",
    "successfactors.eu",
    "sapsf.com",
)

_SAFE_TOKEN_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")
_PUBLIC_POSTING_PATH_RE = re.compile(r"/(\d+)(?:[-_][^/]*)?/?$")
_REQ_ID_RE = re.compile(
    r"\b(?:Req|Requisition)\s+ID\s*:\s*(\d+)\b",
    re.IGNORECASE,
)
_SSO_COMPANY_RE = re.compile(
    r"[\"']?ssoCompanyId[\"']?\s*:\s*[\"']([^\"']+)[\"']"
)
_SSO_URL_RE = re.compile(r"[\"']?ssoUrl[\"']?\s*:\s*[\"']([^\"']+)[\"']")
_BLOCKED_AUTH_RE = re.compile(
    r"forgot|recover|recovery|reset|unlock|password[-_]?help",
    re.IGNORECASE,
)
_IDENTITY_QUERY_KEYS = {
    "career_job_req_id",
    "career_ns",
    "company",
    "login_ns",
}


class _PublicPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.apply_hrefs: list[str] = []
        self.visible_text: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        normalized = tag.casefold()
        if normalized in {"script", "style", "template"}:
            self._hidden_depth += 1
        if normalized != "a":
            return
        href = next(
            (value for key, value in attrs if key.casefold() == "href" and value),
            "",
        )
        if re.search(r"(?:^|/)talentcommunity/apply/", href, re.IGNORECASE):
            self.apply_hrefs.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "template"} and self._hidden_depth:
            self._hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth and data.strip():
            self.visible_text.append(data)


def _host_matches_suffix(host: str, suffix: str) -> bool:
    normalized = host.casefold().strip(".")
    expected = suffix.casefold().strip(".")
    return normalized == expected or normalized.endswith(f".{expected}")


def successfactors_credential_host(host: object) -> bool:
    """Return true only for an exact configured SuccessFactors host suffix."""
    normalized = str(host or "").strip().casefold()
    return bool(
        normalized
        and any(
            _host_matches_suffix(normalized, suffix)
            for suffix in SUCCESSFACTORS_CREDENTIAL_HOST_SUFFIXES
        )
    )


def _safe_https_url(value: object) -> ParseResult | None:
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    route_authority = f"{parsed.netloc}{parsed.path}"
    if (
        re.search(r"%(?:2f|5c)", route_authority, re.IGNORECASE)
        or "\\" in route_authority
    ):
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        return None
    return parsed


def _single_query_values(value: object) -> dict[str, str] | None:
    parsed = _safe_https_url(value)
    if parsed is None:
        return None
    values: dict[str, str] = {}
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        normalized = key.casefold()
        if normalized not in _IDENTITY_QUERY_KEYS:
            continue
        if normalized in values:
            return None
        values[normalized] = item
    return values


def _normalized_origin_path(parsed: ParseResult) -> tuple[str, str]:
    return (
        parsed.hostname.casefold(),
        unquote(parsed.path).rstrip("/") or "/",
    )


def _unresolved(reason: str) -> dict[str, object]:
    return {
        "provider": "successfactors",
        "resolved": False,
        "reason": reason,
    }


def _public_job_route(value: object) -> tuple[ParseResult, str] | None:
    parsed = _safe_https_url(value)
    if parsed is None or not re.search(
        r"(?:^|/)job(?:/|$)", parsed.path, re.IGNORECASE
    ):
        return None
    identity = _PUBLIC_POSTING_PATH_RE.search(parsed.path)
    if identity is None:
        return None
    return parsed, identity.group(1)


def successfactors_probe_candidate(job: Mapping[str, object]) -> bool:
    """Select a narrow Jobs2web candidate without granting provider authority."""
    description = "\n".join(
        str(job.get(key) or "") for key in ("description", "full_description")
    )
    requisition_ids = set(_REQ_ID_RE.findall(description))
    if len(requisition_ids) != 1:
        return False

    url = str(job.get("url") or "").strip()
    application_url = str(job.get("application_url") or "").strip()
    selected = application_url or url
    selected_job = _public_job_route(selected)
    if selected_job is not None:
        return True

    selected_parsed = _safe_https_url(selected)
    source_job = _public_job_route(url)
    if selected_parsed is None or source_job is None:
        return False
    apply_identity = re.fullmatch(
        r"/talentcommunity/apply/(\d+)/?",
        selected_parsed.path,
        re.IGNORECASE,
    )
    return bool(
        apply_identity
        and selected_parsed.hostname.casefold() == source_job[0].hostname.casefold()
        and apply_identity.group(1) == source_job[1]
    )


def parse_successfactors_public_job_page(
    source_url: str,
    html: str,
) -> dict[str, object] | None:
    """Extract one safe official Jobs2web job-to-SSO tuple from public HTML."""
    source = _safe_https_url(source_url)
    if source is None:
        return _unresolved("source_url_invalid")
    source_match = _PUBLIC_POSTING_PATH_RE.search(source.path)
    markers_present = any(
        marker in html
        for marker in ("ssoCompanyId", "ssoUrl", "talentcommunity/apply")
    )
    if not markers_present:
        return None
    if not source_match:
        return _unresolved("source_posting_identity_missing")
    source_posting_id = source_match.group(1)

    company_matches = _SSO_COMPANY_RE.findall(html)
    sso_url_matches = _SSO_URL_RE.findall(html)
    if len(company_matches) != 1 or not _SAFE_TOKEN_RE.fullmatch(company_matches[0]):
        return _unresolved("sso_company_identity_invalid")
    if len(sso_url_matches) != 1:
        return _unresolved("sso_url_identity_invalid")
    company = company_matches[0]
    sso = _safe_https_url(sso_url_matches[0])
    if (
        sso is None
        or not successfactors_credential_host(sso.hostname)
        or sso.path not in {"", "/"}
        or sso.query
        or sso.fragment
    ):
        return _unresolved("sso_url_identity_invalid")

    parser = _PublicPageParser()
    try:
        parser.feed(html)
    except ValueError:
        return _unresolved("public_html_invalid")
    visible_req_ids = set(_REQ_ID_RE.findall(" ".join(parser.visible_text)))
    if len(visible_req_ids) != 1:
        return _unresolved("visible_requisition_identity_invalid")
    job_req_id = next(iter(visible_req_ids))

    public_apply_urls: set[str] = set()
    for href in parser.apply_hrefs:
        absolute = urljoin(source_url, href)
        candidate = _safe_https_url(absolute)
        if candidate is None:
            return _unresolved("public_apply_url_invalid")
        if (
            candidate.hostname.casefold() != source.hostname.casefold()
            or candidate.path.rstrip("/")
            != f"/talentcommunity/apply/{source_posting_id}"
        ):
            return _unresolved("public_apply_identity_mismatch")
        public_apply_urls.add(
            urlunparse(("https", candidate.hostname.casefold(), candidate.path, "", "", ""))
        )
    if len(public_apply_urls) != 1:
        return _unresolved("public_apply_identity_invalid")

    ats_host = sso.hostname.casefold()
    source_host = source.hostname.casefold()
    safe_source_url = urlunparse(("https", source_host, source.path, "", "", ""))
    job_application_url = urlunparse(
        (
            "https",
            ats_host,
            "/career",
            "",
            urlencode(
                (
                    ("company", company),
                    ("career_ns", "job_application"),
                    ("career_job_req_id", job_req_id),
                )
            ),
            "",
        )
    )
    return {
        "provider": "successfactors",
        "resolved": True,
        "source_url": safe_source_url,
        "source_host": source_host,
        "source_posting_id": source_posting_id,
        "public_apply_url": next(iter(public_apply_urls)),
        "ats_host": ats_host,
        "company": company,
        "job_req_id": job_req_id,
        "job_application_url": job_application_url,
        "evidence_kind": "official_jobs2web_dom",
    }


def _default_public_page_transport(url: str) -> Mapping[str, object]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "ApplyPilot/0.1 (+application-identity-binding)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return {
                "status_code": int(getattr(response, "status", 200)),
                "final_url": str(response.geturl()),
                "html": response.read().decode("utf-8", errors="replace"),
            }
    except urllib.error.HTTPError as error:
        return {"status_code": int(error.code), "html": ""}
    except (OSError, ValueError) as error:
        return {"status_code": 0, "html": "", "error": type(error).__name__}


def resolve_successfactors_application_binding(
    job: Mapping[str, object],
    *,
    transport: Callable[[str], Mapping[str, object]] | None = None,
) -> dict[str, object] | None:
    """Resolve one consistent SuccessFactors binding from official public pages."""
    if not successfactors_probe_candidate(job):
        return None
    candidates = tuple(
        dict.fromkeys(
            str(value).strip()
            for value in (job.get("url"), job.get("application_url"))
            if str(value or "").strip()
        )
    )
    fetch = transport or _default_public_page_transport
    resolved: list[dict[str, object]] = []
    recognized_failure: dict[str, object] | None = None
    for candidate in candidates:
        public_job = _public_job_route(candidate)
        if public_job is None:
            continue
        parsed_candidate, _source_posting_id = public_job
        response = fetch(candidate)
        if not isinstance(response, Mapping):
            continue
        try:
            status_code = int(response.get("status_code") or 0)
        except (TypeError, ValueError):
            status_code = 0
        html = response.get("html")
        if status_code != 200 or not isinstance(html, str):
            recognized_failure = _unresolved(
                f"official_job_http_{status_code or 'unavailable'}"
            )
            continue
        final_url = str(response.get("final_url") or candidate)
        parsed_final = _safe_https_url(final_url)
        if parsed_final is None or _normalized_origin_path(
            parsed_candidate
        ) != _normalized_origin_path(parsed_final):
            parsed = parse_successfactors_public_job_page(candidate, html)
            if parsed is not None:
                recognized_failure = _unresolved("source_redirect_identity_mismatch")
            continue
        parsed = parse_successfactors_public_job_page(final_url, html)
        if parsed is None:
            continue
        if parsed.get("resolved") is not True:
            recognized_failure = parsed
            continue
        resolved.append(parsed)
    if not resolved:
        return recognized_failure
    identities = {
        (
            str(item["source_posting_id"]),
            str(item["ats_host"]).casefold(),
            str(item["company"]).casefold(),
            str(item["job_req_id"]),
        )
        for item in resolved
    }
    if len(identities) != 1:
        return _unresolved("official_job_identity_conflict")
    return resolved[0]


def successfactors_binding_is_resolved(binding: object) -> bool:
    if not isinstance(binding, Mapping):
        return False
    return bool(
        binding.get("resolved") is True
        and str(binding.get("provider") or "").casefold() == "successfactors"
        and successfactors_credential_host(binding.get("ats_host"))
        and _SAFE_TOKEN_RE.fullmatch(str(binding.get("company") or ""))
        and re.fullmatch(r"\d+", str(binding.get("job_req_id") or ""))
    )


def successfactors_auth_url_is_bound(
    actual_url: str,
    binding: Mapping[str, object],
) -> bool:
    """Allow ordinary account login for the resolved employer; reject conflicting job IDs."""
    if not successfactors_binding_is_resolved(binding):
        return False
    actual = _safe_https_url(actual_url)
    if actual is None or actual.hostname.casefold() != str(binding["ats_host"]).casefold():
        return False
    if actual.path.rstrip("/").casefold() not in {"/career", "/careers"}:
        return False
    if any(
        _BLOCKED_AUTH_RE.search(key)
        for key, _value in parse_qsl(actual.query, keep_blank_values=True)
    ):
        return False
    values = _single_query_values(actual_url)
    if values is None or values.get("company", "").casefold() != str(
        binding["company"]
    ).casefold():
        return False
    if values.get("career_job_req_id", str(binding["job_req_id"])) != str(
        binding["job_req_id"]
    ):
        return False
    if values.get("career_ns", "job_application").casefold() != "job_application":
        return False
    login_ns = values.get("login_ns", "")
    if _BLOCKED_AUTH_RE.search(login_ns):
        return False
    if login_ns and login_ns.casefold() != "register":
        return False
    return not _BLOCKED_AUTH_RE.search(actual.path)


def successfactors_final_application_is_bound(
    expected_url: str,
    actual_url: str,
    snapshot: Mapping[str, object],
    binding: Mapping[str, object],
) -> bool:
    """Require an exact requisition application/review route before Submit."""
    if not successfactors_binding_is_resolved(binding):
        return False
    expected = _safe_https_url(expected_url)
    actual = _safe_https_url(actual_url)
    source = _safe_https_url(binding.get("source_url"))
    if expected is None or actual is None or source is None:
        return False
    expected_id = _PUBLIC_POSTING_PATH_RE.search(expected.path)
    expected_route = _normalized_origin_path(expected)
    allowed_expected_routes = {
        _normalized_origin_path(source)
    }
    public_apply = _safe_https_url(binding.get("public_apply_url"))
    if public_apply is not None:
        allowed_expected_routes.add(_normalized_origin_path(public_apply))
    if (
        expected_route not in allowed_expected_routes
        or not expected_id
        or expected_id.group(1) != str(binding.get("source_posting_id") or "")
        or actual.hostname.casefold() != str(binding["ats_host"]).casefold()
        or actual.path.rstrip("/").casefold() != "/career"
    ):
        return False
    values = _single_query_values(actual_url)
    if values is None:
        return False
    return bool(
        values.get("company", "").casefold() == str(binding["company"]).casefold()
        and values.get("career_ns", "").casefold() == "job_application"
        and values.get("career_job_req_id") == str(binding["job_req_id"])
        and not values.get("login_ns")
        and not _BLOCKED_AUTH_RE.search(actual.path)
        and int(snapshot.get("submit_control_count") or 0) > 0
    )
