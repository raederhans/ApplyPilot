import json
import threading
import time

import pytest

from applypilot.discovery import official


def _company(provider: str, **extra):
    company = {
        "id": "example",
        "name": "Example Tech",
        "provider": provider,
        "track_tags": ["data_bi_decision"],
    }
    company.update(extra)
    return company


def test_greenhouse_normalises_jobs_and_records_complete_run():
    company = _company("greenhouse", board="example")

    def transport(url, headers=None):
        assert url.endswith("/example/jobs?content=true")
        assert headers["User-Agent"].startswith("ApplyPilotOfficialRadar")
        return {
            "status_code": 200,
            "text": json.dumps(
                {
                    "jobs": [
                        {
                            "id": 42,
                            "title": "Data Analyst Intern",
                            "absolute_url": "https://boards.greenhouse.io/example/jobs/42",
                            "location": {"name": "Singapore"},
                            "content": "&lt;p&gt;Use &lt;strong&gt;SQL&lt;/strong&gt;.&lt;/p&gt;",
                            "updated_at": "2026-08-24T09:00:00Z",
                        }
                    ]
                }
            ),
        }

    run = official.collect_company(company, transport)

    assert run["status"] == "complete"
    assert run["pages_scanned"] == 1
    assert run["raw_count"] == 1
    assert run["normalised_count"] == 1
    job = run["jobs"][0]
    assert job["title"] == "Data Analyst Intern"
    assert job["company_name"] == "Example Tech"
    assert job["location"] == "Singapore"
    assert job["url"] == "https://boards.greenhouse.io/example/jobs/42"
    assert job["external_id"] == "42"
    assert job["description"] == job["full_description"] == "Use SQL ."
    assert job["published_at"] == "2026-08-24T09:00:00Z"
    assert job["source_url"] == "https://boards-api.greenhouse.io/v1/boards/example/jobs?content=true"
    assert job["verification_status"] == "verified_official"


def test_lever_pagination_and_blocked_health_are_explicit():
    company = _company("lever", site="example")
    first_url = "https://api.lever.co/v0/postings/example?mode=json"
    second_url = "https://api.lever.co/v0/postings/example?page=2"

    def paged_transport(url, headers=None):
        if url == first_url:
            return {
                "status_code": 200,
                "text": json.dumps(
                    {
                        "data": [
                            {
                                "id": "one",
                                "text": "Product Operations Intern",
                            "hostedUrl": "https://jobs.example.test/one",
                            "applyUrl": "https://jobs.example.test/one/apply",
                            "categories": {"location": "Singapore", "commitment": "Internship"},
                            "salaryRange": {
                                "min": 1500,
                                "max": 2000,
                                "currency": "SGD",
                                "interval": "per-month-salary",
                            },
                            }
                        ],
                        "next": "?page=2",
                    }
                ),
            }
        assert url == second_url
        return {
            "status_code": 200,
            "text": json.dumps(
                {
                    "data": [
                        {
                            "id": "two",
                            "text": "Solutions Consultant",
                            "hostedUrl": "https://jobs.example.test/two",
                            "categories": {"location": "Singapore"},
                        }
                    ]
                }
            ),
        }

    run = official.collect_company(company, paged_transport)
    assert run["status"] == "complete"
    assert run["pages_scanned"] == 2
    assert run["raw_count"] == 2
    assert [job["job_id"] for job in run["jobs"]] == ["one", "two"]
    assert run["jobs"][0]["application_url"] == "https://jobs.example.test/one/apply"
    assert run["jobs"][0]["employment_type"] == "Internship"
    assert run["jobs"][0]["salary"] == "SGD 1500-2000 per-month-salary"

    blocked = official.collect_company(company, lambda *_args, **_kwargs: {"status_code": 429, "text": "slow down"})
    assert blocked["status"] == "blocked"
    assert blocked["jobs"] == []
    assert blocked["errors"] == [f"HTTP 429 for {first_url}"]


