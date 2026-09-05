"""Read-only progress projection for one immutable application manifest.

The submission ledger remains authoritative.  This module only joins an
already-validated manifest to the existing jobs, attempts, gates, consumption,
and admitted-receipt tables; it never creates or updates storage.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from applypilot.apply.authorization import authorize_job
from applypilot.apply.submission_admission import evaluate_submission_admission

_MAX_PAGE = 10


def open_read_only_database(path: str | Path) -> sqlite3.Connection | None:
    """Open an existing SQLite database without creating a missing file."""
    database_path = Path(path).expanduser().resolve()
    if not database_path.is_file():
        return None
    connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _table_columns(connection: sqlite3.Connection | None, table: str) -> set[str]:
    if connection is None:
        return set()
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if exists is None:
            return set()
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    except sqlite3.DatabaseError:
        return set()


def _rows(
    connection: sqlite3.Connection | None,
    table: str,
    columns: tuple[str, ...],
    where: str,
    params: tuple[object, ...],
) -> list[dict[str, object]]:
    if connection is None or not set(columns).issubset(_table_columns(connection, table)):
        return []
    try:
        cursor = connection.execute(
            f"SELECT {', '.join(columns)} FROM {table} WHERE {where}", params
        )
        names = [str(item[0]) for item in cursor.description or ()]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
    except sqlite3.DatabaseError:
        return []


def _latest_by_url(rows: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for row in rows:
        url = str(row.get("job_url") or "")
        current = result.get(url)
        if current is None or str(row.get("updated_at") or "") >= str(
            current.get("updated_at") or ""
        ):
            result[url] = row
    return result


def consumed_batch_job_urls(
    connection: sqlite3.Connection | None,
    batch_id: str,
) -> set[str]:
    """Return every permanently consumed URL for one exact batch.

    Missing/legacy tables deliberately project as an empty set.  The caller can
    use this as an early acquisition exclusion without initializing storage.
    """
    rows = _rows(
        connection,
        "application_batch_consumptions",
        ("batch_id", "job_url", "status", "updated_at"),
        "batch_id=?",
        (str(batch_id or ""),),
    )
    return {str(row["job_url"]) for row in rows}


def consumed_manifest_urls(
    connection: sqlite3.Connection | None,
    manifest: Mapping[str, object],
) -> frozenset[str]:
    """Return exact manifest URLs already consumed by this same batch."""
    manifest_urls = {
        str(entry.get("url") or "")
        for entry in manifest.get("jobs", [])
        if isinstance(entry, Mapping)
    }
    consumed = consumed_batch_job_urls(connection, str(manifest.get("batch_id") or ""))
    return frozenset(consumed.intersection(manifest_urls))


def _confirmed_receipt_urls(
    connection: sqlite3.Connection | None,
    batch_id: str,
    manifest_urls: set[str],
) -> set[str]:
    required = {
        "application_submission_gates": {
            "gate_id", "attempt_id", "batch_id", "job_url", "claimed_at_epoch"
        },
        "application_receipt_gate_bindings": {
            "receipt_source", "receipt_id", "gate_id", "attempt_id", "batch_id",
            "job_url", "bound_at_epoch"
        },
        "application_receipts": {"receipt_source", "receipt_id", "job_url"},
    }
    if connection is None or any(
        not columns.issubset(_table_columns(connection, table))
        for table, columns in required.items()
    ):
        return set()
    try:
        rows = connection.execute(
            "SELECT DISTINCT b.job_url "
            "FROM application_receipt_gate_bindings b "
            "JOIN application_submission_gates g "
            "ON g.gate_id=b.gate_id AND g.attempt_id=b.attempt_id "
            "AND g.batch_id=b.batch_id AND g.job_url=b.job_url "
            "JOIN application_receipts r "
            "ON r.receipt_source=b.receipt_source AND r.receipt_id=b.receipt_id "
            "AND r.job_url=b.job_url "
            "WHERE b.batch_id=? AND g.claimed_at_epoch IS NOT NULL "
            "AND b.bound_at_epoch>=g.claimed_at_epoch",
            (batch_id,),
        ).fetchall()
    except sqlite3.DatabaseError:
        return set()
    return {str(row[0]) for row in rows if str(row[0]) in manifest_urls}


def _job_for_entry(
    connection: sqlite3.Connection | None,
    entry: Mapping[str, object],
    *,
    jobs_available: bool,
) -> dict[str, object] | None:
    if connection is None or not jobs_available:
        return None
    try:
        cursor = connection.execute(
            "SELECT * FROM jobs WHERE url=? OR application_url=?",
            (entry.get("url"), entry.get("application_url")),
        )
        names = [str(item[0]) for item in cursor.description or ()]
        rows = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
    except sqlite3.DatabaseError:
        return None
    if len(rows) != 1:
        return None
    job = rows[0]
    job["application_url"] = job.get("application_url") or job.get("url")
    return job


def _attempt_active(row: Mapping[str, object] | None, now: datetime) -> bool:
    if not row or str(row.get("status") or "") != "in_progress":
        return False
    try:
        expires = datetime.fromisoformat(str(row.get("lease_expires_at") or ""))
    except ValueError:
        return False
    if expires.tzinfo is None or expires.utcoffset() is None:
        return False
    return expires.astimezone(UTC) > now.astimezone(UTC)


def batch_progress(
    connection: sqlite3.Connection | None,
    manifest: Mapping[str, object],
    profile: Mapping[str, object],
    *,
    minimum_fit_score: int = 6,
    offset: int = 0,
    limit: int = 5,
    now: datetime | None = None,
) -> dict[str, object]:
    """Return a bounded status summary and the next never-consumed candidates."""
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_PAGE:
        raise ValueError("limit must be between 1 and 10")
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")

    entries = [entry for entry in manifest.get("jobs", []) if isinstance(entry, Mapping)]
    batch_id = str(manifest.get("batch_id") or "")
    manifest_urls = {str(entry.get("url") or "") for entry in entries}
    all_consumed_urls = consumed_batch_job_urls(connection, batch_id)
    consumed_urls = all_consumed_urls.intersection(manifest_urls)
    consumptions = _latest_by_url(
        _rows(
            connection,
            "application_batch_consumptions",
            ("batch_id", "job_url", "status", "updated_at"),
            "batch_id=?",
            (batch_id,),
        )
    )
    gates = _latest_by_url(
        _rows(
            connection,
            "application_submission_gates",
            ("batch_id", "job_url", "attempt_id", "state", "updated_at"),
            "batch_id=?",
            (batch_id,),
        )
    )
    attempts = _latest_by_url(
        _rows(
            connection,
            "application_attempts",
            (
                "batch_id", "job_url", "attempt_id", "lease_expires_at", "submit_started",
                "status", "updated_at"
            ),
            "batch_id=?",
            (batch_id,),
        )
    )
    consumptions = {url: row for url, row in consumptions.items() if url in consumed_urls}
    gates = {url: row for url, row in gates.items() if url in manifest_urls}
    attempts = {url: row for url, row in attempts.items() if url in manifest_urls}
    confirmed = _confirmed_receipt_urls(connection, batch_id, manifest_urls).intersection(
        consumed_urls
    )
    # The durable gate counts the whole batch, even if a caller has an older
    # or partial manifest view. Never advertise capacity the gate cannot grant.
    consumed = len(all_consumed_urls)
    maximum = int(manifest.get("max_submissions") or 0)
    remaining_capacity = max(0, maximum - consumed)
    jobs_available = {"url", "application_url"}.issubset(
        _table_columns(connection, "jobs")
    )

    counts = {
        "receipt_confirmed": 0,
        "uncertain": 0,
        "consumed_without_receipt": 0,
        "in_progress": 0,
        "ready": 0,
        "blocked": 0,
    }
    items: list[dict[str, object]] = []
    for index, entry in enumerate(entries):
        url = str(entry.get("url") or "")
        consumption = consumptions.get(url)
        gate = gates.get(url)
        attempt = attempts.get(url)
        if url in confirmed:
            status, reason = "receipt_confirmed", "exact admitted receipt is bound to this batch gate"
        elif _attempt_active(attempt, current):
            status, reason = "in_progress", "an unexpired attempt lease is active"
        elif (
            str((consumption or {}).get("status") or "") == "submission_uncertain"
            or str((gate or {}).get("state") or "") == "submission_uncertain"
            or bool((attempt or {}).get("submit_started"))
        ):
            status, reason = "uncertain", "submit may have started without an admitted receipt"
        elif consumption is not None:
            status, reason = "consumed_without_receipt", "this batch slot is permanently consumed"
        elif remaining_capacity <= 0:
            status, reason = "blocked", "batch submission capacity is exhausted"
        else:
            job = _job_for_entry(connection, entry, jobs_available=jobs_available)
            if job is None:
                status, reason = "blocked", "exact current job is unavailable"
            elif authorize_job(dict(manifest), job) is None:
                status, reason = "blocked", "authorization binding no longer matches the current job"
            else:
                admission = evaluate_submission_admission(
                    job,
                    profile,
                    minimum_fit_score=minimum_fit_score,
                    preview_only=False,
                )
                if admission.get("admitted"):
                    status, reason = "ready", str(admission.get("reason") or "admission passed")
                else:
                    status, reason = "blocked", str(admission.get("reason") or "admission rejected")
        counts[status] += 1
        items.append(
            {
                "manifest_index": index,
                "job_url": url,
                "application_url": str(entry.get("application_url") or ""),
                "status": status,
                "reason": reason,
            }
        )

    ready = [item for item in items if item["status"] == "ready"]
    candidate_cap = min(limit, remaining_capacity)
    candidates = ready[offset : offset + candidate_cap]
    next_offset = offset + len(candidates)
    return {
        "version": 1,
        "batch_id": batch_id,
        "authorized_jobs": len(entries),
        "max_submissions": maximum,
        "consumed": consumed,
        "remaining_capacity": remaining_capacity,
        "counts": counts,
        "next": candidates,
        "page": {
            "offset": offset,
            "limit": limit,
            "returned": len(candidates),
            "next_offset": next_offset if next_offset < len(ready) else None,
        },
        "storage_available": connection is not None,
    }
