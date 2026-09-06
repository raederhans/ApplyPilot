"""Exact previews must report their own gate before starting a browser worker."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from applypilot import cli
from applypilot.database import init_db


@pytest.fixture
def preview_runtime(tmp_path, monkeypatch):
    connection = init_db(tmp_path / "preview.db")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({}), encoding="utf-8")
    launches = []
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr("applypilot.config.PROFILE_PATH", profile)
    monkeypatch.setattr("applypilot.database.get_connection", lambda: connection)
    monkeypatch.setattr("applypilot.services.application.resolve_apply_backend", lambda *_: "codex")
    monkeypatch.setattr("applypilot.services.application.resolve_apply_model", lambda *_: "test-model")
    monkeypatch.setattr("applypilot.apply.chrome.get_browser_executable", lambda *_: "edge")
    monkeypatch.setattr("applypilot.apply.chrome.resolve_browser_backend", lambda *_: "edge")
    monkeypatch.setattr("applypilot.apply.router.resolve_interaction_mode", lambda *_: "playwright")
    monkeypatch.setattr("shutil.which", lambda *_: "codex")
    monkeypatch.setattr("applypilot.apply.launcher.main", lambda **kwargs: launches.append(kwargs))
    yield connection, launches
    connection.close()


def _insert_job(connection, url, application_url, source="official_careers"):
    connection.execute(
        "INSERT INTO jobs (url, application_url, source_site, site, title, company_name, "
        "full_description, tailored_resume_path, tailor_status, cover_letter_status, "
        "eligibility_status, fit_score) "
        "VALUES (?, ?, ?, ?, 'Data Intern', 'Example', 'Verified official job description', "
        "'resume.pdf', 'machine_validated', 'not_required', 'eligible', 8)",
        (url, application_url, source, source),
    )
    connection.commit()


def test_other_ready_job_cannot_mask_exact_preview_target_rejection(preview_runtime):
    connection, launches = preview_runtime
    target = "https://www.linkedin.com/jobs/view/1001"
    _insert_job(connection, target, "https://unverified.example.test/apply/1001", "linkedin")
    _insert_job(connection, "https://careers.example.test/1002", "https://jobs.lever.co/example/1002")
    before = [tuple(row) for row in connection.execute("SELECT * FROM jobs ORDER BY url")]

    result = CliRunner().invoke(cli.app, ["apply", "--dry-run", "--url", target])

    assert result.exit_code == 1, result.output
    assert "unverified_linkedin_external_target" in result.output
    assert "Launching" not in result.output
    assert launches == []
    assert [tuple(row) for row in connection.execute("SELECT * FROM jobs ORDER BY url")] == before


@pytest.mark.parametrize("requested_by", ["url", "application_url"])
def test_exact_preview_accepts_only_its_resolved_job(preview_runtime, requested_by):
    connection, launches = preview_runtime
    urls = {
        "url": "https://careers.example.test/1001",
        "application_url": "https://jobs.lever.co/example/1001",
    }
    _insert_job(connection, urls["url"], urls["application_url"])

    result = CliRunner().invoke(cli.app, ["apply", "--dry-run", "--url", urls[requested_by]])

    assert result.exit_code == 0, result.output
    assert len(launches) == 1
    assert launches[0]["target_url"] == urls[requested_by]
    assert launches[0]["limit"] == 1
    assert launches[0]["dry_run"] is True


@pytest.mark.parametrize("matches", [0, 2])
def test_exact_preview_rejects_missing_or_ambiguous_target(preview_runtime, matches):
    connection, launches = preview_runtime
    target = "https://jobs.lever.co/example/shared"
    _insert_job(connection, "https://careers.example.test/other", "https://jobs.lever.co/example/other")
    for index in range(matches):
        _insert_job(connection, f"https://careers.example.test/{index}", target)

    result = CliRunner().invoke(cli.app, ["apply", "--dry-run", "--url", target])

    assert result.exit_code == 1, result.output
    assert f"found {matches}" in result.output
    assert launches == []