def test_json_pagination_refuses_cross_origin_and_enforces_page_limit():
    company = _company("lever", site="example")
    first_url = "https://api.lever.co/v0/postings/example?mode=json"
    calls = []

    def hostile_transport(url, headers=None):
        calls.append(url)
        return {
            "status_code": 200,
            "text": json.dumps({"data": [], "next": "https://metadata.invalid/secret"}),
        }

    cross_origin = official.collect_company(company, hostile_transport)
    assert calls == [first_url]
    assert cross_origin["status"] == "partial"
    assert "refused cross-origin" in cross_origin["error"]

    limited_company = _company("lever", site="example", max_pages=2)

    def endless_transport(url, headers=None):
        page = len([item for item in calls if item.startswith(first_url.split("?", 1)[0])])
        calls.append(url)
        return {
            "status_code": 200,
            "text": json.dumps({"data": [], "next": f"?page={page + 2}"}),
        }

    limited = official.collect_company(limited_company, endless_transport)
    assert limited["pages_scanned"] == 2
    assert limited["status"] == "partial"
    assert "configured limit (2 pages)" in limited["error"]


def test_ashby_rss_and_jsonld_are_read_only_and_report_partial_records():
    ashby_company = _company("ashby", board="example")
    ashby_urls = []
    ashby = official.collect_company(
        ashby_company,
        lambda url, **_kwargs: ashby_urls.append(url) or {
            "status_code": 200,
            "text": json.dumps(
                {
                    "jobs": [
                        {"id": "missing-url", "title": "Incomplete"},
                        {
                            "jobId": "ashby-1",
                            "title": "AI Solutions Intern",
                            "jobUrl": "https://jobs.example.test/ashby-1",
                            "applyUrl": "https://jobs.example.test/ashby-1/application",
                            "location": "Singapore",
                            "employmentType": "Intern",
                            "compensation": {
                                "scrapeableCompensationSalarySummary": "SGD 1,500 - 2,000"
                            },
                        },
                    ]
                }
            ),
        },
    )
    assert ashby["status"] == "partial"
    assert ashby["raw_count"] == 2
    assert ashby["normalised_count"] == 1
    assert ashby_urls == [
        "https://api.ashbyhq.com/posting-api/job-board/example?includeCompensation=true"
    ]
    assert ashby["jobs"][0]["application_url"].endswith("/application")
    assert ashby["jobs"][0]["salary"] == "SGD 1,500 - 2,000"

    rss_company = _company("rss", feed_url="https://jobs.example.test/feed.xml")
    rss = official.collect_company(
        rss_company,
        lambda *_args, **_kwargs: {
            "status_code": 200,
            "text": """<rss><channel><item><guid>rss-1</guid><title>Urban Data Intern</title>
            <link>https://jobs.example.test/rss-1</link><description><![CDATA[<p>GIS work</p>]]></description>
            <pubDate>2026-08-24</pubDate></item></channel></rss>""",
        },
    )
    assert rss["status"] == "complete"
    assert rss["jobs"][0]["description"] == "GIS work"

    grab_company = _company(
        "rss",
        feed_url="https://jobs.example.test/grab.xml",
    )
    grab = official.collect_company(
        grab_company,
        lambda *_args, **_kwargs: {
            "status_code": 200,
            "text": """<source><job><requisitionid>REQ-9</requisitionid>
            <title>Product Manager</title><url>https://jobs.example.test/grab-9</url>
            <city>Singapore</city><country>Singapore</country><date>2026-08-24</date>
            <description>Consumer product role</description></job></source>""",
        },
    )
    assert grab["status"] == "complete"
    assert grab["jobs"][0]["location"] == "Singapore, Singapore"
    assert grab["jobs"][0]["external_id"] == "REQ-9"

    latest_company = _company(
        "rss",
        feed_url="https://jobs.example.test/latest.xml",
        coverage_mode="latest_only",
    )
    latest = official.collect_company(
        latest_company,
        lambda *_args, **_kwargs: {
            "status_code": 200,
            "text": "<rss><channel></channel></rss>",
        },
    )
    assert latest["status"] == "partial"
    assert latest["pagination_complete"] is False
    assert "latest items only" in latest["error"]

    jsonld_company = _company("jobposting_jsonld", career_url="https://careers.example.test/job/1")
    page = """<html><script type='application/ld+json'>
      {"@graph":[{"@type":"JobPosting","title":"Pre-sales Engineer","url":"/job/1",
      "identifier":{"value":"req-1"},"jobLocation":{"address":{"addressLocality":"Singapore","addressCountry":"SG"}},
      "employmentType":"INTERN","datePosted":"2026-08-24"}]}
    </script></html>"""
    jsonld = official.collect_company(
        jsonld_company, lambda *_args, **_kwargs: {"status_code": 200, "text": page}
    )
    assert jsonld["status"] == "complete"
    assert jsonld["jobs"][0]["url"] == "https://careers.example.test/job/1"
    assert jsonld["jobs"][0]["location"] == "Singapore, SG"
    assert jsonld["jobs"][0]["job_id"] == "req-1"


