"""SQLite implementation of the application attempt and authorization ledgers.

All functions require an explicit connection.  Connection ownership remains in
``applypilot.database`` so transaction and compatibility behavior stay stable
while the application-specific schema and SQL have a single focused home.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Create durable application authorization, attempt, and receipt ledgers."""
    was_in_transaction = connection.in_transaction
    connection.execute("""
        CREATE TABLE IF NOT EXISTS application_batch_consumptions (
            batch_id       TEXT NOT NULL,
            job_url        TEXT NOT NULL,
            reserved_at    TEXT NOT NULL,
            status         TEXT NOT NULL,
            updated_at     TEXT NOT NULL,
            evidence_json  TEXT,
            PRIMARY KEY (batch_id, job_url)
        )
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_application_batch_consumptions_count
            ON application_batch_consumptions(batch_id, status)
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS application_attempts (
            attempt_id       TEXT PRIMARY KEY,
            job_url          TEXT NOT NULL,
            batch_id         TEXT,
            worker_id        TEXT NOT NULL,
            started_at       TEXT NOT NULL,
            lease_expires_at TEXT NOT NULL,
            phase            TEXT NOT NULL,
            submit_started   INTEGER NOT NULL DEFAULT 0,
            status           TEXT NOT NULL,
            updated_at       TEXT NOT NULL,
            evidence_json    TEXT
        )
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_application_attempts_lease
            ON application_attempts(status, lease_expires_at)
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS application_receipts (
            receipt_source TEXT NOT NULL,
            receipt_id     TEXT NOT NULL,
            job_url        TEXT NOT NULL,
            observed_at    TEXT,
            admitted_at    TEXT NOT NULL,
            receipt_digest TEXT NOT NULL,
            PRIMARY KEY (receipt_source, receipt_id)
        )
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_application_receipts_job
            ON application_receipts(job_url, admitted_at)
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS application_risk_events (
            risk_event_id TEXT PRIMARY KEY,
            job_url       TEXT NOT NULL,
            attempt_id    TEXT,
            category      TEXT NOT NULL,
            severity      TEXT NOT NULL,
            state         TEXT NOT NULL,
            evidence_json TEXT,
            created_at    TEXT NOT NULL,
            resolved_at   TEXT
        )
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_application_risk_open
            ON application_risk_events(state, severity, created_at)
    """)
    if not was_in_transaction:
        connection.commit()


def start_attempt(
    connection: sqlite3.Connection,
    job_url: str,
    worker_id: str,
    *,
    batch_id: str | None = None,
    lease_minutes: int = 45,
) -> str:
    """Start one leased attempt inside the caller's transaction when present."""
    if not job_url or not worker_id:
        raise ValueError("job_url and worker_id are required")
    if isinstance(lease_minutes, bool) or not isinstance(lease_minutes, int) or lease_minutes < 5:
        raise ValueError("lease_minutes must be an integer of at least 5")
    ensure_schema(connection)
    now = datetime.now(UTC)
    attempt_id = f"attempt-{uuid.uuid4()}"
    connection.execute(
        "INSERT INTO application_attempts "
        "(attempt_id, job_url, batch_id, worker_id, started_at, lease_expires_at, "
        "phase, submit_started, status, updated_at, evidence_json) "
        "VALUES (?, ?, ?, ?, ?, ?, 'prepare', 0, 'in_progress', ?, NULL)",
        (
            attempt_id,
            job_url,
            str(batch_id or "").strip() or None,
            worker_id,
            now.isoformat(),
            (now + timedelta(minutes=lease_minutes)).isoformat(),
            now.isoformat(),
        ),
    )
    return attempt_id


def record_risk_event(
    connection: sqlite3.Connection,
    job_url: str,
    category: str,
    severity: str,
    *,
    attempt_id: str | None = None,
    evidence: object | None = None,
) -> str:
    """Append one compact global risk item without raw credentials or profile data."""
    if not job_url or not category or severity not in {"low", "medium", "high"}:
        raise ValueError("job_url, category, and a valid severity are required")
    ensure_schema(connection)
    event_id = f"risk-{uuid.uuid4()}"
    connection.execute(
        "INSERT INTO application_risk_events "
        "(risk_event_id, job_url, attempt_id, category, severity, state, evidence_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'open', ?, ?)",
        (
            event_id,
            job_url,
            attempt_id,
            category,
            severity,
            None if evidence is None else _json_text(evidence),
            datetime.now(UTC).isoformat(),
        ),
    )
    return event_id


