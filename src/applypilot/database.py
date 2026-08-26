"""ApplyPilot database layer: schema, migrations, stats, and connection helpers.

Single source of truth for the jobs table schema. All columns from every
pipeline stage are created up front so any stage can run independently
without migration ordering issues.
"""

import csv
import hashlib
import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from applypilot.config import DB_PATH

# Thread-local connection storage — each thread gets its own connection
# (required for SQLite thread safety with parallel workers)
_local = threading.local()


def canonicalize_job_url(url: str) -> str:
    """Return a stable listing URL while preserving the platform job identity."""
    if not url:
        return ""
    parsed = urlsplit(url.strip())
    host = parsed.netloc.casefold().removeprefix("www.")
    path = re.sub(r"/+", "/", parsed.path).rstrip("/")
    if host.endswith("linkedin.com"):
        job_id = extract_platform_job_id(url)
        if job_id:
            return f"https://www.linkedin.com/jobs/view/{job_id.split(':', 1)[-1]}"
    tracking_keys = {"fbclid", "gclid", "mc_cid", "mc_eid"}
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in tracking_keys
    ]
    return urlunsplit(
        (
            parsed.scheme.casefold() or "https",
            host,
            path,
            urlencode(query, doseq=True),
            "",
        )
    )


def extract_platform_job_id(url: str) -> str:
    """Extract a stable platform job ID when one is present in a listing URL."""
    if not url:
        return ""
    parsed = urlsplit(url)
    host = parsed.netloc.casefold()
    if "linkedin.com" in host:
        match = re.search(r"/jobs/(?:view/)?(?:[^/?#]*-)?(\d{6,})(?:/|$)", parsed.path)
        if match:
            return f"linkedin:{match.group(1)}"
        query = parse_qs(parsed.query)
        for key in ("currentJobId", "jobId"):
            value = query.get(key, [""])[0]
            if str(value).isdigit():
                return f"linkedin:{value}"
    return ""