def test_smartrecruiters_paginates_enriches_details_and_normalises_fields():
    company = _company(
        "smartrecruiters",
        company_id="Grab",
        country="sg",
        limit=2,
    )
    first_url = "https://api.smartrecruiters.com/v1/companies/Grab/postings?limit=2&offset=0&country=sg"
    second_url = "https://api.smartrecruiters.com/v1/companies/Grab/postings?limit=2&offset=2&country=sg"
    detail_urls = [
        f"https://api.smartrecruiters.com/v1/companies/Grab/postings/job-{number}"
        for number in range(1, 4)
    ]
    calls = []

    def transport(url, headers=None):
        calls.append(url)
        if url == first_url:
            return {
                "status_code": 200,
                "text": json.dumps(
                    {
                        "totalFound": 3,
                        "content": [
                            {
                                "id": "job-1",
                                "name": "Strategy Intern",
                                "ref": detail_urls[0],
                                "location": {"fullLocation": "Singapore"},
                                "releasedDate": "2026-08-27T00:00:00Z",
                            },
                            {
                                "id": "job-2",
                                "name": "Product Operations Intern",
                                "ref": detail_urls[1],
                                "location": {"fullLocation": "Singapore"},
                            },
                        ],
                    }
                ),
            }
        if url == second_url:
            return {
                "status_code": 200,
                "text": json.dumps(
                    {
                        "totalFound": 3,
                        "content": [
                            {
                                "id": "job-3",
                                "name": "Data Analyst Intern",
                                "ref": detail_urls[2],
                                "location": {"fullLocation": "Singapore"},
                            }
                        ],
                    }
                ),
            }
        assert url in detail_urls
        number = detail_urls.index(url) + 1
        return {
            "status_code": 200,
            "text": json.dumps(
                {
                    "id": f"job-{number}",
                    "uuid": f"00000000-0000-4000-8000-{number:012d}",
                    "name": ["Strategy Intern", "Product Operations Intern", "Data Analyst Intern"][number - 1],
                    "postingUrl": f"https://jobs.smartrecruiters.com/Grab/job-{number}",
                    "applyUrl": f"https://jobs.smartrecruiters.com/Grab/job-{number}/apply",
                    "refNumber": f"REF-{number}",
                    "location": (
                        {"city": "Singapore", "region": "Singapore", "country": "sg"}
                        if number == 2
                        else {"fullLocation": "Singapore, Singapore"}
                    ),
                    "typeOfEmployment": {"label": "Intern"},
                    "function": {"label": "Data Analyst"},
                    "releasedDate": "2026-08-27T00:00:00Z",
                    "jobAd": {
                        "sections": {
                            "jobDescription": {"text": "<p>Build <strong>SQL</strong> dashboards.</p>"},
                            "qualifications": {"text": "Python"},
                        }
                    },
                }
            ),
        }

    run = official.collect_company(company, transport)

    assert run["status"] == "complete"
    assert run["pagination_complete"] is True
    assert run["pages_scanned"] == 2
    assert run["raw_count"] == run["normalised_count"] == 3
    assert calls == [first_url, detail_urls[0], detail_urls[1], second_url, detail_urls[2]]
    assert run["metadata"]["provider_identifier"] == "Grab"
    job = run["jobs"][0]
    assert job["url"] == "https://jobs.smartrecruiters.com/Grab/job-1"
    assert job["application_url"].endswith("/job-1/apply")
    assert job["source_url"] == detail_urls[0]
    assert job["location"] == "Singapore"
    assert job["description"] == "Build SQL dashboards. Python"
    assert job["employment_type"] == "Intern"
    assert job["requisition_id"] == "REF-1"
    assert job["provider_application_id"] == "00000000-0000-4000-8000-000000000001"
    assert run["jobs"][1]["location"] == "Singapore, sg"


