from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from applypilot.database import close_connection, init_db
from applypilot.discovery.explore import (
    board_search_url,
    default_exploration_queries,
    explore_job_boards,
)


@pytest.fixture
def conn(tmp_path):
    close_connection()
    connection = init_db(tmp_path / "explore.db")
    yield connection
    close_connection()


def test_isolated_board_failure_and_leads_never_create_jobs(conn):
    calls = []

    def search(query, site, **kwargs):
        calls.append((query, site))
        if site == "linkedin":
            raise TimeoutError("bounded timeout")
        return {"status": "partial", "raw_count": 2, "jobs": [
            {"url": "https://sg.indeed.com/viewjob?jk=one", "title": "Product Intern",
             "company_name": "Small Company", "full_description": "Role context",
             "application_url": "https://small.example/careers/one"},
            {"url": "https://sg.indeed.com/viewjob?jk=two", "title": "Data Intern"},
        ]}

    result = explore_job_boards(conn, queries=["intern"], search=search)
    assert calls == [("intern", "linkedin"), ("intern", "indeed")]
    assert [s["search_status"] for s in result["sources"]] == ["error", "partial"]
    assert len(result["needs_metadata_review"]) == 1
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    lead = conn.execute("SELECT * FROM radar_leads").fetchone()
    assert lead["status"] == "awaiting_official"
    assert lead["official_job_url"] == "https://small.example/careers/one"
    assert [r[0] for r in conn.execute("SELECT status FROM radar_fetch_runs ORDER BY rowid")] == ["failed", "partial"]


def test_empty_is_not_platform_complete_and_validation_precedes_search(conn):
    result = explore_job_boards(conn, queries=["intern"], sites=["indeed"], search=lambda *a, **k: {
        "status": "empty", "jobs": [], "raw_count": 0,
    })
    assert result["sources"][0]["search_status"] == "empty"
    assert result["sources"][0]["coverage"] == "non_exhaustive"
    assert conn.execute("SELECT pagination_complete FROM radar_fetch_runs").fetchone()[0] == 0
    with pytest.raises(ValueError):
        explore_job_boards(conn, queries=["a", "b", "c", "d"])
    with pytest.raises(ValueError):
        explore_job_boards(conn, sites=["unsupported"])
    with pytest.raises(ValueError):
        explore_job_boards(conn, hours_old=0)


def test_recent_window_is_forwarded_but_remains_unverified(conn):
    calls = []

    def search(query, site, **kwargs):
        calls.append((site, kwargs["hours_old"]))
        return {"status": "empty", "jobs": [], "raw_count": 0}

    result = explore_job_boards(
        conn, queries=["data intern"], hours_old=8, search=search,
    )

    assert calls == [("linkedin", 8), ("indeed", 8)]
    linkedin, indeed = result["sources"]
    assert "f_TPR=r28800" in linkedin["search_url"]
    assert "fromage=1" in indeed["search_url"]
    assert linkedin["requested_time_filter_hours"] == 8
    assert linkedin["time_filter_hours"] is None
    assert indeed["time_filter_hours"] is None
    assert linkedin["provider_requested_time_filter_hours"] == 8
    assert indeed["provider_requested_time_filter_hours"] == 8
    assert linkedin["search_url_requested_time_filter_hours"] == 8
    assert indeed["search_url_requested_time_filter_hours"] == 24
    assert linkedin["time_filter_status"] == "requires_visible_verification"
    assert "treat reposted separately" in linkedin["next_action"]


def test_indeed_job_type_reports_omitted_time_filter(conn):
    calls = []

    def search(query, site, **kwargs):
        calls.append(kwargs)
        return {"status": "empty", "jobs": [], "raw_count": 0}

    result = explore_job_boards(
        conn,
        queries=["business analyst"],
        sites=["indeed"],
        job_type="internship",
        hours_old=24,
        search=search,
    )

    source = result["sources"][0]
    assert calls[0]["hours_old"] == 24
    assert source["requested_time_filter_hours"] == 24
    assert source["time_filter_hours"] is None
    assert source["provider_requested_time_filter_hours"] is None
    assert source["search_url_requested_time_filter_hours"] is None
    assert source["time_filter_status"] == "omitted_for_indeed_job_type"
    assert "jt=internship" in source["search_url"]
    assert "fromage=" not in source["search_url"]


def test_board_search_defaults_to_recent_24_hours():
    assert "f_TPR=r86400" in board_search_url("linkedin", "product intern")
    assert "fromage=1" in board_search_url("indeed", "product intern")