def _normalized_identity_text(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _usable_requisition_id(value: object) -> str:
    """Reject ATS display placeholders before using requisitions as identity."""
    raw = str(value or "").strip()
    normalized = _normalized_identity_text(raw)
    placeholders = {
        "n a",
        "na",
        "none",
        "not available",
        "see opening id",
        "see job id",
        "see posting id",
        "tbd",
    }
    return "" if not normalized or normalized in placeholders else raw


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

    if not hasattr(_local, 'connections'):
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
    if hasattr(_local, 'connections'):
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
    """Create the durable, cross-process ledger for one-shot batch authorization."""
    if conn is None:
        conn = get_connection()
    was_in_transaction = conn.in_transaction
    conn.execute("""
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
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_application_batch_consumptions_count
            ON application_batch_consumptions(batch_id, status)
    """)
    if not was_in_transaction:
        conn.commit()


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
    connection = conn or get_connection()
    ensure_application_batch_schema(connection)
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
        now = datetime.now(UTC).isoformat()
        connection.execute(
            "INSERT INTO application_batch_consumptions "
            "(batch_id, job_url, reserved_at, status, updated_at, evidence_json) "
            "VALUES (?, ?, ?, 'reserved', ?, NULL)",
            (batch_id, job_url, now, now),
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
    connection = conn or get_connection()
    ensure_application_batch_schema(connection)
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


def ensure_radar_schema(conn: sqlite3.Connection | None = None) -> None:
    """Create additive tables used by the read-only multi-source radar.

    Radar lineage is deliberately separate from the application-state columns
    in ``jobs``.  Official listings may link to a job row, while social and
    forum observations remain leads until an official listing is verified.
    """
    if conn is None:
        conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS radar_sources (
            source_id       TEXT PRIMARY KEY,
            company_id      TEXT,
            company_name    TEXT,
            source_type     TEXT NOT NULL,
            provider        TEXT,
            access_mode     TEXT,
            base_url        TEXT,
            priority_tier   TEXT,
            active          INTEGER NOT NULL DEFAULT 1,
            config_json     TEXT,
            registered_at   TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS radar_fetch_runs (
            run_id               TEXT PRIMARY KEY,
            source_id            TEXT NOT NULL,
            started_at           TEXT NOT NULL,
            finished_at          TEXT,
            status               TEXT NOT NULL,
            pagination_complete  INTEGER,
            pages_fetched        INTEGER,
            raw_count            INTEGER NOT NULL DEFAULT 0,
            normalized_count     INTEGER NOT NULL DEFAULT 0,
            new_count            INTEGER NOT NULL DEFAULT 0,
            existing_count       INTEGER NOT NULL DEFAULT 0,
            lead_count           INTEGER NOT NULL DEFAULT 0,
            error                TEXT,
            parser_version       TEXT,
            metadata_json        TEXT
        );

        CREATE TABLE IF NOT EXISTS radar_source_observations (
            observation_key     TEXT PRIMARY KEY,
            source_id           TEXT NOT NULL,
            company_id          TEXT,
            external_id         TEXT,
            source_url          TEXT,
            canonical_url       TEXT,
            title               TEXT,
            company_name        TEXT,
            location            TEXT,
            published_at        TEXT,
            first_seen_at       TEXT NOT NULL,
            last_seen_at        TEXT NOT NULL,
            last_run_id         TEXT NOT NULL,
            publisher_name      TEXT,
            publisher_type      TEXT,
            verification_status TEXT NOT NULL,
            content_fingerprint TEXT NOT NULL,
            payload_json        TEXT
        );

        CREATE TABLE IF NOT EXISTS radar_leads (
            lead_id              TEXT PRIMARY KEY,
            observation_key      TEXT NOT NULL UNIQUE,
            status               TEXT NOT NULL,
            company_id           TEXT,
            title                TEXT,
            location             TEXT,
            source_url           TEXT,
            official_job_url     TEXT,
            promoted_job_url     TEXT,
            publisher_type       TEXT,
            verification_status  TEXT NOT NULL,
            reason               TEXT,
            track_tags_json      TEXT,
            first_seen_at        TEXT NOT NULL,
            last_seen_at         TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS radar_job_sources (
            job_url          TEXT NOT NULL,
            observation_key  TEXT NOT NULL,
            source_id        TEXT NOT NULL,
            is_primary       INTEGER NOT NULL DEFAULT 0,
            link_reason      TEXT,
            linked_at        TEXT NOT NULL,
            PRIMARY KEY (job_url, observation_key)
        );

        CREATE TABLE IF NOT EXISTS radar_exclusion_snapshots (
            snapshot_id       TEXT PRIMARY KEY,
            source            TEXT NOT NULL,
            observed_at       TEXT,
            imported_at       TEXT NOT NULL,
            declared_total    INTEGER,
            pages_read        INTEGER,
            imported_count    INTEGER NOT NULL DEFAULT 0,
            updated_count     INTEGER NOT NULL DEFAULT 0,
            skipped_count     INTEGER NOT NULL DEFAULT 0,
            completeness      TEXT NOT NULL,
            metadata_json     TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_radar_runs_source_started
            ON radar_fetch_runs(source_id, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_radar_observations_company
            ON radar_source_observations(company_id, last_seen_at DESC);
        CREATE INDEX IF NOT EXISTS idx_radar_observations_external
            ON radar_source_observations(source_id, external_id);
        CREATE INDEX IF NOT EXISTS idx_radar_leads_status
            ON radar_leads(status, last_seen_at DESC);
        CREATE INDEX IF NOT EXISTS idx_radar_job_sources_source
            ON radar_job_sources(source_id, job_url);
        CREATE INDEX IF NOT EXISTS idx_radar_exclusion_snapshots_imported
            ON radar_exclusion_snapshots(source, imported_at DESC);
    """)
    conn.commit()


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def register_radar_source(conn: sqlite3.Connection, source: dict) -> None:
    """Register or refresh one configured radar source."""
    source_id = str(source.get("source_id") or "").strip()
    if not source_id:
        raise ValueError("radar source requires source_id")
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO radar_sources (
            source_id, company_id, company_name, source_type, provider,
            access_mode, base_url, priority_tier, active, config_json,
            registered_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id) DO UPDATE SET
            company_id = excluded.company_id,
            company_name = excluded.company_name,
            source_type = excluded.source_type,
            provider = excluded.provider,
            access_mode = excluded.access_mode,
            base_url = excluded.base_url,
            priority_tier = excluded.priority_tier,
            active = excluded.active,
            config_json = excluded.config_json,
            updated_at = excluded.updated_at
        """,
        (
            source_id,
            source.get("company_id"),
            source.get("company_name"),
            source.get("source_type", "official_careers"),
            source.get("provider"),
            source.get("access_mode", "public_read"),
            source.get("base_url") or source.get("career_url"),
            source.get("priority_tier") or source.get("cadence"),
            1 if source.get("active", True) else 0,
            _json_text(source),
            now,
            now,
        ),
    )
    conn.commit()


def start_radar_fetch_run(
    conn: sqlite3.Connection,
    source: dict,
    *,
    parser_version: str | None = None,
    metadata: dict | None = None,
) -> str:
    """Start an auditable source run and return its stable run ID."""
    register_radar_source(conn, source)
    run_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO radar_fetch_runs "
        "(run_id, source_id, started_at, status, parser_version, metadata_json) "
        "VALUES (?, ?, ?, 'running', ?, ?)",
        (
            run_id,
            source["source_id"],
            datetime.now(UTC).isoformat(),
            parser_version,
            _json_text(metadata or {}),
        ),
    )
    conn.commit()
    return run_id


def finish_radar_fetch_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    status: str,
    pagination_complete: bool | None = None,
    pages_fetched: int | None = None,
    raw_count: int = 0,
    normalized_count: int = 0,
    new_count: int = 0,
    existing_count: int = 0,
    lead_count: int = 0,
    error: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Finish a run without converting unavailable sources into zero results."""
    if status not in {"complete", "partial", "blocked", "skipped", "failed"}:
        raise ValueError(f"invalid radar run status: {status}")
    conn.execute(
        """
        UPDATE radar_fetch_runs SET
            finished_at = ?, status = ?, pagination_complete = ?,
            pages_fetched = ?, raw_count = ?, normalized_count = ?,
            new_count = ?, existing_count = ?, lead_count = ?, error = ?,
            metadata_json = COALESCE(?, metadata_json)
        WHERE run_id = ?
        """,
        (
            datetime.now(UTC).isoformat(),
            status,
            None if pagination_complete is None else int(pagination_complete),
            pages_fetched,
            raw_count,
            normalized_count,
            new_count,
            existing_count,
            lead_count,
            error,
            _json_text(metadata) if metadata is not None else None,
            run_id,
        ),
    )
    conn.commit()


def record_radar_observation(
    conn: sqlite3.Connection,
    run_id: str,
    observation: dict,
) -> str:
    """Upsert one source observation and return its deterministic key."""
    source_id = str(observation.get("source_id") or "").strip()
    if not source_id:
        raise ValueError("radar observation requires source_id")
    source_url = str(observation.get("source_url") or observation.get("url") or "").strip()
    external_id = str(
        observation.get("external_id") or observation.get("requisition_id") or ""
    ).strip()
    payload = observation.get("payload") or {
        key: value for key, value in observation.items() if key != "payload"
    }
    fingerprint = hashlib.sha256(_json_text(payload).encode("utf-8")).hexdigest()
    identity = external_id or canonicalize_job_url(source_url) or fingerprint
    observation_key = hashlib.sha256(
        f"{source_id}|{identity}".encode()
    ).hexdigest()
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO radar_source_observations (
            observation_key, source_id, company_id, external_id, source_url,
            canonical_url, title, company_name, location, published_at,
            first_seen_at, last_seen_at, last_run_id, publisher_name,
            publisher_type, verification_status, content_fingerprint,
            payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(observation_key) DO UPDATE SET
            source_url = excluded.source_url,
            canonical_url = excluded.canonical_url,
            title = COALESCE(excluded.title, radar_source_observations.title),
            company_name = COALESCE(excluded.company_name, radar_source_observations.company_name),
            location = COALESCE(excluded.location, radar_source_observations.location),
            published_at = COALESCE(excluded.published_at, radar_source_observations.published_at),
            last_seen_at = excluded.last_seen_at,
            last_run_id = excluded.last_run_id,
            publisher_name = COALESCE(excluded.publisher_name, radar_source_observations.publisher_name),
            publisher_type = COALESCE(excluded.publisher_type, radar_source_observations.publisher_type),
            verification_status = excluded.verification_status,
            content_fingerprint = excluded.content_fingerprint,
            payload_json = excluded.payload_json
        """,
        (
            observation_key,
            source_id,
            observation.get("company_id"),
            external_id or None,
            source_url or None,
            observation.get("canonical_url") or canonicalize_job_url(source_url) or None,
            observation.get("title"),
            observation.get("company_name") or observation.get("company"),
            observation.get("location"),
            observation.get("published_at"),
            now,
            now,
            run_id,
            observation.get("publisher_name"),
            observation.get("publisher_type"),
            observation.get("verification_status", "unverified"),
            fingerprint,
            _json_text(payload),
        ),
    )
    conn.commit()
    return observation_key


def upsert_radar_lead(
    conn: sqlite3.Connection,
    observation_key: str,
    lead: dict,
) -> str:
    """Create or refresh an unverified opportunity lead."""
    lead_id = str(lead.get("lead_id") or f"lead:{observation_key[:24]}")
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO radar_leads (
            lead_id, observation_key, status, company_id, title, location,
            source_url, official_job_url, promoted_job_url, publisher_type,
            verification_status, reason, track_tags_json, first_seen_at,
            last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(observation_key) DO UPDATE SET
            status = excluded.status,
            company_id = COALESCE(excluded.company_id, radar_leads.company_id),
            title = COALESCE(excluded.title, radar_leads.title),
            location = COALESCE(excluded.location, radar_leads.location),
            source_url = COALESCE(excluded.source_url, radar_leads.source_url),
            official_job_url = COALESCE(excluded.official_job_url, radar_leads.official_job_url),
            promoted_job_url = COALESCE(excluded.promoted_job_url, radar_leads.promoted_job_url),
            publisher_type = COALESCE(excluded.publisher_type, radar_leads.publisher_type),
            verification_status = excluded.verification_status,
            reason = excluded.reason,
            track_tags_json = excluded.track_tags_json,
            last_seen_at = excluded.last_seen_at
        """,
        (
            lead_id,
            observation_key,
            lead.get("status", "new"),
            lead.get("company_id"),
            lead.get("title"),
            lead.get("location"),
            lead.get("source_url"),
            lead.get("official_job_url"),
            lead.get("promoted_job_url"),
            lead.get("publisher_type"),
            lead.get("verification_status", "unverified"),
            lead.get("reason"),
            _json_text(lead.get("track_tags", [])),
            now,
            now,
        ),
    )
    conn.commit()
    return lead_id


def link_radar_job_source(
    conn: sqlite3.Connection,
    job_url: str,
    observation_key: str,
    source_id: str,
    *,
    is_primary: bool = False,
    reason: str = "verified_official",
) -> None:
    """Link a normalized job to one retained source observation."""
    conn.execute(
        """
        INSERT INTO radar_job_sources (
            job_url, observation_key, source_id, is_primary, link_reason, linked_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_url, observation_key) DO UPDATE SET
            is_primary = MAX(radar_job_sources.is_primary, excluded.is_primary),
            link_reason = excluded.link_reason
        """,
        (
            job_url,
            observation_key,
            source_id,
            int(is_primary),
            reason,
            datetime.now(UTC).isoformat(),
        ),
    )
    conn.commit()


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


def record_submission_observation(
    url: str,
    observation: dict,
    conn: sqlite3.Connection | None = None,
) -> str | None:
    """Store a thin browser observation and update status without creating a gate.

    A visible receipt or platform ``Applied`` marker is strong enough to update
    the local record to ``applied``. A final click without confirmation becomes
    ``submission_uncertain``. Other observations are retained as context while
    leaving the current status unchanged. None of these paths sets a retry block.
    """
    if conn is None:
        conn = get_connection()
    row = conn.execute(
        "SELECT apply_status FROM jobs WHERE url = ?", (url,)
    ).fetchone()
    if row is None:
        return None

    cleaned = {
        "submit_clicked": bool(observation.get("submit_clicked", False)),
        "receipt_visible": bool(observation.get("receipt_visible", False)),
        "applied_badge_visible": bool(observation.get("applied_badge_visible", False)),
        "captcha_visible": bool(observation.get("captcha_visible", False)),
        "page_url": str(observation.get("page_url", "")).strip()[:1000],
        "note": str(observation.get("note", "")).strip()[:500],
    }
    now = datetime.now(UTC).isoformat()
    observed_status = row["apply_status"]
    if cleaned["receipt_visible"] or cleaned["applied_badge_visible"]:
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
                            apply_retry_blocked = 0, apply_retry_reason = NULL,
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


_STRONG_SUBMISSION_RECEIPT = re.compile(
    r"application (?:was |has been )?(?:submitted|received)|"
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
    evidence: dict,
    conn: sqlite3.Connection | None = None,
) -> dict[str, object]:
    """Idempotently upgrade an uncertain submission from a durable receipt.

    The ingestion seam accepts only a compact evidence envelope, never an email
    body or verification code. The caller must map the receipt to one exact job
    URL and provide matching company and role identity so a confirmation for a
    different application cannot update this row.
    """
    if conn is None:
        conn = get_connection()
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
    if row["apply_status"] not in {"applied", "submission_uncertain"}:
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
    now = datetime.now(UTC).isoformat()
    if row["apply_status"] == "applied":
        try:
            prior_observation = json.loads(row["submission_observation_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            prior_observation = {}
        if (
            isinstance(prior_observation, dict)
            and prior_observation.get("source") == source
            and prior_observation.get("receipt_id") == receipt_id
        ):
            return {"status": "applied", "job_url": job_url, "changed": False}
        conn.execute(
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
        conn.commit()
        return {
            "status": "applied",
            "job_url": job_url,
            "changed": True,
            "source": source,
        }

    conn.execute(
        """
        UPDATE jobs SET apply_status = 'applied', applied_at = COALESCE(applied_at, ?),
                        apply_error = NULL, agent_id = NULL,
                        apply_retry_blocked = 0, apply_retry_reason = NULL,
                        verification_confidence = 'durable_receipt_reconciled',
                        application_evidence = ?, application_recorded_at = ?,
                        submission_observation_json = ?, submission_observed_at = ?
        WHERE url = ? AND apply_status = 'submission_uncertain'
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
    conn.commit()
    return {"status": "applied", "job_url": job_url, "changed": True, "source": source}


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


def get_stats(conn: sqlite3.Connection | None = None) -> dict:
    """Return job counts by pipeline stage.

    Provides a snapshot of how many jobs are at each stage, useful for
    dashboard display and pipeline progress tracking.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        Dictionary with keys:
            total, by_site, pending_detail, with_description,
            scored, unscored, tailored, untailored_eligible,
            with_cover_letter, applied, score_distribution
    """
    if conn is None:
        conn = get_connection()

    from applypilot.eligibility import ELIGIBLE_SQL, refresh_job_eligibility
    refresh_job_eligibility(conn)

    stats: dict = {}

    # Total jobs
    stats["total"] = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE {ELIGIBLE_SQL}"
    ).fetchone()[0]
    stats["excluded_ineligible"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE eligibility_status = 'ineligible'"
    ).fetchone()[0]

    # By site breakdown
    rows = conn.execute(
        f"SELECT COALESCE(source_site, site), COUNT(*) as cnt FROM jobs WHERE {ELIGIBLE_SQL} "
        "GROUP BY COALESCE(source_site, site) ORDER BY cnt DESC"
    ).fetchall()
    stats["by_site"] = [(row[0], row[1]) for row in rows]

    # Enrichment stage
    stats["pending_detail"] = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL AND {ELIGIBLE_SQL}"
    ).fetchone()[0]

    stats["with_description"] = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL AND {ELIGIBLE_SQL}"
    ).fetchone()[0]

    stats["detail_errors"] = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE detail_error IS NOT NULL AND {ELIGIBLE_SQL}"
    ).fetchone()[0]

    # Scoring stage
    stats["scored"] = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL AND {ELIGIBLE_SQL}"
    ).fetchone()[0]

    stats["unscored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        f"WHERE full_description IS NOT NULL AND fit_score IS NULL AND {ELIGIBLE_SQL}"
    ).fetchone()[0]

    # Score distribution
    dist_rows = conn.execute(
        "SELECT fit_score, COUNT(*) as cnt FROM jobs "
        f"WHERE fit_score IS NOT NULL AND {ELIGIBLE_SQL} "
        "GROUP BY fit_score ORDER BY fit_score DESC"
    ).fetchall()
    stats["score_distribution"] = [(row[0], row[1]) for row in dist_rows]

    # Tailoring stage
    stats["tailored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL "
        f"AND tailor_status='machine_validated' AND {ELIGIBLE_SQL}"
    ).fetchone()[0]

    stats["untailored_eligible"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE fit_score >= 7 AND full_description IS NOT NULL "
        f"AND tailored_resume_path IS NULL AND {ELIGIBLE_SQL}"
    ).fetchone()[0]

    stats["tailor_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(tailor_attempts, 0) >= 5 "
        f"AND tailored_resume_path IS NULL AND {ELIGIBLE_SQL}"
    ).fetchone()[0]

    # Cover letter stage
    stats["with_cover_letter"] = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE cover_letter_path IS NOT NULL AND {ELIGIBLE_SQL}"
    ).fetchone()[0]

    stats["cover_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(cover_attempts, 0) >= 5 "
        f"AND (cover_letter_path IS NULL OR cover_letter_path = '') AND {ELIGIBLE_SQL}"
    ).fetchone()[0]

    # Application stage
    stats["applied"] = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL AND {ELIGIBLE_SQL}"
    ).fetchone()[0]

    stats["apply_errors"] = conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE apply_error IS NOT NULL AND {ELIGIBLE_SQL}"
    ).fetchone()[0]

    stats["ready_to_apply"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE tailored_resume_path IS NOT NULL "
        "AND tailor_status = 'machine_validated' "
        "AND ((cover_letter_path IS NOT NULL AND cover_letter_status = 'human_approved') "
        "OR cover_letter_status = 'not_required') "
        "AND applied_at IS NULL "
        f"AND {ELIGIBLE_SQL}"
    ).fetchone()[0]

    return stats


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
            "SELECT url FROM jobs WHERE url = ? LIMIT 1", (url,)
        ).fetchone()
        if not duplicate and platform_job_id:
            duplicate = conn.execute(
                "SELECT url FROM jobs WHERE platform_job_id = ? LIMIT 1",
                (platform_job_id,),
            ).fetchone()
        if not duplicate and canonical_url:
            duplicate = conn.execute(
                "SELECT url FROM jobs WHERE canonical_job_url = ? LIMIT 1",
                (canonical_url,),
            ).fetchone()
        if duplicate:
            conn.execute(
                """
                UPDATE jobs SET
                    last_seen_at = ?,
                    title = COALESCE(NULLIF(title, ''), ?),
                    company_name = COALESCE(NULLIF(company_name, ''), ?),
                    location = COALESCE(NULLIF(location, ''), ?),
                    salary = COALESCE(NULLIF(salary, ''), ?),
                    description = CASE
                        WHEN LENGTH(COALESCE(description, '')) < LENGTH(COALESCE(?, ''))
                        THEN ? ELSE description END,
                    full_description = CASE
                        WHEN LENGTH(COALESCE(full_description, '')) < LENGTH(COALESCE(?, ''))
                        THEN ? ELSE full_description END,
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
                    job.get("description"),
                    job.get("description"),
                    job.get("full_description"),
                    job.get("full_description"),
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


def ingest_radar_official_jobs(
    conn: sqlite3.Connection,
    run_id: str,
    source: dict,
    jobs: list[dict],
) -> dict:
    """Persist verified official jobs plus their source lineage."""
    source_id = str(source.get("source_id") or "").strip()
    if not source_id:
        raise ValueError("official radar ingest requires source_id")
    provider = str(source.get("provider") or "official")
    company_id = str(source.get("company_id") or "")
    new_count = 0
    existing_count = 0
    linked_count = 0

    for raw_job in jobs:
        job = dict(raw_job)
        if job.get("verification_status") not in {"verified_official", "official_open"}:
            raise ValueError("only verified official jobs may enter the jobs table")
        requisition_id = _usable_requisition_id(job.get("requisition_id"))
        external_id = (
            _usable_requisition_id(job.get("external_id"))
            or requisition_id
            or _usable_requisition_id(job.get("job_id"))
        )
        if external_id and not job.get("platform_job_id"):
            company_identity = _normalized_identity_text(
                company_id or job.get("company_id") or job.get("company_name")
            ).replace(" ", "-")
            if requisition_id:
                job["platform_job_id"] = (
                    f"radar:{company_identity}:req:{_normalized_identity_text(requisition_id)}"
                )
            else:
                job["platform_job_id"] = f"{provider}:{company_identity}:{external_id}"
        job.setdefault("application_url", job.get("url"))
        job.setdefault("canonical_job_url", job.get("canonical_url"))
        if job.get("full_description") and not job.get("detail_scraped_at"):
            job["detail_scraped_at"] = datetime.now(UTC).isoformat()

        added, existed = store_jobs(
            conn,
            [job],
            site=source_id,
            strategy=f"radar_{provider}",
        )
        new_count += added
        existing_count += existed

        identity_url = job.get("application_url") or job.get("url") or ""
        platform_job_id = job.get("platform_job_id")
        canonical_url = job.get("canonical_job_url") or canonicalize_job_url(identity_url)
        row = None
        if platform_job_id:
            row = conn.execute(
                "SELECT url FROM jobs WHERE platform_job_id = ? LIMIT 1",
                (platform_job_id,),
            ).fetchone()
        if row is None and canonical_url:
            row = conn.execute(
                "SELECT url FROM jobs WHERE canonical_job_url = ? LIMIT 1",
                (canonical_url,),
            ).fetchone()
        if row is None:
            row = conn.execute("SELECT url FROM jobs WHERE url = ?", (job.get("url"),)).fetchone()
        if row is None:
            continue

        observation = {
            **job,
            "source_id": source_id,
            "company_id": company_id or job.get("company_id"),
            "source_url": job.get("url") or job.get("source_url"),
            "canonical_url": canonical_url,
            "external_id": external_id,
            "publisher_type": "official_company",
            "verification_status": "verified_official",
            "payload": raw_job,
        }
        observation_key = record_radar_observation(conn, run_id, observation)
        link_radar_job_source(
            conn,
            row["url"],
            observation_key,
            source_id,
            is_primary=True,
            reason="verified_official",
        )
        linked_count += 1

    return {
        "new": new_count,
        "existing": existing_count,
        "linked": linked_count,
    }


def ingest_radar_leads(
    conn: sqlite3.Connection,
    run_id: str,
    source: dict,
    leads: list[dict],
) -> dict:
    """Persist social/forum items as leads without creating job rows."""
    source_id = str(source.get("source_id") or "").strip()
    if not source_id:
        raise ValueError("lead ingest requires source_id")
    inserted = 0
    for raw_lead in leads:
        lead = dict(raw_lead)
        observation = {
            **lead,
            "source_id": source_id,
            "source_url": lead.get("source_url") or lead.get("url"),
            "publisher_type": lead.get("publisher_type", "unknown"),
            "verification_status": lead.get("verification_status", "unverified"),
            "payload": raw_lead,
        }
        observation_key = record_radar_observation(conn, run_id, observation)
        upsert_radar_lead(conn, observation_key, lead)
        inserted += 1
    return {"leads": inserted}


def reconcile_radar_leads(
    conn: sqlite3.Connection,
    *,
    official_run_ids: Iterable[str] = (),
    max_age_hours: int = 24,
) -> dict:
    """Promote leads only against exact URLs observed in fresh official runs.

    No fuzzy title/company match is permitted. The social observation remains
    retained as secondary lineage, while the official listing remains the
    authoritative job row.  Callers must supply the just-finished official run
    IDs; an earlier database row alone is not evidence that a role remains
    open.  This intentionally means a lead imported after collection waits for
    the next official refresh.
    """
    run_ids = tuple(
        dict.fromkeys(
            str(item).strip() for item in official_run_ids if str(item).strip()
        )
    )
    if not run_ids:
        return {"promoted": 0}
    if max_age_hours < 1:
        raise ValueError("max_age_hours must be at least 1")
    fresh_since = (datetime.now(UTC) - timedelta(hours=max_age_hours)).isoformat()
    placeholders = ", ".join("?" for _ in run_ids)
    rows = conn.execute(
        """
        SELECT l.lead_id, l.observation_key, l.official_job_url, o.source_id
        FROM radar_leads l
        JOIN radar_source_observations o ON o.observation_key = l.observation_key
        WHERE l.status IN ('new', 'triaged', 'awaiting_official')
          AND l.official_job_url IS NOT NULL
          AND TRIM(l.official_job_url) != ''
        """
    ).fetchall()
    promoted = 0
    for lead in rows:
        official_url = str(lead["official_job_url"]).strip()
        job = conn.execute(
            f"""
            SELECT DISTINCT j.url
            FROM jobs j
            JOIN radar_job_sources l ON l.job_url = j.url
            JOIN radar_source_observations o ON o.observation_key = l.observation_key
            JOIN radar_fetch_runs r ON r.run_id = o.last_run_id
            WHERE o.verification_status = 'verified_official'
              AND o.last_run_id IN ({placeholders})
              AND r.source_id = o.source_id
              AND r.status IN ('complete', 'partial')
              AND r.finished_at IS NOT NULL
              AND r.finished_at >= ?
              AND o.last_seen_at >= r.started_at
              AND (j.url = ? OR j.application_url = ? OR j.canonical_job_url = ?)
            LIMIT 1
            """,
            (*run_ids, fresh_since, official_url, official_url, official_url),
        ).fetchone()
        if job is None:
            continue
        conn.execute(
            """
            UPDATE radar_leads
            SET status = 'promoted', promoted_job_url = ?,
                verification_status = 'official_target_open',
                reason = 'exact official job URL verified',
                last_seen_at = ?
            WHERE lead_id = ?
            """,
            (job["url"], datetime.now(UTC).isoformat(), lead["lead_id"]),
        )
        conn.execute(
            """
            UPDATE radar_source_observations
            SET verification_status = 'official_target_open'
            WHERE observation_key = ?
            """,
            (lead["observation_key"],),
        )
        link_radar_job_source(
            conn,
            job["url"],
            lead["observation_key"],
            lead["source_id"],
            is_primary=False,
            reason="lead_verified_against_exact_official_url",
        )
        promoted += 1
    conn.commit()
    return {"promoted": promoted}


def get_applied_exclusion_set(
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """Return auditable applied identities used only to suppress radar output."""
    if conn is None:
        conn = get_connection()
    rows = conn.execute(
        """
        SELECT url, application_url, canonical_job_url, platform_job_id,
               title, company_name, location, apply_status, applied_at,
               application_evidence, source_site
        FROM jobs
        WHERE apply_status = 'applied' OR applied_at IS NOT NULL
        ORDER BY COALESCE(applied_at, application_recorded_at, discovered_at) DESC
        """
    ).fetchall()
    exclusions = []
    for row in rows:
        item = dict(row)
        evidence = str(item.get("application_evidence") or "").casefold()
        platform_id = str(item.get("platform_job_id") or "").casefold()
        source_site = str(item.get("source_site") or "").casefold()
        item["exclusion_source"] = (
            "linkedin_applied"
            if "linkedin" in evidence
            or platform_id.startswith("linkedin:")
            or source_site == "linkedin"
            else "local_application"
        )
        exclusions.append(item)
    return exclusions


def get_latest_applied_exclusion_snapshot(
    conn: sqlite3.Connection | None = None,
    *,
    snapshot_id: str | None = None,
    max_age_hours: int = 6,
) -> dict:
    """Return integrity and freshness evidence for one Applied import."""
    if conn is None:
        conn = get_connection()
    ensure_radar_schema(conn)
    if snapshot_id:
        row = conn.execute(
            """
            SELECT * FROM radar_exclusion_snapshots
            WHERE source LIKE 'linkedin%' AND snapshot_id = ?
            LIMIT 1
            """,
            (snapshot_id,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT * FROM radar_exclusion_snapshots
            WHERE source LIKE 'linkedin%'
            ORDER BY imported_at DESC
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        return {
            "completeness": "missing",
            "fresh": False,
            "integrity_valid": False,
            "requested_snapshot_id": snapshot_id,
        }
    snapshot = dict(row)
    declared_total = snapshot.get("declared_total")
    imported_count = int(snapshot.get("imported_count") or 0)
    updated_count = int(snapshot.get("updated_count") or 0)
    skipped_count = int(snapshot.get("skipped_count") or 0)
    pages_read = snapshot.get("pages_read")
    observed_at_valid = False
    try:
        observed_at = datetime.fromisoformat(str(snapshot.get("observed_at") or ""))
        observed_at_valid = observed_at.tzinfo is not None
        age = datetime.now(UTC) - observed_at.astimezone(UTC)
        snapshot["fresh"] = (
            -timedelta(minutes=5) <= age <= timedelta(hours=max_age_hours)
        )
    except ValueError:
        snapshot["fresh"] = False
    snapshot["integrity_valid"] = (
        snapshot.get("completeness") == "complete"
        and observed_at_valid
        and isinstance(declared_total, int)
        and declared_total >= 0
        and isinstance(pages_read, int)
        and pages_read > 0
        and skipped_count == 0
        and imported_count + updated_count == declared_total
    )
    return snapshot


def _location_scope(value: object) -> str:
    normalized = _normalized_identity_text(value)
    tokens = set(normalized.split())
    if "singapore" in tokens or "sg" in tokens:
        return "singapore"
    return normalized


def _find_applied_exclusion(
    job: dict,
    applied_exclusions: list[dict],
) -> tuple[dict, str] | None:
    """Match exact identities first, then a conservative cross-site fallback."""
    job_platform_id = str(job.get("platform_job_id") or "").strip()
    job_urls = {
        canonical
        for value in (
            job.get("url"),
            job.get("application_url"),
            job.get("canonical_job_url"),
        )
        if (canonical := canonicalize_job_url(str(value or "")))
    }
    for applied in applied_exclusions:
        applied_platform_id = str(applied.get("platform_job_id") or "").strip()
        if job_platform_id and applied_platform_id == job_platform_id:
            return applied, "exact_platform_job_id"
        applied_urls = {
            canonical
            for value in (
                applied.get("url"),
                applied.get("application_url"),
                applied.get("canonical_job_url"),
            )
            if (canonical := canonicalize_job_url(str(value or "")))
        }
        if job_urls & applied_urls:
            return applied, "exact_canonical_url"

    company = _normalized_identity_text(job.get("company"))
    title = _normalized_identity_text(job.get("title"))
    location = _location_scope(job.get("location"))
    if not company or not title or not location:
        return None
    for applied in applied_exclusions:
        if (
            _normalized_identity_text(applied.get("company_name")) == company
            and _normalized_identity_text(applied.get("title")) == title
            and _location_scope(applied.get("location")) == location
        ):
            return applied, "company_title_location"
    return None


def get_radar_daily_snapshot(
    conn: sqlite3.Connection | None = None,
    *,
    since: str | None = None,
    expected_sources: list[dict] | None = None,
    applied_snapshot_id: str | None = None,
) -> dict:
    """Return serialisable source-run, verified-job, and lead report data."""
    if conn is None:
        conn = get_connection()
    ensure_radar_schema(conn)
    applied_snapshot = get_latest_applied_exclusion_snapshot(
        conn,
        snapshot_id=applied_snapshot_id,
    )
    run_where = "WHERE started_at >= ?" if since else ""
    run_params = (since,) if since else ()
    run_rows = conn.execute(
        f"""
        SELECT r.*, s.source_type
        FROM radar_fetch_runs r
        LEFT JOIN radar_sources s ON s.source_id = r.source_id
        {run_where}
        ORDER BY r.started_at DESC
        """,
        run_params,
    ).fetchall()
    runs = []
    seen_sources: set[str] = set()
    latest_observation_run_ids: list[str] = []
    for row in run_rows:
        if row["source_id"] in seen_sources:
            continue
        seen_sources.add(row["source_id"])
        if row["finished_at"] and row["status"] in {"complete", "partial"}:
            latest_observation_run_ids.append(row["run_id"])
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            metadata = {}
        status = row["status"]
        error = row["error"]
        pagination_complete = None if row["pagination_complete"] is None else bool(row["pagination_complete"])
        if status in {"running", "failed"}:
            status = "partial"
            error = error or "run unfinished"
        if status == "complete" and pagination_complete is False:
            status = "partial"
            error = error or "pagination incomplete"
        runs.append(
            {
                "source": row["source_id"],
                "kind": row["source_type"]
                or ("official_careers" if row["source_id"].startswith("official:") else "social_lead"),
                "status": status,
                "count": metadata.get("accepted_count", row["normalized_count"]),
                "raw_count": row["raw_count"],
                "filtered": metadata.get("filtered_count", 0),
                "location_title_filtered": metadata.get("location_title_filtered_count", 0),
                "track_filtered": metadata.get("track_filtered_count", 0),
                "pages": row["pages_fetched"],
                "pagination_complete": pagination_complete,
                "error": error,
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
            }
        )
    for source in expected_sources or []:
        source_id = str(source.get("source_id") or "").strip()
        if not source_id or source_id in seen_sources:
            continue
        runs.append(
            {
                "source": source_id,
                "kind": source.get("source_type", "official_careers"),
                "status": "skipped",
                "count": 0,
                "raw_count": 0,
                "filtered": 0,
                "location_title_filtered": 0,
                "track_filtered": 0,
                "pages": 0,
                "pagination_complete": False,
                "error": "no run recorded in report window",
                "started_at": None,
                "finished_at": None,
            }
        )

    job_rows = []
    if latest_observation_run_ids:
        placeholders = ", ".join("?" for _ in latest_observation_run_ids)
        job_rows = conn.execute(
            f"""
            SELECT j.url, j.title, j.company_name, j.location, j.full_description,
                   j.description, j.eligibility_status, j.eligibility_reason,
                   j.application_url, j.canonical_job_url, j.platform_job_id,
                   o.source_id, o.source_url, o.external_id, o.published_at,
                   o.verification_status, o.payload_json
            FROM radar_job_sources l
            JOIN radar_source_observations o ON o.observation_key = l.observation_key
            JOIN jobs j ON j.url = l.job_url
            WHERE o.verification_status = 'verified_official'
              AND o.last_run_id IN ({placeholders})
            ORDER BY COALESCE(o.published_at, o.last_seen_at) DESC
            """,
            tuple(latest_observation_run_ids),
        ).fetchall()
    observations_by_job: dict[str, dict] = {}
    for row in job_rows:
        payload = json.loads(row["payload_json"] or "{}")
        observation = observations_by_job.get(row["url"])
        if observation is None:
            observation = {
                "source": row["source_id"],
                "kind": "official_ats",
                "url": row["url"],
                "official_job_url": row["url"],
                "application_url": row["application_url"],
                "canonical_job_url": row["canonical_job_url"],
                "platform_job_id": row["platform_job_id"],
                "source_url": row["source_url"],
                "company": row["company_name"],
                "title": row["title"],
                "location": row["location"],
                "employment_type": payload.get("employment_type"),
                "requisition_id": row["external_id"],
                "published_at": row["published_at"],
                "verification_status": row["verification_status"],
                "subtracks": payload.get("subtracks") or payload.get("track_tags", []),
                "eligibility_status": row["eligibility_status"],
                "eligibility_reason": row["eligibility_reason"],
                "source_ids": [],
                "source_count": 0,
            }
            observations_by_job[row["url"]] = observation
        if row["source_id"] not in observation["source_ids"]:
            observation["source_ids"].append(row["source_id"])
            observation["source_count"] += 1
    applied_set = get_applied_exclusion_set(conn)
    observations = []
    applied_exclusions = []
    for observation in observations_by_job.values():
        matched = _find_applied_exclusion(observation, applied_set)
        if matched is None:
            observations.append(observation)
            continue
        applied, reason = matched
        applied_exclusions.append(
            {
                "title": observation.get("title"),
                "company": observation.get("company"),
                "location": observation.get("location"),
                "official_job_url": observation.get("official_job_url"),
                "matched_applied_url": applied.get("url"),
                "exclusion_source": applied.get("exclusion_source"),
                "reason": reason,
            }
        )

    lead_where = "WHERE last_seen_at >= ?" if since else ""
    lead_params = (since,) if since else ()
    lead_rows = conn.execute(
        f"""
        SELECT l.*, o.source_id, o.payload_json
        FROM radar_leads l
        JOIN radar_source_observations o ON o.observation_key = l.observation_key
        {lead_where.replace('last_seen_at', 'l.last_seen_at')}
        ORDER BY l.last_seen_at DESC
        """,
        lead_params,
    ).fetchall()
    leads = []
    for row in lead_rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            payload = {}
        leads.append(
            {
                "source": row["source_id"],
                "kind": "linkedin_post" if "linkedin.com" in (row["source_url"] or "") else "forum",
                "url": row["source_url"],
                "company": row["company_id"],
                "title": row["title"],
                "location": row["location"],
                "status": row["status"],
                "verification_status": row["verification_status"],
                "reason": row["reason"],
                "official_job_url": row["official_job_url"],
                "promoted_job_url": row["promoted_job_url"],
                "subtracks": payload.get("subtracks") or payload.get("track_tags", []),
            }
        )
    return {
        "source_runs": runs,
        "observations": observations,
        "leads": leads,
        "applied_exclusions": applied_exclusions,
        "applied_snapshot": applied_snapshot,
    }


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
