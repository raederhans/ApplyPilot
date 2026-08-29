from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from applypilot.radar import (
    JobObservation,
    LeadStatus,
    OpportunityLead,
    SourceRun,
    SourceStatus,
    Track,
    build_linkedin_content_search_url,
    build_linkedin_query_matrix,
    classify_job_subtracks,
    conservative_dedupe_key,
    group_by_conservative_dedupe_key,
    parent_track,
    promote_lead,
    promote_observation,
    render_daily_report,
    split_linkedin_role_queries,
)


def _official_job(**overrides) -> JobObservation:
    values = {
        "source_id": "official-databricks",
        "source_kind": "official_ats",
        "source_url": "https://boards.example.com/jobs/42?utm_source=radar",
        "official_job_url": "https://boards.example.com/jobs/42?utm_source=radar",
        "company": "Databricks",
        "title": "Solutions Consultant",
        "location": "Singapore",
        "employment_type": "Full-time",
        "requisition_id": "REQ-42",
        "subtracks": ("pre-sales solution consulting",),
    }
    values.update(overrides)
    return JobObservation(**values)


@pytest.mark.parametrize(
    ("subtrack", "track"),
    [
        ("product management", Track.PRODUCT_CONSULTING),
        ("Business Intelligence", Track.DATA_BI_DECISION),
        ("workflow automation", Track.AI_IMPLEMENTATION),
        ("digital twin", Track.SPATIAL),
    ],
)
def test_four_stable_tracks_cover_new_subtracks(subtrack, track) -> None:
    assert parent_track(subtrack) is track


@pytest.mark.parametrize("window", ["past-24h", "past-week", "past-month"])
def test_linkedin_content_search_url_encodes_latest_window_and_query(window) -> None:
    url = build_linkedin_content_search_url('( "we are hiring" ) AND "product manager"', window=window)
    parsed = parse_qs(urlsplit(url).query)
    assert parsed["keywords"] == ['( "we are hiring" ) AND "product manager"']
    assert parsed["datePosted"] == [f'["{window}"]']
    assert parsed["sortBy"] == ['["date_posted"]']
    assert parsed["origin"] == ["FACETED_SEARCH"]


def test_linkedin_window_and_empty_query_fail_closed() -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        build_linkedin_content_search_url("hiring", window="2026-08-01")
    with pytest.raises(ValueError, match="cannot be empty"):
        build_linkedin_content_search_url("   ")


def test_linkedin_queries_are_single_role_deduplicated_and_targeted() -> None:
    queries = split_linkedin_role_queries(
        ["product manager", "Product Manager", "product ops", "solutions consultant"], max_role_terms=2
    )
    assert queries == (
        "hiring product manager Singapore",
        "hiring product ops Singapore",
        "hiring solutions consultant Singapore",
    )


def test_linkedin_boolean_style_remains_explicitly_available() -> None:
    queries = split_linkedin_role_queries(
        ["product manager", "product ops"],
        hiring_terms=['"we are hiring"'],
        location_terms=["Singapore"],
        max_role_terms=2,
        query_style="boolean",
    )
    assert queries == ('("we are hiring") AND ("product manager" OR "product ops") AND (Singapore)',)


def test_linkedin_hashtag_exact_style_targets_local_hiring_posts() -> None:
    queries = split_linkedin_role_queries(
        ["product manager", "solution engineer"],
        hiring_terms=["#hiring"],
        location_terms=["#singaporejobs"],
        query_style="hashtag_exact",
    )
    assert queries == (
        '#hiring "product manager" #singaporejobs',
        '#hiring "solution engineer" #singaporejobs',
    )


