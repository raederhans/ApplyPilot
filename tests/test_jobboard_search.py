from __future__ import annotations

import pandas as pd
import pytest

from applypilot.discovery import jobspy


@pytest.mark.parametrize("site", ["linkedin", "indeed"])
def test_search_job_board_uses_one_site_with_platform_specific_parameters(monkeypatch, site):
    calls = []

    def fake_scrape(kwargs, timeout_seconds):
        calls.append((kwargs, timeout_seconds))
        return pd.DataFrame([
            {
                "job_url": "https://example.test/job/1",
                "title": "Analyst Intern",
                "company": "Example",
                "location": "Singapore",
                "description": "Role description",
                "job_url_direct": "https://example.test/apply/1",
            }
        ])

    monkeypatch.setattr(jobspy, "_scrape_once_with_timeout", fake_scrape)

    result = jobspy.search_job_board(
        "analyst intern",
        site,
        location="Singapore",
        country="Singapore",
        results_per_site=7,
        hours_old=24,
        timeout_seconds=9,
    )

    assert len(calls) == 1
    kwargs, timeout = calls[0]
    assert kwargs["site_name"] == [site]
    assert kwargs["results_wanted"] == 7
    assert kwargs["hours_old"] == 24
    assert timeout == 9
    if site == "linkedin":
        assert kwargs["linkedin_fetch_description"] is True
        assert "country_indeed" not in kwargs
    else:
        assert kwargs["country_indeed"] == "Singapore"
        assert "linkedin_fetch_description" not in kwargs
    assert result["status"] == "partial"
    assert result["coverage"] == "non_exhaustive"
    assert result["jobs"][0]["full_description"] == "Role description"


def test_search_job_board_reports_exception_as_error_not_empty_success(monkeypatch):
    def fail(_kwargs, _timeout_seconds):
        raise TimeoutError("blocked request")

    monkeypatch.setattr(jobspy, "_scrape_once_with_timeout", fail)

    result = jobspy.search_job_board("intern", "indeed")

    assert result["status"] == "error"
    assert result["jobs"] == []
    assert result["raw_count"] == 0
    assert "TimeoutError" in result["error"]


@pytest.mark.parametrize("site", ["linkedin", "indeed"])
def test_explicit_job_type_is_not_silently_ignored_by_indeed(monkeypatch, site):
    calls = []
    def scrape(kwargs, timeout):
        calls.append(kwargs)
        return pd.DataFrame()
    monkeypatch.setattr(jobspy, "_scrape_once_with_timeout", scrape)
    jobspy.search_job_board("analyst", site, job_type="internship")
    assert calls[0]["job_type"] == "internship"
    assert ("hours_old" in calls[0]) == (site == "linkedin")


def test_search_job_board_filters_invalid_rows_flags_missing_company_and_caps_results(monkeypatch):
    frame = pd.DataFrame([
        {"job_url": float("nan"), "title": "Invalid URL", "company": "A"},
        {"job_url": "https://example.test/no-title", "title": float("nan"), "company": "B"},
        {"job_url": "https://example.test/1", "title": "First", "company": float("nan")},
        {"job_url": "https://example.test/2", "title": "Second", "company": "C"},
    ])
    monkeypatch.setattr(jobspy, "_scrape_once_with_timeout", lambda _kwargs, _timeout: frame)

    result = jobspy.search_job_board("intern", "linkedin", results_per_site=1)

    assert result["raw_count"] == 4
    assert len(result["jobs"]) == 1
    assert result["jobs"][0]["company_name"] is None
    assert result["jobs"][0]["quality_issues"] == ["missing_company_name"]


def test_search_job_board_distinguishes_empty_response_from_error(monkeypatch):
    monkeypatch.setattr(
        jobspy,
        "_scrape_once_with_timeout",
        lambda _kwargs, _timeout: pd.DataFrame(),
    )

    result = jobspy.search_job_board("intern", "linkedin")

    assert result["status"] == "empty"
    assert result["error"] is None


def test_run_one_search_isolates_platform_failures(monkeypatch):
    calls = []

    def fake_scrape(kwargs, **_options):
        calls.append(kwargs["site_name"])
        if kwargs["site_name"] == ["linkedin"]:
            raise RuntimeError("linkedin unavailable")
        return pd.DataFrame([
            {
                "site": "indeed",
                "job_url": "https://example.test/indeed/1",
                "title": "Intern",
                "company": "Example",
                "location": "Singapore",
            }
        ])

    monkeypatch.setattr(jobspy, "_scrape_with_retry", fake_scrape)
    monkeypatch.setattr(jobspy, "get_connection", lambda: object())
    monkeypatch.setattr(jobspy, "store_jobspy_results", lambda *_args: (1, 0))

    result = jobspy._run_one_search(
        {"query": "intern", "location": "Singapore"},
        ["linkedin", "indeed"],
        10,
        168,
        None,
        {"country_indeed": "Singapore", "query_timeout_seconds": 30},
        0,
        ["singapore"],
        [],
        {},
        [],
    )

    assert calls == [["linkedin"], ["indeed"]]
    assert result["new"] == 1
    assert result["errors"] == 1
    assert result["sites"]["linkedin"]["status"] == "error"
    assert result["sites"]["indeed"]["status"] == "partial"