def test_explore_cli_forwards_optional_hour_window(monkeypatch):
    from typer.testing import CliRunner

    from applypilot import cli
    from applypilot.commands import radar as radar_commands

    captured = {}
    monkeypatch.setattr(
        radar_commands,
        "run_radar_explore",
        lambda _runtime, values: captured.update(values),
    )

    result = CliRunner().invoke(
        cli.app,
        ["radar", "explore", "--query", "data intern", "--hours", "8"],
    )

    assert result.exit_code == 0, result.output
    assert captured["hours"] == 8
    assert captured["query"] == ["data intern"]


def test_rotating_queries_cover_all_four_fields():
    start = date(2026, 9, 5)
    pairs = [tuple(default_exploration_queries(start + timedelta(days=i))) for i in range(4)]
    assert len(set(pairs)) == 4


def test_diversity_selects_from_bounded_superset_before_truncating(conn):
    calls = []
    def search(query, site, **kwargs):
        calls.append(kwargs["results_per_site"])
        return {"status": "partial", "raw_count": 4, "jobs": [
            {"url": f"https://sg.indeed.com/viewjob?jk={i}", "title": "Intern",
             "company_name": company}
            for i, company in enumerate(["Same", "Same", "Other", "Third"])
        ]}
    explore_job_boards(conn, queries=["intern"], sites=["indeed"], results_per_site=2, search=search)
    assert calls == [4]
    companies = [r[0] for r in conn.execute("SELECT company_id FROM radar_leads")]
    assert companies == ["Same", "Other"]


def test_unclassified_official_title_survives_for_later_assessment(conn, monkeypatch):
    from typer.testing import CliRunner

    from applypilot import cli, config, database, radar
    from applypilot.discovery import official

    monkeypatch.setattr(cli, "_radar_bootstrap", lambda: None)
    monkeypatch.setattr(database, "get_connection", lambda: conn)
    monkeypatch.setattr(config, "load_radar_config", dict)
    monkeypatch.setattr(config, "radar_location_is_accepted", lambda *a, **k: True)
    monkeypatch.setattr(config, "title_is_excluded", lambda *a, **k: False)
    monkeypatch.setattr(radar, "classify_job_subtracks", lambda *a, **k: [])
    monkeypatch.setattr(official, "load_company_watchlist", lambda: [
        {"id": "small", "name": "Small", "provider": "jobposting_jsonld", "active": True},
    ])
    monkeypatch.setattr(official, "collect_company", lambda c: {
        "status": "complete", "raw_count": 1, "jobs": [{
            "url": "https://small.example/job/1", "title": "Marketplace Enablement Intern",
            "company_name": "Small", "location": "Singapore",
            "verification_status": "verified_official",
        }],
    })
    result = CliRunner().invoke(cli.app, ["radar", "collect"])
    assert result.exit_code == 0, result.output
    assert conn.execute("SELECT title FROM jobs").fetchone()[0] == "Marketplace Enablement Intern"


def test_only_explicit_review_session_can_issue_target_attestation(conn, monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from applypilot import cli, database

    monkeypatch.setenv("APPLYPILOT_ATTENDED_REVIEW", "1")
    monkeypatch.setattr(cli, "_radar_bootstrap", lambda: None)
    monkeypatch.setattr(cli, "_assert_discovery_storage_path", lambda *a: None)
    monkeypatch.setattr(database, "get_connection", lambda: conn)
    file = tmp_path / "reviewed.json"
    file.write_text(json.dumps([{
        "source_url": "https://sg.indeed.com/viewjob?jk=review", "title": "Intern",
        "company_name": "Small", "official_job_url": "https://small.example/job/1",
        "official_target_review": {"method": "source_claim"},
    }]), encoding="utf-8")
    args = ["radar", "import-leads", "--source-id", "indeed-jobs", "--file", str(file)]
    runner = CliRunner()
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    payload = json.loads(conn.execute("SELECT payload_json FROM radar_source_observations").fetchone()[0])
    assert "official_target_review" not in payload
    result = runner.invoke(cli.app, [*args, "--official-targets-reviewed"])
    assert result.exit_code == 0, result.output
    payload = json.loads(conn.execute("SELECT payload_json FROM radar_source_observations").fetchone()[0])
    assert payload["official_target_review"]["method"] == "agent_visible_employer_review"
    assert payload["official_target_review"]["url"] == "https://small.example/job/1"
