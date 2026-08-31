from __future__ import annotations

import json

import pytest

from applypilot import config
from applypilot.apply import page_observation, prompt
from applypilot.services import application


def _full_time_profile() -> dict:
    return {
        "personal": {
            "full_name": "Taylor Chen",
            "email": "candidate@example.com",
            "phone": "+65 9000 0000",
            "city": "Singapore",
            "country": "Singapore",
        },
        "work_authorization": {
            "legally_authorized_to_work": "Role-specific",
            "require_sponsorship": "No for a qualifying internship",
            "form_answer_policy": {
                "programme_credit_bearing_internship": {
                    "legally_authorized": "Yes",
                    "requires_sponsorship": "No",
                }
            },
        },
        "availability": {
            "earliest_start_date": "2026-11-10",
            "generic_application_availability_date": "2026-11-10",
            "credit_bearing_internship_start": "2026-11-10",
            "internship_end_date": "2027-06-30",
            "credit_bearing_internship_hours_per_week": "Full-time",
        },
        "compensation": {
            "salary_expectation": "Negotiable",
            "salary_currency": "SGD",
        },
        "application_facts": [
            {
                "key": "full_time_internship_availability",
                "value": "Full-time from 2026-11-10 through 2027-06-30",
            }
        ],
    }


@pytest.mark.parametrize(
    "legacy_fragment",
    [
        {"availability": {"non_credit_internship_hours_per_week_max": 16}},
        {
            "application_facts": [
                {
                    "key": "non_credit_internship_availability",
                    "value": "Immediately, maximum 16 hours per week",
                }
            ]
        },
        {
            "screening": {
                "available_for_full_time_3_6_month_internship_starting_september": False
            }
        },
    ],
)
def test_profile_validation_rejects_retired_availability_facts(
    legacy_fragment: dict,
) -> None:
    profile = _full_time_profile()
    profile.update(legacy_fragment)

    with pytest.raises(ValueError, match="retired candidate availability facts"):
        config.validate_profile_availability(profile)


def test_service_profile_loader_applies_the_same_guard(tmp_path) -> None:
    path = tmp_path / "profile.json"
    profile = _full_time_profile()
    profile["availability"]["legacy_note"] = "Available for 16 hours per week"
    path.write_text(json.dumps(profile), encoding="utf-8")

    with pytest.raises(ValueError, match="retired candidate availability facts"):
        application.load_profile(path)


def test_full_time_profile_renders_without_retired_schedule_branch() -> None:
    profile = _full_time_profile()

    rendered = "\n".join(
        (
            prompt._build_profile_summary(profile),
            prompt._build_availability_section(profile),
            prompt._build_work_authorization_section(profile),
            prompt._build_application_facts_section(profile),
        )
    )

    assert "2026-11-10" in rendered
    assert "2027-06-30" in rendered
    assert "Confirmed full-time internship availability" in rendered
    assert "Non-credit internship" not in rendered
    assert "16 hours" not in rendered
    assert "part-time eligibility" not in rendered


def test_part_time_job_does_not_fall_back_to_full_time_authorization_branch() -> None:
    profile = _full_time_profile()
    job = {
        "title": "Part-time Data Intern",
        "full_description": "The employer requires 16 hours per week.",
    }

    assert page_observation._work_authorization_answers(profile, job) is None


def test_full_time_internship_uses_programme_credit_authorization_branch() -> None:
    profile = _full_time_profile()
    job = {
        "title": "Data Analyst Intern",
        "full_description": "Full-time internship in Singapore.",
        "application_readiness_reason": "Passed configured eligibility and fit checks.",
    }

    assert page_observation._work_authorization_answers(profile, job) == (True, False)


@pytest.mark.parametrize(
    "description",
    [
        "This is a part time internship.",
        "This internship is non credit.",
        "This internship is not credit-bearing.",
        "This internship is not eligible for academic credit.",
        "No academic credit is available for this internship.",
    ],
)
def test_explicit_non_qualifying_internship_does_not_use_credit_authorization(
    description: str,
) -> None:
    profile = _full_time_profile()
    job = {"title": "Data Intern", "full_description": description}

    assert page_observation._work_authorization_answers(profile, job) is None


def test_prior_employer_answer_requires_target_employer_scope() -> None:
    profile = _full_time_profile()
    profile["application_facts"].append(
        {
            "key": "prior_target_employer_history_policy",
            "value": "No for every target employer",
        }
    )
    job = {"company_name": "Simular Pte Ltd"}

    assert page_observation._expected_screening_answer(
        "Have you ever worked with Python?", profile, job
    ) is None
    for question in (
        "Have you ever worked for Simular?",
        "Are you a former employee of Simular Pte Ltd?",
        "Have you worked for us before?",
    ):
        assert page_observation._expected_screening_answer(question, profile, job) == (
            "previously_worked_for_target_employer",
            False,
        )