def test_linkedin_query_matrix_loads_all_four_tracks_from_shipped_config() -> None:
    config_path = Path(__file__).parents[1] / "src" / "applypilot" / "config" / "linkedin_searches.yaml"
    shipped_config = config_path.read_text(encoding="utf-8")
    # Parsing YAML belongs to the configuration layer; this pure-logic test
    # exercises the same shape while also guarding the shipped registry text.
    assert "general_product_consulting:" in shipped_config
    assert "data_bi_decision:" in shipped_config
    assert "ai_implementation:" in shipped_config
    assert "spatial:" in shipped_config
    matrix = build_linkedin_query_matrix(
        {
            "defaults": {"window": "past-24h", "hiring_terms": ['"we are hiring"'], "location_terms": ["Singapore"]},
            "tracks": {
                "general_product_consulting": {"product_management": ["product manager"]},
                "data_bi_decision": {"business_intelligence": ["business intelligence"]},
                "ai_implementation": {"workflow_automation": ["workflow automation"]},
                "spatial": {"digital_twin": ["digital twin"]},
            },
        }
    )
    assert {item["track"] for item in matrix} == {track.value for track in Track}
    assert {item["subtrack"] for item in matrix} >= {
        "product_management",
        "business_intelligence",
        "workflow_automation",
        "digital_twin",
    }
    assert all(item["window"] == "past-24h" for item in matrix)
    assert all("datePosted=%5B%22past-24h%22%5D" in item["url"] for item in matrix)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Senior Product Manager, APAC", "product_management"),
        ("Solutions Architect", "pre_sales_solution_consulting"),
        ("Business Intelligence Analyst", "business_intelligence"),
        ("Generative AI Engineer", "ai_solutions"),
        ("Transportation Planner", "transport_planning"),
        ("GIS Consultant", "geospatial"),
    ],
)
def test_official_title_classifier_covers_each_direction(title, expected) -> None:
    config_path = Path(__file__).parents[1] / "src" / "applypilot" / "config" / "linkedin_searches.yaml"
    query_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert expected in classify_job_subtracks(title, query_config)


def test_official_title_classifier_fails_closed_for_unrelated_role() -> None:
    config_path = Path(__file__).parents[1] / "src" / "applypilot" / "config" / "linkedin_searches.yaml"
    query_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert classify_job_subtracks("Senior Software Engineer", query_config) == ()


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("AI/Data Intern", {"data_analytics", "ai_solutions"}),
        ("Intern, Product Management", {"product_management"}),
        ("Consulting Intern (Jan - May 2027)", {"implementation_consulting"}),
        ("Data Scientist Intern", {"data_analytics"}),
        ("Data Science Internship", {"data_analytics"}),
        ("Analytics Engineer", {"data_analytics"}),
        ("Map Simulation and Annotation Intern", {"geospatial"}),
        ("Mapping/Localization Intern", {"geospatial"}),
        (
            "Autonomous Driving Data Analysis Intern",
            {"data_analytics", "geospatial"},
        ),
        ("Intern, OmniCommerce", {"product_ops"}),
        (
            "Intern, Commercial Enablement, Sales Enablement",
            {"pre_sales_solution_consulting"},
        ),
        ("Intern, Fleet Systems Analyst", {"workflow_automation"}),
        (
            "Intern Corporate Development and Strategy",
            {"strategy_ops"},
        ),
        ("Product Internship Program 2026", {"product_management"}),
        (
            "Machine Learning Engineer / Data Scientist (OCR/CV)",
            {"data_analytics", "ai_solutions"},
        ),
        ("Internship Programme - AI Product Engineering", {"ai_solutions"}),
        ("Intern, Data Engineering", {"data_analytics"}),
        ("AI/ML Ops and Software Engineering Intern", {"ai_solutions"}),
        ("Robotics AI Software Engineer Intern", {"ai_solutions"}),
    ],
)
def test_official_title_classifier_covers_high_precision_internship_phrases(
    title, expected
) -> None:
    config_path = Path(__file__).parents[1] / "src" / "applypilot" / "config" / "linkedin_searches.yaml"
    query_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert set(classify_job_subtracks(title, query_config)) >= expected


