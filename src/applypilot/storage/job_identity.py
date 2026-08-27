"""Canonical job identity and URL normalization helpers."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit


def canonicalize_job_url(url: str) -> str:
    """Return a stable listing URL while preserving the platform job identity."""
    if not url:
        return ""
    parsed = urlsplit(url.strip())
    host = parsed.netloc.casefold().removeprefix("www.")
    path = re.sub(r"/+", "/", parsed.path).rstrip("/")
    if host.endswith("linkedin.com"):
        job_id = extract_platform_job_id(url)
        if job_id:
            return f"https://www.linkedin.com/jobs/view/{job_id.split(':', 1)[-1]}"
    tracking_keys = {"fbclid", "gclid", "mc_cid", "mc_eid"}
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in tracking_keys
    ]
    return urlunsplit(
        (
            parsed.scheme.casefold() or "https",
            host,
            path,
            urlencode(query, doseq=True),
            "",
        )
    )


def extract_platform_job_id(url: str) -> str:
    """Extract a stable platform job ID when one is present in a listing URL."""
    if not url:
        return ""
    parsed = urlsplit(url)
    host = parsed.netloc.casefold()
    if "linkedin.com" in host:
        match = re.search(r"/jobs/(?:view/)?(?:[^/?#]*-)?(\d{6,})(?:/|$)", parsed.path)
        if match:
            return f"linkedin:{match.group(1)}"
        query = parse_qs(parsed.query)
        for key in ("currentJobId", "jobId"):
            value = query.get(key, [""])[0]
            if str(value).isdigit():
                return f"linkedin:{value}"
    return ""


def _normalized_identity_text(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _usable_requisition_id(value: object) -> str:
    """Reject ATS display placeholders before using requisitions as identity."""
    raw = str(value or "").strip()
    normalized = _normalized_identity_text(raw)
    placeholders = {
        "n a",
        "na",
        "none",
        "not available",
        "see opening id",
        "see job id",
        "see posting id",
        "tbd",
    }
    return "" if not normalized or normalized in placeholders else raw
