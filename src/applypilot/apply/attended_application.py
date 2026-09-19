"""Durable attended execution seam; observations are supplied by the trusted operator.

This does not operate a browser or authenticate host evidence. Child worker output
must never be passed as an independently observed checkpoint. Existing admission,
material, duplicate, audit, capacity, and receipt contracts remain authoritative.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from applypilot.apply.application_jobs import revalidate_duplicate_before_submit
from applypilot.apply.authorization import (
    authorize_job,
    freeze_submission_materials,
    load_manifest,
    resolve_resume_attachment,
)
from applypilot.apply.page_observation import _same_bound_application_flow, _validate_pre_submit_snapshot
from applypilot.apply.submission_admission import evaluate_submission_admission
from applypilot.storage import application_ledger as ledger
from applypilot.storage.submission_receipts import reconcile_submission_receipt


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _required(data, key):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value


def _observation(data):
    if data.get("source") != "attending_host":
        raise ValueError("independent attending_host observation required")
    refs = data.get("evidence_refs")
    if not isinstance(refs, list) or not refs or any(not isinstance(x, str) or not x.strip() for x in refs):
        raise ValueError("evidence_refs required; do not include raw credentials")
    observed = datetime.fromisoformat(_required(data, "observed_at"))
    if observed.tzinfo is None or not 0 <= (datetime.now(UTC) - observed).total_seconds() <= 300:
        raise ValueError("observation must be timezone-aware and within five minutes")


def _validate_query_identity(expected_url, observed_url):
    """Retain job identity on shared embed paths, ignoring tracking parameters."""
    expected = parse_qs(urlparse(expected_url).query, keep_blank_values=True)
    observed = parse_qs(urlparse(observed_url).query, keep_blank_values=True)
    for key in ("token", "for", "gh_jid", "jobId", "job_id"):
        if expected.get(key) != observed.get(key):
            raise ValueError("page query job identity differs")


def execute(connection: sqlite3.Connection, request: dict, profile: dict) -> dict:
    """Execute one durable transition. Caller supplies an idle, row-factory connection."""
    if connection.in_transaction:
        raise ValueError("attended execution requires an idle connection")
    ledger.ensure_schema(connection)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS attended_applications (attempt_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
    )
    connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    try:
        result = _execute(connection, request, profile)
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise


def _save(conn, state):
    conn.execute(
        "INSERT INTO attended_applications VALUES (?, ?) ON CONFLICT(attempt_id) DO UPDATE SET payload=excluded.payload",
        (state["attempt_id"], json.dumps(state, ensure_ascii=False)),
    )


def _execute(conn, req, profile):
    action = _required(req, "action")
    job_url = _required(req, "job_url")
    row = conn.execute("SELECT * FROM jobs WHERE url=?", (job_url,)).fetchone()
    if row is None:
        raise ValueError("exact job not found")
    job = dict(row)
    job["application_url"] = job.get("application_url") or job_url
    host = {k: _required(req, k) for k in ("host_session_id", "tab_id")}
    if action == "begin":
        existing = conn.execute(
            "SELECT h.payload FROM attended_applications h JOIN application_attempts a "
            "ON a.attempt_id=h.attempt_id WHERE a.job_url=? "
            "AND (a.status='in_progress' OR a.submit_started=1) ORDER BY a.started_at DESC",
            (job_url,),
        ).fetchall()
        for candidate in existing:
            saved_state = json.loads(candidate[0])
            if all(saved_state[k] == value for k, value in host.items()):
                if Path(_required(req, "manifest_path")).resolve() != Path(saved_state["manifest_path"]).resolve():
                    raise ValueError("existing attempt manifest binding mismatch")
                return _execute(conn, dict(req, action="resume", attempt_id=saved_state["attempt_id"]), profile)
        if job.get("apply_status") in {"applied", "submission_uncertain", "in_progress", "applying"}:
            raise ValueError("job already active, submitted, or uncertain")
        active = conn.execute(
            "SELECT attempt_id FROM application_attempts WHERE job_url=? AND (status='in_progress' OR submit_started=1)",
            (job_url,),
        ).fetchone()
        if active:
            raise ValueError("existing attempt requires resume or receipt reconciliation")
        duplicate = revalidate_duplicate_before_submit(conn, job_url)
        if not duplicate.get("clear"):
            raise ValueError(str(duplicate))
        manifest = load_manifest(_required(req, "manifest_path"))
        if authorize_job(manifest, job) is None:
            raise ValueError("authorization_manifest_job_mismatch")
        materials = freeze_submission_materials(job, profile)
        attempt = ledger.start_attempt(conn, job_url, "attending_host", batch_id=manifest["batch_id"])
        state = dict(
            host,
            attempt_id=attempt,
            job_url=job_url,
            manifest_path=req["manifest_path"],
            batch_id=manifest["batch_id"],
            materials=materials,
            phase="prepare",
            initial_apply_status=job.get("apply_status"),
            submit_started=False,
        )
        conn.execute(
            "UPDATE jobs SET apply_status='in_progress', agent_id='attending_host', "
            "apply_task_id=?, last_attempted_at=? WHERE url=?",
            (attempt, datetime.now(UTC).isoformat(), job_url),
        )
        _save(conn, state)
        return state
    attempt = _required(req, "attempt_id")
    saved = conn.execute("SELECT payload FROM attended_applications WHERE attempt_id=?", (attempt,)).fetchone()
    if saved is None:
        raise ValueError("attended attempt not found")
    state = json.loads(saved[0])
    if state["job_url"] != job_url or any(state[k] != v for k, v in host.items()):
        raise ValueError("job or host/tab binding mismatch")
    attempt_row = conn.execute("SELECT * FROM application_attempts WHERE attempt_id=?", (attempt,)).fetchone()
    if action == "receipt":
        if not state["submit_started"] or not state.get("gate_id"):
            raise ValueError("receipt requires durable submit intent and gate")
        _observation(req)
        evidence = dict(req.get("receipt") or {})
        for key, value in {
            "job_url": job_url,
            "attempt_id": attempt,
            "batch_id": state["batch_id"],
            "gate_id": state["gate_id"],
        }.items():
            if key in evidence and evidence[key] != value:
                raise ValueError("receipt binding mismatch")
            evidence[key] = value
        conn.execute("SAVEPOINT attended_receipt")
        if state["phase"] != "applied":
            ledger.update_submission_gate_state(conn, attempt, "submission_uncertain")
            ledger.update_batch_submission_status(conn, state["batch_id"], job_url, "submission_uncertain")
            conn.execute("UPDATE jobs SET apply_status='submission_uncertain' WHERE url=?", (job_url,))
            state["phase"] = "submission_uncertain"
            _save(conn, state)
        result = reconcile_submission_receipt(conn, evidence)
        if result.get("status") != "applied":
            conn.execute("ROLLBACK TO SAVEPOINT attended_receipt")
            conn.execute("RELEASE SAVEPOINT attended_receipt")
            return result
        conn.execute("RELEASE SAVEPOINT attended_receipt")
        if result.get("status") == "applied":
            state["phase"] = "applied"
            state["receipt_evidence_refs"] = req["evidence_refs"]
            ledger.finalize_attempt(conn, attempt, "applied", evidence=result)
            _save(conn, state)
        return result
    if job.get("apply_status") == "applied":
        return dict(state, next_action="already_applied; no further writes")
    if job.get("apply_task_id") != attempt:
        raise ValueError("job ownership changed; cannot resume or mutate this attempt")
    if action == "resume" and state["submit_started"]:
        return dict(state, next_action="reconcile_receipt_only")
    if attempt_row["status"] != "in_progress":
        raise ValueError("attempt terminal")
    if action == "fail":
        state["phase"] = "submission_uncertain" if state["submit_started"] else "failed"
        ledger.finalize_attempt(conn, attempt, state["phase"], evidence={"reason": _required(req, "reason")})
        if state.get("gate_id"):
            ledger.update_submission_gate_state(
                conn, attempt, "submission_uncertain" if state["submit_started"] else "cancelled_before_action"
            )
            ledger.update_batch_submission_status(conn, state["batch_id"], job_url, state["phase"])
        conn.execute("UPDATE jobs SET apply_status=?, agent_id=NULL WHERE url=?", (state["phase"], job_url))
        _save(conn, state)
        return state
    if state["submit_started"]:
        raise ValueError("submit already started; reconcile receipt only")
    if datetime.fromisoformat(attempt_row["lease_expires_at"]) <= datetime.now(UTC):
        raise ValueError("attempt lease expired; finalize before starting another attempt")
    if state["phase"] == "material_changed":
        return dict(state, blocked="material_changed; finalize and begin again")
    materials = freeze_submission_materials(job, profile)
    if materials != state["materials"]:
        state.pop("checkpoint", None)
        state.pop("resume_upload", None)
        state["phase"] = "material_changed"
        _save(conn, state)
        return dict(state, blocked="material_changed; finalize and begin with current authorization")
    if action == "resume":
        ledger.update_attempt(conn, attempt, phase=attempt_row["phase"], submit_started=False)
        return state
    if action == "upload-checkpoint":
        if state.get("gate_id"):
            raise ValueError("claimed upload evidence is immutable")
        _observation(req)
        upload = req.get("upload")
        if not isinstance(upload, dict) or upload.get("attempt_id") != attempt:
            raise ValueError("upload must bind the current attempt")
        page_url = _required(upload, "page_url")
        if not _same_bound_application_flow(job["application_url"], page_url, {}):
            raise ValueError("upload page is outside the bound application flow")
        # Some embedded ATS paths are shared by many jobs. The existing generic
        # path check does not bind their query identity, so retain those values.
        _validate_query_identity(job["application_url"], page_url)
        label = _required(upload, "field_label")
        if not re.search(r"\b(?:resume|curriculum vitae|cv)\b", label, re.IGNORECASE):
            raise ValueError("upload field must identify Resume or CV")
        filename = _required(upload, "visible_filename")
        if filename != resolve_resume_attachment(job).name:
            raise ValueError("visible filename differs from bound resume filename")
        resume = next(item for item in materials["materials"] if item["kind"] == "resume")
        if upload.get("sha256") != resume["sha256"] or upload.get("size") != resume["size"]:
            raise ValueError("upload bytes differ from frozen resume")
        marker = _required(upload, "acceptance_marker")
        accepted_text = _required(upload, "accepted_attachment_text")
        if marker not in {"attachment_card", "uploaded_file_list"} or filename not in accepted_text:
            raise ValueError("visible accepted attachment marker and filename required")
        state["resume_upload"] = {
            "attempt_id": attempt,
            "page_url": page_url,
            "field_label": label,
            "visible_filename": filename,
            "sha256": resume["sha256"],
            "size": resume["size"],
            "acceptance_marker": marker,
            "accepted_attachment_digest": _digest(accepted_text),
            "observed_at": req["observed_at"],
            "evidence_refs": req["evidence_refs"],
        }
        # A changed upload observation requires a new final-page checkpoint.
        state.pop("checkpoint", None)
        ledger.update_attempt(conn, attempt, phase="prepare", submit_started=False)
        state["phase"] = "prepare"
        _save(conn, state)
        return state
    if action == "checkpoint":
        if state.get("gate_id"):
            raise ValueError("claimed checkpoint is immutable")
        _observation(req)
        snapshot = req.get("snapshot")
        if not isinstance(snapshot, dict) or not snapshot.get("url"):
            raise ValueError("observed page snapshot required")
        if snapshot.get("resume_field_present") is True and snapshot.get("resume_uploaded") is not True:
            state.pop("resume_upload", None)
        state["checkpoint"] = {k: req[k] for k in ("observed_at", "evidence_refs", "source")}
        state["checkpoint"]["snapshot_digest"] = _digest(snapshot)
        state["phase"] = "prepared"
        ledger.update_attempt(conn, attempt, phase="prepared", submit_started=False)
        _save(conn, state)
        return state
    if action not in {"claim", "submit-intent"}:
        raise ValueError("unknown attended action")
    checkpoint = state.get("checkpoint")
    if not checkpoint:
        raise ValueError("preparation checkpoint required")
    _observation(checkpoint)
    snapshot = req.get("snapshot")
    if not isinstance(snapshot, dict) or _digest(snapshot) != checkpoint["snapshot_digest"]:
        raise ValueError("fresh snapshot must match prepared snapshot; checkpoint again if changed")
    # These are existing snapshot fields, not a substitute operator all-green audit.
    needed = {
        "url",
        "required_unfilled",
        "sensitive_required_unknown",
        "file_fields",
        "form_fields",
        "text_fields",
        "select_fields",
        "full_name_values",
        "email_values",
        "captcha_visible",
        "verification_visible",
        "assessment_visible",
        "resume_field_present",
        "resume_uploaded",
        "submit_control_count",
    }
    if not needed <= snapshot.keys():
        raise ValueError("incomplete pre-submit snapshot: " + ", ".join(sorted(needed - snapshot.keys())))
    _validate_query_identity(job["application_url"], snapshot["url"])
    audit_job = dict(job)
    upload = state.get("resume_upload")
    if upload and upload["attempt_id"] == attempt and snapshot.get("resume_field_present") is False:
        # Only accepted host evidence captured on this leased attempt supplies
        # the existing compatibility proof, never arbitrary request observations.
        audit_job["_agent_observations"] = {
            "resume_upload": {
                "verified": True,
                "field_label": upload["field_label"],
                "visible_filename": upload["visible_filename"],
            }
        }
    issues = _validate_pre_submit_snapshot(snapshot, profile, audit_job)
    if issues:
        raise ValueError("pre-submit audit: " + ", ".join(issues))
    admission_job = dict(job)
    # The active status was acquired by this exact attempt, verified above. Feed
    # the pre-acquisition status to the same admission predicate used by workers.
    if admission_job.get("apply_status") == "in_progress":
        admission_job["apply_status"] = state["initial_apply_status"]
    admission = evaluate_submission_admission(
        admission_job, profile, minimum_fit_score=int(profile.get("submission_policy", {}).get("minimum_fit_score", 4))
    )
    if admission.get("admitted") is not True:
        raise ValueError("submission admission: " + str(admission.get("reason")))
    manifest = load_manifest(state["manifest_path"])
    if manifest["batch_id"] != state["batch_id"] or authorize_job(manifest, job) is None:
        raise ValueError("authorization_manifest_job_mismatch")
    duplicate = revalidate_duplicate_before_submit(conn, job_url)
    if not duplicate.get("clear"):
        raise ValueError(str(duplicate))
    if action == "claim":
        if not ledger.update_attempt(conn, attempt, phase="reservation", submit_started=False):
            raise ValueError("attempt inactive")
        policy = profile.get("submission_policy", {})
        claim = ledger.claim_submission_gate(
            conn,
            manifest["batch_id"],
            job_url,
            manifest["max_submissions"],
            attempt,
            hourly_maximum=policy.get("maximum_verified_submissions_per_rolling_hour", 15),
            minimum_gap_seconds=policy.get("minimum_seconds_between_verified_submissions", 20),
            audit_fingerprint=_digest(
                {"checkpoint": checkpoint, "materials": materials, "resume_upload": upload, **host}
            ),
        )
        if not claim.get("claimed") or claim.get("state") != "claimed":
            raise ValueError(str(claim))
        state.update(gate_id=claim["gate_id"], phase="claimed")
    else:
        if state["phase"] != "claimed":
            raise ValueError("claim required before submit intent")
        _observation(req)
        if req.get("checkpoint_digest") != _digest(checkpoint):
            raise ValueError("current host must attest exact reviewed checkpoint_digest")
        if not ledger.update_attempt(conn, attempt, phase="submit", submit_started=True):
            raise ValueError("attempt inactive")
        state.update(submit_started=True, phase="submit", submit_evidence_refs=req["evidence_refs"])
        conn.execute(
            "UPDATE jobs SET apply_status='applying', apply_attempts=COALESCE(apply_attempts,0)+1 WHERE url=?",
            (job_url,),
        )
    state["checkpoint_digest"] = _digest(checkpoint)
    _save(conn, state)
    return state