@pytest.mark.parametrize("title", ["Operations Intern", "Software Localization Intern"])
def test_official_title_classifier_rejects_ambiguous_internship_phrases(title) -> None:
    config_path = Path(__file__).parents[1] / "src" / "applypilot" / "config" / "linkedin_searches.yaml"
    query_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert classify_job_subtracks(title, query_config) == ()


def test_shipped_watchlist_registers_verified_p0_boards() -> None:
    config_path = Path(__file__).parents[1] / "src" / "applypilot" / "config" / "company_watchlist.yaml"
    companies = yaml.safe_load(config_path.read_text(encoding="utf-8"))["companies"]
    by_id = {company["id"]: company for company in companies}
    expected = {
        "temus": ("greenhouse", "board", "temus"),
        "straitsx": ("greenhouse", "board", "straitsx"),
        "workato": ("greenhouse", "board", "workato"),
        "simplifynext": ("greenhouse", "board", "simplifynext"),
        "geotab": ("greenhouse", "board", "geotab"),
        "shift_technology": ("greenhouse", "board", "shifttechnology"),
        "shopback": ("lever", "site", "shopback-2"),
        "portcast": ("lever", "site", "portcast"),
        "goto_group": ("lever", "site", "GoToGroup"),
        "venti_technologies": ("ashby", "board", "GoVenti"),
        "simular": ("ashby", "board", "simular"),
        "k_id": ("ashby", "board", "k-ID"),
    }
    for company_id, (provider, key, value) in expected.items():
        assert by_id[company_id]["active"] is True
        assert by_id[company_id]["provider"] == provider
        assert by_id[company_id][key] == value
        # Active sources participate in every daily completeness gate.  Until
        # the report has due-source scheduling semantics, a weekly active
        # source would make otherwise healthy daily reports look incomplete.
        assert by_id[company_id]["cadence"] == "daily"
        assert by_id[company_id]["track_tags"]


def test_shipped_watchlist_registers_p1_provider_canaries() -> None:
    config_path = Path(__file__).parents[1] / "src" / "applypilot" / "config" / "company_watchlist.yaml"
    companies = yaml.safe_load(config_path.read_text(encoding="utf-8"))["companies"]
    by_id = {company["id"]: company for company in companies}

    assert by_id["grab"]["provider"] == "smartrecruiters"
    assert by_id["grab"]["company_id"] == "Grab"
    assert by_id["grab"]["country"] == "sg"
    assert by_id["grab"]["active"] is True
    assert by_id["grab"]["cadence"] == "daily"

    assert by_id["porsche_asia_pacific"]["provider"] == "workable"
    assert by_id["porsche_asia_pacific"]["subdomain"] == "porsche-asia-pacific"
    assert by_id["porsche_asia_pacific"]["active"] is True
    assert by_id["porsche_asia_pacific"]["cadence"] == "daily"


def test_official_ats_listing_promotes_to_verified_job() -> None:
    decision = promote_observation(_official_job())
    assert decision.status is LeadStatus.PROMOTED
    assert decision.is_verified_job is True


@pytest.mark.parametrize(
    ("publisher", "open_target", "expected"),
    [
        (True, True, LeadStatus.PROMOTED),
        (True, None, LeadStatus.AWAITING_OFFICIAL),
        (False, True, LeadStatus.AWAITING_OFFICIAL),
    ],
)
def test_linkedin_posts_need_official_publisher_link_and_open_check(publisher, open_target, expected) -> None:
    observation = _official_job(
        source_id="linkedin-content",
        source_kind="linkedin_post",
        source_url="https://www.linkedin.com/posts/company_123",
        is_official_publisher=publisher,
        official_target_open=open_target,
    )
    assert promote_observation(observation).status is expected


