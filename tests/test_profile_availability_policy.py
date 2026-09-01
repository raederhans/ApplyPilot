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
    ("question", "expected"),
    [
        (
            "Will you require any visa sponsorship in order to work in Singapore upon graduation?",
            ("requires_sponsorship", True),
        ),
        (
            "Will you require sponsorship after your graduation?",
            ("requires_sponsorship", True),
        ),
        (
            "Will you require sponsorship following graduation?",
            ("requires_sponsorship", True),
        ),
        (
            "Will you require post-graduation sponsorship?",
            ("requires_sponsorship", True),
        ),
        (
            "Will you require sponsorship when you graduate?",
            ("requires_sponsorship", True),
        ),
        (
            "Will you require sponsorship once you graduate?",
            ("requires_sponsorship", True),
        ),
        (
            "Will you require sponsorship after graduating?",
            ("requires_sponsorship", True),
        ),
        (
            "Will you require sponsorship after you graduate?",
            ("requires_sponsorship", True),
        ),
        (
            "Will you be able to work without sponsorship upon graduation?",
            ("work_without_sponsorship", False),
        ),
        (
            "Will you be able to work without requiring sponsorship upon graduation?",
            ("work_without_sponsorship", False),
        ),
        (
            "Will you be able to work without the need for sponsorship upon graduation?",
            ("work_without_sponsorship", False),
        ),
        (
            "Can you work without sponsorship when you graduate?",
            ("work_without_sponsorship", False),
        ),
        (
            "Could you legally work without requiring sponsorship when you graduate?",
            ("work_without_sponsorship", False),
        ),
        (
            "May you work without visa sponsorship when you graduate?",
            ("work_without_sponsorship", False),
        ),
        (
            "Will you legally work without requiring visa sponsorship when you graduate?",
            ("work_without_sponsorship", False),
        ),
        (
            "Would you work without sponsorship when you graduate?",
            ("work_without_sponsorship", False),
        ),
        (
            "Will you require visa sponsorship for this internship?",
            ("requires_sponsorship", False),
        ),
        (
            "Are you legally authorized to work following graduation?",
            ("legally_authorized_to_work", False),
        ),
        (
            "Are you legally authorized to work for this internship?",
            ("legally_authorized_to_work", True),
        ),
    ],
)
def test_screening_answers_honor_explicit_post_graduation_context(
    question: str,
    expected: tuple[str, bool],
) -> None:
    profile = _full_time_profile()
    profile["work_authorization"]["form_answer_policy"]["post_graduation_full_time"] = {
        "legally_authorized": "No",
        "requires_sponsorship": "Yes",
    }
    job = {
        "title": "Data Analyst Intern",
        "full_description": "Full-time internship in Singapore.",
        "application_readiness_reason": "Passed configured eligibility and fit checks.",
    }

    assert page_observation._expected_screening_answer(question, profile, job) == expected


@pytest.mark.parametrize(
    "branch",
    [
        None,
        {"legally_authorized": "No"},
        {"legally_authorized": "Unknown", "requires_sponsorship": "Yes"},
    ],
)
@pytest.mark.parametrize("field_collection", ["radio_questions", "select_fields"])
def test_post_graduation_policy_unavailable_is_a_blocker(
    branch: dict[str, str] | None,
    field_collection: str,
) -> None:
    profile = _full_time_profile()
    if branch is not None:
        profile["work_authorization"]["form_answer_policy"][
            "post_graduation_full_time"
        ] = branch
    job = {
        "title": "Data Analyst Intern",
        "full_description": "Full-time internship in Singapore.",
    }
    issues = page_observation._validate_pre_submit_snapshot(
        {
            field_collection: [
                {
                    "text": "Will you require sponsorship when you graduate?",
                    "selected": "Yes",
                }
            ]
        },
        profile,
        job,
    )

    blocker = "work_authorization_policy_unavailable:post_graduation_full_time"
    blockers, repairable, advisories = page_observation._partition_pre_submit_issues(
        issues
    )

    assert blocker in issues
    assert blocker in blockers
    assert blocker not in repairable
    assert blocker not in advisories


def test_ambiguous_post_graduation_work_question_fails_closed() -> None:
    profile = _full_time_profile()
    profile["work_authorization"]["form_answer_policy"]["post_graduation_full_time"] = {
        "legally_authorized": "No",
        "requires_sponsorship": "Yes",
    }
    issues = page_observation._validate_pre_submit_snapshot(
        {
            "radio_questions": [
                {
                    "text": "Are you authorized to work or will you require sponsorship when you graduate?",
                    "selected": "Yes",
                }
            ]
        },
        profile,
        {"title": "Data Analyst Intern"},
    )

    blocker = "ambiguous_work_authorization_question:post_graduation"
    blockers, repairable, advisories = page_observation._partition_pre_submit_issues(
        issues
    )

    assert blocker in issues
    assert blocker in blockers
    assert blocker not in repairable
    assert blocker not in advisories


@pytest.mark.parametrize("field_collection", ["radio_questions", "select_fields"])
def test_modal_work_without_sponsorship_uses_combined_post_graduation_policy(
    field_collection: str,
) -> None:
    profile = _full_time_profile()
    profile["work_authorization"]["form_answer_policy"]["post_graduation_full_time"] = {
        "legally_authorized": "No",
        "requires_sponsorship": "Yes",
    }
    job = {"title": "Data Analyst Intern"}
    question = "Can you work without sponsorship when you graduate?"

    selected_no = page_observation._validate_pre_submit_snapshot(
        {field_collection: [{"text": question, "selected": "No"}]},
        profile,
        job,
    )
    selected_yes = page_observation._validate_pre_submit_snapshot(
        {field_collection: [{"text": question, "selected": "Yes"}]},
        profile,
        job,
    )

    blocker = "hard_answer_mismatch:work_without_sponsorship"
    assert blocker not in selected_no
    assert blocker in selected_yes


def test_pre_submit_accepts_post_graduation_sponsorship_yes_for_internship() -> None:
    profile = _full_time_profile()
    profile["work_authorization"]["form_answer_policy"]["post_graduation_full_time"] = {
        "legally_authorized": "No",
        "requires_sponsorship": "Yes",
    }
    job = {
        "title": "Data Analyst Intern",
        "full_description": "Full-time internship in Singapore.",
        "application_readiness_reason": "Passed configured eligibility and fit checks.",
    }

    post_graduation = page_observation._validate_pre_submit_snapshot(
        {
            "radio_questions": [
                {
                    "text": "Will you require any visa sponsorship in order to work in Singapore upon graduation?",
                    "selected": "Yes",
                }
            ]
        },
        profile,
        job,
    )
    ordinary_internship = page_observation._validate_pre_submit_snapshot(
        {
            "radio_questions": [
                {
                    "text": "Will you require visa sponsorship for this internship?",
                    "selected": "Yes",
                }
            ]
        },
        profile,
        job,
    )

    assert "hard_answer_mismatch:requires_sponsorship" not in post_graduation
    assert "hard_answer_mismatch:requires_sponsorship" in ordinary_internship


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
