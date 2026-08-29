"""ApplyPilot database layer: schema, migrations, stats, and connection helpers.

Single source of truth for the jobs table schema. All columns from every
pipeline stage are created up front so any stage can run independently
without migration ordering issues.
"""

import csv
import json
import sqlite3
import threading
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from applypilot.apply import human_handoff as _human_handoff
from applypilot.apply.contracts import AgentCheckpoint, ApplicationEvent, HumanRequest
from applypilot.config import DB_PATH
from applypilot.storage import agent_control as _agent_control
from applypilot.storage import application_ledger as _application_ledger
from applypilot.storage import job_identity as _job_identity
from applypilot.storage import job_stats as _job_stats
from applypilot.storage import radar as _radar
from applypilot.storage import submission_receipts as _submission_receipts
from applypilot.storage import task_journal as _task_journal

canonicalize_job_url = _job_identity.canonicalize_job_url
extract_platform_job_id = _job_identity.extract_platform_job_id
_normalized_identity_text = _job_identity._normalized_identity_text
_usable_requisition_id = _job_identity._usable_requisition_id
_PORTAL_SUBMITTED_STATES = _submission_receipts._PORTAL_SUBMITTED_STATES
_STRONG_SUBMISSION_RECEIPT = _submission_receipts._STRONG_SUBMISSION_RECEIPT
_receipt_identity_matches = _submission_receipts._receipt_identity_matches
register_radar_source = _radar.register_radar_source
start_radar_fetch_run = _radar.start_radar_fetch_run
finish_radar_fetch_run = _radar.finish_radar_fetch_run
record_radar_observation = _radar.record_radar_observation
upsert_radar_lead = _radar.upsert_radar_lead
link_radar_job_source = _radar.link_radar_job_source
ingest_radar_leads = _radar.ingest_radar_leads
ingest_radar_company_seeds = _radar.ingest_radar_company_seeds
reconcile_radar_leads = _radar.reconcile_radar_leads
_location_scope = _radar._location_scope
_find_applied_exclusion = _radar._find_applied_exclusion


def get_stats(conn: sqlite3.Connection | None = None) -> dict:
    """Return job counts by pipeline stage."""
    return _job_stats.get_stats(conn or get_connection())


def record_submission_observation(
    url: str,
    observation: dict,
    conn: sqlite3.Connection | None = None,
) -> str | None:
    return _submission_receipts.record_submission_observation(
        conn or get_connection(),
        url,
        observation,
    )


def reconcile_submission_receipt(
    evidence: dict,
    conn: sqlite3.Connection | None = None,
) -> dict[str, object]:
    return _submission_receipts.reconcile_submission_receipt(
        conn or get_connection(),
        evidence,
    )