def test_recruiter_and_forum_items_remain_leads() -> None:
    recruiter = _official_job(
        source_id="linkedin-content",
        source_kind="linkedin_post",
        source_url="https://www.linkedin.com/posts/recruiter_1",
        official_job_url=None,
        is_verified_recruiter=True,
    )
    forum = _official_job(
        source_id="forum-discourse",
        source_kind="forum",
        source_url="https://forum.example/t/jobs/1",
        official_job_url=None,
    )
    assert promote_observation(recruiter).status is LeadStatus.AWAITING_OFFICIAL
    assert promote_observation(forum).status is LeadStatus.AWAITING_OFFICIAL
    assert promote_lead(
        {
            "source": "forum-discourse",
            "kind": "forum",
            "url": "https://forum.example/t/jobs/1",
            "company": "Databricks",
            "title": "Solutions Consultant",
        }
    ).status is LeadStatus.AWAITING_OFFICIAL


def test_conservative_dedupe_prefers_requisition_then_url_then_full_candidate_key() -> None:
    assert conservative_dedupe_key(_official_job()).startswith("req:databricks:req-42")
    by_url = _official_job(requisition_id=None)
    assert conservative_dedupe_key(by_url) == "url:https://boards.example.com/jobs/42"
    candidate = _official_job(requisition_id=None, official_job_url=None)
    assert conservative_dedupe_key(candidate).startswith("candidate:databricks:solutions-consultant:singapore:full-time")
    incomplete = _official_job(requisition_id=None, official_job_url=None, location=None)
    assert conservative_dedupe_key(incomplete) is None


def test_unkeyed_observations_are_not_merged() -> None:
    first = _official_job(requisition_id=None, official_job_url=None, location=None)
    second = _official_job(
        requisition_id=None,
        official_job_url=None,
        location=None,
        source_url="https://boards.example.com/jobs/43",
    )
    assert len(group_by_conservative_dedupe_key([first, second])) == 2


def test_daily_report_allows_zero_only_after_complete_run() -> None:
    report = render_daily_report(
        source_runs=[SourceRun("official-google", SourceStatus.COMPLETE, observations_seen=0)],
    )
    assert "0 verified jobs (all relevant official source runs complete)" in report
    assert "unavailable: lead zero cannot be claimed (no lead source run)" in report


def test_daily_report_does_not_claim_applied_zero_without_fresh_complete_snapshot() -> None:
    report = render_daily_report(
        source_runs=[SourceRun("official-google", SourceStatus.COMPLETE)],
    )
    assert "LinkedIn Applied exclusion completeness is not freshly verified" in report
    assert "LinkedIn Applied exclusions: 0 matched listings" not in report


@pytest.mark.parametrize("status", [SourceStatus.PARTIAL, SourceStatus.BLOCKED])
def test_daily_report_calls_incomplete_source_unavailable_not_zero(status) -> None:
    report = render_daily_report(
        source_runs=[SourceRun("official-google", status, error="pagination incomplete")],
    )
    assert "unavailable: verified-job zero cannot be claimed" in report
    assert "0 verified jobs" not in report


def test_daily_report_separates_verified_jobs_from_leads() -> None:
    verified = _official_job()
    linkedin_lead = OpportunityLead(
        _official_job(
            source_id="linkedin-content",
            source_kind="linkedin_post",
            source_url="https://www.linkedin.com/posts/recruiter_1",
            official_job_url=None,
            is_verified_recruiter=True,
        ),
        LeadStatus.AWAITING_OFFICIAL,
        "verify company careers page",
    )
    report = render_daily_report(
        source_runs=[SourceRun("official-databricks", SourceStatus.COMPLETE, observations_seen=1)],
        observations=[verified],
        leads=[linkedin_lead],
    )
    assert "## Verified jobs by track" in report
    assert "## Leads awaiting official verification" in report
    assert "Solutions Consultant | Databricks" in report
    assert "verify company careers page" in report