def test_smartrecruiters_detail_fetch_uses_bounded_configured_concurrency() -> None:
    company = _company(
        "smartrecruiters",
        company_id="Grab",
        detail_concurrency=2,
    )
    list_url = (
        "https://api.smartrecruiters.com/v1/companies/Grab/postings?limit=100&offset=0"
    )
    detail_base = "https://api.smartrecruiters.com/v1/companies/Grab/postings/"
    lock = threading.Lock()
    active = 0
    peak = 0

    def transport(url, headers=None):
        nonlocal active, peak
        if url == list_url:
            return {
                "status_code": 200,
                "text": json.dumps(
                    {
                        "totalFound": 4,
                        "content": [
                            {
                                "id": f"job-{index}",
                                "name": f"Job {index}",
                                "ref": f"{detail_base}job-{index}",
                            }
                            for index in range(4)
                        ],
                    }
                ),
            }
        identity = url.rsplit("/", 1)[-1]
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.03)
            return {
                "status_code": 200,
                "text": json.dumps(
                    {
                        "id": identity,
                        "name": identity,
                        "postingUrl": f"https://jobs.smartrecruiters.com/Grab/{identity}",
                    }
                ),
            }
        finally:
            with lock:
                active -= 1

    run = official.collect_company(company, transport)

    assert run["status"] == "complete"
    assert peak == 2
    assert [job["job_id"] for job in run["jobs"]] == [
        "job-0",
        "job-1",
        "job-2",
        "job-3",
    ]


def test_smartrecruiters_detail_failure_keeps_summary_and_refuses_hostile_ref():
    company = _company("smartrecruiters", company_id="Grab")
    list_url = "https://api.smartrecruiters.com/v1/companies/Grab/postings?limit=100&offset=0"
    good_ref = "https://api.smartrecruiters.com/v1/companies/Grab/postings/good"
    calls = []

    def transport(url, headers=None):
        calls.append(url)
        if url == list_url:
            return {
                "status_code": 200,
                "text": json.dumps(
                    {
                        "totalFound": 2,
                        "content": [
                            {
                                "id": "good",
                                "name": "Mapping Intern",
                                "ref": good_ref,
                                "location": {"fullLocation": "Singapore"},
                            },
                            {
                                "id": "hostile",
                                "name": "Hostile Ref",
                                "ref": "https://metadata.invalid/secret",
                            },
                        ],
                    }
                ),
            }
        assert url == good_ref
        return {"status_code": 503, "text": "unavailable"}

    run = official.collect_company(company, transport)

    assert calls == [list_url, good_ref]
    assert run["status"] == "partial"
    assert run["pagination_complete"] is False
    assert run["raw_count"] == 2
    assert run["normalised_count"] == 0
    assert run["jobs"] == []
    assert "HTTP 503 for SmartRecruiters detail" in run["error"]
    assert "safe same-origin detail ref" in run["error"]
    assert good_ref not in [job["url"] for job in run["jobs"]]


