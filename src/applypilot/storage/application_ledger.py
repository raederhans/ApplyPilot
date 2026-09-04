"""SQLite implementation of the application attempt and authorization ledgers.

All functions require an explicit connection.  Connection ownership remains in
``applypilot.database`` so transaction and compatibility behavior stay stable
while the application-specific schema and SQL have a single focused home.
"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

from applypilot.apply.performance_attribution import safe_normalize_attribution


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
        CREATE TABLE IF NOT EXISTS application_submission_gates (
            gate_id            TEXT PRIMARY KEY,
            attempt_id         TEXT NOT NULL UNIQUE,
            batch_id           TEXT NOT NULL,
            job_url            TEXT NOT NULL,
            claimed_at         TEXT NOT NULL,
            claimed_at_epoch   REAL,
            state              TEXT NOT NULL,
            updated_at         TEXT NOT NULL,
            audit_fingerprint  TEXT,
            idempotency_key    TEXT NOT NULL UNIQUE,
            evidence_json      TEXT
        )
    """)
    gate_columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(application_submission_gates)"
        ).fetchall()
    }
    if "claimed_at_epoch" not in gate_columns:
        connection.execute(
            "ALTER TABLE application_submission_gates ADD COLUMN claimed_at_epoch REAL"
        )
    stale_claims = connection.execute(
        "SELECT gate_id, claimed_at FROM application_submission_gates "
        "WHERE claimed_at_epoch IS NULL"
    ).fetchall()
    for row in stale_claims:
        try:
            claimed = datetime.fromisoformat(str(row[1]))
            if claimed.tzinfo is None or claimed.utcoffset() is None:
                continue
            claimed_epoch = claimed.astimezone(UTC).timestamp()
        except (TypeError, ValueError, OverflowError):
            continue
        connection.execute(
            "UPDATE application_submission_gates SET claimed_at_epoch=? "
            "WHERE gate_id=? AND claimed_at_epoch IS NULL",
            (claimed_epoch, str(row[0])),
        )
    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_application_submission_gates_rate
            ON application_submission_gates(claimed_at, state)
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
        CREATE TABLE IF NOT EXISTS application_receipt_gate_bindings (
            receipt_source TEXT NOT NULL,
            receipt_id     TEXT NOT NULL,
            gate_id        TEXT NOT NULL,
            batch_id       TEXT NOT NULL,
            job_url        TEXT NOT NULL,
            attempt_id     TEXT NOT NULL,
            bound_at_epoch REAL NOT NULL,
            PRIMARY KEY (receipt_source, receipt_id)
        )
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_receipt_gate_bindings_exact
            ON application_receipt_gate_bindings(
                gate_id, batch_id, job_url, attempt_id
            )
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