def resolve_risks(
    connection: sqlite3.Connection,
    job_url: str,
    *,
    categories: tuple[str, ...],
) -> int:
    """Resolve selected open risks after stronger evidence is admitted."""
    if not categories:
        return 0
    placeholders = ",".join("?" for _ in categories)
    cursor = connection.execute(
        f"UPDATE application_risk_events SET state='resolved', resolved_at=? "
        f"WHERE job_url=? AND state='open' AND category IN ({placeholders})",
        (datetime.now(UTC).isoformat(), job_url, *categories),
    )
    return cursor.rowcount


def update_attempt(
    connection: sqlite3.Connection,
    attempt_id: str | None,
    *,
    phase: str,
    submit_started: bool,
    lease_minutes: int = 45,
    evidence: object | None = None,
) -> bool:
    """Advance an active attempt and renew its lease without reviving terminal rows."""
    if not attempt_id:
        return False
    ensure_schema(connection)
    now = datetime.now(UTC)
    cursor = connection.execute(
        "UPDATE application_attempts SET phase=?, submit_started=?, lease_expires_at=?, "
        "updated_at=?, evidence_json=COALESCE(?, evidence_json) "
        "WHERE attempt_id=? AND status='in_progress' AND lease_expires_at > ?",
        (
            str(phase or "prepare")[:50],
            int(bool(submit_started)),
            (now + timedelta(minutes=lease_minutes)).isoformat(),
            now.isoformat(),
            None if evidence is None else _json_text(evidence),
            attempt_id,
            now.isoformat(),
        ),
    )
    return cursor.rowcount == 1


def finalize_attempt(
    connection: sqlite3.Connection,
    attempt_id: str | None,
    status: str,
    *,
    evidence: object | None = None,
) -> bool:
    """Finalize one active attempt; repeated finalization is a no-op."""
    if not attempt_id:
        return False
    ensure_schema(connection)
    cursor = connection.execute(
        "UPDATE application_attempts SET status=?, updated_at=?, evidence_json=? "
        "WHERE attempt_id=? AND status='in_progress'",
        (
            str(status or "unknown")[:80],
            datetime.now(UTC).isoformat(),
            None if evidence is None else _json_text(evidence),
            attempt_id,
        ),
    )
    return cursor.rowcount == 1


