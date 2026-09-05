from __future__ import annotations

import pytest

from applypilot.discovery.ecosystem import (
    company_seed_identity,
    get_ecosystem_source,
    load_ecosystem_sources,
    normalize_company_seed,
    normalize_job_lead,
    radar_source_descriptor,
)


def test_registry_classifies_sources_and_record_kinds() -> None:
    sources = {source["id"]: source for source in load_ecosystem_sources()}

    assert set(sources) == {
        "linkedin-jobs",
        "indeed-jobs",
        "linkedin-content-manual",
        "careeraxis",
        "mycareersfuture",
        "careers-gov-early-careers",
        "sfa-job-portal",
        "sginnovate-dtc",
        "startup-sg-directory",
    }
    assert sources["sfa-job-portal"]["enabled"] is False
    for source_id in ("linkedin-jobs", "indeed-jobs"):
        assert sources[source_id]["record_kinds"] == ["job_lead"]
        assert radar_source_descriptor(source_id, "job_lead")["collection_mode"] == "bounded_job_search"
    assert sources["startup-sg-directory"]["record_kinds"] == ["company_seed"]
    assert sources["sginnovate-dtc"]["record_kinds"] == ["job_lead", "company_seed"]
    assert all(source["coverage_mode"] == "non_exhaustive" for source in sources.values())
    assert radar_source_descriptor("careeraxis", "job_lead")["source_type"] == "ecosystem_lead"
    assert radar_source_descriptor("startup-sg-directory", "company_seed")["source_type"] == "company_seed"
    assert radar_source_descriptor("sginnovate-dtc", "job_lead")["collection_mode"] == "manual_url_import"
    assert radar_source_descriptor("sginnovate-dtc", "company_seed")["collection_mode"] == "public_company_seed"


def test_unknown_disabled_and_unsupported_sources_are_rejected() -> None:
    with pytest.raises(KeyError, match="unknown ecosystem source"):
        get_ecosystem_source("not-registered")
    with pytest.raises(ValueError, match="disabled"):
        get_ecosystem_source("sfa-job-portal")
    with pytest.raises(ValueError, match="does not support job_lead"):
        radar_source_descriptor("startup-sg-directory", "job_lead")


def test_job_lead_rejects_wrong_source_host() -> None:
    with pytest.raises(ValueError, match="host is not allowed"):
        normalize_job_lead(
            {
                "title": "Data Intern",
                "company_name": "Example",
                "source_url": "https://evil.example/jobs/1",
            },
            "mycareersfuture",
        )


def test_job_lead_overrides_untrusted_promotion_and_verification_fields() -> None:
    lead = normalize_job_lead(
        {
            "title": "Product Intern",
            "company_name": "Example Pte Ltd",
            "source_url": "https://www.linkedin.com/posts/example-123",
            "official_job_url": "https://jobs.example.com/product-intern",
            "publisher_type": "official_company",
            "status": "promoted",
            "verification_status": "verified_official",
            "promoted": True,
            "verified": True,
            "is_official_publisher": True,
            "official_target_open": True,
        },
        "linkedin-content-manual",
    )

    assert lead["status"] == "awaiting_official"
    assert lead["verification_status"] == "unverified"
    assert lead["publisher_type"] == "social_content"
    assert "promoted" not in lead
    assert "verified" not in lead
    assert "is_official_publisher" not in lead
    assert "official_target_open" not in lead


@pytest.mark.parametrize("field", ["official_job_url", "official_url", "careers_url"])
def test_ecosystem_portal_cannot_be_used_as_employer_official_url(field: str) -> None:
    if field == "official_job_url":
        row = {
            "title": "Data Intern",
            "company_name": "Example",
            "source_url": "https://www.mycareersfuture.gov.sg/job/1",
            field: "https://careeraxis.ntu.edu.sg/jobs/1",
        }
        normalize = normalize_job_lead
        source = "mycareersfuture"
    else:
        row = {
            "company_name": "Example",
            "source_url": "https://www.startupsg.gov.sg/directory/startups/example",
            field: "https://www.sginnovate.com/example",
        }
        normalize = normalize_company_seed
        source = "startup-sg-directory"
    with pytest.raises(ValueError, match="employer-controlled URL"):
        normalize(row, source)


def test_company_seed_normalization_and_cross_source_official_identity() -> None:
    startup_seed = normalize_company_seed(
        {
            "company_name": "Example Labs",
            "source_url": "https://www.startupsg.gov.sg/directory/startups/example-labs",
            "official_url": "https://www.examplelabs.com/",
            "careers_url": "https://careers.examplelabs.com/jobs",
            "sectors": ["AI"],
        },
        "startup-sg-directory",
    )
    dtc_seed = normalize_company_seed(
        {
            "company_name": "EXAMPLE LABS PTE. LTD.",
            "source_url": "https://central.sginnovate.com/hub/company/example-labs",
            "careers_url": "https://careers.examplelabs.com/jobs",
        },
        "sginnovate-dtc",
    )

    assert startup_seed["status"] == "awaiting_official_careers"
    assert startup_seed["verification_status"] == "company_seed_unverified"
    assert "title" not in startup_seed
    assert startup_seed["official_domain"] == "examplelabs.com"
    assert startup_seed["company_key"] == dtc_seed["company_key"]
    assert startup_seed["company_key"] == company_seed_identity(
        "unrelated display name",
        official_url="https://examplelabs.com/company",
    )