def admit_direct_email_sent_receipt(
    job_url: str,
    evidence: dict,
    conn: sqlite3.Connection | None = None,
    *,
    gate_binding: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return _submission_receipts.admit_direct_email_sent_receipt(
        conn or get_connection(),
        job_url,
        evidence,
        gate_binding=gate_binding,
    )


def ensure_radar_schema(conn: sqlite3.Connection | None = None) -> None:
    _radar.ensure_radar_schema(conn or get_connection())


def ingest_radar_official_jobs(
    conn: sqlite3.Connection,
    run_id: str,
    source: dict,
    jobs: list[dict],
) -> dict:
    return _radar.ingest_radar_official_jobs(
        conn,
        run_id,
        source,
        jobs,
        store_jobs_fn=store_jobs,
    )


def get_applied_exclusion_set(
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    return _radar.get_applied_exclusion_set(conn or get_connection())


def get_latest_applied_exclusion_snapshot(
    conn: sqlite3.Connection | None = None,
    *,
    snapshot_id: str | None = None,
    max_age_hours: int = 6,
) -> dict:
    return _radar.get_latest_applied_exclusion_snapshot(
        conn or get_connection(),
        snapshot_id=snapshot_id,
        max_age_hours=max_age_hours,
    )


def get_radar_daily_snapshot(
    conn: sqlite3.Connection | None = None,
    *,
    since: str | None = None,
    expected_sources: list[dict] | None = None,
    applied_snapshot_id: str | None = None,
) -> dict:
    return _radar.get_radar_daily_snapshot(
        conn or get_connection(),
        since=since,
        expected_sources=expected_sources,
        applied_snapshot_id=applied_snapshot_id,
    )

# Thread-local connection storage — each thread gets its own connection
# (required for SQLite thread safety with parallel workers)
_local = threading.local()










def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Get a thread-local cached SQLite connection with WAL mode enabled.

    Each thread gets its own connection (required for SQLite thread safety).
    Connections are cached and reused within the same thread.

    Args:
        db_path: Override the default DB_PATH. Useful for testing.

    Returns:
        sqlite3.Connection configured with WAL mode and row factory.
    """
    path = str(db_path or DB_PATH)

    if not hasattr(_local, "connections"):
        _local.connections = {}

    conn = _local.connections.get(path)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.ProgrammingError:
            pass

    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    _local.connections[path] = conn
    return conn


def close_connection(db_path: Path | str | None = None) -> None:
    """Close the cached connection for the current thread."""
    path = str(db_path or DB_PATH)
    if hasattr(_local, "connections"):
        conn = _local.connections.pop(path, None)
        if conn is not None:
            conn.close()


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Create the full jobs table with all columns from every pipeline stage.

    This is idempotent -- safe to call on every startup. Uses CREATE TABLE IF NOT EXISTS
    so it won't destroy existing data.

    Schema columns by stage:
      - Discovery:  url, title, salary, description, location, company_name,
                    source_site, site (legacy source alias), strategy, discovered_at
      - Enrichment: full_description, application_url, detail_scraped_at, detail_error
      - Scoring:    fit_score, score_reasoning, scored_at
      - Tailoring:  tailored_resume_path, tailored_at, tailor_attempts
      - Cover:      cover_letter_path, cover_letter_at, cover_attempts,
                    cover_letter_status, cover_letter_approved_at
      - Apply:      applied_at, apply_status, apply_error, apply_attempts,
                   apply_retry_blocked, apply_retry_reason, agent_id,
                   last_attempted_at, apply_duration_ms, apply_task_id,
                   verification_confidence

    Args:
        db_path: Override the default DB_PATH.

    Returns:
        sqlite3.Connection with the schema initialized.
    """
    path = db_path or DB_PATH

    # Ensure parent directory exists
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            -- Discovery stage (smart_extract / job_search)
            url                   TEXT PRIMARY KEY,
            title                 TEXT,
            salary                TEXT,
            description           TEXT,
            location              TEXT,
            company_name          TEXT,
            source_site           TEXT,
            site                  TEXT,
            strategy              TEXT,
            discovered_at         TEXT,
            last_seen_at          TEXT,
            platform_job_id       TEXT,
            canonical_job_url     TEXT,
            dedupe_status         TEXT,
            possible_repost_of    TEXT,

            -- Enrichment stage (detail_scraper)
            full_description      TEXT,
            application_url       TEXT,
            detail_scraped_at     TEXT,
            detail_error          TEXT,

            -- Scoring stage (job_scorer)
            fit_score             INTEGER,
            score_reasoning       TEXT,
            scored_at             TEXT,

            -- Tailoring stage (resume tailor)
            tailored_resume_path  TEXT,
            tailored_at           TEXT,
            tailor_attempts       INTEGER DEFAULT 0,
            tailor_status         TEXT,
            tailor_error          TEXT,
            tailor_source_resume_path TEXT,
            tailor_report_path    TEXT,

            -- Cover letter stage
            cover_letter_path     TEXT,
            cover_letter_at       TEXT,
            cover_attempts        INTEGER DEFAULT 0,
            cover_letter_status   TEXT,
            cover_letter_error    TEXT,
            cover_letter_approved_at TEXT,
            cover_letter_approved_by TEXT,
            cover_letter_source_resume_path TEXT,
            cover_letter_evidence_sources TEXT,

            -- Application stage
            applied_at            TEXT,
            apply_status          TEXT,
            apply_error           TEXT,
            apply_attempts        INTEGER DEFAULT 0,
            apply_retry_blocked   INTEGER DEFAULT 0,
            apply_retry_reason    TEXT,
            agent_id              TEXT,
            last_attempted_at     TEXT,
            apply_duration_ms     INTEGER,
            apply_task_id         TEXT,
            verification_confidence TEXT,
            application_evidence TEXT,
            application_recorded_at TEXT,
            submission_observation_json TEXT,
            submission_observed_at TEXT,
            unanswered_questions_json TEXT,
            unanswered_questions_updated_at TEXT,
            application_readiness_status TEXT,
            application_readiness_reason TEXT,
            application_readiness_reviewed_at TEXT,
            application_readiness_reviewed_by TEXT,
            application_readiness_fingerprint TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS application_fact_revisions (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            fact_key       TEXT NOT NULL,
            old_value_json TEXT,
            new_value_json TEXT NOT NULL,
            context        TEXT,
            source         TEXT,
            confirmed_at   TEXT,
            note           TEXT,
            recorded_at    TEXT NOT NULL
        )
    """)
    ensure_application_batch_schema(conn)
    ensure_radar_schema(conn)
    from applypilot.resume_library import ensure_resume_library_schema

    ensure_resume_library_schema(conn)
    conn.commit()

    # Run migrations for any columns added after initial schema
    added_columns = ensure_columns(conn)
    if "apply_retry_blocked" in added_columns:
        conn.execute("""
            UPDATE jobs
            SET apply_retry_blocked = CASE
                    WHEN apply_status != 'applied' AND apply_attempts >= 99 THEN 1
                    ELSE 0
                END,
                apply_retry_reason = CASE
                    WHEN apply_status != 'applied' AND apply_attempts >= 99
                    THEN COALESCE(apply_error, 'legacy_permanent_failure')
                    ELSE NULL
                END,
                apply_attempts = CASE
                    WHEN apply_attempts >= 99 THEN apply_attempts - 99
                    ELSE apply_attempts
                END
        """)
    # ``site`` historically stored the discovery board. Preserve that value as
    # source metadata, but never guess an employer name from it.
    conn.execute(
        "UPDATE jobs SET source_site = site "
        "WHERE (source_site IS NULL OR source_site = '') AND site IS NOT NULL"
    )
    _backfill_job_identities(conn)
    conn.commit()

    return conn


# Complete column registry: column_name -> SQL type with optional default.
# This is the single source of truth. Adding a column here is all that's needed
# for it to appear in both new databases and migrated ones.
_ALL_COLUMNS: dict[str, str] = {
    # Discovery
    "url": "TEXT PRIMARY KEY",
    "title": "TEXT",
    "salary": "TEXT",
    "description": "TEXT",
    "location": "TEXT",
    "company_name": "TEXT",
    "source_site": "TEXT",
    "site": "TEXT",
    "strategy": "TEXT",
    "discovered_at": "TEXT",
    "last_seen_at": "TEXT",
    "platform_job_id": "TEXT",
    "canonical_job_url": "TEXT",
    "dedupe_status": "TEXT",
    "possible_repost_of": "TEXT",
    # Enrichment
    "full_description": "TEXT",
    "application_url": "TEXT",
    "detail_scraped_at": "TEXT",
    "detail_error": "TEXT",
    # Scoring
    "fit_score": "INTEGER",
    "score_reasoning": "TEXT",
    "scored_at": "TEXT",
    # Tailoring
    "tailored_resume_path": "TEXT",
    "tailored_at": "TEXT",
    "tailor_attempts": "INTEGER DEFAULT 0",
    "tailor_status": "TEXT",
    "tailor_error": "TEXT",
    "tailor_source_resume_path": "TEXT",
    "tailor_report_path": "TEXT",
    # Cover letter
    "cover_letter_path": "TEXT",
    "cover_letter_at": "TEXT",
    "cover_attempts": "INTEGER DEFAULT 0",
    "cover_letter_status": "TEXT",
    "cover_letter_error": "TEXT",
    "cover_letter_approved_at": "TEXT",
    "cover_letter_approved_by": "TEXT",
    "cover_letter_source_resume_path": "TEXT",
    "cover_letter_evidence_sources": "TEXT",
    # Application
    "applied_at": "TEXT",
    "apply_status": "TEXT",
    "apply_error": "TEXT",
    "apply_attempts": "INTEGER DEFAULT 0",
    "apply_retry_blocked": "INTEGER DEFAULT 0",
    "apply_retry_reason": "TEXT",
    "agent_id": "TEXT",
    "last_attempted_at": "TEXT",
    "apply_duration_ms": "INTEGER",
    "apply_task_id": "TEXT",
    "verification_confidence": "TEXT",
    "application_evidence": "TEXT",
    "application_recorded_at": "TEXT",
    "submission_observation_json": "TEXT",
    "submission_observed_at": "TEXT",
    "unanswered_questions_json": "TEXT",
    "unanswered_questions_updated_at": "TEXT",
    # Evidence-based review of work authorization, availability, location,
    # and other job-specific hard requirements. This is intentionally
    # separate from deterministic eligibility screening.
    "application_readiness_status": "TEXT",
    "application_readiness_reason": "TEXT",
    "application_readiness_reviewed_at": "TEXT",
    "application_readiness_reviewed_by": "TEXT",
    "application_readiness_fingerprint": "TEXT",
    # Scoring failures must not be represented as real zero scores.
    "score_status": "TEXT",
    "score_error": "TEXT",
    "score_attempts": "INTEGER DEFAULT 0",
    # Deterministic hard-eligibility screening
    "eligibility_status": "TEXT",
    "eligibility_reason": "TEXT",
    "eligibility_evaluated_at": "TEXT",
}


def ensure_columns(conn: sqlite3.Connection | None = None) -> list[str]:
    """Add any missing columns to the jobs table (forward migration).

    Reads the current table schema via PRAGMA table_info and compares against
    the full column registry. Any missing columns are added with ALTER TABLE.

    This makes it safe to upgrade the database from any previous version --
    columns are only added, never removed or renamed.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        List of column names that were added (empty if schema was already current).
    """
    if conn is None:
        conn = get_connection()

    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    added = []

    for col, dtype in _ALL_COLUMNS.items():
        if col not in existing:
            # PRIMARY KEY columns can't be added via ALTER TABLE, but url
            # is always created with the table itself so this is safe
            if "PRIMARY KEY" in dtype:
                continue
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {dtype}")
            added.append(col)

    if added:
        conn.commit()

    return added


def ensure_application_batch_schema(conn: sqlite3.Connection | None = None) -> None:
    """Create durable application and Agent control ledgers."""
    connection = conn or get_connection()
    _application_ledger.ensure_schema(connection)
    _agent_control.ensure_schema(connection)
    _task_journal.ensure_schema(connection)
    _human_handoff.ensure_schema(connection)


def append_agent_event(
    event: ApplicationEvent,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Append one advisory control event without changing application state."""
    connection = conn or get_connection()
    owns_transaction = not connection.in_transaction
    try:
        inserted = _agent_control.append_event(connection, event)
        if owns_transaction:
            connection.commit()
        return inserted
    except (sqlite3.Error, TypeError, ValueError):
        if owns_transaction:
            connection.rollback()
        raise


def record_agent_turn_control(
    event: ApplicationEvent,
    checkpoint: AgentCheckpoint,
    human_request: HumanRequest | None = None,
    conn: sqlite3.Connection | None = None,
) -> tuple[bool, bool, bool | None]:
    """Atomically append a completed turn, checkpoint, and optional handoff."""
    connection = conn or get_connection()
    owns_transaction = not connection.in_transaction
    try:
        event_inserted = _agent_control.append_event(connection, event)
        checkpoint_inserted = _agent_control.append_checkpoint(connection, checkpoint)
        request_inserted = (
            None
            if human_request is None
            else _agent_control.create_human_request(connection, human_request)
        )
        if owns_transaction:
            connection.commit()
        return event_inserted, checkpoint_inserted, request_inserted
    except (sqlite3.Error, TypeError, ValueError):
        if owns_transaction:
            connection.rollback()
        raise


def start_application_attempt(
    job_url: str,
    worker_id: str,
    *,
    batch_id: str | None = None,
    lease_minutes: int = 45,
    conn: sqlite3.Connection | None = None,
) -> str:
    """Start one leased attempt inside the caller's transaction when present."""
    if not job_url or not worker_id:
        raise ValueError("job_url and worker_id are required")
    if isinstance(lease_minutes, bool) or not isinstance(lease_minutes, int) or lease_minutes < 5:
        raise ValueError("lease_minutes must be an integer of at least 5")
    connection = conn or get_connection()
    owns_transaction = not connection.in_transaction
    attempt_id = _application_ledger.start_attempt(
        connection,
        job_url,
        worker_id,
        batch_id=batch_id,
        lease_minutes=lease_minutes,
    )
    if owns_transaction:
        connection.commit()
    return attempt_id


def record_application_risk_event(
    job_url: str,
    category: str,
    severity: str,
    *,
    attempt_id: str | None = None,
    evidence: object | None = None,
    conn: sqlite3.Connection | None = None,
) -> str:
    """Append one compact global risk item without raw credentials or profile data."""
    if not job_url or not category or severity not in {"low", "medium", "high"}:
        raise ValueError("job_url, category, and a valid severity are required")
    connection = conn or get_connection()
    owns_transaction = not connection.in_transaction
    event_id = _application_ledger.record_risk_event(
        connection,
        job_url,
        category,
        severity,
        attempt_id=attempt_id,
        evidence=evidence,
    )
    if owns_transaction:
        connection.commit()
    return event_id


def resolve_application_risks(
    job_url: str,
    *,
    categories: tuple[str, ...],
    conn: sqlite3.Connection | None = None,
) -> int:
    """Resolve selected open risks after stronger evidence is admitted."""
    if not categories:
        return 0
    connection = conn or get_connection()
    owns_transaction = not connection.in_transaction
    count = _application_ledger.resolve_risks(connection, job_url, categories=categories)
    if owns_transaction:
        connection.commit()
    return count


def update_application_attempt(
    attempt_id: str | None,
    *,
    phase: str,
    submit_started: bool,
    lease_minutes: int = 45,
    evidence: object | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Advance an active attempt and renew its lease without reviving terminal rows."""
    if not attempt_id:
        return False
    connection = conn or get_connection()
    owns_transaction = not connection.in_transaction
    updated = _application_ledger.update_attempt(
        connection,
        attempt_id,
        phase=phase,
        submit_started=submit_started,
        lease_minutes=lease_minutes,
        evidence=evidence,
    )
    if owns_transaction:
        connection.commit()
    return updated


def finalize_application_attempt(
    attempt_id: str | None,
    status: str,
    *,
    evidence: object | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Finalize one active attempt; repeated finalization is a no-op."""
    if not attempt_id:
        return False
    connection = conn or get_connection()
    owns_transaction = not connection.in_transaction
    finalized = _application_ledger.finalize_attempt(
        connection,
        attempt_id,
        status,
        evidence=evidence,
    )
    if owns_transaction:
        connection.commit()
    return finalized


def record_application_attempt_performance(
    attempt_id: str | None,
    performance: object,
    *,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Persist final bounded timing evidence after the submit lane is released."""
    if not attempt_id:
        return False
    connection = conn or get_connection()
    owns_transaction = not connection.in_transaction
    recorded = _application_ledger.record_attempt_performance(
        connection,
        attempt_id,
        performance,
    )
    if owns_transaction:
        connection.commit()
    return recorded


def recover_stale_application_attempts(
    conn: sqlite3.Connection | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Recover expired attempts, preserving uncertainty after submit began."""
    return _application_ledger.recover_stale_attempts(conn or get_connection(), now=now)


def prune_application_runtime_history(
    *,
    retention_days: int = 180,
    execute: bool = False,
    conn: sqlite3.Connection | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Preview or remove old terminal attempt rows, never receipts or uncertainty."""
    if isinstance(retention_days, bool) or not isinstance(retention_days, int) or retention_days < 30:
        raise ValueError("retention_days must be an integer of at least 30")
    if not execute:
        if conn is not None:
            return _application_ledger.preview_runtime_history(
                conn,
                retention_days=retention_days,
                now=now,
            )
        if not DB_PATH.is_file():
            return {
                "eligible_attempts": 0,
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
        uri = f"{DB_PATH.resolve().as_uri()}?mode=ro"
        read_only = sqlite3.connect(uri, uri=True)
        read_only.row_factory = sqlite3.Row
        try:
            return _application_ledger.preview_runtime_history(
                read_only,
                retention_days=retention_days,
                now=now,
            )
        finally:
            read_only.close()
    return _application_ledger.prune_runtime_history(
        conn or get_connection(),
        retention_days=retention_days,
        execute=True,
        now=now,
    )


def reserve_batch_submission(
    batch_id: str,
    job_url: str,
    max_submissions: int,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Atomically consume one batch slot; reservations are deliberately permanent."""
    batch_id = str(batch_id or "").strip()
    job_url = str(job_url or "").strip()
    if not batch_id or not job_url:
        raise ValueError("batch_id and job_url are required")
    if isinstance(max_submissions, bool) or not isinstance(max_submissions, int) or max_submissions <= 0:
        raise ValueError("max_submissions must be a positive integer")
    return _application_ledger.reserve_batch_submission(
        conn or get_connection(),
        batch_id,
        job_url,
        max_submissions,
    )


def claim_submission_gate(
    batch_id: str,
    job_url: str,
    max_submissions: int,
    attempt_id: str,
    *,
    success_target: int | None = None,
    hourly_maximum: int = 15,
    minimum_gap_seconds: float = 20,
    audit_fingerprint: str | None = None,
    conn: sqlite3.Connection | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Atomically claim batch, attempt, and global submission authority."""
    return _application_ledger.claim_submission_gate(
        conn or get_connection(),
        batch_id,
        job_url,
        max_submissions,
        attempt_id,
        success_target=success_target,
        hourly_maximum=hourly_maximum,
        minimum_gap_seconds=minimum_gap_seconds,
        audit_fingerprint=audit_fingerprint,
        now=now,
    )


def update_submission_gate_state(
    attempt_id: str,
    state: str,
    evidence: object | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Record the durable outcome of one claimed submission authority."""
    return _application_ledger.update_submission_gate_state(
        conn or get_connection(),
        attempt_id,
        state,
        evidence,
    )


def has_admitted_submission_receipt(
    batch_id: str,
    job_url: str,
    attempt_id: str,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Verify a durable receipt for one exact batch/job/attempt claim."""
    return _application_ledger.has_admitted_submission_receipt(
        conn or get_connection(),
        batch_id,
        job_url,
        attempt_id,
    )


def bind_admitted_receipt_to_gate(
    receipt_source: str,
    receipt_id: str,
    gate_id: str,
    batch_id: str,
    job_url: str,
    attempt_id: str,
    *,
    bound_at: datetime | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Bind an admitted receipt to one exact gate; legacy receipts stay unbound."""
    return _application_ledger.bind_admitted_receipt_to_gate(
        conn or get_connection(),
        receipt_source,
        receipt_id,
        gate_id,
        batch_id,
        job_url,
        attempt_id,
        bound_at=bound_at,
    )


def update_batch_submission_status(
    batch_id: str,
    job_url: str,
    status: str,
    evidence: object | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Update an existing reservation without releasing its consumed slot."""
    batch_id = str(batch_id or "").strip()
    job_url = str(job_url or "").strip()
    status = str(status or "").strip()
    if not batch_id or not job_url or not status:
        raise ValueError("batch_id, job_url, and status are required")
    _application_ledger.update_batch_submission_status(
        conn or get_connection(),
        batch_id,
        job_url,
        status,
        evidence,
    )




def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)














def _backfill_job_identities(conn: sqlite3.Connection) -> None:
    """Populate lightweight identity fields for pre-existing job records."""
    rows = conn.execute(
        "SELECT url, application_url FROM jobs "
        "WHERE platform_job_id IS NULL OR canonical_job_url IS NULL OR last_seen_at IS NULL"
    ).fetchall()
    now = datetime.now(UTC).isoformat()
    for row in rows:
        identity_url = row["application_url"] or row["url"] or ""
        conn.execute(
            "UPDATE jobs SET platform_job_id = COALESCE(platform_job_id, ?), "
            "canonical_job_url = COALESCE(canonical_job_url, ?), "
            "last_seen_at = COALESCE(last_seen_at, discovered_at, ?) WHERE url = ?",
            (
                extract_platform_job_id(identity_url) or None,
                canonicalize_job_url(identity_url) or None,
                now,
                row["url"],
            ),
        )


def record_unanswered_questions(
    url: str, questions: list[dict], conn: sqlite3.Connection | None = None
) -> None:
    """Attach sanitized unresolved form questions to the existing job record."""
    if conn is None:
        conn = get_connection()
    cleaned: list[dict] = []
    for item in questions:
        if not isinstance(item, dict) or not str(item.get("question", "")).strip():
            continue
        cleaned.append({
            "question": str(item.get("question", "")).strip()[:500],
            "field_type": str(item.get("field_type", "unknown")).strip()[:80],
            "required": bool(item.get("required", False)),
            "reason": str(item.get("reason", "missing confirmed fact")).strip()[:300],
            "proposed_context": str(item.get("proposed_context", "job-specific")).strip()[:200],
        })
    conn.execute(
        "UPDATE jobs SET unanswered_questions_json = ?, "
        "unanswered_questions_updated_at = ? WHERE url = ?",
        (json.dumps(cleaned, ensure_ascii=False), datetime.now(UTC).isoformat(), url),
    )
    conn.commit()


def get_unanswered_questions(conn: sqlite3.Connection | None = None) -> list[dict]:
    """Return jobs that still have one or more unresolved application questions."""
    if conn is None:
        conn = get_connection()
    rows = conn.execute(
        "SELECT url, title, company_name, unanswered_questions_json, "
        "unanswered_questions_updated_at FROM jobs "
        "WHERE unanswered_questions_json IS NOT NULL "
        "AND unanswered_questions_json != '[]' "
        "ORDER BY unanswered_questions_updated_at DESC"
    ).fetchall()
    result: list[dict] = []
    for row in rows:
        try:
            questions = json.loads(row["unanswered_questions_json"])
        except (json.JSONDecodeError, TypeError):
            questions = []
        if questions:
            result.append({
                "url": row["url"],
                "title": row["title"],
                "company_name": row["company_name"],
                "questions": questions,
                "updated_at": row["unanswered_questions_updated_at"],
            })
    return result




def import_linkedin_applied_export(
    file_path: Path | str,
    conn: sqlite3.Connection | None = None,
) -> dict[str, object]:
    """Merge a lightweight LinkedIn Applied JSON/CSV export into local state.

    The export may be a JSON list, ``{"applications": [...]}``, or CSV. Expected
    fields are ``url`` (or ``job_url``), plus optional title, company and
    applied_at. Existing descriptive data is preserved; the operation only adds
    missing identity text and refreshes application state.
    """
    path = Path(file_path)
    snapshot_metadata: dict = {}
    if path.suffix.casefold() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            records = list(csv.DictReader(handle))
        snapshot_metadata = {"source": "linkedin_applied_csv"}
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        records = payload.get("applications", []) if isinstance(payload, dict) else payload
        if isinstance(payload, dict):
            snapshot_metadata = {
                key: value for key, value in payload.items() if key != "applications"
            }
    if not isinstance(records, list):
        raise TypeError("LinkedIn Applied export must contain a list of records")

    if conn is None:
        conn = get_connection()
    ensure_radar_schema(conn)
    counts = {"inserted": 0, "updated": 0, "skipped": 0}
    now = datetime.now(UTC).isoformat()
    for item in records:
        if not isinstance(item, dict):
            counts["skipped"] += 1
            continue
        raw_url = str(
            item.get("url") or item.get("job_url") or item.get("canonical_url") or ""
        ).strip()
        platform_job_id = extract_platform_job_id(raw_url)
        canonical_url = canonicalize_job_url(raw_url)
        if not platform_job_id or not canonical_url:
            counts["skipped"] += 1
            continue
        title = str(item.get("title") or "").strip()[:500] or None
        company = str(item.get("company") or item.get("company_name") or "").strip()[:500] or None
        location = str(item.get("location") or "").strip()[:500] or None
        applied_at = str(item.get("applied_at") or item.get("date") or now).strip()[:80]
        existing = conn.execute(
            "SELECT url FROM jobs WHERE platform_job_id = ? OR canonical_job_url = ? LIMIT 1",
            (platform_job_id, canonical_url),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE jobs SET title = COALESCE(NULLIF(title, ''), ?),
                                company_name = COALESCE(NULLIF(company_name, ''), ?),
                                location = COALESCE(NULLIF(location, ''), ?),
                                source_site = COALESCE(NULLIF(source_site, ''), 'linkedin'),
                                site = COALESCE(NULLIF(site, ''), 'linkedin'),
                                platform_job_id = ?, canonical_job_url = ?,
                                apply_status = 'applied', applied_at = COALESCE(applied_at, ?),
                                apply_error = NULL, agent_id = NULL,
                                apply_retry_blocked = 0, apply_retry_reason = NULL,
                                verification_confidence = 'platform_export',
                                application_evidence = 'linkedin_applied_export',
                                application_recorded_at = ?
                WHERE url = ?
                """,
                (
                    title,
                    company,
                    location,
                    platform_job_id,
                    canonical_url,
                    applied_at,
                    now,
                    existing["url"],
                ),
            )
            counts["updated"] += 1
        else:
            conn.execute(
                """
                INSERT INTO jobs (
                    url, title, company_name, location, source_site, site, discovered_at,
                    last_seen_at, platform_job_id, canonical_job_url, dedupe_status,
                    applied_at, apply_status, verification_confidence,
                    application_evidence, application_recorded_at
                ) VALUES (?, ?, ?, ?, 'linkedin', 'linkedin', ?, ?, ?, ?,
                          'platform_applied_import', ?, 'applied', 'platform_export',
                          'linkedin_applied_export', ?)
                """,
                (
                    canonical_url, title, company, location, now, now, platform_job_id,
                    canonical_url, applied_at, now,
                ),
            )
            counts["inserted"] += 1
    declared_total = snapshot_metadata.get("observed_total")
    pages_read = snapshot_metadata.get("pages_read")
    try:
        declared_total = int(declared_total) if declared_total is not None else None
    except (TypeError, ValueError):
        declared_total = None
    try:
        pages_read = int(pages_read) if pages_read is not None else None
    except (TypeError, ValueError):
        pages_read = None
    observed_at = str(snapshot_metadata.get("observed_at") or "").strip()[:80] or None
    observed_at_valid = False
    if observed_at:
        try:
            observed_timestamp = datetime.fromisoformat(observed_at)
            observed_at_valid = observed_timestamp.tzinfo is not None
        except ValueError:
            observed_at_valid = False
    completeness = (
        "complete"
        if snapshot_metadata.get("complete") is True
        and declared_total == len(records)
        and pages_read is not None
        and pages_read > 0
        and counts["skipped"] == 0
        and observed_at_valid
        else "partial"
    )
    snapshot_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO radar_exclusion_snapshots (
            snapshot_id, source, observed_at, imported_at, declared_total,
            pages_read, imported_count, updated_count, skipped_count,
            completeness, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            str(snapshot_metadata.get("source") or "linkedin_applied_import")[:200],
            observed_at,
            now,
            declared_total,
            pages_read,
            counts["inserted"],
            counts["updated"],
            counts["skipped"],
            completeness,
            _json_text(snapshot_metadata),
        ),
    )
    conn.commit()
    return {
        **counts,
        "snapshot_id": snapshot_id,
        "source": str(
            snapshot_metadata.get("source") or "linkedin_applied_import"
        )[:200],
        "observed_at": observed_at,
        "declared_total": declared_total,
        "pages_read": pages_read,
        "completeness": completeness,
    }


def record_application_fact_revision(
    fact_key: str,
    old_value: object,
    new_value: object,
    *,
    context: str = "application",
    source: str = "user_confirmed",
    confirmed_at: str | None = None,
    note: str = "",
    conn: sqlite3.Connection | None = None,
) -> int:
    """Append one current-fact change to the lightweight local knowledge base."""
    key = fact_key.strip()
    if not key:
        raise ValueError("fact_key is required")
    if conn is None:
        conn = get_connection()
    now = datetime.now(UTC).isoformat()
    cursor = conn.execute(
        """
        INSERT INTO application_fact_revisions (
            fact_key, old_value_json, new_value_json, context, source,
            confirmed_at, note, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            key,
            json.dumps(old_value, ensure_ascii=False),
            json.dumps(new_value, ensure_ascii=False),
            context.strip()[:300],
            source.strip()[:100],
            (confirmed_at or now).strip()[:80],
            note.strip()[:500],
            now,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def get_application_fact_revisions(
    fact_key: str | None = None,
    limit: int = 100,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """Read recent fact changes for agent guidance and user review."""
    if conn is None:
        conn = get_connection()
    params: list[object] = []
    where = ""
    if fact_key:
        where = "WHERE fact_key = ?"
        params.append(fact_key.strip())
    params.append(max(1, min(int(limit), 500)))
    rows = conn.execute(
        f"SELECT * FROM application_fact_revisions {where} "
        "ORDER BY id DESC LIMIT ?",
        params,
    ).fetchall()
    result: list[dict] = []
    for row in rows:
        item = dict(row)
        for field in ("old_value_json", "new_value_json"):
            try:
                item[field.removesuffix("_json")] = json.loads(item.pop(field))
            except (json.JSONDecodeError, TypeError):
                item[field.removesuffix("_json")] = item.pop(field)
        result.append(item)
    return result




def store_jobs(conn: sqlite3.Connection, jobs: list[dict],
               site: str, strategy: str) -> tuple[int, int]:
    """Store discovered jobs, skipping duplicates by URL.

    Args:
        conn: Database connection.
        jobs: List of job dicts with keys: url, title, salary, description, location.
        site: Source site name (e.g. "RemoteOK", "Dice").
        strategy: Extraction strategy used (e.g. "json_ld", "api_response", "css_selectors").

    Returns:
        Tuple of (new_count, duplicate_count).
    """
    now = datetime.now(UTC).isoformat()
    from applypilot.eligibility import evaluate_job_eligibility
    new = 0
    existing = 0

    def refreshed_text(existing_value: object, candidate_value: object) -> str | None:
        """Prefer richer text, except when a shorter candidate removes encoded HTML."""
        existing_text = str(existing_value or "").strip()
        candidate_text = str(candidate_value or "").strip()
        if not candidate_text:
            return existing_text or None
        if not existing_text:
            return candidate_text
        encoded_markers = ("&lt;", "&gt;", "&quot;")
        existing_is_encoded = any(marker in existing_text for marker in encoded_markers)
        candidate_is_encoded = any(marker in candidate_text for marker in encoded_markers)
        if existing_is_encoded and not candidate_is_encoded:
            return candidate_text
        if len(candidate_text) > len(existing_text):
            return candidate_text
        return existing_text

    for job in jobs:
        url = job.get("url")
        if not url:
            continue
        identity_url = job.get("application_url") or url
        platform_job_id = str(
            job.get("platform_job_id") or extract_platform_job_id(identity_url) or ""
        )
        canonical_url = str(
            job.get("canonical_job_url") or canonicalize_job_url(identity_url) or ""
        )
        duplicate = conn.execute(
            "SELECT url, description, full_description FROM jobs WHERE url = ? LIMIT 1", (url,)
        ).fetchone()
        if not duplicate and platform_job_id:
            duplicate = conn.execute(
                "SELECT url, description, full_description FROM jobs "
                "WHERE platform_job_id = ? LIMIT 1",
                (platform_job_id,),
            ).fetchone()
        if not duplicate and canonical_url:
            duplicate = conn.execute(
                "SELECT url, description, full_description FROM jobs "
                "WHERE canonical_job_url = ? LIMIT 1",
                (canonical_url,),
            ).fetchone()
        if duplicate:
            refreshed_description = refreshed_text(
                duplicate["description"], job.get("description")
            )
            refreshed_full_description = refreshed_text(
                duplicate["full_description"], job.get("full_description")
            )
            conn.execute(
                """
                UPDATE jobs SET
                    last_seen_at = ?,
                    title = COALESCE(NULLIF(title, ''), ?),
                    company_name = COALESCE(NULLIF(company_name, ''), ?),
                    location = COALESCE(NULLIF(location, ''), ?),
                    salary = COALESCE(NULLIF(salary, ''), ?),
                    description = ?,
                    full_description = ?,
                    application_url = CASE
                        WHEN NULLIF(?, '') IS NULL THEN application_url
                        WHEN application_url IS NULL OR application_url = '' OR application_url = url
                        THEN ? ELSE application_url END,
                    detail_scraped_at = COALESCE(detail_scraped_at, ?),
                    detail_error = COALESCE(detail_error, ?),
                    source_site = COALESCE(NULLIF(source_site, ''), ?),
                    site = COALESCE(NULLIF(site, ''), ?),
                    platform_job_id = COALESCE(NULLIF(platform_job_id, ''), ?),
                    canonical_job_url = COALESCE(NULLIF(canonical_job_url, ''), ?)
                WHERE url = ?
                """,
                (
                    now,
                    job.get("title"),
                    job.get("company_name") or job.get("company"),
                    job.get("location"),
                    job.get("salary"),
                    refreshed_description,
                    refreshed_full_description,
                    job.get("application_url"),
                    job.get("application_url"),
                    job.get("detail_scraped_at"),
                    job.get("detail_error"),
                    site,
                    site,
                    platform_job_id or None,
                    canonical_url or None,
                    duplicate["url"],
                ),
            )
            existing += 1
            continue

        possible_repost_of = None
        normalized_title = _normalized_identity_text(job.get("title"))
        normalized_company = _normalized_identity_text(
            job.get("company_name") or job.get("company")
        )
        if platform_job_id and normalized_title and normalized_company:
            applied_rows = conn.execute(
                "SELECT url, title, company_name, platform_job_id FROM jobs "
                "WHERE applied_at IS NOT NULL ORDER BY applied_at DESC"
            ).fetchall()
            for applied in applied_rows:
                if (
                    applied["platform_job_id"] != platform_job_id
                    and _normalized_identity_text(applied["title"]) == normalized_title
                    and _normalized_identity_text(applied["company_name"]) == normalized_company
                ):
                    possible_repost_of = applied["url"]
                    break
        try:
            eligibility_status, eligibility_reason = evaluate_job_eligibility(job)
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, company_name, source_site, "
                "site, strategy, discovered_at, last_seen_at, platform_job_id, canonical_job_url, "
                "dedupe_status, possible_repost_of, eligibility_status, eligibility_reason, "
                "eligibility_evaluated_at, full_description, application_url, detail_scraped_at, "
                "detail_error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (url, job.get("title"), job.get("salary"), job.get("description"),
                 job.get("location"), job.get("company_name") or job.get("company"),
                 site, site, strategy, now, now, platform_job_id or None,
                 canonical_url or None,
                 "possible_repost" if possible_repost_of else "new_identity",
                 possible_repost_of, eligibility_status, eligibility_reason, now,
                 job.get("full_description"), job.get("application_url"),
                 job.get("detail_scraped_at"), job.get("detail_error")),
            )
            new += 1
        except sqlite3.IntegrityError:
            existing += 1

    conn.commit()
    return new, existing


















def get_jobs_by_stage(conn: sqlite3.Connection | None = None,
                      stage: str = "discovered",
                      min_score: int | None = None,
                      limit: int = 100) -> list[dict]:
    """Fetch jobs filtered by pipeline stage.

    Args:
        conn: Database connection. Uses get_connection() if None.
        stage: One of "discovered", "enriched", "scored", "tailored", "applied".
        min_score: Minimum fit_score filter (only relevant for scored+ stages).
        limit: Maximum number of rows to return.

    Returns:
        List of job dicts.
    """
    if conn is None:
        conn = get_connection()

    from applypilot.eligibility import ELIGIBLE_SQL, refresh_job_eligibility
    refresh_job_eligibility(conn)

    conditions = {
        "discovered": "1=1",
        "pending_detail": "detail_scraped_at IS NULL",
        "enriched": "full_description IS NOT NULL",
        "pending_score": "full_description IS NOT NULL AND fit_score IS NULL",
        "scored": "fit_score IS NOT NULL",
        "pending_tailor": (
            "fit_score >= ? AND full_description IS NOT NULL "
            "AND tailored_resume_path IS NULL AND COALESCE(tailor_attempts, 0) < 5"
        ),
        "tailored": (
            "tailored_resume_path IS NOT NULL AND tailor_status='machine_validated'"
        ),
        "pending_apply": (
            "tailored_resume_path IS NOT NULL AND applied_at IS NULL "
            "AND tailor_status = 'machine_validated' "
            "AND ((cover_letter_path IS NOT NULL AND cover_letter_status = 'human_approved') "
            "OR cover_letter_status = 'not_required')"
        ),
        "applied": "applied_at IS NOT NULL",
    }

    where = conditions.get(stage, "1=1")
    where += f" AND {ELIGIBLE_SQL}"
    params: list = []

    if "?" in where and min_score is not None:
        params.append(min_score)
    elif "?" in where:
        params.append(7)  # default min_score

    if min_score is not None and "fit_score" not in where and stage in ("scored", "tailored", "applied"):
        where += " AND fit_score >= ?"
        params.append(min_score)

    query = f"SELECT * FROM jobs WHERE {where} ORDER BY fit_score DESC NULLS LAST, discovered_at DESC"
    if limit > 0:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()

    # Convert sqlite3.Row objects to dicts
    if rows:
        columns = rows[0].keys()
        return [dict(zip(columns, row)) for row in rows]
    return []