def claim_submission_gate(
    connection: sqlite3.Connection,
    batch_id: str,
    job_url: str,
    max_submissions: int,
    attempt_id: str,
    *,
    success_target: int | None = None,
    hourly_maximum: int = 15,
    minimum_gap_seconds: float = 20,
    audit_fingerprint: str | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Atomically reserve batch, attempt owner, and global submission capacity.

    The gate serializes only the final authority claim. Browser preparation and
    read-only checks remain parallel. A replay for the same attempt is
    idempotent; a different attempt can never reuse the claim.
    """
    batch_id = str(batch_id or "").strip()
    job_url = str(job_url or "").strip()
    attempt_id = str(attempt_id or "").strip()
    if not batch_id or not job_url or not attempt_id:
        raise ValueError("batch_id, job_url, and attempt_id are required")
    if (
        isinstance(max_submissions, bool)
        or not isinstance(max_submissions, int)
        or max_submissions <= 0
    ):
        raise ValueError("max_submissions must be a positive integer")
    if (
        success_target is not None
        and (
            isinstance(success_target, bool)
            or not isinstance(success_target, int)
            or success_target <= 0
        )
    ):
        raise ValueError("success_target must be a positive integer when provided")
    if isinstance(hourly_maximum, bool) or not isinstance(hourly_maximum, int):
        raise TypeError("hourly_maximum must be an integer")
    if hourly_maximum < 0 or minimum_gap_seconds < 0:
        raise ValueError("submission rate limits cannot be negative")

    ensure_schema(connection)
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(UTC)
    current_text = current.isoformat()
    current_epoch = current.timestamp()
    gate_uuid = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"{batch_id}|{attempt_id}|{job_url}",
    )
    gate_id = f"submit:{gate_uuid}"
    idempotency_key = gate_id.removeprefix("submit:")
    owns_transaction = not connection.in_transaction
    try:
        if owns_transaction:
            connection.execute("BEGIN IMMEDIATE")

        replay = connection.execute(
            "SELECT gate_id, batch_id, job_url, state, audit_fingerprint, "
            "idempotency_key FROM application_submission_gates WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if replay is not None:
            same_claim = (
                str(replay[1]) == batch_id
                and str(replay[2]) == job_url
                and str(replay[4] or "") == str(audit_fingerprint or "")
            )
            if owns_transaction:
                connection.commit() if same_claim else connection.rollback()
            if not same_claim:
                return {"claimed": False, "reason": "submission_gate_claim_conflict"}
            return {
                "claimed": True,
                "reason": "submission_gate_replay",
                "gate_id": str(replay[0]),
                "idempotency_key": str(replay[5]),
                "state": str(replay[3]),
                "replay": True,
            }

        attempt = connection.execute(
            "SELECT job_url, phase, submit_started, status, lease_expires_at "
            "FROM application_attempts WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if attempt is None:
            if owns_transaction:
                connection.rollback()
            return {"claimed": False, "reason": "submission_gate_attempt_missing"}
        if str(attempt[0]) != job_url:
            if owns_transaction:
                connection.rollback()
            return {"claimed": False, "reason": "submission_gate_job_mismatch"}
        if (
            str(attempt[1]) != "reservation"
            or int(attempt[2]) != 0
            or str(attempt[3]) != "in_progress"
        ):
            if owns_transaction:
                connection.rollback()
            return {"claimed": False, "reason": "submission_gate_attempt_not_ready"}
        try:
            lease_expires_at = datetime.fromisoformat(str(attempt[4]))
            if lease_expires_at.tzinfo is None:
                lease_expires_at = lease_expires_at.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            lease_expires_at = current - timedelta(seconds=1)
        if lease_expires_at <= current:
            if owns_transaction:
                connection.rollback()
            return {"claimed": False, "reason": "submission_gate_attempt_lease_expired"}

        # This check intentionally runs in the same write transaction as the
        # reservation below.  A terminal gate alone is not success: a durable,
        # admitted receipt for that job is required.
        if success_target is not None:
            confirmed_successes = connection.execute(
                "SELECT COUNT(DISTINCT c.job_url) "
                "FROM application_batch_consumptions c "
                "JOIN application_submission_gates g "
                "ON g.batch_id=c.batch_id AND g.job_url=c.job_url "
                "JOIN application_receipt_gate_bindings b "
                "ON b.gate_id=g.gate_id AND b.batch_id=g.batch_id "
                "AND b.job_url=g.job_url AND b.attempt_id=g.attempt_id "
                "JOIN application_receipts r "
                "ON r.receipt_source=b.receipt_source AND r.receipt_id=b.receipt_id "
                "WHERE c.batch_id=? AND c.status='applied' AND g.state='applied' "
                "AND g.claimed_at_epoch IS NOT NULL "
                "AND b.bound_at_epoch>=g.claimed_at_epoch",
                (batch_id,),
            ).fetchone()[0]
            if confirmed_successes >= success_target:
                if owns_transaction:
                    connection.rollback()
                return {"claimed": False, "reason": "run_success_target_reached"}

        # Batch target and authorization capacity are one ordered decision:
        # success wins first, then exact-job replay conflict, then slot cap.
        existing = connection.execute(
            "SELECT 1 FROM application_batch_consumptions WHERE batch_id=? AND job_url=?",
            (batch_id, job_url),
        ).fetchone()
        used = connection.execute(
            "SELECT COUNT(*) FROM application_batch_consumptions WHERE batch_id=?",
            (batch_id,),
        ).fetchone()[0]
        if existing is not None:
            if owns_transaction:
                connection.rollback()
            return {
                "claimed": False,
                "reason": (
                    "job_already_reserved"
                    if success_target is not None
                    else "authorization_batch_reservation_denied"
                ),
            }
        if used >= max_submissions:
            if owns_transaction:
                connection.rollback()
            return {
                "claimed": False,
                "reason": (
                    "authorization_batch_capacity_exhausted"
                    if success_target is not None
                    else "authorization_batch_reservation_denied"
                ),
            }

        cutoff_epoch = (current - timedelta(hours=1)).timestamp()
        recent_gate_rows = connection.execute(
            "SELECT claimed_at_epoch FROM application_submission_gates "
            "WHERE claimed_at_epoch>=? AND state!='cancelled_before_action' "
            "ORDER BY claimed_at_epoch",
            (cutoff_epoch,),
        ).fetchall()
        represented_urls = {
            str(row[0])
            for row in connection.execute(
                "SELECT job_url FROM application_submission_gates "
                "WHERE claimed_at_epoch>=? AND state!='cancelled_before_action'",
                (cutoff_epoch,),
            ).fetchall()
        }
        cutoff = (current - timedelta(hours=1)).isoformat()
        historical_rows = connection.execute(
            "SELECT url, applied_at FROM jobs WHERE applied_at IS NOT NULL AND applied_at>=?",
            (cutoff,),
        ).fetchall()
        unrepresented_applied = [
            row for row in historical_rows if str(row[0]) not in represented_urls
        ]
        recent_count = len(recent_gate_rows) + len(unrepresented_applied)
        if hourly_maximum > 0 and recent_count >= hourly_maximum:
            timestamps = [
                datetime.fromtimestamp(float(row[0]), tz=UTC)
                for row in recent_gate_rows
            ]
            retry_after = 0.0
            parsed = list(timestamps)
            for value in (str(row[1]) for row in unrepresented_applied):
                try:
                    parsed_value = datetime.fromisoformat(value)
                    if parsed_value.tzinfo is None or parsed_value.utcoffset() is None:
                        continue
                    parsed_value = parsed_value.astimezone(UTC)
                    parsed.append(parsed_value)
                except (ValueError, OverflowError):
                    continue
            if parsed:
                retry_after = max(
                    0.0,
                    (min(parsed) + timedelta(hours=1) - current).total_seconds(),
                )
            if owns_transaction:
                connection.rollback()
            return {
                "claimed": False,
                "reason": "rolling_hour_submission_cap",
                "retry_after_seconds": retry_after,
            }

        latest_candidates: list[datetime] = []
        latest_candidates.extend(
            datetime.fromtimestamp(float(row[0]), tz=UTC)
            for row in recent_gate_rows
        )
        for value in (str(row[1]) for row in unrepresented_applied):
            try:
                parsed_value = datetime.fromisoformat(value)
                if parsed_value.tzinfo is None or parsed_value.utcoffset() is None:
                    continue
                parsed_value = parsed_value.astimezone(UTC)
                latest_candidates.append(parsed_value)
            except (ValueError, OverflowError):
                continue
        if latest_candidates and minimum_gap_seconds > 0:
            remaining = minimum_gap_seconds - (
                current - max(latest_candidates)
            ).total_seconds()
            if remaining > 0:
                if owns_transaction:
                    connection.rollback()
                return {
                    "claimed": False,
                    "reason": "minimum_submission_gap",
                    "retry_after_seconds": remaining,
                }

        active_writer = connection.execute(
            "SELECT g.attempt_id, a.lease_expires_at "
            "FROM application_submission_gates g "
            "JOIN application_attempts a ON a.attempt_id=g.attempt_id "
            "WHERE g.state='claimed' AND g.attempt_id!=? "
            "AND a.status='in_progress' AND a.lease_expires_at>? "
            "ORDER BY g.claimed_at_epoch LIMIT 1",
            (attempt_id, current_text),
        ).fetchone()
        if active_writer is not None:
            try:
                active_until = datetime.fromisoformat(str(active_writer[1]))
                if active_until.tzinfo is None:
                    active_until = active_until.replace(tzinfo=UTC)
                remaining = max(0.25, (active_until - current).total_seconds())
            except (TypeError, ValueError):
                remaining = 1.0
            if owns_transaction:
                connection.rollback()
            return {
                "claimed": False,
                "reason": "submit_writer_busy",
                "retry_after_seconds": min(5.0, remaining),
                "active_attempt_id": str(active_writer[0]),
            }

        connection.execute(
            "INSERT INTO application_batch_consumptions "
            "(batch_id, job_url, reserved_at, status, updated_at, evidence_json) "
            "VALUES (?, ?, ?, 'reserved', ?, NULL)",
            (batch_id, job_url, current_text, current_text),
        )
        connection.execute(
            "INSERT INTO application_submission_gates "
            "(gate_id, attempt_id, batch_id, job_url, claimed_at, claimed_at_epoch, "
            "state, updated_at, "
            "audit_fingerprint, idempotency_key, evidence_json) "
            "VALUES (?, ?, ?, ?, ?, ?, 'claimed', ?, ?, ?, NULL)",
            (
                gate_id,
                attempt_id,
                batch_id,
                job_url,
                current_text,
                current_epoch,
                current_text,
                str(audit_fingerprint or "") or None,
                idempotency_key,
            ),
        )
        if owns_transaction:
            connection.commit()
        return {
            "claimed": True,
            "reason": "submission_gate_claimed",
            "gate_id": gate_id,
            "idempotency_key": idempotency_key,
            "state": "claimed",
            "replay": False,
        }
    except sqlite3.IntegrityError:
        if owns_transaction:
            connection.rollback()
        return {"claimed": False, "reason": "submission_gate_claim_conflict"}
    except Exception:
        if owns_transaction:
            connection.rollback()
        raise


def update_submission_gate_state(
    connection: sqlite3.Connection,
    attempt_id: str,
    state: str,
    evidence: object | None = None,
) -> bool:
    """Record a bounded terminal state without releasing the consumed claim."""
    attempt_id = str(attempt_id or "").strip()
    state = str(state or "").strip()
    if not attempt_id or not state:
        raise ValueError("attempt_id and state are required")
    ensure_schema(connection)
    owns_transaction = not connection.in_transaction
    cursor = connection.execute(
        "UPDATE application_submission_gates SET state=?, updated_at=?, evidence_json=? "
        "WHERE attempt_id=?",
        (
            state,
            datetime.now(UTC).isoformat(),
            None if evidence is None else _json_text(evidence),
            attempt_id,
        ),
    )
    if owns_transaction:
        connection.commit()
    return cursor.rowcount == 1


def record_attempt_performance(
    connection: sqlite3.Connection,
    attempt_id: str | None,
    performance: object,
) -> bool:
    """Merge final bounded orchestration timings into one terminal attempt."""
    if not attempt_id or not isinstance(performance, dict):
        return False
    if performance.get("version") != 1:
        return False
    allowed = {
        "metrics": {
            "pre_submit_audit_ms",
            "submission_gate_wait_ms",
            "submit_agent_ms",
            "post_submit_observer_ms",
            "prepare_repair_agent_ms",
            "validation_repair_agent_ms",
            "submit_lane_wait_ms",
            "submit_lane_hold_ms",
            "submit_lane_acquisitions",
        },
        "acquisition": {
            "stale_recovery_ms",
            "profile_load_ms",
            "eligibility_refresh_ms",
            "transaction_wait_ms",
            "candidate_fetch_ms",
            "candidate_rows",
            "admission_scan_ms",
            "admission_rows_scanned",
            "total_ms",
            "worker_call_ms",
        },
    }
    bounded: dict[str, object] = {"version": 1}
    for section, keys in allowed.items():
        supplied = performance.get(section)
        values: dict[str, float] = {}
        if isinstance(supplied, dict):
            for key in keys:
                value = supplied.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                numeric = float(value)
                if math.isfinite(numeric) and numeric >= 0:
                    values[key] = round(min(numeric, 86_400_000.0), 3)
        bounded[section] = values
    attribution = safe_normalize_attribution(performance.get("attribution"))
    if attribution is not None:
        bounded["attribution"] = attribution
    ensure_schema(connection)
    row = connection.execute(
        "SELECT evidence_json FROM application_attempts "
        "WHERE attempt_id=? AND status!='in_progress'",
        (attempt_id,),
    ).fetchone()
    if row is None:
        return False
    try:
        existing = json.loads(row[0]) if row[0] else {}
    except (TypeError, json.JSONDecodeError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    existing["orchestration_performance"] = bounded
    cursor = connection.execute(
        "UPDATE application_attempts SET evidence_json=?, updated_at=? "
        "WHERE attempt_id=? AND status!='in_progress'",
        (
            _json_text(existing),
            datetime.now(UTC).isoformat(),
            attempt_id,
        ),
    )
    return cursor.rowcount == 1


def has_admitted_submission_receipt(
    connection: sqlite3.Connection,
    batch_id: str,
    job_url: str,
    attempt_id: str,
) -> bool:
    """Verify a receipt explicitly bound to this exact submission claim."""
    batch_id = str(batch_id or "").strip()
    job_url = str(job_url or "").strip()
    attempt_id = str(attempt_id or "").strip()
    if not batch_id or not job_url or not attempt_id:
        return False
    ensure_schema(connection)
    row = connection.execute(
        "SELECT 1 FROM application_submission_gates g "
        "JOIN application_receipt_gate_bindings b "
        "ON b.gate_id=g.gate_id AND b.batch_id=g.batch_id "
        "AND b.job_url=g.job_url AND b.attempt_id=g.attempt_id "
        "JOIN application_receipts r "
        "ON r.receipt_source=b.receipt_source AND r.receipt_id=b.receipt_id "
        "WHERE g.batch_id=? AND g.job_url=? AND g.attempt_id=? "
        "AND g.claimed_at_epoch IS NOT NULL "
        "AND b.bound_at_epoch>=g.claimed_at_epoch LIMIT 1",
        (batch_id, job_url, attempt_id),
    ).fetchone()
    return row is not None


def bind_admitted_receipt_to_gate(
    connection: sqlite3.Connection,
    receipt_source: str,
    receipt_id: str,
    gate_id: str,
    batch_id: str,
    job_url: str,
    attempt_id: str,
    *,
    bound_at: datetime | None = None,
) -> bool:
    """Atomically bind an admitted receipt to one exact, valid gate identity."""
    values = tuple(
        str(value or "").strip()
        for value in (
            receipt_source,
            receipt_id,
            gate_id,
            batch_id,
            job_url,
            attempt_id,
        )
    )
    if any(not value for value in values):
        return False
    source, receipt, gate, batch, url, attempt = values
    admitted = bound_at or datetime.now(UTC)
    if admitted.tzinfo is None or admitted.utcoffset() is None:
        return False
    try:
        bound_epoch = admitted.astimezone(UTC).timestamp()
    except (OverflowError, OSError, ValueError):
        return False
    ensure_schema(connection)
    gate_row = connection.execute(
        "SELECT claimed_at_epoch FROM application_submission_gates "
        "WHERE gate_id=? AND batch_id=? AND job_url=? AND attempt_id=?",
        (gate, batch, url, attempt),
    ).fetchone()
    if gate_row is None or gate_row[0] is None:
        return False
    try:
        claimed_epoch = float(gate_row[0])
    except (TypeError, ValueError, OverflowError):
        return False
    if bound_epoch < claimed_epoch:
        return False
    receipt_row = connection.execute(
        "SELECT job_url FROM application_receipts "
        "WHERE receipt_source=? AND receipt_id=?",
        (source, receipt),
    ).fetchone()
    if receipt_row is None or str(receipt_row[0]) != url:
        return False
    existing = connection.execute(
        "SELECT gate_id, batch_id, job_url, attempt_id "
        "FROM application_receipt_gate_bindings "
        "WHERE receipt_source=? AND receipt_id=?",
        (source, receipt),
    ).fetchone()
    expected = (gate, batch, url, attempt)
    if existing is not None:
        return tuple(str(value) for value in existing) == expected
    connection.execute(
        "INSERT INTO application_receipt_gate_bindings "
        "(receipt_source, receipt_id, gate_id, batch_id, job_url, attempt_id, "
        "bound_at_epoch) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (source, receipt, gate, batch, url, attempt, bound_epoch),
    )
    return True


def mark_bound_submission_receipt_applied(
    connection: sqlite3.Connection,
    receipt_source: str,
    receipt_id: str,
    gate_id: str,
    batch_id: str,
    job_url: str,
    attempt_id: str,
) -> bool:
    """Close one exact uncertain gate after its bound receipt is admitted.

    The caller owns the transaction. Mixed states and every state other than
    ``submission_uncertain``/``applied`` fail closed so reconciliation cannot
    upgrade a different or merely prepared submission.
    """
    values = tuple(
        str(value or "").strip()
        for value in (
            receipt_source,
            receipt_id,
            gate_id,
            batch_id,
            job_url,
            attempt_id,
        )
    )
    if any(not value for value in values):
        return False
    source, receipt, gate, batch, url, attempt = values
    ensure_schema(connection)
    row = connection.execute(
        "SELECT g.state, c.status FROM application_submission_gates g "
        "JOIN application_batch_consumptions c "
        "ON c.batch_id=g.batch_id AND c.job_url=g.job_url "
        "JOIN application_receipt_gate_bindings b "
        "ON b.gate_id=g.gate_id AND b.batch_id=g.batch_id "
        "AND b.job_url=g.job_url AND b.attempt_id=g.attempt_id "
        "JOIN application_receipts r "
        "ON r.receipt_source=b.receipt_source AND r.receipt_id=b.receipt_id "
        "WHERE b.receipt_source=? AND b.receipt_id=? AND g.gate_id=? "
        "AND g.batch_id=? AND g.job_url=? AND g.attempt_id=? "
        "AND g.claimed_at_epoch IS NOT NULL "
        "AND b.bound_at_epoch>=g.claimed_at_epoch",
        (source, receipt, gate, batch, url, attempt),
    ).fetchone()
    if row is None:
        return False
    gate_state, consumption_status = str(row[0]), str(row[1])
    if gate_state == "applied" and consumption_status == "applied":
        return True
    if (
        gate_state != "submission_uncertain"
        or consumption_status != "submission_uncertain"
    ):
        return False
    updated_at = datetime.now(UTC).isoformat()
    gate_cursor = connection.execute(
        "UPDATE application_submission_gates SET state='applied', updated_at=? "
        "WHERE gate_id=? AND batch_id=? AND job_url=? AND attempt_id=? "
        "AND state='submission_uncertain'",
        (updated_at, gate, batch, url, attempt),
    )
    consumption_cursor = connection.execute(
        "UPDATE application_batch_consumptions "
        "SET status='applied', updated_at=? "
        "WHERE batch_id=? AND job_url=? AND status='submission_uncertain'",
        (updated_at, batch, url),
    )
    return gate_cursor.rowcount == 1 and consumption_cursor.rowcount == 1


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
