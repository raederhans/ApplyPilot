"""Validated registry and pure normalization for ecosystem discovery seeds."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from applypilot.config import CONFIG_DIR

_REGISTRY_PATH = CONFIG_DIR / "ecosystem_sources.yaml"
_RECORD_KINDS = {"job_lead", "company_seed"}
_ACCESS_MODES = {
    "candidate_visible_login",
    "singpass_login",
    "public_manual_review",
    "public_directory",
}
_COLLECTION_MODES = {"manual_url_import", "public_company_seed", "bounded_job_search"}
_OFFICIAL_URL_POLICY = "reject_ecosystem_hosts"
_HOST_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_COMPOUND_PUBLIC_SUFFIXES = {
    "co.in",
    "co.jp",
    "co.uk",
    "com.au",
    "com.sg",
    "edu.sg",
    "gov.sg",
    "net.sg",
    "org.sg",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _validate_host(value: Any, *, source_id: str) -> str:
    host = _text(value).casefold().rstrip(".")
    if not host or ":" in host or "/" in host or not _HOST_RE.fullmatch(host):
        raise ValueError(f"ecosystem source {source_id} has invalid allowed host: {value!r}")
    return host


def _host_matches(host: str, allowed_host: str) -> bool:
    return host == allowed_host or host.endswith(f".{allowed_host}")


def _https_url(value: Any, *, field: str) -> tuple[str, str]:
    url = _text(value)
    parsed = urlsplit(url)
    if parsed.scheme.casefold() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{field} must be an HTTPS URL with a public host")
    return url, parsed.hostname.casefold().rstrip(".")


def _validate_source(source: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(source)
    source_id = _text(source.get("id"))
    if not source_id:
        raise ValueError("ecosystem source is missing id")
    normalized["id"] = source_id
    if not _text(source.get("name")):
        raise ValueError(f"ecosystem source {source_id} is missing name")
    if not isinstance(source.get("enabled"), bool):
        raise TypeError(f"ecosystem source {source_id} enabled must be boolean")

    kinds = source.get("record_kinds")
    if not isinstance(kinds, list) or not kinds or any(kind not in _RECORD_KINDS for kind in kinds):
        raise ValueError(f"ecosystem source {source_id} has invalid record_kinds")
    if len(set(kinds)) != len(kinds):
        raise ValueError(f"ecosystem source {source_id} has duplicate record_kinds")
    normalized["record_kinds"] = list(kinds)

    if source.get("coverage_mode") != "non_exhaustive":
        raise ValueError(f"ecosystem source {source_id} must use non_exhaustive coverage")
    if source.get("access_mode") not in _ACCESS_MODES:
        raise ValueError(f"ecosystem source {source_id} has invalid access_mode")
    collection_modes = source.get("collection_modes")
    if not isinstance(collection_modes, Mapping) or set(collection_modes) != set(kinds):
        raise ValueError(
            f"ecosystem source {source_id} collection_modes must match record_kinds"
        )
    if any(mode not in _COLLECTION_MODES for mode in collection_modes.values()):
        raise ValueError(f"ecosystem source {source_id} has invalid collection mode")
    normalized["collection_modes"] = dict(collection_modes)
    if source.get("employer_official_url_policy") != _OFFICIAL_URL_POLICY:
        raise ValueError(f"ecosystem source {source_id} has invalid employer official URL policy")
    if not _text(source.get("publisher_type")):
        raise ValueError(f"ecosystem source {source_id} is missing publisher_type")
    if not _text(source.get("login_requirement")):
        raise ValueError(f"ecosystem source {source_id} is missing login_requirement")

    hosts = source.get("allowed_hosts")
    if not isinstance(hosts, list) or not hosts:
        raise ValueError(f"ecosystem source {source_id} requires allowed_hosts")
    normalized["allowed_hosts"] = [_validate_host(host, source_id=source_id) for host in hosts]
    if len(set(normalized["allowed_hosts"])) != len(normalized["allowed_hosts"]):
        raise ValueError(f"ecosystem source {source_id} has duplicate allowed_hosts")

    discovery_url, discovery_host = _https_url(source.get("discovery_url"), field="discovery_url")
    if not any(_host_matches(discovery_host, host) for host in normalized["allowed_hosts"]):
        raise ValueError(f"ecosystem source {source_id} discovery_url is outside allowed_hosts")
    normalized["discovery_url"] = discovery_url

    disable_reason = _text(source.get("disable_reason"))
    if not source["enabled"] and not disable_reason:
        raise ValueError(f"disabled ecosystem source {source_id} requires disable_reason")
    normalized["disable_reason"] = disable_reason or None
    return normalized


def load_ecosystem_sources(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Load and strictly validate the ecosystem source registry."""
    registry_path = _REGISTRY_PATH if path is None else Path(path)
    with registry_path.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    raw_sources = payload.get("sources") if isinstance(payload, Mapping) else None
    if not isinstance(raw_sources, list):
        raise TypeError("ecosystem_sources.yaml must contain a sources list")

    sources: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_source in raw_sources:
        if not isinstance(raw_source, Mapping):
            raise TypeError("each ecosystem source must be a mapping")
        source = _validate_source(raw_source)
        folded_id = source["id"].casefold()
        if folded_id in seen_ids:
            raise ValueError(f"ecosystem registry contains duplicate id: {source['id']}")
        seen_ids.add(folded_id)
        sources.append(source)
    return sources


