from __future__ import annotations

import pytest

from applypilot.apply import page_observation

PUBLIC_URL = (
    "https://jobs.temasek.com.sg/job/Data-Engineer-Intern%2C-Technology-"
    "%28Jan-Jun-2027%29-238891/1369169257/"
)


def _binding() -> dict[str, object]:
    return {
        "provider": "successfactors",
        "resolved": True,
        "source_url": PUBLIC_URL,
        "source_host": "jobs.temasek.com.sg",
        "source_posting_id": "1369169257",
        "public_apply_url": (
            "https://jobs.temasek.com.sg/talentcommunity/apply/1369169257/"
        ),
        "ats_host": "career2.successfactors.eu",
        "company": "temasekcapP2",
        "job_req_id": "12189",
    }


@pytest.mark.parametrize(
    "expected",
    [
        PUBLIC_URL,
        (
            "https://jobs.temasek.com.sg/talentcommunity/apply/1369169257"
            "?locale=en_GB"
        ),
    ],
)
def test_exact_successfactors_job_review_with_submit_is_bound(expected: str) -> None:
    actual = (
        "https://career2.successfactors.eu/career?company=temasekcapP2"
        "&career_ns=job_application&career_job_req_id=12189"
    )

    assert page_observation._same_bound_application_flow(
        expected,
        actual,
        {"submit_control_count": 1},
        _binding(),
    )


@pytest.mark.parametrize(
    "actual",
    [
        (
            "https://career2.successfactors.eu/career?company=temasekcapP2"
            "&career_ns=job_application&career_job_req_id=999"
        ),
        "https://career2.successfactors.eu/careers?company=temasekcapP2",
        (
            "https://career2.successfactors.eu/career?company=temasekcapP2"
            "&login_ns=register&career_ns=job_application&career_job_req_id=12189"
        ),
        (
            "https://career2.successfactors.eu/career?company=temasekcapP2"
            "&login_ns=forgot_pwd&career_ns=job_application&career_job_req_id=12189"
        ),
        (
            "https://career2.successfactors.eu/career?company=temasekcapP2"
            "&career_ns=job_application"
        ),
    ],
)
def test_successfactors_auth_or_other_job_never_passes_final_gate(actual: str) -> None:
    assert not page_observation._same_bound_application_flow(
        PUBLIC_URL,
        actual,
        {"submit_control_count": 1},
        _binding(),
    )


def test_successfactors_final_gate_requires_submit_evidence() -> None:
    actual = (
        "https://career2.successfactors.eu/career?company=temasekcapP2"
        "&career_ns=job_application&career_job_req_id=12189"
    )
    assert not page_observation._same_bound_application_flow(
        PUBLIC_URL,
        actual,
        {"submit_control_count": 0},
        _binding(),
    )


def test_unresolved_successfactors_route_cannot_use_query_blind_fallback() -> None:
    expected = (
        "https://career2.successfactors.eu/career?company=temasekcapP2"
        "&career_ns=job_application&career_job_req_id=12189"
    )
    actual = expected.replace("12189", "999")

    assert not page_observation._same_bound_application_flow(
        expected,
        actual,
        {"submit_control_count": 1},
        None,
    )
