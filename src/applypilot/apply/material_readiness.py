"""Deterministic, byte-bound submission-material readiness checks."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from applypilot.apply.authorization import compute_file_binding, compute_job_fingerprint
from applypilot.scoring.cover_letter import read_resume_source
from applypilot.scoring.validator import current_profile_resume_fact_errors

_KNOWN_LABELS = {
    "resume": ("resume", "cv", "curriculum vitae"),
    "cover_letter": ("cover letter", "cover note"),
    "transcript": ("transcript", "academic record"),
    "portfolio": ("portfolio",),
    "certificate": ("certificate", "certification"),
    "supporting_document": ("supporting document", "additional document"),
}


def evaluate_profile_resume_fact_freshness(
    job: Mapping[str, object],
    profile: Mapping[str, object],
) -> dict[str, object]:
    """Read one validated resume once and classify its current-profile facts.

    This is deliberately a read-only predicate: candidate selection, manifest
    creation, worker allocation, and runtime acquisition can all consume the
    same result without creating a database side effect.  Only an *explicit*
    conflict is ``stale_profile_fact``.  A missing path, an unreadable source,
    or a PDF whose text cannot be extracted is kept distinct so none of those
    operational/material failures can retire a resume as a factual conflict.
    """
    if str(job.get("tailor_status") or "").casefold() != "machine_validated":
        return {"state": "not_applicable", "fact_errors": []}
    raw_path = str(job.get("tailored_resume_path") or "").strip()
    if not raw_path:
        return {"state": "resume_missing", "fact_errors": []}

    path = Path(raw_path).expanduser()
    if not path.is_file():
        return {"state": "resume_unavailable", "fact_errors": []}

    try:
        if path.suffix.casefold() == ".pdf":
            text_sidecar = path.with_suffix(".txt")
            if text_sidecar.is_file():
                text = read_resume_source(text_sidecar)
            else:
                try:
                    from pypdf import PdfReader

                    text = "\n".join(
                        page.extract_text() or "" for page in PdfReader(path).pages
                    )
                except Exception:  # noqa: BLE001 - legacy PDFs may not expose text
                    return {"state": "resume_text_unavailable", "fact_errors": []}
        else:
            text = read_resume_source(path)
    except (OSError, TypeError, ValueError):
        return {"state": "resume_unreadable", "fact_errors": []}

    errors = current_profile_resume_fact_errors(text, dict(profile))
    return {
        "state": "stale_profile_fact" if errors else "fresh",
        "fact_errors": errors,
    }


def _canonical_label(label: str) -> str | None:
    normalized = " ".join(label.casefold().replace("/", " ").split())
    for kind, tokens in _KNOWN_LABELS.items():
        if any(token in normalized for token in tokens):
            return kind
    return None


def _required_labels(job: Mapping[str, object]) -> tuple[list[str], set[str]]:
    labels: list[str] = []
    supplied: set[str] = set()
    keys = ("required_materials", "required_file_labels", "_observed_required_file_labels")
    for key in keys:
        raw = job.get(key)
        if not isinstance(raw, (list, tuple)):
            continue
        for item in raw:
            if isinstance(item, Mapping):
                if item.get("required", True) is False:
                    continue
                label = str(item.get("label") or item.get("kind") or "").strip()
                if item.get("provided") is True:
                    canonical = _canonical_label(label)
                    if canonical:
                        supplied.add(canonical)
            else:
                label = str(item).strip()
            if label and label not in labels:
                labels.append(label)
    raw_supplied = job.get("provided_materials")
    if isinstance(raw_supplied, Mapping):
        for key, value in raw_supplied.items():
            if not value:
                continue
            normalized = str(key).casefold()
            supplied.add(_canonical_label(normalized) or normalized)
    elif isinstance(raw_supplied, (list, tuple)):
        for value in raw_supplied:
            normalized = str(value).casefold()
            supplied.add(_canonical_label(normalized) or normalized)
    return labels, supplied


def _expected_resume_binding(job: Mapping[str, object]) -> tuple[str | None, int | None, str]:
    for key, source in (
        ("_authorization_entry", "authorization_manifest"),
        ("_bound_submission_materials", "material_binding"),
        ("_material_snapshot", "material_snapshot"),
    ):
        value = job.get(key)
        if not isinstance(value, Mapping):
            continue
        if key == "_authorization_entry":
            size = value.get("resume_size")
            return (
                str(value.get("resume_sha256") or "") or None,
                int(size) if size is not None else None,
                source,
            )
        materials = value.get("materials")
        if isinstance(materials, (list, tuple)):
            for material in materials:
                if isinstance(material, Mapping) and material.get("kind") == "resume":
                    size = material.get("size")
                    return (
                        str(material.get("sha256") or "") or None,
                        int(size) if size is not None else None,
                        source,
                    )
    digest = str(job.get("tailored_resume_sha256") or "").strip() or None
    size = job.get("tailored_resume_size")
    return digest, int(size) if size is not None else None, "machine_validation_metadata"


def _validation_timestamp_covers(path: Path, job: Mapping[str, object]) -> bool:
    raw = str(job.get("tailored_at") or "").strip()
    if not raw:
        return False
    try:
        validated_at = datetime.fromisoformat(raw)
        if validated_at.tzinfo is None:
            validated_at = validated_at.replace(tzinfo=UTC)
        return path.stat().st_mtime <= validated_at.timestamp()
    except (OSError, TypeError, ValueError):
        return False


def material_snapshot_identity(job: Mapping[str, object]) -> dict[str, str]:
    """Bind specialist idempotency to the exact job and current material bytes."""
    fingerprint = compute_job_fingerprint(dict(job))
    bindings: list[dict[str, object]] = []
    for key in ("tailored_resume_path", "cover_letter_path"):
        raw = str(job.get(key) or "").strip()
        if not raw:
            continue
        path = Path(raw).expanduser().with_suffix(".pdf")
        if path.is_file():
            digest, size = compute_file_binding(path)
            bindings.append({"kind": key, "sha256": digest, "size": size})
        else:
            bindings.append({"kind": key, "state": "missing"})
    required, supplied = _required_labels(job)
    expected_digest, expected_size, expected_source = _expected_resume_binding(job)
    authorization_entry = job.get("_authorization_entry")
    current_resume_path = str(job.get("tailored_resume_path") or "").strip()
    authorized_resume_path = (
        str(authorization_entry.get("resume_path") or "").strip()
        if isinstance(authorization_entry, Mapping)
        else ""
    )
    payload = {
        "job_fingerprint": fingerprint,
        "bindings": bindings,
        "required": required,
        "supplied": sorted(supplied),
        "tailor_status": str(job.get("tailor_status") or ""),
        "tailored_at": str(job.get("tailored_at") or ""),
        "current_resume_path": current_resume_path,
        "authorized_resume_path": authorized_resume_path,
        "cover_letter_status": str(job.get("cover_letter_status") or ""),
        "allow_runtime_cover_letter": bool(job.get("_allow_runtime_cover_letter")),
        "expected_resume": {
            "sha256": expected_digest,
            "size": expected_size,
            "source": expected_source,
        },
        "authorization_job_fingerprint": (
            authorization_entry.get("job_fingerprint")
            if isinstance(authorization_entry, Mapping)
            else None
        ),
    }
    digest = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return {"job_fingerprint": fingerprint, "material_snapshot_digest": digest}


def evaluate_material_readiness(job: Mapping[str, object]) -> dict[str, object]:
    blocked: list[str] = []
    human: list[str] = []
    materials: list[dict[str, object]] = []
    fingerprint = compute_job_fingerprint(dict(job))
    resume_text = str(job.get("tailored_resume_path") or "").strip()
    resume_path = Path(resume_text).expanduser() if resume_text else None
    resume_pdf = None
    if str(job.get("tailor_status") or "").casefold() != "machine_validated":
        blocked.append("tailored_resume_not_validated")
    if resume_path is None:
        blocked.append("resume")
    else:
        resume_pdf = (
            resume_path
            if resume_path.suffix.casefold() == ".pdf"
            else resume_path.with_suffix(".pdf")
        )
        if not resume_pdf.is_file():
            blocked.append("resume")
        else:
            digest, size = compute_file_binding(resume_pdf)
            expected_digest, expected_size, binding_source = _expected_resume_binding(job)
            authorization_entry = job.get("_authorization_entry")
            if isinstance(authorization_entry, Mapping):
                try:
                    bound_path = Path(str(authorization_entry.get("resume_path") or "")).expanduser().resolve()
                    current_path = resume_pdf.resolve()
                except (OSError, RuntimeError, ValueError):
                    bound_path = None
                    current_path = None
                if (
                    authorization_entry.get("job_fingerprint") != fingerprint
                    or bound_path is None
                    or current_path != bound_path
                ):
                    blocked.append("authorization_binding_mismatch")
            binding_verified = bool(
                expected_digest
                and expected_size is not None
                and expected_digest == digest
                and expected_size == size
            )
            if expected_digest and not binding_verified:
                blocked.append("resume_byte_binding_mismatch")
            elif not binding_verified and not _validation_timestamp_covers(resume_pdf, job):
                human.append("resume_validation_binding_unknown")
            else:
                binding_verified = True
                if not expected_digest:
                    binding_source = "machine_validation_timestamp"
            materials.append(
                {
                    "kind": "resume",
                    "sha256": digest,
                    "size": size,
                    "binding_verified": binding_verified,
                    "binding_source": binding_source,
                }
            )

    cover_status = str(job.get("cover_letter_status") or "").casefold()
    allow_runtime_cover = bool(job.get("_allow_runtime_cover_letter"))
    cover_ready = cover_status == "not_required"
    if cover_status in {"human_approved", "agent_validated"}:
        raw_cover = str(job.get("cover_letter_path") or "").strip()
        cover_pdf = Path(raw_cover).expanduser().with_suffix(".pdf") if raw_cover else None
        cover_ready = bool(cover_pdf and cover_pdf.is_file())
        if not cover_ready:
            blocked.append("cover_letter")
        elif cover_pdf is not None:
            digest, size = compute_file_binding(cover_pdf)
            materials.append({"kind": "cover_letter", "sha256": digest, "size": size})
    elif not cover_ready and not allow_runtime_cover:
        blocked.append("cover_letter")

    required_labels, supplied = _required_labels(job)
    unknown_labels: list[str] = []
    available = {"resume"} if resume_pdf and resume_pdf.is_file() else set()
    if cover_ready:
        available.add("cover_letter")
    available.update(supplied)
    for label in required_labels:
        kind = _canonical_label(label)
        if kind is None:
            unknown_labels.append(label)
        elif kind not in available and kind not in blocked:
            blocked.append(kind)

    state = "blocked" if blocked else "human_required" if human or unknown_labels else "ready"
    snapshot_payload = {
        "job_fingerprint": fingerprint,
        "materials": materials,
        "required_labels": required_labels,
    }
    snapshot_digest = hashlib.sha256(
        json.dumps(
            snapshot_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": "2",
        "state": state,
        "ready": state == "ready",
        "job_fingerprint": fingerprint,
        "material_snapshot_digest": snapshot_digest,
        "materials": materials,
        "missing_kinds": sorted(set(blocked)),
        "human_reason_codes": sorted(set(human)),
        "unknown_required_labels": unknown_labels,
        "runtime_cover_discovery": allow_runtime_cover and not cover_ready,
        "manifest_bound": isinstance(job.get("_authorization_entry"), Mapping),
    }
