from __future__ import annotations

import json

import pytest

from applypilot.database import (
    close_connection,
    finish_radar_fetch_run,
    ingest_radar_company_seeds,
    ingest_radar_leads,
    init_db,
    start_radar_fetch_run,
)
from applypilot.discovery import advance
from applypilot.discovery.advance import advance_radar_queue, safe_public_get


def _jsonld_page(
    *,
    url: str,
    company: str | None = "Page Employer",
    title: str = "Data Analyst Intern",
    valid_through: str | None = None,
) -> str:
    node = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": title,
        "url": url,
        "description": "Build useful data products.",
        "jobLocation": {
            "@type": "Place",
            "address": {"addressLocality": "Singapore", "addressCountry": "SG"},
        },
    }
    if company is not None:
        node["hiringOrganization"] = {
            "@type": "Organization",
            "name": company,
        }
    if valid_through is not None:
        node["validThrough"] = valid_through
    return (
        '<html><script type="application/ld+json">'
        f"{json.dumps(node)}"
        "</script></html>"
    )


def _add_lead(
    conn,
    official_url: str | None,
    *,
    company: str = "unverified-company-label",
) -> str:
    source = {
        "source_id": "linkedin-content-manual",
        "source_type": "social_lead",
        "provider": "candidate_reviewed_import",
    }
    run_id = start_radar_fetch_run(conn, source)
    ingest_radar_leads(
        conn,
        run_id,
        source,
        [
            {
                "source_url": "https://www.linkedin.com/posts/example-lead",
                "official_job_url": official_url,
                "title": "Data Analyst Intern",
                "company_id": company,
                "status": "awaiting_official",
                "verification_status": "unverified",
            }
        ],
    )
    finish_radar_fetch_run(conn, run_id, status="partial", pagination_complete=False)
    return conn.execute("SELECT lead_id FROM radar_leads").fetchone()[0]


def _add_seed(
    conn,
    company_key: str,
    careers_url: str,
    *,
    company: str = "Unverified Seed Name",
    official_domain: str | None = None,
) -> None:
    source = {
        "source_id": f"seed-source:{company_key}",
        "source_type": "company_seed",
        "provider": "candidate_reviewed_import",
    }
    run_id = start_radar_fetch_run(conn, source)
    ingest_radar_company_seeds(
        conn,
        run_id,
        source,
        [
            {
                "company_key": company_key,
                "company_name": company,
                "official_domain": official_domain,
                "source_url": f"https://directory.example/{company_key}",
                "careers_url": careers_url,
                "status": "awaiting_official_careers",
                "verification_status": "company_seed_unverified",
            }
        ],
    )
    finish_radar_fetch_run(conn, run_id, status="partial", pagination_complete=False)


def test_advance_verifies_jsonld_finishes_fresh_run_and_promotes_exact_lead(tmp_path):
    db_path = tmp_path / "radar.db"
    conn = init_db(db_path)
    official_url = "https://jobs.example.com/roles/data-intern"
    _add_lead(conn, official_url, company="  employer   FROM page ")

    result = advance_radar_queue(
        conn,
        limit=1,
        transport=lambda *_args, **_kwargs: _jsonld_page(
            url=official_url,
            company="Employer From Page",
        ),
    )

    assert result["attempted"] == 1
    assert result["newjobs"] == 1
    assert result["promoted"] == 1
    assert result["attempts"][0]["status"] == "verified"
    job = conn.execute("SELECT company_name, url FROM jobs").fetchone()
    assert dict(job) == {"company_name": "Employer From Page", "url": official_url}
    lead = conn.execute("SELECT status, verification_status FROM radar_leads").fetchone()
    assert dict(lead) == {
        "status": "promoted",
        "verification_status": "official_target_open",
    }
    official_run = conn.execute(
        "SELECT status, finished_at FROM radar_fetch_runs "
        "WHERE source_id LIKE 'official:queue:%'"
    ).fetchone()
    assert official_run["status"] == "complete"
    assert official_run["finished_at"]
    close_connection(db_path)


def test_company_alias_mismatch_and_expired_job_remain_pending_without_freshness_change(
    tmp_path,
):
    db_path = tmp_path / "radar.db"
    conn = init_db(db_path)
    official_url = "https://jobs.example.com/roles/data-intern"
    lead_id = _add_lead(conn, official_url, company="Expected Employer")
    before = conn.execute(
        "SELECT last_seen_at FROM radar_leads WHERE lead_id = ?", (lead_id,)
    ).fetchone()[0]

    alias = advance_radar_queue(
        conn,
        transport=lambda *_args, **_kwargs: _jsonld_page(
            url=official_url,
            company="Different Employer",
        ),
    )

    assert alias["attempts"][0]["next_action"] == "confirm_company_alias"
    assert conn.execute(
        "SELECT last_seen_at FROM radar_leads WHERE lead_id = ?", (lead_id,)
    ).fetchone()[0] == before
    run = conn.execute(
        "SELECT status, metadata_json FROM radar_fetch_runs "
        "WHERE parser_version = 'radar-advance-jsonld-v2'"
    ).fetchone()
    assert run["status"] == "partial"
    assert json.loads(run["metadata_json"])["attempt_status"] == "pending"

    conn.execute(
        "UPDATE radar_fetch_runs SET started_at='2020-01-01T00:00:00+00:00', "
        "finished_at='2020-01-01T00:00:01+00:00' WHERE run_id = ?",
        (alias["attempts"][0]["run_id"],),
    )
    conn.commit()
    expired = advance_radar_queue(
        conn,
        transport=lambda *_args, **_kwargs: _jsonld_page(
            url=official_url,
            company="Expected Employer",
            valid_through="2020-01-01",
        ),
    )
    assert "validThrough" in expired["attempts"][0]["reason"]
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    close_connection(db_path)


