"""Composition helpers for parent-owned semantic resume upload operations."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

from applypilot.apply.semantic_browser_ops import (
    BoundResumeArtifact,
    ResumeUploadObservation,
    ResumeUploadPostcondition,
    ResumeUploadRequest,
    SemanticWriteDenied,
    resume_postcondition_digest,
)
from applypilot.apply.semantic_resume_upload import (
    ADAPTER_VERSION,
    LocalPdfArtifact,
    observe_resume_upload,
    provider_acceptance_verified,
    upload_resume,
)
from applypilot.apply.semantic_resume_upload import (
    ResumeUploadObservation as DriverObservation,
)
from applypilot.storage import semantic_browser_writes as write_journal

_SUPPORTED_PROVIDER_HOSTS = {
    "workday": ("myworkdayjobs.com", "myworkdaysite.com"),
    "smartrecruiters": ("smartrecruiters.com",),
}
_REASON_CHARACTER_RE = re.compile(r"[^a-z0-9_.:-]+")


class SemanticResumeTargetError(SemanticWriteDenied):
    """The supported ATS page did not expose one exact safe resume target."""

    def __init__(self, status: str, reason: str) -> None:
        super().__init__(f"semantic resume target {status}: {reason}")
        self.status = status
        self.reason = reason


class PlaywrightResumeUploadDriver:
    """Bridge the provider driver to the core semantic operation protocol."""

    def __init__(self, surface: object, provider: str) -> None:
        self._surface = surface
        self._provider = provider

    @property
    def adapter_version(self) -> str:
        return ADAPTER_VERSION

    def discover(self) -> DriverObservation:
        return observe_resume_upload(self._surface, self._provider)  # type: ignore[arg-type]

    def _required_observation(
        self,
        expected: LocalPdfArtifact | None = None,
    ) -> DriverObservation:
        observed = observe_resume_upload(  # type: ignore[arg-type]
            self._surface,
            self._provider,
            expected=expected,
        )
        if observed.status != "ready" or not observed.container_key:
            raise SemanticResumeTargetError(observed.status, observed.reason)
        return observed

    def observe_resume(self, request: ResumeUploadRequest) -> ResumeUploadObservation:
        observed = self._required_observation(
            LocalPdfArtifact(
                path=request.artifact.path,
                filename=request.artifact.filename,
                size_bytes=request.artifact.size_bytes,
            )
        )
        accepted = provider_acceptance_verified(observed)
        return ResumeUploadObservation(
            container_key=observed.container_key or "",
            filename=observed.uploaded_filename if accepted else None,
            size_bytes=observed.uploaded_size if accepted else None,
        )

    def upload_resume(self, request: ResumeUploadRequest) -> None:
        observed = self._required_observation()
        if observed.container_key != request.container_key:
            raise SemanticResumeTargetError("manual", "container_identity_changed")
        result = upload_resume(
            observed,
            LocalPdfArtifact(
                path=request.artifact.path,
                filename=request.artifact.filename,
                size_bytes=request.artifact.size_bytes,
            ),
        )
        if result.status != "uploaded":
            raise SemanticResumeTargetError(result.status, result.reason)


class DurableSemanticWriteLifecycle:
    """Journal callbacks consumed by ``SemanticBrowserOps``."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        operation_id: str,
        operation_digest: str,
    ) -> None:
        self._connection = connection
        self._operation_id = operation_id
        self._operation_digest = operation_digest

    def _record(self) -> write_journal.SemanticWriteRecord:
        record = write_journal.get_operation(self._connection, self._operation_id)
        if record is None or record.operation_digest != self._operation_digest:
            raise write_journal.SemanticWriteCollision(
                "semantic lifecycle operation binding changed"
            )
        return record

    def _require_digest(self, operation_digest: str) -> None:
        if operation_digest != self._operation_digest:
            raise write_journal.SemanticWriteCollision(
                "semantic lifecycle digest does not match the claimed operation"
            )

    def require_claimed(self, operation_digest: str) -> None:
        self._require_digest(operation_digest)
        record = self._record()
        if record.state != "started" or record.dispatch_count not in (1, 2):
            raise write_journal.SemanticWriteTransitionError(
                "semantic operation is not in a claimed dispatch state"
            )

    def mark_effect_observed(self, operation_digest: str) -> None:
        self._require_digest(operation_digest)
        write_journal.mark_effect_observed(self._connection, self._operation_id)

    def mark_verified(self, operation_digest: str, result_epoch: int) -> None:
        self._require_digest(operation_digest)
        write_journal.mark_verified(
            self._connection,
            self._operation_id,
            resulting_page_epoch=result_epoch,
        )

    def park_unknown(self, operation_digest: str, reason: str) -> None:
        self._require_digest(operation_digest)
        write_journal.park_side_effect_unknown(
            self._connection,
            self._operation_id,
            reason_code=_reason_code(reason, prefix="write_unknown"),
        )

    def park_stale_after_effect(
        self,
        operation_digest: str,
        expected_epoch: int,
    ) -> None:
        self._require_digest(operation_digest)
        record = self._record()
        if expected_epoch != record.expected_page_epoch:
            raise write_journal.SemanticWriteTransitionError(
                "semantic stale-effect epoch does not match the journal"
            )
        write_journal.park_stale_after_effect(
            self._connection,
            self._operation_id,
            reason_code="page_epoch_cas_failed",
        )