def test_smartrecruiters_reports_blocked_malformed_and_incomplete_pagination():
    company = _company("smartrecruiters", company_id="Grab", limit=1)
    first_url = "https://api.smartrecruiters.com/v1/companies/Grab/postings?limit=1&offset=0"

    blocked = official.collect_company(
        company,
        lambda *_args, **_kwargs: {"status_code": 429, "text": "slow down"},
    )
    assert blocked["status"] == "blocked"
    assert blocked["pages_scanned"] == 0
    assert blocked["error"] == f"HTTP 429 for {first_url}"

    malformed = official.collect_company(
        company,
        lambda *_args, **_kwargs: {"status_code": 200, "text": json.dumps({"content": []})},
    )
    assert malformed["status"] == "partial"
    assert "valid totalFound" in malformed["error"]

    empty_before_total = official.collect_company(
        company,
        lambda *_args, **_kwargs: {
            "status_code": 200,
            "text": json.dumps({"totalFound": 1, "content": []}),
        },
    )
    assert empty_before_total["status"] == "partial"
    assert "empty page before totalFound" in empty_before_total["error"]

    detail_base = "https://api.smartrecruiters.com/v1/companies/Grab/postings/"

    def overlapping_transport(url, headers=None):
        if "offset=0" in url:
            content = [
                {"id": identity, "name": identity.upper(), "ref": f"{detail_base}{identity}"}
                for identity in ("a", "b")
            ]
            return {"status_code": 200, "text": json.dumps({"totalFound": 3, "offset": 0, "content": content})}
        if "offset=2" in url:
            return {
                "status_code": 200,
                "text": json.dumps(
                    {
                        "totalFound": 3,
                        "offset": 2,
                        "content": [{"id": "b", "name": "B", "ref": f"{detail_base}b"}],
                    }
                ),
            }
        identity = url.rsplit("/", 1)[-1]
        return {
            "status_code": 200,
            "text": json.dumps(
                {
                    "id": identity,
                    "name": identity.upper(),
                    "postingUrl": f"https://jobs.smartrecruiters.com/Grab/{identity}",
                }
            ),
        }

    overlapping = official.collect_company(
        _company("smartrecruiters", company_id="Grab", limit=2),
        overlapping_transport,
    )
    assert overlapping["status"] == "partial"
    assert overlapping["raw_count"] == 3
    assert overlapping["normalised_count"] == 2
    assert "repeated posting" in overlapping["error"]
    assert "unique posting count (2) did not match totalFound (3)" in overlapping["error"]


def test_workable_reads_public_details_collection_and_maps_job_and_apply_urls():
    company = _company("workable", subdomain="porsche-asia-pacific")
    url = "https://www.workable.com/api/accounts/porsche-asia-pacific?details=true"
    calls = []

    def transport(actual_url, headers=None):
        calls.append(actual_url)
        return {
            "status_code": 200,
            "text": json.dumps(
                {
                    "name": "Porsche Asia Pacific",
                    "jobs": [
                        {
                            "title": "Business Analytics Intern",
                            "code": "BA-1",
                            "shortcode": "ABC123",
                            "city": "Singapore",
                            "country": "Singapore",
                            "published_on": "2026-08-27",
                            "url": "https://apply.workable.com/porsche-asia-pacific/j/ABC123/",
                            "application_url": "https://apply.workable.com/porsche-asia-pacific/j/ABC123/apply/",
                            "shortlink": "https://wrkbl.ink/ABC123",
                            "description": "<p>Build dashboards.</p>",
                            "employment_type": "Internship",
                        },
                        {
                            "title": "Product Intern",
                            "code": "BA-1",
                            "shortcode": "XYZ789",
                            "country": "Singapore",
                            # Workable's field table describes the inverse of
                            # the current Porsche live payload.  Path semantics
                            # must win for either provider response shape.
                            "url": "https://apply.workable.com/porsche-asia-pacific/j/XYZ789/apply/",
                            "application_url": "https://apply.workable.com/porsche-asia-pacific/j/XYZ789/",
                        },
                    ],
                }
            ),
        }

    run = official.collect_company(company, transport)

    assert calls == [url]
    assert run["status"] == "complete"
    assert run["pages_scanned"] == 1
    assert run["raw_count"] == run["normalised_count"] == 2
    assert run["metadata"]["provider_identifier"] == "porsche-asia-pacific"
    job = run["jobs"][0]
    assert job["job_id"] == "ABC123"
    assert job["url"] == "https://apply.workable.com/porsche-asia-pacific/j/ABC123/"
    assert job["canonical_url"] == "https://apply.workable.com/porsche-asia-pacific/j/ABC123"
    assert job["application_url"] == "https://apply.workable.com/porsche-asia-pacific/j/ABC123/apply/"
    assert job["location"] == "Singapore"
    assert job["description"] == "Build dashboards."
    assert job["published_at"] == "2026-08-27"
    assert job["employment_type"] == "Internship"
    assert [item["job_id"] for item in run["jobs"]] == ["ABC123", "XYZ789"]
    assert run["jobs"][1]["url"].endswith("/j/XYZ789/")
    assert run["jobs"][1]["application_url"].endswith("/j/XYZ789/apply/")