def test_cross_site_job_url_and_untrusted_seed_host_are_not_verified(tmp_path):
    db_path = tmp_path / "radar.db"
    conn = init_db(db_path)
    source_url = "https://jobs.example.com/source"
    _add_lead(conn, source_url, company="Expected Employer")
    cross_site = advance_radar_queue(
        conn,
        transport=lambda *_args, **_kwargs: _jsonld_page(
            url="https://other.example.net/job",
            company="Expected Employer",
        ),
    )
    assert "cross-site" in cross_site["attempts"][0]["reason"]

    _add_seed(
        conn,
        "untrusted-host",
        "https://untrusted.example.net/careers",
        company="Expected Employer",
        official_domain="expected.example.com",
    )
    conn.execute(
        "UPDATE radar_fetch_runs SET started_at='2020-01-01T00:00:00+00:00', "
        "finished_at='2020-01-01T00:00:01+00:00' "
        "WHERE parser_version='radar-advance-jsonld-v2'"
    )
    conn.commit()
    seed = advance_radar_queue(
        conn,
        limit=2,
        transport=lambda *_args, **_kwargs: _jsonld_page(
            url="https://untrusted.example.net/careers",
            company="Expected Employer",
        ),
    )
    seed_attempt = next(item for item in seed["attempts"] if item["kind"] == "company_seed")
    assert seed_attempt["next_action"] == "confirm_official_domain"
    assert conn.execute(
        "SELECT status FROM radar_company_seeds WHERE company_key='untrusted-host'"
    ).fetchone()[0] == "awaiting_official_careers"
    close_connection(db_path)


def test_missing_hiring_organization_stays_pending_for_manual_review(tmp_path):
    db_path = tmp_path / "radar.db"
    conn = init_db(db_path)
    official_url = "https://jobs.example.com/roles/no-org"
    _add_lead(conn, official_url)

    result = advance_radar_queue(
        conn,
        limit=1,
        transport=lambda *_args, **_kwargs: _jsonld_page(
            url=official_url,
            company=None,
        ),
    )

    attempt = result["attempts"][0]
    assert attempt["status"] == "pending"
    assert "hiringOrganization" in attempt["reason"]
    assert attempt["next_action"] == (
        "manual_review_or_configure_supported_official_provider"
    )
    assert attempt["source_url"] == "https://www.linkedin.com/posts/example-lead"
    assert attempt["target_url"] == official_url
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM radar_leads").fetchone()[0] == (
        "awaiting_official"
    )
    close_connection(db_path)


def test_unresolved_missing_urls_are_visible_without_consuming_attempt_limit(tmp_path):
    db_path = tmp_path / "radar.db"
    conn = init_db(db_path)
    _add_lead(conn, None)
    _add_seed(conn, "missing-careers", "")
    _add_seed(conn, "ready", "https://jobs.example.com/ready")

    result = advance_radar_queue(
        conn,
        limit=1,
        transport=lambda *_args, **_kwargs: _jsonld_page(
            url="https://jobs.example.com/ready",
        ),
    )

    assert result["attempted"] == 1
    assert result["attempts"][0]["item_id"] == "ready"
    assert len(result["unresolved"]) == 1
    assert result["unresolved"][0] == {
        "kind": "lead",
        "item_id": result["unresolved"][0]["item_id"],
        "company": "unverified-company-label",
        "title": "Data Analyst Intern",
        "source_url": "https://www.linkedin.com/posts/example-lead",
        "target_url": None,
        "status": "pending",
        "reason": "missing official_job_url",
        "next_action": "find_official_job_url",
    }
    close_connection(db_path)


