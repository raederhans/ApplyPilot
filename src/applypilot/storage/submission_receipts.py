"""Browser observations and durable submission-receipt reconciliation."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime

from applypilot.storage import application_ledger


def record_submission_observation(
    conn: sqlite3.Connection,
    url: str,
    observation: dict,
) -> str | None:
    """Store a thin browser observation and update status without creating a gate.

    A decisive receipt or platform ``Applied`` marker can update the local
    record to ``applied``. A final click without confirmation becomes
    retry-blocked ``submission_uncertain``. Other observations are retained as
    context while leaving the current status unchanged.
    """
    row = conn.execute(
        "SELECT apply_status FROM jobs WHERE url = ?", (url,)
    ).fetchone()
    if row is None:
        return None

    cleaned = {
        "submit_clicked": bool(observation.get("submit_clicked", False)),
        "receipt_visible": bool(observation.get("receipt_visible", False)),
        "receipt_structured": bool(observation.get("receipt_structured", False)),
        "applied_badge_visible": bool(observation.get("applied_badge_visible", False)),
        "captcha_visible": bool(observation.get("captcha_visible", False)),
        "form_visible": observation.get("form_visible"),
        "submit_control_count": int(observation.get("submit_control_count") or 0),
        "page_url": str(observation.get("page_url", "")).strip()[:1000],
        "note": str(observation.get("note", "")).strip()[:500],
    }
    now_value = datetime.now(UTC)
    now = now_value.isoformat()
    observed_status = row["apply_status"]
    decisive_receipt = cleaned["receipt_visible"] and (
        cleaned["receipt_structured"]
        or (
            cleaned["form_visible"] is False
            and cleaned["submit_control_count"] == 0
        )
    )
    if decisive_receipt or cleaned["applied_badge_visible"]:
        observed_status = "applied"
        conn.execute(
            """
            UPDATE jobs SET apply_status = 'applied', applied_at = COALESCE(applied_at, ?),
                            apply_error = NULL, agent_id = NULL,
                            apply_retry_blocked = 0, apply_retry_reason = NULL,
                            verification_confidence = 'browser_observation',
                            application_evidence = 'platform_applied_or_receipt_observed',
                            application_recorded_at = ?,
                            submission_observation_json = ?, submission_observed_at = ?
            WHERE url = ?
            """,
            (now, now, json.dumps(cleaned, ensure_ascii=False), now, url),
        )
    elif cleaned["submit_clicked"] and row["apply_status"] != "applied":
        observed_status = "submission_uncertain"
        conn.execute(
            """
            UPDATE jobs SET apply_status = 'submission_uncertain', applied_at = NULL,
                            apply_error = NULL, agent_id = NULL,
                            apply_retry_blocked = 1,
                            apply_retry_reason = 'submission_uncertain_requires_review',
                            verification_confidence = 'browser_observation_pending',
                            application_evidence = 'submit_clicked_without_visible_confirmation',
                            application_recorded_at = ?,
                            submission_observation_json = ?, submission_observed_at = ?
            WHERE url = ?
            """,
            (now, json.dumps(cleaned, ensure_ascii=False), now, url),
        )
    else:
        conn.execute(
            "UPDATE jobs SET submission_observation_json = ?, submission_observed_at = ? "
            "WHERE url = ?",
            (json.dumps(cleaned, ensure_ascii=False), now, url),
        )
    conn.commit()
    return observed_status


def admit_direct_email_sent_receipt(
    conn: sqlite3.Connection,
    job_url: str,
    evidence: dict,
    *,
    gate_binding: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Uniquely bind one provider Sent message to one exact application."""
    job_url = str(job_url or "").strip()
    provider_message_id = str(evidence.get("provider_message_id") or "").strip()[:180]
    recipient = str(evidence.get("recipient") or "").strip().casefold()
    subject = " ".join(str(evidence.get("subject") or "").split())
    body_sha256 = str(evidence.get("body_sha256") or "").strip().casefold()
    attachment_names = evidence.get("attachment_names")
    if (
        not job_url
        or str(evidence.get("folder") or "").strip().casefold() != "sent"
        or not provider_message_id
        or not recipient
        or not subject
        or not re.fullmatch(r"[a-f0-9]{64}", body_sha256)
        or not isinstance(attachment_names, list)
        or not attachment_names
        or any(not isinstance(name, str) or not name.strip() for name in attachment_names)
    ):
        return {"status": "rejected", "reason": "invalid_direct_email_receipt"}

    cleaned = {
        "folder": "sent",
        "recipient": recipient,
        "subject": subject,
        "attachment_names": [str(name).strip()[:180] for name in attachment_names[:8]],
        "body_sha256": body_sha256,
        "provider_message_id": provider_message_id,
    }
    digest = hashlib.sha256(
        json.dumps(
            cleaned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    application_ledger.ensure_schema(conn)
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT 1 FROM jobs WHERE url=?", (job_url,)).fetchone() is None:
            if owns_transaction:
                conn.rollback()
            return {"status": "not_found", "job_url": job_url}
        existing = conn.execute(
            "SELECT job_url, receipt_digest FROM application_receipts "
            "WHERE receipt_source='direct_email_sent' AND receipt_id=?",
            (provider_message_id,),
        ).fetchone()
        now = datetime.now(UTC)
        if existing is not None:
            existing_url = existing["job_url"] if isinstance(existing, sqlite3.Row) else existing[0]
            existing_digest = (
                existing["receipt_digest"] if isinstance(existing, sqlite3.Row) else existing[1]
            )
            if existing_url != job_url or existing_digest != digest:
                if owns_transaction:
                    conn.rollback()
                return {
                    "status": "rejected",
                    "reason": "receipt_replay_conflict",
                    "job_url": job_url,
                }
        inserted = existing is None
        if inserted:
            now_text = now.isoformat()
            conn.execute(
                "INSERT INTO application_receipts "
                "(receipt_source, receipt_id, job_url, observed_at, admitted_at, "
                "receipt_digest) VALUES ('direct_email_sent', ?, ?, ?, ?, ?)",
                (provider_message_id, job_url, now_text, now_text, digest),
            )
        gate_bound = False
        if gate_binding is not None:
            gate_bound = application_ledger.bind_admitted_receipt_to_gate(
                conn,
                "direct_email_sent",
                provider_message_id,
                str(gate_binding.get("gate_id") or ""),
                str(gate_binding.get("batch_id") or ""),
                job_url,
                str(gate_binding.get("attempt_id") or ""),
                bound_at=now,
            )
            if not gate_bound:
                if inserted:
                    conn.execute(
                        "DELETE FROM application_receipts "
                        "WHERE receipt_source='direct_email_sent' AND receipt_id=?",
                        (provider_message_id,),
                    )
                if owns_transaction:
                    conn.rollback()
                return {
                    "status": "rejected",
                    "reason": "submission_gate_binding_invalid",
                    "job_url": job_url,
                }
        if owns_transaction:
            conn.commit()
        result: dict[str, object] = {
            "status": "already_admitted" if existing is not None else "admitted",
            "job_url": job_url,
        }
        if gate_binding is not None:
            result["gate_bound"] = gate_bound
        return result
    except Exception:
        if owns_transaction:
            conn.rollback()
        raise


_STRONG_SUBMISSION_RECEIPT = re.compile(
    r"application (?:was |has been )?(?:successfully )?(?:submitted|received)|"
    r"we(?:'ve| have) received your resume|"
    r"thank you for (?:applying|submitting your application)|"
    r"we (?:have )?received your application|"
    r"申请已提交|投递成功|申请成功",
    re.IGNORECASE,
)
_PORTAL_SUBMITTED_STATES = {
    "applied",
    "submitted",
    "application received",
    "application submitted",
    "已申请",
    "已投递",
    "申请已提交",
}


def _receipt_identity_matches(expected: str, observed: str) -> bool:
    """Allow formatting variation while requiring meaningful identity overlap."""
    expected_text = re.sub(r"[^\w]+", " ", expected.casefold()).strip()
    observed_text = re.sub(r"[^\w]+", " ", observed.casefold()).strip()
    if not expected_text or not observed_text:
        return False
    if expected_text in observed_text or observed_text in expected_text:
        return True
    expected_tokens = {token for token in expected_text.split() if len(token) >= 3}
    observed_tokens = {token for token in observed_text.split() if len(token) >= 3}
    if not expected_tokens:
        return False
    return len(expected_tokens & observed_tokens) / len(expected_tokens) >= 0.6


def reconcile_submission_receipt(
    conn: sqlite3.Connection,
    evidence: dict,
) -> dict[str, object]:
    """Idempotently upgrade an uncertain submission from a durable receipt.

    The ingestion seam accepts only a compact evidence envelope, never an email
    body or verification code. The caller must map the receipt to one exact job
    URL and provide matching company and role identity so a confirmation for a
    different application cannot update this row.
    """
    job_url = str(evidence.get("job_url") or "").strip()
    source = str(evidence.get("source") or "").strip().casefold()
    receipt_id = str(evidence.get("receipt_id") or "").strip()[:500]
    if not job_url or source not in {
        "confirmation_email",
        "candidate_portal",
        "browser_receipt",
    }:
        return {"status": "rejected", "reason": "invalid_receipt_envelope"}
    if not receipt_id:
        return {"status": "rejected", "reason": "receipt_id_required"}

    row = conn.execute(
        "SELECT apply_status, company_name, title, application_evidence, "
        "submission_observation_json FROM jobs WHERE url = ?",
        (job_url,),
    ).fetchone()
    if row is None:
        return {"status": "not_found", "job_url": job_url}
    prior_status = row["apply_status"]
    manual_external_receipt = source in {"browser_receipt", "confirmation_email"} and prior_status in {
        None,
        "",
        "failed",
        "previewed",
    }
    if prior_status not in {"applied", "submission_uncertain"} and not manual_external_receipt:
        return {
            "status": "ignored",
            "reason": "job_not_submission_uncertain",
            "job_url": job_url,
        }

    observed_company = str(evidence.get("company_name") or "").strip()
    observed_title = str(evidence.get("job_title") or "").strip()
    if not _receipt_identity_matches(str(row["company_name"] or ""), observed_company):
        return {"status": "rejected", "reason": "company_mismatch", "job_url": job_url}
    if not _receipt_identity_matches(str(row["title"] or ""), observed_title):
        return {"status": "rejected", "reason": "job_title_mismatch", "job_url": job_url}

    confirmation_text = str(evidence.get("confirmation_text") or "").strip()[:1000]
    portal_status = " ".join(
        str(evidence.get("portal_status") or "").casefold().split()
    )[:200]
    positive = bool(_STRONG_SUBMISSION_RECEIPT.search(confirmation_text))
    if source == "candidate_portal":
        positive = positive or portal_status in _PORTAL_SUBMITTED_STATES
    if not positive:
        return {
            "status": "rejected",
            "reason": "no_decisive_submission_signal",
            "job_url": job_url,
        }

    cleaned = {
        "source": source,
        "receipt_id": receipt_id,
        "job_url": job_url,
        "company_name": observed_company[:300],
        "job_title": observed_title[:300],
        "confirmation_text": confirmation_text,
        "portal_status": portal_status,
        "observed_at": str(evidence.get("observed_at") or "").strip()[:100],
    }
    receipt_digest = hashlib.sha256(
        json.dumps(
            {key: value for key, value in cleaned.items() if key != "observed_at"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    now_value = datetime.now(UTC)
    now = now_value.isoformat()
    application_ledger.ensure_schema(conn)
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute("SAVEPOINT reconcile_submission_receipt")

    def rollback_reconciliation() -> None:
        if owns_transaction:
            conn.rollback()
        else:
            conn.execute("ROLLBACK TO SAVEPOINT reconcile_submission_receipt")
            conn.execute("RELEASE SAVEPOINT reconcile_submission_receipt")

    def commit_reconciliation() -> None:
        if owns_transaction:
            conn.commit()
        else:
            conn.execute("RELEASE SAVEPOINT reconcile_submission_receipt")

    try:
        locked_row = conn.execute(
            "SELECT apply_status FROM jobs WHERE url=?",
            (job_url,),
        ).fetchone()
        if locked_row is None:
            rollback_reconciliation()
            return {"status": "not_found", "job_url": job_url}

        existing = conn.execute(
            "SELECT job_url, receipt_digest FROM application_receipts "
            "WHERE receipt_source=? AND receipt_id=?",
            (source, receipt_id),
        ).fetchone()
        if existing is not None and (
            existing["job_url"] != job_url or existing["receipt_digest"] != receipt_digest
        ):
            rollback_reconciliation()
            return {
                "status": "rejected",
                "reason": "receipt_replay_conflict",
                "job_url": job_url,
            }
        inserted = existing is None
        if inserted:
            conn.execute(
                "INSERT INTO application_receipts "
                "(receipt_source, receipt_id, job_url, observed_at, admitted_at, receipt_digest) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    source,
                    receipt_id,
                    job_url,
                    cleaned["observed_at"] or None,
                    now,
                    receipt_digest,
                ),
            )

        gate_identity = {
            "gate_id": str(evidence.get("gate_id") or "").strip(),
            "batch_id": str(evidence.get("batch_id") or "").strip(),
            "attempt_id": str(evidence.get("attempt_id") or "").strip(),
        }
        binding_requested = any(gate_identity.values())
        if binding_requested:
            gate_bound = application_ledger.bind_admitted_receipt_to_gate(
                conn,
                source,
                receipt_id,
                gate_identity["gate_id"],
                gate_identity["batch_id"],
                job_url,
                gate_identity["attempt_id"],
                bound_at=now_value,
            )
            if not gate_bound:
                rollback_reconciliation()
                return {
                    "status": "rejected",
                    "reason": "submission_gate_binding_invalid",
                    "job_url": job_url,
                }
            gate_closed = application_ledger.mark_bound_submission_receipt_applied(
                conn,
                source,
                receipt_id,
                gate_identity["gate_id"],
                gate_identity["batch_id"],
                job_url,
                gate_identity["attempt_id"],
            )
            if not gate_closed:
                rollback_reconciliation()
                return {
                    "status": "rejected",
                    "reason": "submission_gate_transition_invalid",
                    "job_url": job_url,
                }

        locked_status = locked_row["apply_status"]
        if locked_status == "applied":
            if existing is not None:
                commit_reconciliation()
                result: dict[str, object] = {
                    "status": "applied",
                    "job_url": job_url,
                    "changed": False,
                }
                if binding_requested:
                    result["gate_bound"] = True
                return result
            cursor = conn.execute(
                """
                UPDATE jobs SET verification_confidence = 'durable_receipt_reconciled',
                                application_evidence = COALESCE(application_evidence, ?),
                                application_recorded_at = COALESCE(application_recorded_at, ?),
                                submission_observation_json = ?, submission_observed_at = ?
                WHERE url = ? AND apply_status = 'applied'
                """,
                (
                    f"{source}:{receipt_id}",
                    now,
                    json.dumps(cleaned, ensure_ascii=False),
                    now,
                    job_url,
                ),
            )
        else:
            cursor = conn.execute(
                """
                UPDATE jobs SET apply_status = 'applied', applied_at = COALESCE(applied_at, ?),
                                apply_error = NULL, agent_id = NULL,
                                apply_retry_blocked = 0, apply_retry_reason = NULL,
                                verification_confidence = 'durable_receipt_reconciled',
                                application_evidence = ?, application_recorded_at = ?,
                                submission_observation_json = ?, submission_observed_at = ?
                WHERE url = ?
                  AND (apply_status IN ('submission_uncertain', 'failed', 'previewed')
                       OR apply_status IS NULL)
                """,
                (
                    now,
                    f"{source}:{receipt_id}",
                    now,
                    json.dumps(cleaned, ensure_ascii=False),
                    now,
                    job_url,
                ),
            )
        if cursor.rowcount != 1:
            rollback_reconciliation()
            return {
                "status": "ignored",
                "reason": "job_state_changed_during_reconciliation",
                "job_url": job_url,
            }
        application_ledger.resolve_risks(
            conn,
            job_url,
            categories=("duplicate_submission_risk",),
        )
        commit_reconciliation()
        result = {
            "status": "applied",
            "job_url": job_url,
            "changed": True,
            "source": source,
        }
        if binding_requested:
            result["gate_bound"] = True
        return result
    except sqlite3.IntegrityError:
        rollback_reconciliation()
        return {
            "status": "rejected",
            "reason": "receipt_replay_conflict",
            "job_url": job_url,
        }
    except Exception:
        rollback_reconciliation()
        raise
