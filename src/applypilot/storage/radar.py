"""Radar source lineage, lead reconciliation, and daily snapshots."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

from applypilot.storage import job_identity

canonicalize_job_url = job_identity.canonicalize_job_url
_normalized_identity_text = job_identity._normalized_identity_text
_usable_requisition_id = job_identity._usable_requisition_id

_LEGACY_REQUISITION_ID_PROVIDERS = (
    "ashby",
    "greenhouse",
    "jobposting-jsonld",
    "jobposting_jsonld",
    "lever",
    "rss",
    "smartrecruiters",
    "workable",
)


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _existing_requisition_platform_id(
    conn: sqlite3.Connection,
    company_identity: str,
    requisition_id: str,
) -> str:
    """Return an existing identity when an official source provider changes."""
    if not company_identity or not requisition_id:
        return ""
    normalized_requisition = _normalized_identity_text(requisition_id)
    canonical_id = f"radar:{company_identity}:req:{normalized_requisition}"
    aliases = [canonical_id]
    for provider in _LEGACY_REQUISITION_ID_PROVIDERS:
        aliases.append(f"{provider}:{company_identity}:{requisition_id}")
        aliases.append(f"{provider}:{company_identity}:{normalized_requisition}")
    folded_aliases = list(dict.fromkeys(alias.casefold() for alias in aliases))
    placeholders = ", ".join("?" for _ in folded_aliases)
    row = conn.execute(
        f"""
        SELECT platform_job_id
        FROM jobs
        WHERE lower(platform_job_id) IN ({placeholders})
        ORDER BY CASE WHEN lower(platform_job_id) = ? THEN 0 ELSE 1 END, rowid
        LIMIT 1
        """,
        (*folded_aliases, canonical_id.casefold()),
    ).fetchone()
    return str(row["platform_job_id"] or "") if row is not None else ""


def ensure_radar_schema(conn: sqlite3.Connection) -> None:
    """Create additive tables used by the read-only multi-source radar.

    Radar lineage is deliberately separate from the application-state columns
    in ``jobs``.  Official listings may link to a job row, while social and
    forum observations remain leads until an official listing is verified.
    """
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
            source_type          TEXT,
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
            source_type         TEXT,
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

        CREATE TABLE IF NOT EXISTS radar_company_seeds (
            company_key          TEXT PRIMARY KEY,
            company_name         TEXT NOT NULL,
            official_domain      TEXT,
            official_url         TEXT,
            careers_url          TEXT,
            location             TEXT,
            sectors_json         TEXT,
            track_tags_json      TEXT,
            status               TEXT NOT NULL,
            verification_status  TEXT NOT NULL,
            first_seen_at        TEXT NOT NULL,
            last_seen_at         TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS radar_company_seed_sources (
            company_key      TEXT NOT NULL,
            source_id        TEXT NOT NULL,
            source_url       TEXT NOT NULL,
            last_run_id      TEXT NOT NULL,
            payload_json     TEXT,
            first_seen_at    TEXT NOT NULL,
            last_seen_at     TEXT NOT NULL,
            PRIMARY KEY (company_key, source_id, source_url)
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
        CREATE INDEX IF NOT EXISTS idx_radar_company_seeds_status
            ON radar_company_seeds(status, last_seen_at DESC);
        CREATE INDEX IF NOT EXISTS idx_radar_company_seed_sources_source
            ON radar_company_seed_sources(source_id, last_seen_at DESC);
        CREATE INDEX IF NOT EXISTS idx_radar_job_sources_source
            ON radar_job_sources(source_id, job_url);
        CREATE INDEX IF NOT EXISTS idx_radar_exclusion_snapshots_imported
            ON radar_exclusion_snapshots(source, imported_at DESC);
    """)
    additive_columns = (
        ("radar_fetch_runs", "source_type"),
        ("radar_source_observations", "source_type"),
    )
    for table_name, column_name in additive_columns:
        existing_columns = {
            row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")
        }
        if column_name not in existing_columns:
            conn.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} TEXT"
            )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_radar_runs_source_type_started "
        "ON radar_fetch_runs(source_id, source_type, started_at DESC)"
    )
    conn.commit()


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
        "(run_id, source_id, source_type, started_at, status, parser_version, "
        "metadata_json) VALUES (?, ?, ?, ?, 'running', ?, ?)",
        (
            run_id,
            source["source_id"],
            source.get("source_type"),
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
            observation_key, source_id, source_type, company_id, external_id,
            source_url, canonical_url, title, company_name, location,
            published_at, first_seen_at, last_seen_at, last_run_id,
            publisher_name, publisher_type, verification_status,
            content_fingerprint, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(observation_key) DO UPDATE SET
            source_type = COALESCE(
                excluded.source_type,
                radar_source_observations.source_type
            ),
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
            observation.get("source_type"),
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
            status = CASE
                WHEN radar_leads.status IN ('promoted', 'rejected', 'expired')
                    THEN radar_leads.status
                ELSE excluded.status
            END,
            company_id = COALESCE(excluded.company_id, radar_leads.company_id),
            title = COALESCE(excluded.title, radar_leads.title),
            location = COALESCE(excluded.location, radar_leads.location),
            source_url = COALESCE(excluded.source_url, radar_leads.source_url),
            official_job_url = COALESCE(excluded.official_job_url, radar_leads.official_job_url),
            promoted_job_url = COALESCE(radar_leads.promoted_job_url, excluded.promoted_job_url),
            publisher_type = COALESCE(excluded.publisher_type, radar_leads.publisher_type),
            verification_status = CASE
                WHEN radar_leads.status IN ('promoted', 'rejected', 'expired')
                    THEN radar_leads.verification_status
                ELSE excluded.verification_status
            END,
            reason = CASE
                WHEN radar_leads.status IN ('promoted', 'rejected', 'expired')
                    THEN radar_leads.reason
                ELSE excluded.reason
            END,
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
            _json_text(lead.get("track_tags") or lead.get("subtracks", [])),
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


def ingest_radar_official_jobs(
    conn: sqlite3.Connection,
    run_id: str,
    source: dict,
    jobs: list[dict],
    *,
    store_jobs_fn: Callable[..., tuple[int, int]],
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
                canonical_platform_id = (
                    f"radar:{company_identity}:req:{_normalized_identity_text(requisition_id)}"
                )
                job["platform_job_id"] = (
                    _existing_requisition_platform_id(
                        conn,
                        company_identity,
                        requisition_id,
                    )
                    or canonical_platform_id
                )
            else:
                job["platform_job_id"] = f"{provider}:{company_identity}:{external_id}"
        job.setdefault("application_url", job.get("url"))
        job.setdefault("canonical_job_url", job.get("canonical_url"))
        if job.get("full_description") and not job.get("detail_scraped_at"):
            job["detail_scraped_at"] = datetime.now(UTC).isoformat()

        added, existed = store_jobs_fn(
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
            "source_type": source.get("source_type"),
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
            "source_type": source.get("source_type"),
            "source_url": lead.get("source_url") or lead.get("url"),
            "publisher_type": lead.get("publisher_type", "unknown"),
            "verification_status": lead.get("verification_status", "unverified"),
            "payload": raw_lead,
        }
        observation_key = record_radar_observation(conn, run_id, observation)
        upsert_radar_lead(conn, observation_key, lead)
        inserted += 1
    return {"leads": inserted}


def _company_seed_key(seed: dict) -> str:
    supplied = str(seed.get("company_key") or "").strip()
    if supplied:
        return supplied
    official_domain = str(seed.get("official_domain") or "").strip().casefold()
    if official_domain:
        return f"domain:{official_domain}"
    company_name = _normalized_identity_text(seed.get("company_name")).replace(" ", "-")
    if not company_name:
        raise ValueError("company seed requires company_name")
    return f"name:{company_name}"


def ingest_radar_company_seeds(
    conn: sqlite3.Connection,
    run_id: str,
    source: dict,
    seeds: list[dict],
) -> dict:
    """Persist ecosystem company candidates without creating jobs or leads."""
    source_id = str(source.get("source_id") or "").strip()
    if not source_id:
        raise ValueError("company seed ingest requires source_id")
    now = datetime.now(UTC).isoformat()
    new_count = 0
    existing_count = 0
    for raw_seed in seeds:
        seed = dict(raw_seed)
        company_name = str(seed.get("company_name") or "").strip()
        source_url = str(seed.get("source_url") or "").strip()
        if not company_name or not source_url:
            raise ValueError("company seed requires company_name and source_url")
        company_key = _company_seed_key(seed)
        exists = conn.execute(
            "SELECT 1 FROM radar_company_seeds WHERE company_key = ?",
            (company_key,),
        ).fetchone()
        if exists is None:
            new_count += 1
        else:
            existing_count += 1
        conn.execute(
            """
            INSERT INTO radar_company_seeds (
                company_key, company_name, official_domain, official_url,
                careers_url, location, sectors_json, track_tags_json, status,
                verification_status, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(company_key) DO UPDATE SET
                company_name = excluded.company_name,
                official_domain = COALESCE(
                    excluded.official_domain,
                    radar_company_seeds.official_domain
                ),
                official_url = COALESCE(
                    excluded.official_url,
                    radar_company_seeds.official_url
                ),
                careers_url = COALESCE(
                    excluded.careers_url,
                    radar_company_seeds.careers_url
                ),
                location = COALESCE(excluded.location, radar_company_seeds.location),
                sectors_json = excluded.sectors_json,
                track_tags_json = excluded.track_tags_json,
                status = CASE
                    WHEN radar_company_seeds.status IN ('rejected', 'source_configured')
                        THEN radar_company_seeds.status
                    ELSE excluded.status
                END,
                verification_status = CASE
                    WHEN radar_company_seeds.status IN ('rejected', 'source_configured')
                        THEN radar_company_seeds.verification_status
                    ELSE excluded.verification_status
                END,
                last_seen_at = excluded.last_seen_at
            """,
            (
                company_key,
                company_name,
                seed.get("official_domain"),
                seed.get("official_url"),
                seed.get("careers_url"),
                seed.get("location"),
                _json_text(seed.get("sectors", [])),
                _json_text(seed.get("track_tags", [])),
                seed.get("status", "awaiting_official_careers"),
                seed.get("verification_status", "company_seed_unverified"),
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO radar_company_seed_sources (
                company_key, source_id, source_url, last_run_id, payload_json,
                first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(company_key, source_id, source_url) DO UPDATE SET
                last_run_id = excluded.last_run_id,
                payload_json = excluded.payload_json,
                last_seen_at = excluded.last_seen_at
            """,
            (
                company_key,
                source_id,
                source_url,
                run_id,
                _json_text(raw_seed),
                now,
                now,
            ),
        )
    conn.commit()
    return {
        "seeds": len(seeds),
        "new": new_count,
        "existing": existing_count,
    }


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


def get_applied_exclusion_set(conn: sqlite3.Connection) -> list[dict]:
    """Return auditable applied identities used only to suppress radar output."""
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
    conn: sqlite3.Connection,
    *,
    snapshot_id: str | None = None,
    max_age_hours: int = 6,
) -> dict:
    """Return integrity and freshness evidence for one Applied import."""
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
    conn: sqlite3.Connection,
    *,
    since: str | None = None,
    expected_sources: list[dict] | None = None,
    applied_snapshot_id: str | None = None,
) -> dict:
    """Return serialisable source-run, verified-job, and lead report data."""
    ensure_radar_schema(conn)
    applied_snapshot = get_latest_applied_exclusion_snapshot(
        conn,
        snapshot_id=applied_snapshot_id,
    )
    run_where = "WHERE started_at >= ?" if since else ""
    run_params = (since,) if since else ()
    run_rows = conn.execute(
        f"""
        SELECT r.*, COALESCE(r.source_type, s.source_type) AS resolved_source_type
        FROM radar_fetch_runs r
        LEFT JOIN radar_sources s ON s.source_id = r.source_id
        {run_where}
        ORDER BY r.started_at DESC
        """,
        run_params,
    ).fetchall()
    runs = []
    seen_run_keys: set[tuple[str, str]] = set()
    seen_source_ids: set[str] = set()
    latest_observation_run_ids: list[str] = []
    for row in run_rows:
        source_id = str(row["source_id"])
        source_type = str(row["resolved_source_type"] or "")
        run_key = (source_id, source_type)
        if run_key in seen_run_keys:
            continue
        seen_run_keys.add(run_key)
        seen_source_ids.add(source_id)
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
                "source": source_id,
                "kind": source_type
                or ("official_careers" if source_id.startswith("official:") else "social_lead"),
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
        if not source_id or source_id in seen_source_ids:
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
        SELECT l.*, o.source_id, o.payload_json,
               COALESCE(o.source_type, s.source_type) AS resolved_source_type
        FROM radar_leads l
        JOIN radar_source_observations o ON o.observation_key = l.observation_key
        LEFT JOIN radar_sources s ON s.source_id = o.source_id
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
                "kind": row["resolved_source_type"]
                or (
                    "linkedin_post"
                    if "linkedin.com" in (row["source_url"] or "")
                    else "forum"
                ),
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
    seed_where = "WHERE last_seen_at >= ?" if since else ""
    seed_params = (since,) if since else ()
    seed_rows = conn.execute(
        f"""
        SELECT *
        FROM radar_company_seeds
        {seed_where}
        ORDER BY last_seen_at DESC, company_name
        """,
        seed_params,
    ).fetchall()
    company_seeds = []
    for row in seed_rows:
        lineage = conn.execute(
            """
            SELECT source_id, source_url
            FROM radar_company_seed_sources
            WHERE company_key = ?
            ORDER BY source_id, source_url
            """,
            (row["company_key"],),
        ).fetchall()
        try:
            sectors = json.loads(row["sectors_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            sectors = []
        try:
            track_tags = json.loads(row["track_tags_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            track_tags = []
        company_seeds.append(
            {
                "company_key": row["company_key"],
                "company_name": row["company_name"],
                "official_domain": row["official_domain"],
                "official_url": row["official_url"],
                "careers_url": row["careers_url"],
                "location": row["location"],
                "sectors": sectors,
                "track_tags": track_tags,
                "status": row["status"],
                "verification_status": row["verification_status"],
                "source_count": len(lineage),
                "source_ids": [item["source_id"] for item in lineage],
                "source_urls": [item["source_url"] for item in lineage],
            }
        )
    return {
        "source_runs": runs,
        "observations": observations,
        "leads": leads,
        "company_seeds": company_seeds,
        "applied_exclusions": applied_exclusions,
        "applied_snapshot": applied_snapshot,
    }