def get_ecosystem_source(
    source_id: str,
    *,
    include_disabled: bool = False,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Return one configured source, rejecting unknown and disabled IDs."""
    wanted = _text(source_id).casefold()
    for source in load_ecosystem_sources(path):
        if source["id"].casefold() != wanted:
            continue
        if not source["enabled"] and not include_disabled:
            raise ValueError(f"ecosystem source is disabled: {source['id']} ({source['disable_reason']})")
        return source
    raise KeyError(f"unknown ecosystem source: {source_id}")


def _resolved_source(source: str | Mapping[str, Any]) -> dict[str, Any]:
    resolved = get_ecosystem_source(source) if isinstance(source, str) else _validate_source(source)
    if not resolved["enabled"]:
        raise ValueError(f"ecosystem source is disabled: {resolved['id']} ({resolved['disable_reason']})")
    return resolved


def radar_source_descriptor(source: str | Mapping[str, Any], record_kind: str) -> dict[str, Any]:
    """Build the storage-facing descriptor for one supported record kind."""
    resolved = _resolved_source(source)
    if record_kind not in resolved["record_kinds"]:
        raise ValueError(f"ecosystem source {resolved['id']} does not support {record_kind}")
    return {
        "source_id": resolved["id"],
        "source_type": "ecosystem_lead" if record_kind == "job_lead" else "company_seed",
        "provider": resolved["publisher_type"],
        "access_mode": resolved["access_mode"],
        "base_url": resolved["discovery_url"],
        "active": True,
        "coverage_mode": resolved["coverage_mode"],
        "collection_mode": resolved["collection_modes"][record_kind],
        "record_kind": record_kind,
    }


def _source_url(row: Mapping[str, Any], source: Mapping[str, Any]) -> str:
    url, host = _https_url(row.get("source_url"), field="source_url")
    if not any(_host_matches(host, allowed) for allowed in source["allowed_hosts"]):
        raise ValueError(f"source_url host is not allowed for ecosystem source {source['id']}")
    return url


def _ecosystem_hosts() -> set[str]:
    return {host for source in load_ecosystem_sources() for host in source["allowed_hosts"]}


def _official_url(value: Any, *, field: str) -> str | None:
    if value in (None, ""):
        return None
    url, host = _https_url(value, field=field)
    if any(_host_matches(host, portal_host) for portal_host in _ecosystem_hosts()):
        raise ValueError(f"{field} must be an employer-controlled URL, not an ecosystem portal URL")
    return url


def _registrable_domain(host: str) -> str:
    labels = host.removeprefix("www.").split(".")
    if len(labels) <= 2:
        return ".".join(labels)
    suffix_length = 2 if ".".join(labels[-2:]) in _COMPOUND_PUBLIC_SUFFIXES else 1
    return ".".join(labels[-(suffix_length + 1) :])


def _required_text(row: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = _text(row.get(key))
        if value:
            return value
    raise ValueError(f"record requires {keys[0]}")


def _text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = re.split(r"[;,|]", value)
    elif isinstance(value, (list, tuple, set)):
        values = value
    elif value in (None, ""):
        values = []
    else:
        raise TypeError("list field must be a sequence or delimited string")
    return list(dict.fromkeys(item for raw in values if (item := _text(raw))))


def normalize_job_lead(row: Mapping[str, Any], source: str | Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one untrusted ecosystem row into an unverified job lead."""
    resolved = _resolved_source(source)
    descriptor = radar_source_descriptor(resolved, "job_lead")
    company_id = _text(row.get("company_id")) or None
    company_name = _text(row.get("company_name") or row.get("company")) or company_id
    if not company_name:
        raise ValueError("record requires company_name")
    normalized = {
        "source_id": resolved["id"],
        "source_type": descriptor["source_type"],
        "title": _required_text(row, "title", "job_title"),
        "company_id": company_id or company_name,
        "company_name": company_name,
        "location": _text(row.get("location")) or None,
        "source_url": _source_url(row, resolved),
        "official_job_url": _official_url(row.get("official_job_url"), field="official_job_url"),
        "publisher_name": _text(row.get("publisher_name")) or resolved["name"],
        "publisher_type": resolved["publisher_type"],
        "published_at": _text(row.get("published_at")) or None,
        "closing_at": _text(row.get("closing_at")) or None,
        "employment_type": _text(row.get("employment_type")) or None,
        "external_id": _text(row.get("external_id")) or None,
        "subtracks": _text_list(row.get("subtracks") or row.get("track_tags")),
        "status": "awaiting_official",
        "verification_status": "unverified",
    }
    return normalized


def company_seed_identity(
    company_name: str,
    *,
    official_url: str | None = None,
    careers_url: str | None = None,
) -> str:
    """Return a source-independent company identity, preferring official host evidence."""
    official_host = ""
    for candidate, field in ((official_url, "official_url"), (careers_url, "careers_url")):
        if candidate:
            _, official_host = _https_url(candidate, field=field)
            official_host = _registrable_domain(official_host)
            break
    normalized_name = " ".join(_text(company_name).casefold().split())
    if not normalized_name:
        raise ValueError("company seed requires company_name")
    identity = f"domain:{official_host}" if official_host else f"name:{normalized_name}"
    return f"company:{hashlib.sha256(identity.encode()).hexdigest()[:24]}"


def normalize_company_seed(row: Mapping[str, Any], source: str | Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one ecosystem directory row without inventing a job listing."""
    resolved = _resolved_source(source)
    descriptor = radar_source_descriptor(resolved, "company_seed")
    company_name = _required_text(row, "company_name")
    source_url = _source_url(row, resolved)
    official_url = _official_url(row.get("official_url"), field="official_url")
    careers_url = _official_url(row.get("careers_url"), field="careers_url")
    official_domain = ""
    if official_url or careers_url:
        _, official_host = _https_url(official_url or careers_url, field="official_url")
        official_domain = _registrable_domain(official_host)
    return {
        "company_key": company_seed_identity(
            company_name,
            official_url=official_url,
            careers_url=careers_url,
        ),
        "source_id": resolved["id"],
        "source_type": descriptor["source_type"],
        "company_name": company_name,
        "source_url": source_url,
        "official_url": official_url,
        "careers_url": careers_url,
        "official_domain": official_domain or None,
        "location": _text(row.get("location")) or None,
        "sectors": _text_list(row.get("sectors")),
        "track_tags": _text_list(row.get("track_tags")),
        "status": "awaiting_official_careers",
        "verification_status": "company_seed_unverified",
    }
