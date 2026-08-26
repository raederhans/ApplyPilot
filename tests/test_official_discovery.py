import json

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
                            "content": "<p>Use <strong>SQL</strong>.</p>",
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


def test_watchlist_keeps_unverified_company_pages_inactive_and_run_has_no_writes():
    companies = official.load_company_watchlist()
    active = [company for company in companies if company.get("active")]
    inactive = [company for company in companies if not company.get("active")]
    assert active
    assert {company["provider"] for company in active} == {"greenhouse", "ashby", "rss"}
    assert inactive and all(company.get("activation_note") for company in inactive)

    report = official.run_official_discovery(
        [active[0], inactive[0]],
        transport=lambda *_args, **_kwargs: {"status_code": 200, "text": '{"jobs": []}'},
    )
    assert report["read_only"] is True
    assert report["complete"] == 1
    assert report["skipped"] == [{"company_id": inactive[0]["id"], "reason": "inactive"}]
