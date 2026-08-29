from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from applypilot import config
from applypilot.cli import app
from applypilot.database import (
    canonicalize_job_url,
    close_connection,
    finish_radar_fetch_run,
    get_latest_applied_exclusion_snapshot,
    get_radar_daily_snapshot,
    import_linkedin_applied_export,
    ingest_radar_company_seeds,
    ingest_radar_leads,
    ingest_radar_official_jobs,
    init_db,
    reconcile_radar_leads,
    start_radar_fetch_run,
    store_jobs,
)
from applypilot.discovery.ecosystem import radar_source_descriptor
from applypilot.radar import render_daily_report
from applypilot.storage.radar import ensure_radar_schema


def _source(company: str, provider: str) -> dict:
    return {
        "source_id": f"official:{company}:{provider}",
        "company_id": company,
        "company_name": company.title(),
        "source_type": "official_careers",
        "provider": provider,
        "access_mode": "public_read",
        "active": True,
    }


def _official_job(url: str, external_id: str, *, title: str = "Solutions Consultant") -> dict:
    return {
        "url": url,
        "canonical_url": url,
        "title": title,
        "company_name": "Databricks",
        "company_id": "databricks",
        "location": "Singapore",
        "description": "Help customers implement data and AI solutions.",
        "full_description": "Help customers implement data and AI solutions.",
        "external_id": external_id,
        "requisition_id": external_id,
        "published_at": "2026-08-24T00:00:00Z",
        "verification_status": "verified_official",
        "track_tags": ["pre_sales_solution_consulting"],
    }


