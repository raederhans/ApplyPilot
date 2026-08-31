"""Provider-neutral post-submit mailbox receipt observation and reconciliation."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime

from applypilot.storage.submission_receipts import (
    _receipt_identity_matches,
    reconcile_submission_receipt,
)

SUPPORTED_PROVIDERS = frozenset({"gmail", "outlook"})
_DOMAIN = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def _aware(value: object, name: str) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an ISO timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS mailbox_receipt_watermarks ("
        "provider TEXT PRIMARY KEY, received_at TEXT NOT NULL, "
        "message_id TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS mailbox_receipt_scans ("
        "scan_id TEXT PRIMARY KEY, provider TEXT NOT NULL, job_url TEXT NOT NULL, "
        "status TEXT NOT NULL, candidate_count INTEGER NOT NULL, "
        "observed_at TEXT NOT NULL, details_json TEXT NOT NULL)"
    )


def mailbox_watermark(
    connection: sqlite3.Connection,
    provider: str,
) -> dict[str, str] | None:
    normalized = provider.strip().casefold()
    if normalized not in SUPPORTED_PROVIDERS:
        raise ValueError("unsupported mailbox provider")
    ensure_schema(connection)
    row = connection.execute(
        "SELECT received_at, message_id FROM mailbox_receipt_watermarks "
        "WHERE provider=?",
        (normalized,),
    ).fetchone()
    if row is None:
        return None
    return {"received_at": str(row[0]), "message_id": str(row[1])}


def _advance_watermark(
    connection: sqlite3.Connection,
    provider: str,
    received_at: datetime,
    message_id: str,
) -> bool:
    received_utc = received_at.astimezone(UTC)
    received_text = received_utc.isoformat()
    now = datetime.now(UTC).isoformat()
    cursor = connection.execute(
        "INSERT INTO mailbox_receipt_watermarks "
        "(provider, received_at, message_id, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(provider) DO UPDATE SET received_at=excluded.received_at, "
        "message_id=excluded.message_id, updated_at=excluded.updated_at "
        "WHERE excluded.received_at > mailbox_receipt_watermarks.received_at "
        "OR (excluded.received_at = mailbox_receipt_watermarks.received_at "
        "AND excluded.message_id > mailbox_receipt_watermarks.message_id)",
        (provider, received_text, message_id, now),
    )
    return cursor.rowcount == 1


def receipt_observer_context(
    connection: sqlite3.Connection,
    job: Mapping[str, object],
    *,
    provider: str,
    submitted_at: datetime,
) -> dict[str, object]:
    """Build a compact mailbox search context with no mailbox content."""
    normalized = provider.strip().casefold()
    if normalized not in SUPPORTED_PROVIDERS:
        raise ValueError("unsupported mailbox provider")
    ensure_schema(connection)
    submitted = _aware(submitted_at, "submitted_at")
    return {
        "schema_version": "1",
        "provider": normalized,
        "job_url": str(job.get("url") or ""),
        "company_name": str(job.get("company_name") or "")[:300],
        "job_title": str(job.get("title") or "")[:300],
        "platform_job_id": str(
            job.get("platform_job_id")
            or job.get("requisition_id")
            or job.get("provider_application_id")
            or ""
        )[:200],
        "submitted_at": submitted.isoformat(),
        # A provider-global watermark can be newer than this application when
        # workers scan different jobs out of order.  It is therefore advisory
        # for ordering/deduplication, never a lower bound that can exclude an
        # exact receipt received after this job's own submission.
        "search_after": submitted.isoformat(),
        "watermark": mailbox_watermark(connection, normalized),
    }


def _record_scan(
    connection: sqlite3.Connection,
    *,
    provider: str,
    job_url: str,
    status: str,
    candidate_count: int,
    details: Mapping[str, object],
) -> None:
    safe_details = {
        key: value
        for key, value in details.items()
        if key
        in {
            "watermark_advanced",
            "ambiguous",
            "reconciliation_status",
            "reconciliation_reason",
        }
    }
    observed_at = datetime.now(UTC).isoformat()
    identity = json.dumps(
        {
            "provider": provider,
            "job_url": job_url,
            "status": status,
            "candidate_count": candidate_count,
            "observed_at": observed_at,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    scan_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    connection.execute(
        "INSERT OR IGNORE INTO mailbox_receipt_scans "
        "(scan_id, provider, job_url, status, candidate_count, observed_at, "
        "details_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            scan_id,
            provider,
            job_url,
            status,
            max(0, candidate_count),
            observed_at,
            json.dumps(safe_details, sort_keys=True, separators=(",", ":")),
        ),
    )


def process_receipt_observation(
    connection: sqlite3.Connection,
    job: Mapping[str, object],
    *,
    provider: str,
    submitted_at: datetime,
    observation: Mapping[str, object],
    gate_binding: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Admit one exact confirmation or retain a non-success state fail closed."""
    normalized = provider.strip().casefold()
    if normalized not in SUPPORTED_PROVIDERS:
        raise ValueError("unsupported mailbox provider")
    ensure_schema(connection)
    submitted = _aware(submitted_at, "submitted_at")
    job_url = str(job.get("url") or "").strip()
    scan = observation.get("receipt_scan")
    scan = scan if isinstance(scan, Mapping) else {}
    candidate_count = scan.get("candidate_count", 0)
    candidate_count = (
        candidate_count
        if isinstance(candidate_count, int) and not isinstance(candidate_count, bool)
        else 0
    )
    if scan.get("provider") != normalized or scan.get("scan_succeeded") is not True:
        _record_scan(
            connection,
            provider=normalized,
            job_url=job_url,
            status="provider_error",
            candidate_count=candidate_count,
            details={"watermark_advanced": False},
        )
        connection.commit()
        return {
            "status": "provider_error",
            "provider": normalized,
            "watermark_advanced": False,
        }
    if scan.get("ambiguous") is True:
        _record_scan(
            connection,
            provider=normalized,
            job_url=job_url,
            status="ambiguous",
            candidate_count=candidate_count,
            details={"watermark_advanced": False, "ambiguous": True},
        )
        connection.commit()
        return {
            "status": "ambiguous",
            "provider": normalized,
            "watermark_advanced": False,
        }

    receipt = observation.get("confirmation_receipt")
    if receipt is None:
        watermark_advanced = False
        max_received_at = scan.get("max_received_at")
        max_message_id = str(scan.get("max_message_id") or "").strip()
        if max_received_at and max_message_id:
            observed_max = _aware(max_received_at, "max_received_at")
            if observed_max >= submitted:
                watermark_advanced = _advance_watermark(
                    connection,
                    normalized,
                    observed_max,
                    max_message_id,
                )
        _record_scan(
            connection,
            provider=normalized,
            job_url=job_url,
            status="no_match",
            candidate_count=candidate_count,
            details={"watermark_advanced": watermark_advanced},
        )
        connection.commit()
        return {
            "status": "no_match",
            "provider": normalized,
            "watermark_advanced": watermark_advanced,
        }
    if not isinstance(receipt, Mapping):
        raise TypeError("confirmation_receipt must be an object or null")

    receipt_provider = str(receipt.get("provider") or "").strip().casefold()
    message_id = str(receipt.get("provider_message_id") or "").strip()[:500]
    sender_domain = str(receipt.get("sender_domain") or "").strip().casefold().strip(".")
    received_at = _aware(receipt.get("received_at"), "received_at")
    observed_company = str(receipt.get("company_name") or "").strip()
    observed_title = str(receipt.get("job_title") or "").strip()
    expected_job_id = str(
        job.get("platform_job_id")
        or job.get("requisition_id")
        or job.get("provider_application_id")
        or ""
    ).strip()
    observed_job_id = str(receipt.get("platform_job_id") or "").strip()
    identity_matches = (
        receipt_provider == normalized
        and bool(message_id)
        and received_at > submitted
        and bool(_DOMAIN.fullmatch(sender_domain))
        and receipt.get("exact_job_identity_matched") is True
        and _receipt_identity_matches(
            str(job.get("company_name") or ""), observed_company
        )
        and _receipt_identity_matches(str(job.get("title") or ""), observed_title)
        and (not expected_job_id or expected_job_id == observed_job_id)
    )
    if not identity_matches:
        _record_scan(
            connection,
            provider=normalized,
            job_url=job_url,
            status="ambiguous",
            candidate_count=candidate_count,
            details={"watermark_advanced": False, "ambiguous": True},
        )
        connection.commit()
        return {
            "status": "ambiguous",
            "provider": normalized,
            "watermark_advanced": False,
        }

    binding = dict(gate_binding or {})
    evidence = {
        "job_url": job_url,
        "source": "confirmation_email",
        "receipt_id": f"{normalized}:{message_id}",
        "company_name": observed_company,
        "job_title": observed_title,
        "confirmation_text": str(receipt.get("confirmation_text") or "")[:1000],
        "observed_at": received_at.isoformat(),
        "gate_id": str(binding.get("gate_id") or ""),
        "batch_id": str(binding.get("batch_id") or ""),
        "attempt_id": str(binding.get("attempt_id") or ""),
    }
    reconciliation = reconcile_submission_receipt(connection, evidence)
    applied = reconciliation.get("status") == "applied"
    watermark_advanced = False
    if applied:
        watermark_advanced = _advance_watermark(
            connection,
            normalized,
            received_at,
            message_id,
        )
    status = "applied" if applied else "ambiguous"
    _record_scan(
        connection,
        provider=normalized,
        job_url=job_url,
        status=status,
        candidate_count=candidate_count,
        details={
            "watermark_advanced": watermark_advanced,
            "ambiguous": not applied,
            "reconciliation_status": reconciliation.get("status"),
            "reconciliation_reason": reconciliation.get("reason"),
        },
    )
    connection.commit()
    return {
        "status": status,
        "provider": normalized,
        "watermark_advanced": watermark_advanced,
        "reconciliation": reconciliation,
    }
