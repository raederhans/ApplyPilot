from pathlib import Path

from applypilot import config
from applypilot.apply import launcher, prompt
from applypilot.database import init_db
from applypilot.enrichment import detail


def _store_ready_job(
    conn, *, url: str, source_site: str, application_url: str | None = None
) -> None:
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, source_site, site, application_url, "
        "tailored_resume_path, tailor_status, cover_letter_status, eligibility_status) "
        "VALUES (?, 'Data Analyst Intern', 'Example', ?, ?, ?, 'resume.txt', "
        "'machine_validated', 'not_required', 'eligible')",
        (url, source_site, source_site, application_url or url),
    )
    conn.commit()


def test_portal_policy_matches_domain_and_original_source_site() -> None:
    jobstreet = config.get_portal_policy("https://sg.jobstreet.com/data-analyst-jobs")
    assert jobstreet is not None
    assert jobstreet["application_mode"] == "manual_only"

    # JobStreet may hand the applicant to an employer ATS; the source remains
    # protected even after that navigation target changes domain.
    external_jobstreet = config.get_portal_policy(
        "https://careers.example.com/apply/123",
        source_site="JobStreet Singapore",
    )
    assert external_jobstreet is not None
    assert external_jobstreet["name"] == "JobStreet Singapore"

    internsg = config.get_portal_policy("https://www.internsg.com/job-apply/123/")
    assert internsg is not None
    assert internsg["application_mode"] == "review_only"

    career_axis = config.get_portal_policy(
        "https://careeraxis.ntu.edu.sg/students/jobs/882308"
    )
    assert career_axis is not None
    assert career_axis["application_mode"] == "standing_authorized"
    assert career_axis["discovery_mode"] == "visible_agent_browse"
    assert "bounded visible agent browsing" in config.portal_discovery_gate(
        "https://careeraxis.ntu.edu.sg/students/jobs/882308"
    )
    assert config.get_portal_policy(
        "https://careers.example.com/apply/123", source_site="Career Axis"
    ) == career_axis
    assert config.portal_application_gate(
        "https://careers.example.com/apply/123", source_site="Career Axis", preview_only=False
    ) is None
    assert config.portal_application_gate(
        "https://www.internsg.com/job-apply/123/", preview_only=True
    ) is None
    assert "must submit manually" in config.portal_application_gate(
        "https://www.internsg.com/job-apply/123/", preview_only=False
    )
    assert "handed off to an external ATS" in config.portal_application_gate(
        "https://careers.example.com/apply/123",
        source_site="InternSG",
        preview_only=True,
    )
    assert "authorised export" in config.portal_discovery_gate(
        "https://sg.jobstreet.com/data-analyst-jobs"
    )
    assert "external ATS" in prompt._build_portal_handoff_rule(
        {
            "url": "https://www.internsg.com/job-apply/123/",
            "source_site": "InternSG",
            "site": "InternSG",
        }
    )


def test_portal_policy_blocks_jobstreet_and_submission_but_allows_internsg_preview(
    monkeypatch, tmp_path: Path
) -> None:
    conn = init_db(tmp_path / "jobs.db")
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    jobstreet_url = "https://sg.jobstreet.com/data-analyst-jobs/job-123"
    _store_ready_job(
        conn,
        url=jobstreet_url,
        source_site="JobStreet Singapore",
        application_url="https://careers.example.com/apply/123",
    )
    assert launcher.acquire_job(target_url=jobstreet_url, preview_only=True) is None
    jobstreet_row = conn.execute(
        "SELECT apply_status, apply_error FROM jobs WHERE url=?", (jobstreet_url,)
    ).fetchone()
    assert tuple(jobstreet_row) == (
        "manual",
        "JobStreet Singapore requires a candidate-operated manual application.",
    )

    internsg_url = "https://www.internsg.com/job-apply/456/"
    _store_ready_job(conn, url=internsg_url, source_site="InternSG")
    assert launcher.acquire_job(target_url=internsg_url, preview_only=False) is None
    internsg_row = conn.execute(
        "SELECT apply_status, apply_error FROM jobs WHERE url=?", (internsg_url,)
    ).fetchone()
    assert tuple(internsg_row) == (
        "manual",
        "InternSG permits only a visible fill-only review; the candidate must submit manually.",
    )

    conn.execute("UPDATE jobs SET apply_status=NULL, apply_error=NULL WHERE url=?", (internsg_url,))
    conn.commit()
    acquired = launcher.acquire_job(target_url=internsg_url, preview_only=True)
    assert acquired is not None
    assert acquired["url"] == internsg_url


def test_authorised_portal_listing_csv_is_local_intake(monkeypatch, tmp_path: Path) -> None:
    from applypilot import single_job

    conn = init_db(tmp_path / "jobs.db")
    monkeypatch.setattr(single_job, "get_connection", lambda: conn)
    csv_path = tmp_path / "listings.csv"
    csv_path.write_text(
        "url,title,company,location,description,portal\n"
        "https://sg.jobstreet.com/data-analyst-jobs/job-789,Data Analyst Intern,Example,"
        "Singapore,Use SQL to analyse public data.,JobStreet Singapore\n",
        encoding="utf-8",
    )

    result = single_job.import_portal_listings(csv_path)

    assert result["imported"] == 1
    assert result["with_description"] == 1
    row = conn.execute(
        "SELECT source_site, strategy, full_description FROM jobs"
    ).fetchone()
    assert tuple(row) == (
        "JobStreet Singapore",
        "candidate_provided_portal_listing",
        "Use SQL to analyse public data.",
    )
    listings = single_job.list_portal_listings("JobStreet", limit=10)
    assert len(listings) == 1
    assert listings[0]["url"] == "https://sg.jobstreet.com/data-analyst-jobs/job-789"


def test_enrichment_never_fetches_a_portal_listing_that_requires_manual_intake(
    monkeypatch, tmp_path: Path
) -> None:
    conn = init_db(tmp_path / "jobs.db")
    url = "https://sg.jobstreet.com/data-analyst-jobs/job-999"
    conn.execute(
        "INSERT INTO jobs (url, title, source_site, site, eligibility_status) "
        "VALUES (?, 'Data Analyst Intern', 'JobStreet Singapore', 'JobStreet Singapore', 'eligible')",
        (url,),
    )
    conn.commit()
    monkeypatch.setattr(
        detail,
        "scrape_site_batch",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("portal must not be fetched")),
    )

    result = detail._run_detail_scraper(conn)

    assert result["processed"] == 0
    assert result["skipped_manual"] == 1
    row = conn.execute(
        "SELECT detail_scraped_at, detail_error FROM jobs WHERE url=?", (url,)
    ).fetchone()
    assert row[0] is not None
    assert "authorised export" in row[1]