def test_radar_schema_adds_source_type_to_existing_run_and_observation_tables(
    tmp_path,
) -> None:
    db_path = tmp_path / "legacy-radar.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE radar_fetch_runs (
            run_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            pagination_complete INTEGER,
            pages_fetched INTEGER,
            raw_count INTEGER NOT NULL DEFAULT 0,
            normalized_count INTEGER NOT NULL DEFAULT 0,
            new_count INTEGER NOT NULL DEFAULT 0,
            existing_count INTEGER NOT NULL DEFAULT 0,
            lead_count INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            parser_version TEXT,
            metadata_json TEXT
        );
        CREATE TABLE radar_source_observations (
            observation_key TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            company_id TEXT,
            external_id TEXT,
            source_url TEXT,
            canonical_url TEXT,
            title TEXT,
            company_name TEXT,
            location TEXT,
            published_at TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_run_id TEXT NOT NULL,
            publisher_name TEXT,
            publisher_type TEXT,
            verification_status TEXT NOT NULL,
            content_fingerprint TEXT NOT NULL,
            payload_json TEXT
        );
        """
    )

    ensure_radar_schema(conn)

    run_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(radar_fetch_runs)")
    }
    observation_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(radar_source_observations)")
    }
    assert "source_type" in run_columns
    assert "source_type" in observation_columns
    conn.close()


def test_official_ingest_dedupes_job_but_retains_source_lineage(tmp_path) -> None:
    db_path = tmp_path / "radar.db"
    conn = init_db(db_path)
    greenhouse = _source("databricks", "greenhouse")
    jsonld = _source("databricks", "jobposting_jsonld")
    url = "https://boards.example.test/databricks/jobs/42"

    first_run = start_radar_fetch_run(conn, greenhouse)
    first = ingest_radar_official_jobs(conn, first_run, greenhouse, [_official_job(url, "42")])
    finish_radar_fetch_run(conn, first_run, status="complete", pagination_complete=True)

    second_run = start_radar_fetch_run(conn, jsonld)
    second = ingest_radar_official_jobs(conn, second_run, jsonld, [_official_job(url, "REQ-42")])
    finish_radar_fetch_run(conn, second_run, status="complete", pagination_complete=True)

    assert first == {"new": 1, "existing": 0, "linked": 1}
    assert second == {"new": 0, "existing": 1, "linked": 1}
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM radar_source_observations").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM radar_job_sources").fetchone()[0] == 2
    close_connection(db_path)


def test_official_reingest_replaces_encoded_html_with_clean_description(tmp_path) -> None:
    db_path = tmp_path / "radar.db"
    conn = init_db(db_path)
    source = _source("workato", "greenhouse")
    url = "https://boards.example.test/workato/jobs/42"

    encoded = _official_job(url, "42")
    encoded["description"] = encoded["full_description"] = (
        "&lt;div class=&quot;content-intro&quot;&gt;Build &lt;strong&gt;AI&lt;/strong&gt; "
        "solutions.&lt;/div&gt;"
    )
    first_run = start_radar_fetch_run(conn, source)
    ingest_radar_official_jobs(conn, first_run, source, [encoded])

    clean = _official_job(url, "42")
    clean["description"] = clean["full_description"] = "Build AI solutions."
    second_run = start_radar_fetch_run(conn, source)
    counts = ingest_radar_official_jobs(conn, second_run, source, [clean])

    row = conn.execute(
        "SELECT description, full_description FROM jobs WHERE url = ?", (url,)
    ).fetchone()
    assert counts == {"new": 0, "existing": 1, "linked": 1}
    assert dict(row) == {
        "description": "Build AI solutions.",
        "full_description": "Build AI solutions.",
    }
    close_connection(db_path)


def test_different_requisitions_with_same_title_remain_distinct(tmp_path) -> None:
    db_path = tmp_path / "radar.db"
    conn = init_db(db_path)
    source = _source("databricks", "greenhouse")
    run_id = start_radar_fetch_run(conn, source)
    counts = ingest_radar_official_jobs(
        conn,
        run_id,
        source,
        [
            _official_job("https://boards.example.test/jobs/100", "100"),
            _official_job("https://boards.example.test/jobs/101", "101"),
        ],
    )
    assert counts["new"] == 2
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    close_connection(db_path)


def test_same_requisition_across_providers_and_urls_forms_one_job(tmp_path) -> None:
    db_path = tmp_path / "radar.db"
    conn = init_db(db_path)
    greenhouse = _source("databricks", "greenhouse")
    rss = _source("databricks", "rss")
    first = _official_job("https://boards.example.test/jobs/100", "gh-100")
    first["requisition_id"] = "REQ-SG-100"
    second = _official_job("https://careers.example.test/jobs/req-sg-100", "rss-9")
    second["requisition_id"] = "REQ-SG-100"

    first_run = start_radar_fetch_run(conn, greenhouse)
    ingest_radar_official_jobs(conn, first_run, greenhouse, [first])
    finish_radar_fetch_run(conn, first_run, status="complete", pagination_complete=True)
    second_run = start_radar_fetch_run(conn, rss)
    counts = ingest_radar_official_jobs(conn, second_run, rss, [second])
    finish_radar_fetch_run(conn, second_run, status="complete", pagination_complete=True)

    assert counts == {"new": 0, "existing": 1, "linked": 1}
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    snapshot = get_radar_daily_snapshot(conn)
    assert len(snapshot["observations"]) == 1
    assert snapshot["observations"][0]["source_count"] == 2
    report = render_daily_report(**snapshot)
    assert report.count("Solutions Consultant | Databricks") == 1
    assert "2 sources: official-databricks-greenhouse, official-databricks-rss" in report
    close_connection(db_path)


def test_provider_migration_reuses_legacy_requisition_identity(tmp_path) -> None:
    db_path = tmp_path / "radar.db"
    conn = init_db(db_path)
    legacy_url = (
        "https://www.grab.careers/en/jobs/744000145885499/"
        "intern-strategy-insights/"
    )
    store_jobs(
        conn,
        [{
            "url": legacy_url,
            "canonical_job_url": legacy_url,
            "application_url": legacy_url,
            "title": "Intern, Strategy & Insights",
            "company_name": "Grab",
            "location": "Singapore",
            "platform_job_id": "rss:grab:REF5967M",
        }],
        site="official:grab:rss",
        strategy="radar_rss",
    )

    source = _source("grab", "smartrecruiters")
    run_id = start_radar_fetch_run(conn, source)
    smartrecruiters_job = _official_job(
        "https://jobs.smartrecruiters.com/Grab/744000145885499-intern-strategy-insights",
        "744000145885499",
        title="Intern, Strategy & Insights",
    )
    smartrecruiters_job.update({
        "application_url": (
            "https://jobs.smartrecruiters.com/Grab/"
            "744000145885499-intern-strategy-insights?trid=example"
        ),
        "company_name": "Grab",
        "company_id": "grab",
        "requisition_id": "REF5967M",
    })
    counts = ingest_radar_official_jobs(
        conn,
        run_id,
        source,
        [smartrecruiters_job],
    )

    assert counts == {"new": 0, "existing": 1, "linked": 1}
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    row = conn.execute(
        "SELECT url, platform_job_id FROM jobs"
    ).fetchone()
    assert dict(row) == {
        "url": legacy_url,
        "platform_job_id": "rss:grab:REF5967M",
    }
    linked = conn.execute(
        """
        SELECT rjs.job_url, rjs.source_id
        FROM radar_job_sources AS rjs
        """
    ).fetchone()
    assert dict(linked) == {
        "job_url": legacy_url,
        "source_id": "official:grab:smartrecruiters",
    }
    close_connection(db_path)


def test_requisition_display_placeholder_does_not_merge_distinct_jobs(tmp_path) -> None:
    db_path = tmp_path / "radar.db"
    conn = init_db(db_path)
    source = _source("stripe", "greenhouse")
    run_id = start_radar_fetch_run(conn, source)
    first = _official_job("https://stripe.example/jobs?gh_jid=1", "1")
    second = _official_job("https://stripe.example/jobs?gh_jid=2", "2")
    for job in (first, second):
        job["company_id"] = "stripe"
        job["company_name"] = "Stripe"
        job["requisition_id"] = "See opening ID"
    counts = ingest_radar_official_jobs(conn, run_id, source, [first, second])
    assert counts["new"] == 2
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    close_connection(db_path)


def test_placeholder_in_every_external_id_field_falls_back_to_distinct_urls(tmp_path) -> None:
    db_path = tmp_path / "radar.db"
    conn = init_db(db_path)
    source = _source("example", "test")
    run_id = start_radar_fetch_run(conn, source)
    jobs = []
    for suffix in ("one", "two"):
        job = _official_job(f"https://careers.example/jobs/view?job={suffix}", "See opening ID")
        job["requisition_id"] = "See opening ID"
        job["job_id"] = "See opening ID"
        jobs.append(job)
    counts = ingest_radar_official_jobs(conn, run_id, source, jobs)
    assert counts["new"] == 2
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    close_connection(db_path)


def test_non_linkedin_canonical_url_preserves_meaningful_query_identity(tmp_path) -> None:
    db_path = tmp_path / "radar.db"
    conn = init_db(db_path)
    first = "https://www.indeed.com/viewjob?jk=first&utm_source=radar"
    second = "https://www.indeed.com/viewjob?jk=second&utm_source=radar"
    assert canonicalize_job_url(first).endswith("viewjob?jk=first")
    assert store_jobs(conn, [{"url": first}, {"url": second}], "indeed", "test") == (2, 0)
    close_connection(db_path)


def test_report_excludes_cross_site_linkedin_applied_by_company_title_location(
    tmp_path,
) -> None:
    db_path = tmp_path / "radar.db"
    conn = init_db(db_path)
    applied_export = tmp_path / "applied.json"
    applied_export.write_text(
        json.dumps({
            "source": "linkedin_job_tracker_visible_complete_read",
            "complete": True,
            "observed_at": datetime.now(UTC).isoformat(),
            "observed_total": 1,
            "pages_read": 1,
            "applications": [{
                "url": "https://www.linkedin.com/jobs/view/123456789/",
                "title": "Solutions Consultant",
                "company": "Databricks",
                "location": "Singapore (Hybrid)",
            }]
        }),
        encoding="utf-8",
    )
    import_linkedin_applied_export(applied_export, conn)
    source = _source("databricks", "greenhouse")
    run_id = start_radar_fetch_run(conn, source)
    ingest_radar_official_jobs(
        conn,
        run_id,
        source,
        [_official_job("https://boards.example.test/jobs/solutions-42", "42")],
    )
    finish_radar_fetch_run(conn, run_id, status="complete", pagination_complete=True)

    snapshot = get_radar_daily_snapshot(conn)
    assert snapshot["observations"] == []
    assert len(snapshot["applied_exclusions"]) == 1
    assert snapshot["applied_exclusions"][0]["reason"] == "company_title_location"
    assert snapshot["applied_snapshot"]["completeness"] == "complete"
    assert snapshot["applied_snapshot"]["fresh"] is True
    report = render_daily_report(**snapshot)
    assert "LinkedIn Applied exclusions: 1 matched listings" in report
    assert "Solutions Consultant | Databricks" not in report
    close_connection(db_path)


def test_report_does_not_fuzzy_exclude_same_company_title_in_another_location(
    tmp_path,
) -> None:
    db_path = tmp_path / "radar.db"
    conn = init_db(db_path)
    applied_export = tmp_path / "applied.json"
    applied_export.write_text(
        json.dumps({
            "source": "linkedin_job_tracker_visible_complete_read",
            "complete": True,
            "observed_at": datetime.now(UTC).isoformat(),
            "observed_total": 1,
            "pages_read": 1,
            "applications": [{
                "url": "https://www.linkedin.com/jobs/view/123456780/",
                "title": "Solutions Consultant",
                "company": "Databricks",
                "location": "London, United Kingdom",
            }]
        }),
        encoding="utf-8",
    )
    import_linkedin_applied_export(applied_export, conn)
    source = _source("databricks", "greenhouse")
    run_id = start_radar_fetch_run(conn, source)
    ingest_radar_official_jobs(
        conn,
        run_id,
        source,
        [_official_job("https://boards.example.test/jobs/solutions-sg", "sg-42")],
    )
    finish_radar_fetch_run(conn, run_id, status="complete", pagination_complete=True)

    snapshot = get_radar_daily_snapshot(conn)
    assert len(snapshot["observations"]) == 1
    assert snapshot["applied_exclusions"] == []
    close_connection(db_path)


def test_social_import_creates_lead_without_creating_job(tmp_path) -> None:
    db_path = tmp_path / "radar.db"
    conn = init_db(db_path)
    source = {
        "source_id": "linkedin-content-manual",
        "source_type": "social_lead",
        "provider": "candidate_reviewed_import",
    }
    run_id = start_radar_fetch_run(conn, source)
    result = ingest_radar_leads(
        conn,
        run_id,
        source,
        [
            {
                "source_url": "https://www.linkedin.com/posts/example_123",
                "title": "Product Manager",
                "company_id": "example",
                "location": "Singapore",
                "status": "awaiting_official",
                "verification_status": "unverified",
            }
        ],
    )
    finish_radar_fetch_run(
        conn,
        run_id,
        status="partial",
        pagination_complete=False,
        lead_count=result["leads"],
    )
    snapshot = get_radar_daily_snapshot(conn)
    assert result == {"leads": 1}
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert len(snapshot["leads"]) == 1
    assert snapshot["source_runs"][0]["status"] == "partial"
    close_connection(db_path)


def test_company_seed_ingest_dedupes_lineage_without_creating_jobs_or_leads(
    tmp_path,
) -> None:
    db_path = tmp_path / "radar.db"
    conn = init_db(db_path)
    sources = [
        {
            "source_id": "sginnovate-dtc",
            "source_type": "company_seed",
            "provider": "candidate_reviewed_import",
        },
        {
            "source_id": "startup-sg-directory",
            "source_type": "company_seed",
            "provider": "candidate_reviewed_import",
        },
    ]
    seed = {
        "company_key": "domain:example.ai",
        "company_name": "Example AI",
        "official_domain": "example.ai",
        "official_url": "https://example.ai/",
        "location": "Singapore",
        "sectors": ["AI", "urban technology"],
        "status": "awaiting_official_careers",
        "verification_status": "company_seed_unverified",
    }

    first_run = start_radar_fetch_run(conn, sources[0])
    first = ingest_radar_company_seeds(
        conn,
        first_run,
        sources[0],
        [{**seed, "source_url": "https://central.sginnovate.com/company/example"}],
    )
    finish_radar_fetch_run(
        conn,
        first_run,
        status="partial",
        pagination_complete=False,
        new_count=first["new"],
        existing_count=first["existing"],
    )
    second_run = start_radar_fetch_run(conn, sources[1])
    second = ingest_radar_company_seeds(
        conn,
        second_run,
        sources[1],
        [{**seed, "source_url": "https://www.startupsg.gov.sg/directory/example"}],
    )
    finish_radar_fetch_run(
        conn,
        second_run,
        status="partial",
        pagination_complete=False,
        new_count=second["new"],
        existing_count=second["existing"],
    )

    assert first == {"seeds": 1, "new": 1, "existing": 0}
    assert second == {"seeds": 1, "new": 0, "existing": 1}
    assert conn.execute("SELECT COUNT(*) FROM radar_company_seeds").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM radar_company_seed_sources"
    ).fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM radar_leads").fetchone()[0] == 0
    snapshot = get_radar_daily_snapshot(conn)
    assert snapshot["company_seeds"][0]["source_count"] == 2
    assert snapshot["company_seeds"][0]["source_ids"] == [
        "sginnovate-dtc",
        "startup-sg-directory",
    ]
    close_connection(db_path)


@pytest.mark.parametrize(
    "record_order",
    [("job_lead", "company_seed"), ("company_seed", "job_lead")],
)
def test_multikind_ecosystem_source_retains_run_and_lead_types(
    tmp_path,
    record_order,
) -> None:
    db_path = tmp_path / "radar.db"
    conn = init_db(db_path)
    sources = {
        kind: radar_source_descriptor("sginnovate-dtc", kind)
        for kind in record_order
    }

    for kind in record_order:
        source = sources[kind]
        run_id = start_radar_fetch_run(conn, source)
        if kind == "job_lead":
            result = ingest_radar_leads(
                conn,
                run_id,
                source,
                [
                    {
                        "title": "Data Scientist",
                        "company_id": "Example AI",
                        "source_url": (
                            "https://central.sginnovate.com/hub/marketplace/"
                            "openings/example"
                        ),
                        "status": "awaiting_official",
                        "verification_status": "unverified",
                    }
                ],
            )
            finish_radar_fetch_run(
                conn,
                run_id,
                status="partial",
                lead_count=result["leads"],
            )
        else:
            result = ingest_radar_company_seeds(
                conn,
                run_id,
                source,
                [
                    {
                        "company_key": "domain:example.ai",
                        "company_name": "Example AI",
                        "source_url": (
                            "https://central.sginnovate.com/hub/marketplace/"
                            "organisations/example"
                        ),
                    }
                ],
            )
            finish_radar_fetch_run(
                conn,
                run_id,
                status="partial",
                new_count=result["new"],
                existing_count=result["existing"],
            )

    snapshot = get_radar_daily_snapshot(conn)

    assert sorted(run["kind"] for run in snapshot["source_runs"]) == [
        "company_seed",
        "ecosystem_lead",
    ]
    assert len(snapshot["leads"]) == 1
    assert snapshot["leads"][0]["kind"] == "ecosystem_lead"
    assert len(snapshot["company_seeds"]) == 1
    close_connection(db_path)


def test_lead_promotes_only_against_exact_existing_official_url(tmp_path) -> None:
    db_path = tmp_path / "radar.db"
    conn = init_db(db_path)
    official_source = _source("databricks", "greenhouse")
    official_url = "https://boards.example.test/jobs/verified-42"
    official_run = start_radar_fetch_run(conn, official_source)
    ingest_radar_official_jobs(
        conn,
        official_run,
        official_source,
        [_official_job(official_url, "42")],
    )
    finish_radar_fetch_run(
        conn,
        official_run,
        status="complete",
        pagination_complete=True,
    )
    lead_source = {
        "source_id": "linkedin-content-manual",
        "source_type": "social_lead",
        "provider": "candidate_reviewed_import",
    }
    lead_run = start_radar_fetch_run(conn, lead_source)
    ingest_radar_leads(
        conn,
        lead_run,
        lead_source,
        [
            {
                "source_url": "https://www.linkedin.com/posts/example_42",
                "official_job_url": official_url,
                "title": "Solutions Consultant",
                "company_id": "databricks",
                "location": "Singapore",
                "status": "awaiting_official",
                "verification_status": "unverified",
            },
            {
                "source_url": "https://www.linkedin.com/posts/example_43",
                "official_job_url": "https://boards.example.test/jobs/missing-43",
                "title": "Product Manager",
                "company_id": "databricks",
                "location": "Singapore",
                "status": "awaiting_official",
                "verification_status": "unverified",
            },
        ],
    )
    assert reconcile_radar_leads(conn, official_run_ids=[official_run]) == {"promoted": 1}
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM radar_job_sources").fetchone()[0] == 2
    statuses = [row[0] for row in conn.execute("SELECT status FROM radar_leads ORDER BY source_url")]
    assert statuses == ["promoted", "awaiting_official"]

    reimport_run = start_radar_fetch_run(conn, lead_source)
    ingest_radar_leads(
        conn,
        reimport_run,
        lead_source,
        [
            {
                "source_url": "https://www.linkedin.com/posts/example_42",
                "official_job_url": official_url,
                "title": "Solutions Consultant",
                "company_id": "databricks",
                "location": "Singapore",
                "status": "awaiting_official",
                "verification_status": "unverified",
            }
        ],
    )
    retained = conn.execute(
        "SELECT status, verification_status, promoted_job_url "
        "FROM radar_leads WHERE source_url = ?",
        ("https://www.linkedin.com/posts/example_42",),
    ).fetchone()
    assert dict(retained) == {
        "status": "promoted",
        "verification_status": "official_target_open",
        "promoted_job_url": official_url,
    }
    close_connection(db_path)


def test_aggregator_job_cannot_promote_a_social_lead(tmp_path) -> None:
    db_path = tmp_path / "radar.db"
    conn = init_db(db_path)
    target = "https://jobs.example.test/aggregated-42"
    store_jobs(
        conn,
        [{"url": target, "title": "Product Manager", "company_name": "Example"}],
        "job-board",
        "aggregate",
    )
    lead_source = {
        "source_id": "linkedin-content-manual",
        "source_type": "social_lead",
        "provider": "candidate_reviewed_import",
    }
    run_id = start_radar_fetch_run(conn, lead_source)
    ingest_radar_leads(
        conn,
        run_id,
        lead_source,
        [
            {
                "source_url": "https://www.linkedin.com/posts/example_aggregate",
                "official_job_url": target,
                "title": "Product Manager",
                "company_id": "example",
                "status": "awaiting_official",
                "verification_status": "unverified",
            }
        ],
    )
    assert reconcile_radar_leads(conn, official_run_ids=["not-an-official-run"]) == {"promoted": 0}
    assert conn.execute("SELECT status FROM radar_leads").fetchone()[0] == "awaiting_official"
    close_connection(db_path)


def test_stale_official_observation_cannot_promote_a_new_social_lead(tmp_path) -> None:
    db_path = tmp_path / "radar.db"
    conn = init_db(db_path)
    official_source = _source("databricks", "greenhouse")
    official_url = "https://boards.example.test/jobs/stale-42"
    official_run = start_radar_fetch_run(conn, official_source)
    ingest_radar_official_jobs(
        conn,
        official_run,
        official_source,
        [_official_job(official_url, "stale-42")],
    )
    finish_radar_fetch_run(
        conn,
        official_run,
        status="complete",
        pagination_complete=True,
    )
    conn.execute(
        "UPDATE radar_source_observations SET last_seen_at = '2020-01-01T00:00:00+00:00'"
    )
    conn.commit()

    lead_source = {
        "source_id": "linkedin-content-manual",
        "source_type": "social_lead",
        "provider": "candidate_reviewed_import",
    }
    lead_run = start_radar_fetch_run(conn, lead_source)
    ingest_radar_leads(
        conn,
        lead_run,
        lead_source,
        [{
            "source_url": "https://www.linkedin.com/posts/example_stale",
            "official_job_url": official_url,
            "title": "Solutions Consultant",
            "company_id": "databricks",
            "status": "awaiting_official",
            "verification_status": "unverified",
        }],
    )

    assert reconcile_radar_leads(conn, official_run_ids=[official_run]) == {"promoted": 0}
    assert conn.execute("SELECT status FROM radar_leads").fetchone()[0] == "awaiting_official"
    close_connection(db_path)


def test_report_marks_missing_running_and_incomplete_active_sources_unavailable(tmp_path) -> None:
    db_path = tmp_path / "radar.db"
    conn = init_db(db_path)
    complete_source = _source("complete", "greenhouse")
    missing_source = _source("missing", "greenhouse")
    running_source = _source("running", "greenhouse")
    complete_run = start_radar_fetch_run(conn, complete_source)
    finish_radar_fetch_run(
        conn,
        complete_run,
        status="complete",
        pagination_complete=False,
        normalized_count=0,
    )
    start_radar_fetch_run(conn, running_source)
    snapshot = get_radar_daily_snapshot(
        conn,
        expected_sources=[complete_source, missing_source, running_source],
    )
    by_source = {item["source"]: item for item in snapshot["source_runs"]}
    assert by_source[complete_source["source_id"]]["status"] == "partial"
    assert by_source[running_source["source_id"]]["status"] == "partial"
    assert by_source[missing_source["source_id"]]["status"] == "skipped"
    report = render_daily_report(**snapshot)
    assert "unavailable: verified-job zero cannot be claimed" in report
    assert "0 verified jobs (all relevant official source runs complete)" not in report
    close_connection(db_path)


def test_daily_snapshot_does_not_carry_prior_run_jobs_into_latest_zero_run(
    tmp_path,
) -> None:
    db_path = tmp_path / "radar.db"
    conn = init_db(db_path)
    source = _source("databricks", "greenhouse")
    first_run = start_radar_fetch_run(conn, source)
    ingest_radar_official_jobs(
        conn,
        first_run,
        source,
        [_official_job("https://boards.example.test/jobs/old-42", "old-42")],
    )
    finish_radar_fetch_run(
        conn,
        first_run,
        status="complete",
        pagination_complete=True,
        normalized_count=1,
    )
    latest_run = start_radar_fetch_run(conn, source)
    finish_radar_fetch_run(
        conn,
        latest_run,
        status="complete",
        pagination_complete=True,
        normalized_count=0,
    )

    snapshot = get_radar_daily_snapshot(conn)
    assert snapshot["source_runs"][0]["count"] == 0
    assert snapshot["observations"] == []
    close_connection(db_path)


def test_nested_location_and_title_filters_are_applied() -> None:
    search_config = {
        "location": {
            "accept_patterns": ["Singapore", "Remote"],
            "reject_patterns": ["US only"],
        },
        "exclude_titles": ["Vice President", "Head of"],
    }
    accept, reject = config.get_location_filters(search_config)
    assert config.location_is_accepted("Singapore", accept, reject)
    assert config.location_is_accepted("Remote - US only", accept, reject) is False
    assert config.location_is_accepted("London", accept, reject) is False
    assert config.radar_location_is_accepted("Remote - California", accept, reject) is False
    assert config.radar_location_is_accepted("Remote - Singapore", accept, reject)
    assert config.radar_location_is_accepted("Remote", accept, reject) is False
    assert config.radar_location_is_accepted(
        "Remote", accept, reject, allow_ambiguous_remote=True
    )
    assert config.title_is_excluded("Head of Product", search_config=search_config)
    assert not config.title_is_excluded("Product Manager", search_config=search_config)


def test_default_radar_policy_excludes_clearly_senior_titles_but_keeps_product_manager() -> None:
    search_config = config.load_radar_config()
    assert config.title_is_excluded("Senior Product Manager", search_config=search_config)
    assert config.title_is_excluded("Lead Product Manager", search_config=search_config)
    assert config.title_is_excluded("Sr. Solutions Engineer", search_config=search_config)
    assert not config.title_is_excluded("Product Manager", search_config=search_config)


def test_radar_queries_cli_covers_multiple_tracks_and_windows() -> None:
    runner = CliRunner()
    daily = runner.invoke(app, ["radar", "queries", "--track", "ai_implementation", "--json"])
    weekly = runner.invoke(
        app,
        ["radar", "queries", "--track", "spatial", "--window", "past-week", "--json"],
    )
    assert daily.exit_code == 0, daily.output
    assert '"ai_implementation"' in daily.output
    assert "past-24h" in daily.output
    assert weekly.exit_code == 0, weekly.output
    assert '"spatial"' in weekly.output
    assert "past-week" in weekly.output


def test_radar_collect_dry_run_does_not_bootstrap_or_write(monkeypatch) -> None:
    from applypilot import cli

    monkeypatch.setattr(
        cli,
        "_radar_bootstrap",
        lambda: (_ for _ in ()).throw(AssertionError("write path called")),
    )
    result = CliRunner().invoke(
        app,
        ["radar", "collect", "--dry-run", "--include-inactive"],
    )
    assert result.exit_code == 0, result.output
    assert '"read_only": true' in result.output
    assert '"active": false' in result.output


def test_complete_applied_import_returns_and_binds_persisted_snapshot_id(tmp_path) -> None:
    conn = init_db(tmp_path / "radar.db")
    observed_at = datetime.now(UTC).isoformat()
    export_path = tmp_path / "applied.json"
    export_path.write_text(
        json.dumps({
            "source": "linkedin_job_tracker_visible_complete_read",
            "complete": True,
            "observed_at": observed_at,
            "observed_total": 1,
            "pages_read": 1,
            "applications": [{
                "url": "https://www.linkedin.com/jobs/view/123456700/",
                "title": "Data Analyst",
                "company": "Example",
                "location": "Singapore",
            }],
        }),
        encoding="utf-8",
    )

    result = import_linkedin_applied_export(export_path, conn)
    snapshot = get_latest_applied_exclusion_snapshot(
        conn,
        snapshot_id=str(result["snapshot_id"]),
    )

    assert result["completeness"] == "complete"
    assert snapshot["snapshot_id"] == result["snapshot_id"]
    assert snapshot["observed_at"] == observed_at
    assert snapshot["integrity_valid"] is True
    assert snapshot["fresh"] is True
    missing = get_latest_applied_exclusion_snapshot(conn, snapshot_id="missing")
    assert missing["completeness"] == "missing"
    assert missing["fresh"] is False
    close_connection(tmp_path / "radar.db")


def test_applied_snapshot_freshness_uses_observed_time_and_checks_counts(tmp_path) -> None:
    conn = init_db(tmp_path / "radar.db")
    export_path = tmp_path / "applied.json"
    export_path.write_text(
        json.dumps({
            "source": "linkedin_job_tracker_visible_complete_read",
            "complete": True,
            "observed_at": (datetime.now(UTC) - timedelta(hours=7)).isoformat(),
            "observed_total": 1,
            "pages_read": 1,
            "applications": [{
                "url": "https://www.linkedin.com/jobs/view/123456701/",
                "title": "Product Analyst",
                "company": "Example",
                "location": "Singapore",
            }],
        }),
        encoding="utf-8",
    )
    result = import_linkedin_applied_export(export_path, conn)
    snapshot_id = str(result["snapshot_id"])
    stale = get_latest_applied_exclusion_snapshot(conn, snapshot_id=snapshot_id)
    assert stale["integrity_valid"] is True
    assert stale["fresh"] is False

    conn.execute(
        "UPDATE radar_exclusion_snapshots SET imported_count = 0 WHERE snapshot_id = ?",
        (snapshot_id,),
    )
    conn.commit()
    inconsistent = get_latest_applied_exclusion_snapshot(conn, snapshot_id=snapshot_id)
    assert inconsistent["integrity_valid"] is False
    close_connection(tmp_path / "radar.db")


def test_required_applied_snapshot_failure_blocks_report_file(tmp_path, monkeypatch) -> None:
    from applypilot import cli, database

    monkeypatch.setattr(cli, "_radar_bootstrap", lambda: None)
    monkeypatch.setattr(
        database,
        "get_radar_daily_snapshot",
        lambda **_kwargs: {
            "source_runs": [],
            "observations": [],
            "leads": [],
            "applied_exclusions": [],
            "applied_snapshot": {
                "snapshot_id": "other-snapshot",
                "completeness": "complete",
                "integrity_valid": True,
                "fresh": True,
            },
        },
    )
    output = tmp_path / "should-not-exist.md"
    result = CliRunner().invoke(
        app,
        [
            "radar",
            "report",
            "--require-applied-snapshot",
            "required-snapshot",
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 2
    assert "report publication is blocked" in result.output
    assert not output.exists()


def test_discovery_only_mode_blocks_non_radar_commands() -> None:
    result = CliRunner().invoke(
        app,
        ["fact-history"],
        env={"APPLYPILOT_DISCOVERY_ONLY": "1"},
    )
    assert result.exit_code == 2
    assert "Discovery-only mode blocks" in result.output


def test_discovery_only_sync_rejects_file_outside_radar_imports(
    tmp_path,
    monkeypatch,
) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(config, "RADAR_IMPORT_DIR", tmp_path / "allowed")
    result = CliRunner().invoke(
        app,
        ["sync-linkedin-applied", "--file", str(outside)],
        env={"APPLYPILOT_DISCOVERY_ONLY": "1"},
    )
    assert result.exit_code == 2
    assert "Applied export must stay under" in result.output


@pytest.mark.parametrize(
    "source_id",
    ["official:openai:ashby", "sfa-job-portal", "startup-sg-directory"],
)
def test_discovery_only_lead_import_rejects_source_identity_override(
    tmp_path,
    monkeypatch,
    source_id,
) -> None:
    from applypilot import cli

    import_root = tmp_path / "radar-imports"
    import_root.mkdir()
    lead_file = import_root / "leads.json"
    lead_file.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(config, "RADAR_IMPORT_DIR", import_root)
    monkeypatch.setattr(
        cli,
        "_radar_bootstrap",
        lambda: (_ for _ in ()).throw(AssertionError("invalid source wrote storage")),
    )
    result = CliRunner().invoke(
        app,
        [
            "radar",
            "import-leads",
            "--file",
            str(lead_file),
            "--source-id",
            source_id,
        ],
        env={
            "APPLYPILOT_DISCOVERY_ONLY": "1",
            "APPLYPILOT_ATTENDED_REVIEW": "1",
        },
    )
    assert result.exit_code == 2
    assert "Lead source is not enabled for P2 import" in result.output


def test_ecosystem_lead_cli_is_unverified_idempotent_and_creates_no_job(
    tmp_path,
    monkeypatch,
) -> None:
    from applypilot import database

    app_dir = tmp_path / "data"
    import_root = app_dir / "radar-imports"
    import_root.mkdir(parents=True)
    db_path = app_dir / "applypilot.db"
    lead_file = import_root / "careeraxis-leads.json"
    lead_file.write_text(
        json.dumps(
            {
                "leads": [
                    {
                        "title": "Data Analyst",
                        "company_name": "Example Labs",
                        "location": "Singapore",
                        "source_url": "https://careeraxis.ntu.edu.sg/jobs/12345",
                        "official_job_url": "https://jobs.examplelabs.com/ai-product-intern",
                        "status": "promoted",
                        "verification_status": "verified_official",
                        "is_official_publisher": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "APP_DIR", app_dir)
    monkeypatch.setattr(config, "RADAR_IMPORT_DIR", import_root)
    monkeypatch.setattr(config, "RADAR_REPORT_DIR", app_dir / "reports")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    args = [
        "radar",
        "import-leads",
        "--file",
        str(lead_file),
        "--source-id",
        "careeraxis",
    ]
    environment = {
        "APPLYPILOT_DISCOVERY_ONLY": "1",
        "APPLYPILOT_ATTENDED_REVIEW": "1",
    }

    first = CliRunner().invoke(app, args, env=environment)
    second = CliRunner().invoke(app, args, env=environment)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    conn = database.get_connection()
    lead = conn.execute(
        "SELECT status, verification_status, track_tags_json FROM radar_leads"
    ).fetchone()
    source = conn.execute(
        "SELECT source_type FROM radar_sources WHERE source_id = 'careeraxis'"
    ).fetchone()
    assert tuple(lead) == (
        "awaiting_official",
        "unverified",
        '["data_analytics"]',
    )
    assert source["source_type"] == "ecosystem_lead"
    assert conn.execute("SELECT COUNT(*) FROM radar_leads").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    close_connection(db_path)


def test_company_seed_cli_retains_lineage_without_creating_job_or_lead(
    tmp_path,
    monkeypatch,
) -> None:
    from applypilot import database

    app_dir = tmp_path / "data"
    import_root = app_dir / "radar-imports"
    import_root.mkdir(parents=True)
    db_path = app_dir / "applypilot.db"
    seed_file = import_root / "startup-sg-companies.json"
    seed_file.write_text(
        json.dumps(
            {
                "companies": [
                    {
                        "company_name": "Example Spatial AI",
                        "source_url": (
                            "https://www.startupsg.gov.sg/directory/startups/"
                            "example-spatial-ai"
                        ),
                        "official_url": "https://example-spatial.ai/",
                        "sectors": ["AI", "Geospatial"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "APP_DIR", app_dir)
    monkeypatch.setattr(config, "RADAR_IMPORT_DIR", import_root)
    monkeypatch.setattr(config, "RADAR_REPORT_DIR", app_dir / "reports")
    monkeypatch.setattr(database, "DB_PATH", db_path)

    result = CliRunner().invoke(
        app,
        [
            "radar",
            "import-company-seeds",
            "--file",
            str(seed_file),
            "--source-id",
            "startup-sg-directory",
        ],
        env={
            "APPLYPILOT_DISCOVERY_ONLY": "1",
            "APPLYPILOT_ATTENDED_REVIEW": "1",
        },
    )

    assert result.exit_code == 0, result.output
    assert '"jobs": 0' in result.output
    assert '"leads": 0' in result.output
    conn = database.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM radar_company_seeds").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM radar_company_seed_sources").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM radar_leads").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    close_connection(db_path)


def test_discovery_only_report_rejects_non_markdown_output(
    tmp_path,
    monkeypatch,
) -> None:
    report_root = tmp_path / "reports"
    report_root.mkdir()
    monkeypatch.setattr(config, "RADAR_REPORT_DIR", report_root)
    result = CliRunner().invoke(
        app,
        [
            "radar",
            "report",
            "--require-applied-snapshot",
            "snapshot",
            "--output",
            str(report_root / "report.json"),
        ],
        env={"APPLYPILOT_DISCOVERY_ONLY": "1"},
    )
    assert result.exit_code == 2
    assert "must use a .md output" in result.output


def test_radar_report_console_preserves_bracketed_tags_and_status(monkeypatch) -> None:
    from applypilot import cli, database

    monkeypatch.setattr(cli, "_radar_bootstrap", lambda: None)
    monkeypatch.setattr(
        database,
        "get_radar_daily_snapshot",
        lambda **_kwargs: {
            "source_runs": [
                {
                    "source": "careeraxis",
                    "kind": "ecosystem_lead",
                    "status": "partial",
                    "count": 1,
                }
            ],
            "observations": [],
            "leads": [
                {
                    "source": "careeraxis",
                    "kind": "ecosystem_lead",
                    "url": "https://careeraxis.ntu.edu.sg/students/jobs/1",
                    "company": "Example",
                    "title": "Data Analyst",
                    "subtracks": ["data_analytics"],
                    "status": "awaiting_official",
                    "reason": "requires official verification",
                }
            ],
            "company_seeds": [],
            "applied_exclusions": [],
            "applied_snapshot": {},
        },
    )

    result = CliRunner().invoke(app, ["radar", "report", "--hours", "24"])

    assert result.exit_code == 0, result.output
    assert "[data_analytics]" in result.output
    assert "[awaiting_official; requires official verification]" in result.output


def test_radar_bootstrap_creates_no_application_material_or_worker_dirs(
    tmp_path,
    monkeypatch,
) -> None:
    from applypilot import cli, database

    app_dir = tmp_path / "data"
    db_path = app_dir / "applypilot.db"
    monkeypatch.setattr(config, "APP_DIR", app_dir)
    monkeypatch.setattr(config, "RADAR_IMPORT_DIR", app_dir / "radar-imports")
    monkeypatch.setattr(config, "RADAR_REPORT_DIR", app_dir / "reports")
    monkeypatch.setattr(database, "DB_PATH", db_path)

    cli._radar_bootstrap()

    assert db_path.exists()
    assert (app_dir / "radar-imports").is_dir()
    assert (app_dir / "reports").is_dir()
    for forbidden in (
        "tailored_resumes",
        "cover_letters",
        "logs",
        "chrome-workers",
        "apply-workers",
    ):
        assert not (app_dir / forbidden).exists()
    close_connection(db_path)


def test_fresh_radar_config_uses_singapore_policy_not_us_search_example(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "RADAR_CONFIG_PATH", tmp_path / "radar.yaml")
    monkeypatch.setattr(config, "SEARCH_CONFIG_PATH", tmp_path / "searches.yaml")
    radar_config = config.load_radar_config()
    accept, _reject = config.get_location_filters(radar_config)
    assert "Singapore" in accept
    assert "San Francisco" not in accept