def test_queue_limit_is_shared_and_failed_head_moves_behind_older_pending_item(tmp_path):
    db_path = tmp_path / "radar.db"
    conn = init_db(db_path)
    _add_seed(conn, "first", "http://localhost/private")
    _add_seed(
        conn,
        "second",
        "https://jobs.example.com/second",
        company="Actual Page Company",
        official_domain="jobs.example.com",
    )
    conn.execute(
        "UPDATE radar_company_seeds SET last_seen_at = CASE company_key "
        "WHEN 'first' THEN '2020-01-01T00:00:00+00:00' "
        "ELSE '2020-01-02T00:00:00+00:00' END"
    )
    conn.commit()

    first = advance_radar_queue(conn, limit=1, transport=lambda *_args, **_kwargs: "")
    second = advance_radar_queue(
        conn,
        limit=1,
        transport=lambda *_args, **_kwargs: _jsonld_page(
            url="https://jobs.example.com/second",
            company="Actual Page Company",
        ),
    )

    assert first["attempts"][0]["item_id"] == "first"
    assert first["attempts"][0]["status"] == "pending"
    assert second["attempts"][0]["item_id"] == "second"
    assert second["attempts"][0]["status"] == "verified"
    assert second["attempts"][0]["queue_status"] == "official_careers_verified"
    assert tuple(
        conn.execute(
            "SELECT status, verification_status FROM radar_company_seeds "
            "WHERE company_key = 'second'"
        ).fetchone()
    ) == ("official_careers_verified", "official_careers_verified")
    assert conn.execute("SELECT company_name FROM jobs").fetchone()[0] == (
        "Actual Page Company"
    )
    close_connection(db_path)


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "http://jobs.example.com/role",
        "https://user:secret@jobs.example.com/role",
        "https://localhost/role",
        "https://127.0.0.1/role",
        "https://10.0.0.5/role",
        "https://[::1]/role",
    ],
)
def test_safe_public_get_rejects_unsafe_urls_before_fake_transport(unsafe_url):
    called = False

    def transport(*_args, **_kwargs):
        nonlocal called
        called = True
        return "unused"

    with pytest.raises(ValueError):
        safe_public_get(unsafe_url, transport=transport)
    assert called is False


def test_safe_public_get_rechecks_redirect_target_and_caps_body():
    def private_redirect(url, headers=None):
        assert url == "https://jobs.example.com/start"
        return {
            "status_code": 302,
            "headers": {"Location": "https://192.168.1.5/private"},
        }

    with pytest.raises(ValueError, match="non-public"):
        safe_public_get("https://jobs.example.com/start", transport=private_redirect)

    with pytest.raises(ValueError, match="size limit"):
        safe_public_get(
            "https://jobs.example.com/large",
            transport=lambda *_args, **_kwargs: b"12345",
            max_body_bytes=4,
        )


def test_default_get_pins_validated_address_and_normalizes_location_header(monkeypatch):
    captured = {}

    class Response:
        status = 302

        @staticmethod
        def read(_limit):
            return b""

        @staticmethod
        def getheaders():
            return [("Location", "/next")]

    class Connection:
        def __init__(self, hostname, address, port, timeout):
            captured.update(
                hostname=hostname,
                address=address,
                port=port,
                timeout=timeout,
            )

        @staticmethod
        def request(method, target, headers):
            captured.update(method=method, target=target, headers=headers)

        @staticmethod
        def getresponse():
            return Response()

        @staticmethod
        def close():
            return None

    monkeypatch.setattr(advance, "_PinnedHTTPSConnection", Connection)
    response = advance._default_get_once(
        "https://jobs.example.com/path?q=1",
        {"User-Agent": "test"},
        ["93.184.216.34"],
        timeout_seconds=3,
        max_body_bytes=10,
    )

    assert captured["hostname"] == "jobs.example.com"
    assert captured["address"] == "93.184.216.34"
    assert captured["target"] == "/path?q=1"
    assert response["headers"]["location"] == "/next"


def test_redirect_uses_final_url_for_relative_jsonld_and_verified_seed_waits_24h(tmp_path):
    db_path = tmp_path / "radar.db"
    conn = init_db(db_path)
    _add_seed(
        conn,
        "redirected",
        "https://careers.example.com/start",
        company="Example Employer",
        official_domain="example.com",
    )

    def transport(url, headers=None):
        if url.endswith("/start"):
            return {"status_code": 302, "headers": {"Location": "/final/"}}
        assert url == "https://careers.example.com/final/"
        return _jsonld_page(url="job/1", company="Example Employer")

    first = advance_radar_queue(conn, transport=transport)
    second = advance_radar_queue(conn, transport=transport)

    assert first["attempts"][0]["final_url"] == "https://careers.example.com/final/"
    assert conn.execute("SELECT url FROM jobs").fetchone()[0] == (
        "https://careers.example.com/final/job/1"
    )
    assert second["attempted"] == 0
    close_connection(db_path)


def test_redirect_to_known_aggregator_is_blocked_before_jsonld_verification(tmp_path):
    db_path = tmp_path / "radar.db"
    conn = init_db(db_path)
    _add_lead(
        conn,
        "https://jobs.example.com/start",
        company="Expected Employer",
    )

    def transport(url, headers=None):
        if url == "https://jobs.example.com/start":
            return {
                "status_code": 302,
                "headers": {"Location": "https://www.linkedin.com/jobs/view/123"},
            }
        return _jsonld_page(url=url, company="Expected Employer")

    result = advance_radar_queue(conn, transport=transport)

    assert result["attempts"][0]["status"] == "pending"
    assert "portal" in result["attempts"][0]["reason"]
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    close_connection(db_path)


def test_advance_rejects_limit_above_hard_cap(tmp_path):
    conn = init_db(tmp_path / "radar.db")
    with pytest.raises(ValueError, match="between 1 and 10"):
        advance_radar_queue(conn, limit=11, transport=lambda *_args: "")
    close_connection(tmp_path / "radar.db")