def test_daily_report_retains_portal_provenance_for_candidate_official_url() -> None:
    report = render_daily_report(
        source_runs=[
            SourceRun(
                "careeraxis",
                SourceStatus.PARTIAL,
                source_kind="ecosystem_lead",
            )
        ],
        leads=[
            {
                "source": "careeraxis",
                "kind": "ecosystem_lead",
                "url": "https://careeraxis.ntu.edu.sg/students/jobs/861866",
                "official_job_url": "https://jobs.edp.com/job-invite/180475/",
                "company": "EDP APAC",
                "title": "Intern, Data Engineering",
                "location": "Singapore",
                "status": "awaiting_official",
            }
        ],
    )

    assert "https://jobs.edp.com/job-invite/180475" in report
    assert "lead source: careeraxis" in report
    assert "https://careeraxis.ntu.edu.sg/students/jobs/861866" in report


def test_daily_report_separates_company_seeds_from_job_leads() -> None:
    report = render_daily_report(
        source_runs=[
            {
                "source": "startup-sg-directory",
                "kind": "company_seed",
                "status": "partial",
                "count": 1,
                "pages": 1,
            }
        ],
        company_seeds=[
            {
                "company_name": "Example AI",
                "location": "Singapore",
                "official_url": "https://example.ai/",
                "sectors": ["AI", "urban technology"],
                "status": "awaiting_official_careers",
                "source_ids": ["startup-sg-directory"],
            }
        ],
    )

    assert "## Company seeds awaiting official careers verification" in report
    assert "Example AI | Singapore | AI, urban technology | https://example.ai/" in report
    assert "awaiting_official_careers; 1 sources: startup-sg-directory" in report
    assert "unavailable: lead zero cannot be claimed (no lead source run)" in report
    assert "Coverage: company directories are non-exhaustive" in report


def test_daily_report_accepts_serialisable_dicts_from_cli_or_db_layer() -> None:
    report = render_daily_report(
        source_runs=[{"source": "official-google", "status": "complete", "count": 1, "pages": 1}],
        observations=[
            {
                "source": "official-google",
                "kind": "official careers",
                "url": "https://careers.example/jobs/1?utm_source=radar",
                "company": "Google",
                "title": "Product Manager",
                "location": "Singapore",
                "employment_type": "full time",
                "subtrack": "product management",
            }
        ],
        leads=[
            {
                "source": "linkedin-content",
                "kind": "linkedin post",
                "url": "https://www.linkedin.com/posts/example",
                "company": "Google",
                "title": "Product Manager",
                "status": "awaiting_official",
            }
        ],
    )
    assert "Product Manager | Google" in report
    assert "awaiting_official" in report


def test_daily_report_turns_unpromoted_observation_into_lead_and_uses_kind_for_coverage() -> None:
    report = render_daily_report(
        source_runs=[{"source": "databricks", "kind": "official ats", "status": "partial"}],
        observations=[
            {
                "source": "linkedin-content",
                "kind": "linkedin post",
                "url": "https://www.linkedin.com/posts/example",
                "company": "Databricks",
                "title": "AI Solutions Consultant",
            }
        ],
    )
    assert "unavailable: verified-job zero cannot be claimed" in report
    assert "AI Solutions Consultant | Databricks" in report
    assert "LinkedIn post requires official-job verification" in report


def test_daily_report_shows_all_source_health_exclusions_and_dedupes_jobs() -> None:
    job = _official_job()
    duplicate = _official_job(
        source_id="official-databricks-jsonld",
        source_url="https://careers.example.com/jobs/42",
    )
    report = render_daily_report(
        source_runs=[
            {
                "source": "official-databricks",
                "kind": "official_ats",
                "status": "complete",
                "count": 1,
                "filtered": 8,
            },
            {
                "source": "linkedin-content-manual",
                "kind": "social_lead",
                "status": "partial",
                "error": "candidate-reviewed import is non-exhaustive",
            },
        ],
        observations=[job, duplicate],
    )
    assert "official-databricks: complete" in report
    assert "linkedin-content-manual: partial" in report
    assert "official-databricks: 8 excluded" in report
    assert report.count("Solutions Consultant | Databricks") == 1
    assert "### Product, consulting, and pre-sales" in report
    assert "not an application or published recommendation list" in report
