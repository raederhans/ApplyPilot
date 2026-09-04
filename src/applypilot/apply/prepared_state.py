"""Lease-bound corroboration evidence carried between prepare repair turns.

This is deliberately not a browser-state snapshot.  It retains only compact
proofs that an independent observer can safely combine with the live DOM while
the exact browser/page lease remains continuous.
"""

from __future__ import annotations

from collections.abc import Mapping

from applypilot.apply.browser_broker import BrowserBrokerError, BrowserLeaseBundle

_SCHEMA_VERSION = "1"


def _lease_binding(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        bundle = BrowserLeaseBundle.from_mapping(value)
    except (BrowserBrokerError, TypeError, ValueError):
        return None
    page = bundle.page_binding
    return {
        "attempt_id": page.attempt_id,
        "runtime_id": page.runtime_id,
        "page_id": page.page_id,
        "page_lease_id": page.page_lease_id,
        "page_lease_epoch": page.page_lease_epoch,
        "profile_lease_id": page.profile_lease_id,
        "page_epoch": page.page_epoch,
    }


def _same_continuity(previous: object, current: object) -> bool:
    if not isinstance(previous, Mapping) or not isinstance(current, Mapping):
        return False
    identity = (
        "attempt_id",
        "runtime_id",
        "page_id",
        "page_lease_id",
        "page_lease_epoch",
        "profile_lease_id",
    )
    if any(str(previous.get(key) or "") != str(current.get(key) or "") for key in identity):
        return False
    try:
        return int(previous.get("page_epoch") or 0) <= int(current.get("page_epoch") or 0)
    except (TypeError, ValueError):
        return False


def _resume_upload_proof(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping) or value.get("verified") is not True:
        return None
    label = " ".join(str(value.get("field_label") or "").split())[:120]
    visible_filename = value.get("visible_filename")
    if not label or not (
        visible_filename is True
        or (isinstance(visible_filename, str) and visible_filename.strip())
    ):
        return None
    # The actual filename is not required for cross-component corroboration.
    return {
        "verified": True,
        "field_label": label,
        "visible_filename": True,
    }


def bind_prepared_state_evidence(
    job: dict,
    *,
    run_id: str,
    observations: Mapping[str, object] | None,
) -> dict[str, object]:
    """Bind safe prepare proofs to the current attempt and page continuity."""
    binding = _lease_binding(job.get("_browser_lease_binding"))
    if binding is None or not str(run_id or "").strip():
        job.pop("_prepared_state_evidence", None)
        return {}

    merged: dict[str, object] = {}
    previous = job.get("_prepared_state_evidence")
    if (
        isinstance(previous, Mapping)
        and _same_continuity(previous.get("binding"), binding)
        and isinstance(previous.get("observations"), Mapping)
    ):
        merged.update(previous["observations"])

    raw = observations if isinstance(observations, Mapping) else {}
    if "resume_upload" in raw:
        merged.pop("resume_upload", None)
        proof = _resume_upload_proof(raw.get("resume_upload"))
        if proof is not None:
            merged["resume_upload"] = proof

    evidence: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "run_id": str(run_id),
        "binding": binding,
        "observations": merged,
    }
    job["_prepared_state_evidence"] = evidence
    return evidence


def current_prepared_observations(job: Mapping[str, object]) -> dict[str, object]:
    """Return proofs only while the original browser/page lease is continuous."""
    evidence = job.get("_prepared_state_evidence")
    current = _lease_binding(job.get("_browser_lease_binding"))
    if (
        not isinstance(evidence, Mapping)
        or current is None
        or not _same_continuity(evidence.get("binding"), current)
        or not isinstance(evidence.get("observations"), Mapping)
    ):
        return {}
    return dict(evidence["observations"])