def test_workable_reports_blocked_partial_and_unexpected_pagination():
    company = _company("workable", subdomain="example")

    blocked = official.collect_company(
        company,
        lambda *_args, **_kwargs: {"status_code": 403, "text": "forbidden"},
    )
    assert blocked["status"] == "blocked"

    malformed = official.collect_company(
        company,
        lambda *_args, **_kwargs: {"status_code": 200, "text": json.dumps({"name": "Example"})},
    )
    assert malformed["status"] == "partial"
    assert "missing jobs list" in malformed["error"]

    paginated = official.collect_company(
        company,
        lambda *_args, **_kwargs: {
            "status_code": 200,
            "text": json.dumps(
                {
                    "jobs": [
                        {
                            "title": "Incomplete",
                            "code": "missing-url",
                        },
                        {
                            "title": "Valid",
                            "shortcode": "valid",
                            "application_url": "https://apply.workable.com/example/j/valid/",
                        },
                    ],
                    "paging": {"next": "https://example.invalid/page-2"},
                }
            ),
        },
    )
    assert paginated["status"] == "partial"
    assert paginated["raw_count"] == 2
    assert paginated["normalised_count"] == 1
    assert "unexpectedly indicated pagination" in paginated["error"]
    assert "without title or public URL" in paginated["error"]


def test_watchlist_keeps_unverified_company_pages_inactive_and_run_has_no_writes():
    companies = official.load_company_watchlist()
    active = [company for company in companies if company.get("active")]
    inactive = [company for company in companies if not company.get("active")]
    assert active
    assert {"greenhouse", "lever", "ashby", "rss", "smartrecruiters", "workable"} <= {
        company["provider"] for company in active
    }
    assert inactive and all(company.get("activation_note") for company in inactive)

    report = official.run_official_discovery(
        [active[0], inactive[0]],
        transport=lambda *_args, **_kwargs: {"status_code": 200, "text": '{"jobs": []}'},
    )
    assert report["read_only"] is True
    assert report["complete"] == 1
    assert report["skipped"] == [{"company_id": inactive[0]["id"], "reason": "inactive"}]


def test_watchlist_fails_before_network_for_invalid_active_provider_config(tmp_path):
    invalid = tmp_path / "invalid-watchlist.yaml"
    invalid.write_text(
        """companies:
  - id: grab
    name: Grab
    provider: smartrecruiters
    identifier: Grab
    cadence: daily
    active: true
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="requires canonical key company_id"):
        official.load_company_watchlist(str(invalid))

    weekly = tmp_path / "weekly-watchlist.yaml"
    weekly.write_text(
        """companies:
  - id: example
    name: Example
    provider: greenhouse
    board: example
    cadence: weekly
    active: true
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must use daily cadence"):
        official.load_company_watchlist(str(weekly))
