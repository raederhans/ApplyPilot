from __future__ import annotations

from applypilot.apply.submission_admission import evaluate_submission_admission
from applypilot.apply.submission_surfaces import classify_submission_surface


HENKEL_CSOD_URL = (
    "https://henkel.csod.com/ux/ats/careersite/1/"
    "requisition/87202/application?c=henkel"
)


def _job(**overrides: object) -> dict[str, object]:
    job: dict[str, object] = {
        "url": "https://www.linkedin.com/jobs/view/1001",
        "application_url": HENKEL_CSOD_URL,
        "source_site": "linkedin",
        "site": "linkedin",
        "title": "Data Intelligence Intern",
        "company_name": "Henkel",
        "full_description": "Use SQL and Python to build dashboards.",
        "fit_score": 8,
        "eligibility_status": "eligible",
        "tailored_resume_path": "resume.pdf",
        "tailor_status": "machine_validated",
        "cover_letter_status": "not_required",
        "apply_attempts": 0,
        "apply_status": None,
        "apply_retry_blocked": 0,
    }
    job.update(overrides)
    return job


def _profile() -> dict[str, object]:
    return {
        "submission_policy": {
            "allowed_submission_surfaces": ["linkedin_to_official_ats", "official_ats"],
        }
    }


def test_henkel_csod_linkedin_handoff_is_recognized_without_host_trust() -> None:
    job = _job()

    assert classify_submission_surface(job) == "linkedin_to_official_ats"
    result = evaluate_submission_admission(job, _profile(), minimum_fit_score=6)

    assert result["admitted"] is True
    assert result["surface"] == "linkedin_to_official_ats"
    assert result["metadata"]["target_verification"] == "recognized_ats"


def test_henkel_csod_official_source_is_classified_as_official_ats() -> None:
    job = _job(
        url=HENKEL_CSOD_URL,
        source_site="official_careers",
        site="official_careers",
    )

    assert classify_submission_surface(job) == "official_ats"
