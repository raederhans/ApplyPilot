"""Lightweight read-only ApplicationPlan snapshot for existing attended applications."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any


def build_attended_plan(connection: sqlite3.Connection, attempt_id: str) -> dict[str, Any]:
    """Build a read-only ApplicationPlan snapshot for an existing attended attempt.

    Derives bindings, durable phase/status, material digests, persisted blockers,
    and verified receipt references without performing any writes or schema migrations.

    Args:
        connection: Open SQLite connection. Must not be mutated.
        attempt_id: Exact identifier of the attended application attempt.

    Returns:
        A concise dictionary snapshot of the attended application plan.

    Raises:
        ValueError: If attempt_id is missing, malformed, or bindings mismatch.
    """
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        raise ValueError(f"attempt_id is required: {attempt_id!r}")
    attempt_id = attempt_id.strip()

    # Read attended_applications row without schema migrations or writes
    try:
        saved = connection.execute(
            "SELECT payload FROM attended_applications WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        raise ValueError(f"attended attempt not found: {attempt_id!r}") from exc

    if saved is None:
        raise ValueError(f"attended attempt not found: {attempt_id!r}")

    try:
        state = json.loads(saved[0])
    except Exception as exc:
        raise ValueError(f"malformed attended attempt payload for {attempt_id!r}") from exc

    if not isinstance(state, dict):
        raise ValueError(f"malformed attended attempt payload for {attempt_id!r}")  # noqa: TRY004 - corrupt stored JSON

    # Read corresponding application_attempts record
    try:
        raw_attempt = connection.execute(
            "SELECT attempt_id, job_url, batch_id, started_at, lease_expires_at, "
            "phase, submit_started, status, updated_at "
            "FROM application_attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        raise ValueError(f"application attempt record not found for {attempt_id!r}") from exc

    if raw_attempt is None:
        raise ValueError(f"application attempt record not found for {attempt_id!r}")

    attempt_dict = {
        "attempt_id": str(raw_attempt[0] or "").strip(),
        "job_url": str(raw_attempt[1] or "").strip(),
        "batch_id": str(raw_attempt[2] or "").strip() if raw_attempt[2] is not None else None,
        "started_at": raw_attempt[3],
        "lease_expires_at": raw_attempt[4],
        "phase": raw_attempt[5],
        "submit_started": bool(raw_attempt[6]),
        "status": raw_attempt[7],
        "updated_at": raw_attempt[8],
    }

    # Validate exact bindings and non-empty host/tab
    payload_attempt_id = str(state.get("attempt_id") or "").strip()
    payload_job_url = str(state.get("job_url") or "").strip()
    payload_batch_id = str(state.get("batch_id") or "").strip() if state.get("batch_id") is not None else None
    host_session_id = state.get("host_session_id")
    tab_id = state.get("tab_id")

    if not isinstance(host_session_id, str) or not host_session_id.strip():
        raise ValueError(f"host_session_id must be non-empty string in attempt {attempt_id!r}")
    if not isinstance(tab_id, str) or not tab_id.strip():
        raise ValueError(f"tab_id must be non-empty string in attempt {attempt_id!r}")

    host_session_id = host_session_id.strip()
    tab_id = tab_id.strip()

    if payload_attempt_id != attempt_dict["attempt_id"]:
        raise ValueError(f"attempt_id mismatch: payload {payload_attempt_id!r} != attempt {attempt_dict['attempt_id']!r}")
    if payload_job_url != attempt_dict["job_url"]:
        raise ValueError(f"job_url mismatch: payload {payload_job_url!r} != attempt {attempt_dict['job_url']!r}")
    if payload_batch_id != attempt_dict["batch_id"]:
        raise ValueError(f"batch_id mismatch: payload {payload_batch_id!r} != attempt {attempt_dict['batch_id']!r}")

    phase = str(state.get("phase") or attempt_dict["phase"] or "prepare")
    status = str(attempt_dict["status"] or "in_progress")
    submit_started = bool(state.get("submit_started") or attempt_dict["submit_started"])

    # Historical checkpoint info: stored checkpoint is historical, not a claim of current review
    checkpoint = state.get("checkpoint")
    checkpoint_present = isinstance(checkpoint, dict) and bool(checkpoint)
    checkpoint_observed_at = checkpoint.get("observed_at") if checkpoint_present else None
    checkpoint_digest = state.get("checkpoint_digest") or (
        checkpoint.get("snapshot_digest") if checkpoint_present else None
    )

    # Reconciled receipt: ONLY admitted when exact attempt+job+gate receipt binding joined admitted application_receipts
    receipt_ref: str | None = None
    submitted = False

    receipt_tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('application_receipt_gate_bindings', 'application_receipts')"
        ).fetchall()
    }
    if receipt_tables == {"application_receipt_gate_bindings", "application_receipts"}:
        gate_id = str(state.get("gate_id") or "").strip()
        query = (
            "SELECT b.receipt_source, b.receipt_id "
            "FROM application_receipt_gate_bindings b "
            "JOIN application_receipts r "
            "  ON b.receipt_source = r.receipt_source AND b.receipt_id = r.receipt_id AND b.job_url = r.job_url "
            "WHERE b.attempt_id = ? AND b.job_url = ? AND b.gate_id = ? AND b.batch_id = ?"
        )
        params = [attempt_id, payload_job_url, gate_id, payload_batch_id]
        receipt_row = connection.execute(query, tuple(params)).fetchone()
        if receipt_row is not None:
            receipt_ref = f"{receipt_row[0]}:{receipt_row[1]}"
            submitted = True

    # needs_host_review: true unless terminal verified receipt or explicit terminal failure
    needs_host_review = not (submitted or status in {"failed", "abandoned_pre_submit"})

    # next_action: derived from durable state, avoiding ready_to_submit
    if submitted:
        next_action = "terminal"
    elif submit_started or phase in {"submission_uncertain", "submit", "applied"} or status in {"submission_uncertain", "applied"}:
        next_action = "reconcile_receipt_only"
    elif status in {"failed", "abandoned_pre_submit"}:
        next_action = "terminal"
    else:
        next_action = "host_review_then_existing_gate"

    # Derive blocker codes from existing persisted evidence
    blockers: list[str] = []
    if phase == "material_changed" or state.get("blocked"):
        blockers.append("material_changed")

    lease_expires_at = attempt_dict.get("lease_expires_at")
    if lease_expires_at:
        try:
            lease_dt = datetime.fromisoformat(str(lease_expires_at))
            if lease_dt.tzinfo is None:
                lease_dt = lease_dt.replace(tzinfo=UTC)
            if lease_dt <= datetime.now(UTC):
                blockers.append("lease_expired")
        except (ValueError, TypeError):
            pass

    if status in {"failed", "abandoned_pre_submit"}:
        blockers.append("attempt_terminal")

    if phase == "submission_uncertain" or status == "submission_uncertain":
        blockers.append("submission_uncertain")

    # Optional risk query checking actual schema columns; fail visibly if schema is malformed
    risk_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_risk_events'"
    ).fetchone()
    if risk_table is not None:
        cols = {row[1] for row in connection.execute("PRAGMA table_info(application_risk_events)").fetchall()}
        if not {"category", "state", "job_url", "attempt_id"} <= cols:
            raise ValueError("application_risk_events schema is malformed")
        for (cat,) in connection.execute(
            "SELECT category FROM application_risk_events WHERE (attempt_id = ? OR job_url = ?) AND state = 'open'",
            (attempt_id, payload_job_url),
        ).fetchall():
            blockers.append(f"risk:{cat}")

    # Optional jobs table check for retry blocked
    jobs_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
    ).fetchone()
    if jobs_table is not None:
        cols = {row[1] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()}
        if "apply_retry_blocked" in cols:
            job_row = connection.execute(
                "SELECT apply_retry_blocked FROM jobs WHERE url = ?",
                (payload_job_url,),
            ).fetchone()
            if job_row is not None and bool(job_row[0]):
                blockers.append("job_retry_blocked")

    # Material references and digests without raw contents or validation text
    material_refs: list[dict[str, Any]] = []
    material_digests: dict[str, str] = {}

    raw_materials = state.get("materials")
    if isinstance(raw_materials, dict):
        items = raw_materials.get("materials")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    kind = str(item.get("kind") or "").strip()
                    sha256 = str(item.get("sha256") or "").strip()
                    if kind:
                        ref_entry: dict[str, Any] = {"kind": "material", "purpose": kind}
                        if sha256:
                            ref_entry["content_sha256"] = sha256
                            ref_entry["ref"] = f"sha256:{sha256}"
                            material_digests[kind] = sha256
                        if isinstance(item.get("size"), int):
                            ref_entry["size"] = item["size"]
                        if item.get("state") == "not_required":
                            ref_entry["state"] = "not_required"
                        material_refs.append(ref_entry)

    # Gather evidence references
    evidence_refs: list[str] = []
    if checkpoint_present and isinstance(checkpoint.get("evidence_refs"), list):
        evidence_refs.extend(checkpoint["evidence_refs"])
    resume_upload = state.get("resume_upload")
    if isinstance(resume_upload, dict) and isinstance(resume_upload.get("evidence_refs"), list):
        evidence_refs.extend(resume_upload["evidence_refs"])
    if isinstance(state.get("submit_evidence_refs"), list):
        evidence_refs.extend(state["submit_evidence_refs"])
    if isinstance(state.get("receipt_evidence_refs"), list):
        evidence_refs.extend(state["receipt_evidence_refs"])

    deduped_evidence_refs = list(dict.fromkeys(ref for ref in evidence_refs if isinstance(ref, str) and ref.strip()))

    return {
        "schema_version": "1",
        "attempt_id": attempt_id,
        "job_url": payload_job_url,
        "batch_id": payload_batch_id,
        "host_session_id": host_session_id,
        "tab_id": tab_id,
        "phase": phase,
        "status": status,
        "submit_started": submit_started,
        "next_action": next_action,
        "needs_host_review": needs_host_review,
        "checkpoint_present": checkpoint_present,
        "checkpoint_observed_at": checkpoint_observed_at,
        "checkpoint_digest": checkpoint_digest,
        "material_refs": material_refs,
        "material_digests": material_digests,
        "evidence_refs": deduped_evidence_refs,
        "receipt_ref": receipt_ref,
        "blockers": list(dict.fromkeys(blockers)),
        "submitted": submitted,
        "submit_authority": False,
    }