def provider_for_url(value: object) -> str | None:
    """Return a supported exact ATS provider from an HTTPS page URL."""

    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme.casefold() != "https" or not host:
        return None
    for provider, domains in _SUPPORTED_PROVIDER_HOSTS.items():
        if any(host == domain or host.endswith(f".{domain}") for domain in domains):
            return provider
    return None


def application_binding_hash(
    job: Mapping[str, object],
    authorization_entry: Mapping[str, object],
    *,
    page_url: object,
) -> str:
    """Bind the attempt, authorized job identity, route, and observed page."""

    parsed = urlparse(str(page_url or ""))
    page_identity = {
        "host": (parsed.hostname or "").casefold().rstrip("."),
        "path": parsed.path or "/",
    }
    return canonical_digest(
        {
            "schema_version": 1,
            "attempt_id": str(job.get("_attempt_id") or ""),
            "job_url": str(job.get("url") or ""),
            "application_url": str(job.get("application_url") or ""),
            "authorized_application_url": str(
                authorization_entry.get("application_url") or ""
            ),
            "authorized_target_host": str(
                authorization_entry.get("target_host") or ""
            ),
            "job_fingerprint": str(
                authorization_entry.get("job_fingerprint") or ""
            ),
            "page": page_identity,
        }
    )


def material_binding_hash(
    job: Mapping[str, object],
    authorization_entry: Mapping[str, object],
) -> str:
    """Bind authorized resume bytes without including their local path."""

    return canonical_digest(
        {
            "schema_version": 1,
            "artifact_kind": "resume",
            "artifact_sha256": str(
                authorization_entry.get("resume_sha256") or ""
            ),
            "artifact_size": authorization_entry.get("resume_size"),
            "job_fingerprint": str(
                authorization_entry.get("job_fingerprint") or ""
            ),
            "validation_state": str(job.get("tailor_status") or ""),
        }
    )


def expected_postcondition_digest(artifact: BoundResumeArtifact) -> str:
    """Hash the bounded browser-visible postcondition without storing a path."""

    return resume_postcondition_digest(
        ResumeUploadPostcondition(
            filename=artifact.filename,
            size_bytes=artifact.size_bytes,
        )
    )


def operation_id(operation_digest: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", operation_digest):
        raise ValueError("operation_digest must be a lowercase SHA-256 digest")
    return f"semantic-resume:{operation_digest}"


def canonical_digest(value: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def bound_artifact(path: Path, sha256: str, size_bytes: int) -> BoundResumeArtifact:
    resolved = path.expanduser().resolve(strict=True)
    return BoundResumeArtifact(
        path=resolved,
        sha256=sha256,
        size_bytes=size_bytes,
        filename=resolved.name,
    )


def _reason_code(value: str, *, prefix: str) -> str:
    safe = _REASON_CHARACTER_RE.sub("_", value.strip().casefold()).strip("_")
    safe = safe[: max(0, 119 - len(prefix))]
    return f"{prefix}:{safe or 'unspecified'}"
