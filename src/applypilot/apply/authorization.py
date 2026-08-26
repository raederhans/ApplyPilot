"""Explicit, expiring authorization manifests for real job submissions."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

_FINGERPRINT_FIELDS = (
    "url",
    "application_url",
    "title",
    "company_name",
    "location",
    "full_description",
)
_MANIFEST_FIELDS = {
    "version",
    "batch_id",
    "authorized_at",
    "expires_at",
    "max_submissions",
    "jobs",
}
_ENTRY_FIELDS = {
    "url",
    "application_url",
    "target_host",
    "resume_path",
    "resume_sha256",
    "resume_size",
    "job_fingerprint",
}
_FINAL_AUTHORIZATION_FIELDS = {
    "version",
    "batch_id",
    "manifest_path",
    "manifest_sha256",
    "manifest_size",
    "final_authorized_at",
    "expires_at",
}


def compute_file_binding(path: str | Path) -> tuple[str, int]:
    """Return the SHA-256 digest and byte length for one immutable attachment."""
    file_path = Path(path)
    digest = hashlib.sha256()
    size = 0
    with file_path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def resolve_resume_attachment(job: dict) -> Path:
    """Resolve the exact PDF bytes that the browser will upload for a job."""
    raw_path = str(job.get("tailored_resume_path") or "").strip()
    if not raw_path:
        raise ValueError("tailored_resume_path is required")
    candidate = Path(raw_path).expanduser().resolve()
    if candidate.suffix.casefold() != ".pdf":
        candidate = candidate.with_suffix(".pdf")
    if not candidate.is_file():
        raise FileNotFoundError(f"Tailored resume PDF does not exist: {candidate}")
    return candidate


def compute_job_fingerprint(job: dict) -> str:
    """Return a stable digest binding the job identity and application content."""
    payload = {
        field: str(job.get(field) or "").strip()
        for field in _FINGERPRINT_FIELDS
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


job_fingerprint = compute_job_fingerprint


def build_bound_manifest(
    jobs: list[dict],
    *,
    now: datetime | None = None,
    ttl_minutes: int = 120,
    batch_id: str | None = None,
    max_submissions: int | None = None,
) -> dict:
    """Create an in-memory, exact-job manifest from already-ready jobs.

    This is used for a standing user authorization.  It deliberately retains
    the same immutable URL, host, resume-byte, and job-content bindings as a
    user-supplied manifest; the only thing it removes is the needless
    per-batch confirmation ceremony.
    """
    if not jobs:
        raise ValueError("Cannot create an authorization manifest without jobs")
    if not isinstance(ttl_minutes, int) or isinstance(ttl_minutes, bool) or not 1 <= ttl_minutes <= 240:
        raise ValueError("ttl_minutes must be between 1 and 240")

    authorized_at = now or datetime.now(UTC)
    if authorized_at.tzinfo is None or authorized_at.utcoffset() is None:
        raise ValueError("now must be timezone-aware")

    entries: list[dict] = []
    seen_urls: set[str] = set()
    for index, job in enumerate(jobs):
        job_url = str(job.get("url") or "").strip()
        application_url = str(job.get("application_url") or job_url).strip()
        _https_hostname(job_url, f"jobs[{index}].url")
        target_host = _https_hostname(application_url, f"jobs[{index}].application_url")
        if job_url in seen_urls or application_url in seen_urls:
            raise ValueError(f"jobs[{index}] contains a duplicate URL")
        seen_urls.update((job_url, application_url))

        resume_path = resolve_resume_attachment(job)
        resume_sha256, resume_size = compute_file_binding(resume_path)
        fingerprint_job = dict(job)
        fingerprint_job["application_url"] = application_url
        entries.append(
            {
                "url": job_url,
                "application_url": application_url,
                "target_host": target_host,
                "resume_path": str(resume_path),
                "resume_sha256": resume_sha256,
                "resume_size": resume_size,
                "job_fingerprint": compute_job_fingerprint(fingerprint_job),
            }
        )

    submission_cap = len(entries) if max_submissions is None else max_submissions
    if (
        isinstance(submission_cap, bool)
        or not isinstance(submission_cap, int)
        or not 1 <= submission_cap <= len(entries)
    ):
        raise ValueError("max_submissions must be between 1 and the number of bound jobs")

    return {
        "version": 1,
        "batch_id": batch_id or f"standing-{uuid.uuid4()}",
        "authorized_at": authorized_at.isoformat(),
        "expires_at": (authorized_at + timedelta(minutes=ttl_minutes)).isoformat(),
        "max_submissions": submission_cap,
        "jobs": entries,
    }


def _aware_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a timezone-aware ISO datetime")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"{field} must be a timezone-aware ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone offset")
    return parsed


def _https_hostname(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty HTTPS URL")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{field} must be an HTTPS URL without embedded credentials")
    return parsed.hostname


def load_manifest(path: str | Path, now: datetime | None = None) -> dict:
    """Load and strictly validate one version-1 submission authorization."""
    manifest_path = Path(path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid authorization manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        # Public contract intentionally normalizes every manifest failure to ValueError.
        raise ValueError("Authorization manifest must be a JSON object")  # noqa: TRY004
    if set(manifest) != _MANIFEST_FIELDS:
        raise ValueError("Authorization manifest fields do not match the version-1 schema")
    if manifest.get("version") != 1:
        raise ValueError("Authorization manifest version must be 1")
    if not isinstance(manifest.get("batch_id"), str) or not manifest["batch_id"].strip():
        raise ValueError("batch_id must be a non-empty string")

    authorized_at = _aware_datetime(manifest.get("authorized_at"), "authorized_at")
    expires_at = _aware_datetime(manifest.get("expires_at"), "expires_at")
    if expires_at <= authorized_at:
        raise ValueError("expires_at must be later than authorized_at")
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if current < authorized_at:
        raise ValueError("Authorization manifest is not active yet")
    if current >= expires_at:
        raise ValueError("Authorization manifest has expired")

    maximum = manifest.get("max_submissions")
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0:
        raise ValueError("max_submissions must be a positive integer")
    jobs = manifest.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("jobs must be a non-empty list")
    if maximum > len(jobs):
        raise ValueError("max_submissions cannot exceed the number of authorized jobs")

    seen_urls: set[str] = set()
    validated = deepcopy(manifest)
    for index, entry in enumerate(validated["jobs"]):
        if not isinstance(entry, dict):
            raise ValueError(f"jobs[{index}] must be an object")  # noqa: TRY004
        if set(entry) != _ENTRY_FIELDS:
            raise ValueError(f"jobs[{index}] fields do not match the version-1 schema")
        _https_hostname(entry["url"], f"jobs[{index}].url")
        application_host = _https_hostname(entry["application_url"], f"jobs[{index}].application_url")
        target_host = entry.get("target_host")
        if not isinstance(target_host, str) or target_host != application_host:
            raise ValueError(f"jobs[{index}].target_host must exactly match the application URL hostname")
        if entry["url"] in seen_urls or entry["application_url"] in seen_urls:
            raise ValueError(f"jobs[{index}] contains a duplicate URL")
        seen_urls.update((entry["url"], entry["application_url"]))

        resume_path = Path(str(entry["resume_path"])).expanduser().resolve()
        if not resume_path.is_file():
            raise ValueError(f"jobs[{index}].resume_path does not exist: {resume_path}")
        entry["resume_path"] = str(resume_path)
        resume_digest = entry.get("resume_sha256")
        if not isinstance(resume_digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", resume_digest
        ):
            raise ValueError(f"jobs[{index}].resume_sha256 must be a lowercase SHA-256 digest")
        resume_size = entry.get("resume_size")
        if isinstance(resume_size, bool) or not isinstance(resume_size, int) or resume_size < 0:
            raise ValueError(f"jobs[{index}].resume_size must be a non-negative integer")
        try:
            current_digest, current_size = compute_file_binding(resume_path)
        except OSError as exc:
            raise ValueError(f"jobs[{index}].resume_path could not be read: {resume_path}") from exc
        if current_digest != resume_digest or current_size != resume_size:
            raise ValueError(f"jobs[{index}].resume attachment bytes have changed")
        fingerprint = entry.get("job_fingerprint")
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError(f"jobs[{index}].job_fingerprint must be a lowercase SHA-256 digest")
    return validated


def build_final_authorization(
    manifest_path: str | Path,
    *,
    now: datetime | None = None,
    ttl_minutes: int = 120,
) -> dict:
    """Bind one final batch approval to the exact initial manifest bytes."""
    if not isinstance(ttl_minutes, int) or isinstance(ttl_minutes, bool) or not 1 <= ttl_minutes <= 240:
        raise ValueError("ttl_minutes must be between 1 and 240")
    authorized_at = now or datetime.now(UTC)
    if authorized_at.tzinfo is None or authorized_at.utcoffset() is None:
        raise ValueError("now must be timezone-aware")

    resolved_manifest = Path(manifest_path).expanduser().resolve(strict=True)
    manifest = load_manifest(resolved_manifest, now=authorized_at)
    manifest_expires_at = _aware_datetime(manifest["expires_at"], "expires_at")
    final_expires_at = min(
        manifest_expires_at,
        authorized_at + timedelta(minutes=ttl_minutes),
    )
    if final_expires_at <= authorized_at:
        raise ValueError("Initial batch authorization expires too soon to finalize")
    manifest_sha256, manifest_size = compute_file_binding(resolved_manifest)
    return {
        "version": 1,
        "batch_id": manifest["batch_id"],
        "manifest_path": str(resolved_manifest),
        "manifest_sha256": manifest_sha256,
        "manifest_size": manifest_size,
        "final_authorized_at": authorized_at.isoformat(),
        "expires_at": final_expires_at.isoformat(),
    }


def load_final_authorization(
    final_authorization_path: str | Path,
    manifest_path: str | Path,
    *,
    now: datetime | None = None,
) -> dict:
    """Validate the final approval and return its exact bound batch manifest."""
    final_path = Path(final_authorization_path).expanduser().resolve()
    try:
        final_authorization = json.loads(final_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid final batch authorization: {exc}") from exc
    if not isinstance(final_authorization, dict):
        raise ValueError("Final batch authorization must be a JSON object")  # noqa: TRY004
    if set(final_authorization) != _FINAL_AUTHORIZATION_FIELDS:
        raise ValueError("Final batch authorization fields do not match the version-1 schema")
    if final_authorization.get("version") != 1:
        raise ValueError("Final batch authorization version must be 1")

    authorized_at = _aware_datetime(
        final_authorization.get("final_authorized_at"),
        "final_authorized_at",
    )
    expires_at = _aware_datetime(final_authorization.get("expires_at"), "expires_at")
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if current < authorized_at:
        raise ValueError("Final batch authorization is not active yet")
    if current >= expires_at:
        raise ValueError("Final batch authorization has expired")

    resolved_manifest = Path(manifest_path).expanduser().resolve(strict=True)
    bound_manifest = Path(str(final_authorization.get("manifest_path") or "")).expanduser().resolve()
    if resolved_manifest != bound_manifest:
        raise ValueError("Final authorization is bound to a different initial manifest")
    manifest_sha256, manifest_size = compute_file_binding(resolved_manifest)
    if (
        final_authorization.get("manifest_sha256") != manifest_sha256
        or final_authorization.get("manifest_size") != manifest_size
    ):
        raise ValueError("Initial manifest changed after final authorization")

    manifest = load_manifest(resolved_manifest, now=current)
    if final_authorization.get("batch_id") != manifest.get("batch_id"):
        raise ValueError("Final authorization batch_id does not match the initial manifest")
    validated = deepcopy(manifest)
    validated["_final_submission_authorized"] = True
    validated["_final_authorization_path"] = str(final_path)
    validated["_final_authorized_at"] = authorized_at.isoformat()
    return validated


def authorize_job(manifest: dict, job: dict) -> dict | None:
    """Return the exact authorized entry when all current job bindings match."""
    job_url = str(job.get("url") or "")
    application_url = str(job.get("application_url") or "")
    for entry in manifest.get("jobs", []):
        if entry.get("url") != job_url or entry.get("application_url") != application_url:
            continue
        try:
            current_host = _https_hostname(application_url, "job.application_url")
        except ValueError:
            return None
        if entry.get("target_host") != current_host:
            return None
        try:
            current_resume = resolve_resume_attachment(job).resolve(strict=True)
            authorized_resume = Path(str(entry.get("resume_path") or "")).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            return None
        if not current_resume.is_file() or current_resume != authorized_resume:
            return None
        try:
            current_digest, current_size = compute_file_binding(current_resume)
        except OSError:
            return None
        if (
            entry.get("resume_sha256") != current_digest
            or entry.get("resume_size") != current_size
        ):
            return None
        if entry.get("job_fingerprint") != compute_job_fingerprint(job):
            return None
        return entry
    return None