def recover_stale_attempts(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Recover expired attempts, preserving uncertainty after submit began."""
    ensure_schema(connection)
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN IMMEDIATE")
    rows = connection.execute(
        "SELECT attempt_id, job_url, submit_started FROM application_attempts "
        "WHERE status='in_progress' AND lease_expires_at <= ?",
        (current.isoformat(),),
    ).fetchall()
    recovered = {"pre_submit": 0, "submission_uncertain": 0}
    for row in rows:
        if row["submit_started"]:
            outcome = "submission_uncertain"
            connection.execute(
                "UPDATE jobs SET apply_status='submission_uncertain', applied_at=NULL, "
                "agent_id=NULL, apply_retry_blocked=1, "
                "apply_retry_reason='stale_submit_attempt_requires_review', "
                "verification_confidence='browser_observation_pending', "
                "application_evidence='stale_attempt_after_submit_started' "
                "WHERE url=? AND apply_status='in_progress'",
                (row["job_url"],),
            )
            recovered["submission_uncertain"] += 1
        else:
            outcome = "abandoned_pre_submit"
            connection.execute(
                "UPDATE jobs SET apply_status='failed', agent_id=NULL, "
                "apply_error='stale_pre_submit_attempt_recovered', "
                "apply_retry_blocked=0, apply_retry_reason=NULL "
                "WHERE url=? AND apply_status='in_progress'",
                (row["job_url"],),
            )
            recovered["pre_submit"] += 1
        connection.execute(
            "UPDATE application_attempts SET status=?, updated_at=? WHERE attempt_id=?",
            (outcome, current.isoformat(), row["attempt_id"]),
        )
    if owns_transaction:
        connection.commit()
    return recovered


def prune_runtime_history(
    connection: sqlite3.Connection,
    *,
    retention_days: int = 180,
    execute: bool = False,
    now: datetime | None = None,
) -> dict[str, object]:
    """Preview or remove old terminal attempt rows, never receipts or uncertainty."""
    if isinstance(retention_days, bool) or not isinstance(retention_days, int) or retention_days < 30:
        raise ValueError("retention_days must be an integer of at least 30")
    owns_transaction = not connection.in_transaction
    ensure_schema(connection)
    current = now or datetime.now(UTC)
    cutoff = (current - timedelta(days=retention_days)).isoformat()
    eligible_statuses = ("failed", "previewed", "released", "abandoned_pre_submit")
    placeholders = ",".join("?" for _ in eligible_statuses)
    params = (*eligible_statuses, cutoff)
    count = connection.execute(
        f"SELECT COUNT(*) FROM application_attempts WHERE status IN ({placeholders}) "
        "AND updated_at < ?",
        params,
    ).fetchone()[0]
    if execute and count:
        connection.execute(
            f"DELETE FROM application_attempts WHERE status IN ({placeholders}) "
            "AND updated_at < ?",
            params,
        )
        if owns_transaction:
            connection.commit()
    return {
        "eligible_attempts": int(count),
        "deleted_attempts": int(count) if execute else 0,
        "retention_days": retention_days,
        "execute": execute,
        "preserved": [
            "application_receipts",
            "applied",
            "submission_uncertain",
            "in_progress",
        ],
    }


def preview_runtime_history(
    connection: sqlite3.Connection,
    *,
    retention_days: int = 180,
    now: datetime | None = None,
) -> dict[str, object]:
    """Count prune candidates without creating tables or changing a transaction."""
    if isinstance(retention_days, bool) or not isinstance(retention_days, int) or retention_days < 30:
        raise ValueError("retention_days must be an integer of at least 30")
    table_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_attempts'"
    ).fetchone()
    count = 0
    if table_exists is not None:
        current = now or datetime.now(UTC)
        cutoff = (current - timedelta(days=retention_days)).isoformat()
        eligible_statuses = ("failed", "previewed", "released", "abandoned_pre_submit")
        placeholders = ",".join("?" for _ in eligible_statuses)
        count = int(connection.execute(
            f"SELECT COUNT(*) FROM application_attempts WHERE status IN ({placeholders}) "
            "AND updated_at < ?",
            (*eligible_statuses, cutoff),
        ).fetchone()[0])
    return {
        "eligible_attempts": count,
        "deleted_attempts": 0,
        "retention_days": retention_days,
        "execute": False,
        "preserved": [
            "application_receipts",
            "applied",
            "submission_uncertain",
            "in_progress",
        ],
    }


def reserve_batch_submission(
    connection: sqlite3.Connection,
    batch_id: str,
    job_url: str,
    max_submissions: int,
) -> bool:
    """Atomically consume one batch slot; reservations are deliberately permanent."""
    batch_id = str(batch_id or "").strip()
    job_url = str(job_url or "").strip()
    if not batch_id or not job_url:
        raise ValueError("batch_id and job_url are required")
    if isinstance(max_submissions, bool) or not isinstance(max_submissions, int) or max_submissions <= 0:
        raise ValueError("max_submissions must be a positive integer")
    ensure_schema(connection)
    owns_transaction = not connection.in_transaction
    try:
        if owns_transaction:
            connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT 1 FROM application_batch_consumptions WHERE batch_id=? AND job_url=?",
            (batch_id, job_url),
        ).fetchone()
        if existing is not None:
            if owns_transaction:
                connection.rollback()
            return False
        used = connection.execute(
            "SELECT COUNT(*) FROM application_batch_consumptions WHERE batch_id=?",
            (batch_id,),
        ).fetchone()[0]
        if used >= max_submissions:
            if owns_transaction:
                connection.rollback()
            return False
        current = datetime.now(UTC).isoformat()
        connection.execute(
            "INSERT INTO application_batch_consumptions "
            "(batch_id, job_url, reserved_at, status, updated_at, evidence_json) "
            "VALUES (?, ?, ?, 'reserved', ?, NULL)",
            (batch_id, job_url, current, current),
        )
        if owns_transaction:
            connection.commit()
        return True
    except sqlite3.IntegrityError:
        if owns_transaction:
            connection.rollback()
        return False
    except Exception:
        if owns_transaction:
            connection.rollback()
        raise


def update_batch_submission_status(
    connection: sqlite3.Connection,
    batch_id: str,
    job_url: str,
    status: str,
    evidence: object | None = None,
) -> None:
    """Update an existing reservation without releasing its consumed slot."""
    batch_id = str(batch_id or "").strip()
    job_url = str(job_url or "").strip()
    status = str(status or "").strip()
    if not batch_id or not job_url or not status:
        raise ValueError("batch_id, job_url, and status are required")
    ensure_schema(connection)
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN IMMEDIATE")
    evidence_json = None if evidence is None else _json_text(evidence)
    cursor = connection.execute(
        "UPDATE application_batch_consumptions SET status=?, updated_at=?, evidence_json=? "
        "WHERE batch_id=? AND job_url=?",
        (status, datetime.now(UTC).isoformat(), evidence_json, batch_id, job_url),
    )
    if cursor.rowcount != 1:
        if owns_transaction:
            connection.rollback()
        raise ValueError("batch submission reservation does not exist")
    if owns_transaction:
        connection.commit()
