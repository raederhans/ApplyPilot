from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from applypilot.apply.answer_resolution import (
    AnswerRequest,
    SensitiveAnswerError,
)
from applypilot.apply.answer_resolution import (
    resolve_answer as _resolve_answer,
)
from applypilot.apply.application_facts import ApplicationFact, resolve_application_fact


def resolve_answer(request: AnswerRequest):  # type: ignore[no-untyped-def]
    """Give legacy behavior tests explicit current-fact provenance."""
    if request.confirmed_fact is not None:
        fact_resolution = resolve_application_fact(
            (
                ApplicationFact(
                    "profile:test-fact",
                    "test_fact",
                    request.confirmed_fact,
                    source="profile.json",
                    scope="test",
                    confirmed_at=datetime(2026, 8, 31, tzinfo=UTC),
                    expires_at=datetime(2027, 8, 31, tzinfo=UTC),
                    sensitivity="high",
                ),
            ),
            key="test_fact",
            scope="test",
        )
        request = replace(
            request,
            fact_resolution=fact_resolution,
        )
    return _resolve_answer(request)


def test_degree_taxonomy_prefers_broader_same_level_category() -> None:
    result = resolve_answer(
        AnswerRequest(
            field_semantic="Highest degree type",
            options=("Bachelor's Degree", "Master's Degree", "Doctorate"),
            confirmed_fact="Master of Computing in Applied AI",
            required=True,
            direct_impact=True,
            can_explain=True,
        )
    )

    assert result.relation == "broader"
    assert result.action == "select_and_record"
    assert result.selected_option == "Master's Degree"
    assert result.value == "Master of Computing in Applied AI"


def test_degree_taxonomy_can_use_nearest_named_category_when_auditable() -> None:
    result = resolve_answer(
        AnswerRequest(
            field_semantic="Degree category",
            options=("Bachelor of Science", "Master of Arts", "Master of Science", "MBA"),
            confirmed_fact="Master of Computing in Applied AI",
            required=True,
            direct_impact=True,
        )
    )

    assert result.relation == "closest_non_equivalent"
    assert result.action == "select_and_record"
    assert result.selected_option == "Master of Science"
    assert result.audit["fact_ref"] == "profile:test-fact"
    assert result.audit["confidence"] == result.confidence


@pytest.mark.parametrize("semantic", ("Password", "OTP security code", "NRIC identity number"))
def test_sensitive_credentials_and_identity_never_enter_resolver_audit(semantic: str) -> None:
    with pytest.raises(SensitiveAnswerError):
        resolve_answer(
            AnswerRequest(
                field_semantic=semantic,
                confirmed_fact="must-not-appear-in-audit",
                required=True,
            )
        )


@pytest.mark.parametrize(
    "semantic",
    ("Available January 2026 to June 2026?", "Available July 2026 to December 2026?"),
)
def test_stale_availability_windows_are_answered_no_and_continue(semantic: str) -> None:
    result = resolve_answer(
        AnswerRequest(
            field_semantic=semantic,
            options=("Yes", "No"),
            confirmed_fact={"start": "2027-01-01", "end": "2027-05-31"},
            required=True,
            direct_impact=True,
        )
    )

    assert result.relation == "truthful_negative"
    assert result.action == "answer_negative_and_continue"
    assert result.selected_option == "No"


def test_unknown_named_skill_is_answered_no_instead_of_blocking() -> None:
    result = resolve_answer(
        AnswerRequest(
            field_semantic="Do you have experience with Jira?",
            options=("Yes", "No"),
            confirmed_fact=False,
            required=True,
            direct_impact=True,
        )
    )

    assert result.relation == "truthful_negative"
    assert result.action == "answer_negative_and_continue"
    assert result.selected_option == "No"


def test_unlisted_skill_uses_other_and_preserves_exact_fact() -> None:
    result = resolve_answer(
        AnswerRequest(
            field_semantic="Primary project management tool",
            options=("Jira", "Asana", "Other"),
            confirmed_fact="Trello",
            required=True,
            can_explain=True,
        )
    )

    assert result.relation == "broader"
    assert result.action == "select_and_record"
    assert result.selected_option == "Other"
    assert result.value == "Trello"


def test_salary_preference_selects_containing_bucket() -> None:
    result = resolve_answer(
        AnswerRequest(
            field_semantic="Expected monthly salary",
            options=("$1,000-$1,499", "$1,500-$1,999", "$2,000-$2,499"),
            confirmed_fact=1800,
            required=True,
            preference=True,
        )
    )

    assert result.relation == "preference"
    assert result.action == "select"
    assert result.selected_option == "$1,500-$1,999"


def test_legal_declaration_rejects_nearest_non_equivalent_answer() -> None:
    result = resolve_answer(
        AnswerRequest(
            field_semantic="I declare that I possess a relevant work visa",
            options=("I possess a work visa", "I require sponsorship"),
            confirmed_fact="Student's Pass with a conditional work-pass exemption",
            required=True,
            direct_impact=True,
            declaration=True,
            can_explain=True,
        )
    )

    assert result.relation == "contradiction"
    assert result.action == "review"
    assert result.selected_option is None


def test_only_unknown_required_high_impact_fact_requires_review() -> None:
    low_impact = resolve_answer(AnswerRequest(field_semantic="Preferred team", options=("A", "B"), required=True))
    high_impact = resolve_answer(
        AnswerRequest(
            field_semantic="Citizenship declaration",
            options=("Citizen", "Non-citizen"),
            required=True,
            direct_impact=True,
            declaration=True,
        )
    )

    assert low_impact.action == "research_then_select"
    assert low_impact.selected_option is None
    assert low_impact.audit["failure_code"] == "answer_policy_unresolved"
    assert high_impact.action == "review"
    assert high_impact.relation == "contradiction"
    assert high_impact.audit["failure_code"] == "unsupported_legal_declaration"


def test_automatic_resolution_has_no_failure_code() -> None:
    result = resolve_answer(
        AnswerRequest(
            field_semantic="Years of Python experience",
            options=("0 years", "5+"),
            confirmed_fact=6,
            required=True,
        )
    )

    assert result.action == "select"
    assert "failure_code" not in result.audit


@pytest.mark.parametrize(
    ("option", "fact"),
    [("5+", 6), ("5 years or more", 6), ("At least 5 years", 6)],
)
def test_experience_lower_bound_buckets_never_fall_back_to_zero(option: str, fact: int) -> None:
    result = resolve_answer(
        AnswerRequest(
            field_semantic="Years of Python experience",
            options=("0 years", "1-2 years", option),
            confirmed_fact=fact,
            required=True,
        )
    )

    assert result.relation == "containing_bucket"
    assert result.selected_option == option


def test_positive_experience_never_uses_zero_when_no_bucket_contains_it() -> None:
    result = resolve_answer(
        AnswerRequest(
            field_semantic="Years of Python experience",
            options=("0 years", "1-2 years", "3-4 years"),
            confirmed_fact=6,
            required=True,
        )
    )

    assert result.action == "review"
    assert result.selected_option is None


def test_verbose_negative_availability_continues() -> None:
    result = resolve_answer(
        AnswerRequest(
            field_semantic="Available January 2026 to June 2026?",
            options=("Yes, I am available", "No, I am not available"),
            confirmed_fact={"start": "2027-01-01", "end": "2027-05-31"},
            required=True,
            direct_impact=True,
        )
    )

    assert result.relation == "truthful_negative"
    assert result.selected_option == "No, I am not available"


@pytest.mark.parametrize(
    ("options", "fact", "expected"),
    [
        (("是", "否"), True, "是"),
        (("是", "否"), False, "否"),
        (("Yes", "不是"), False, "不是"),
        (("是", "不是"), True, "是"),
        (("是", "不是"), False, "不是"),
        (("是 / Yes，我符合", "否 / No，我不符合"), True, "是 / Yes，我符合"),
        (("同意 (I agree)", "不适用 (Not applicable)"), False, "不适用 (Not applicable)"),
        (("同意", "不同意"), False, "不同意"),
        (("同意（I agree）", "不同意（I disagree）"), False, "不同意（I disagree）"),
    ],
)
def test_chinese_boolean_labels_preserve_visible_option(options: tuple[str, str], fact: bool, expected: str) -> None:
    result = resolve_answer(
        AnswerRequest(
            field_semantic="Confirmed boolean question",
            options=options,
            confirmed_fact=fact,
            required=True,
            direct_impact=True,
        )
    )

    assert result.selected_option == expected
    assert result.confidence == 1.0


@pytest.mark.parametrize("option", ("是否", "同意与否", "不确定", "适用情况未知"))
def test_ambiguous_chinese_options_are_not_treated_as_boolean(option: str) -> None:
    result = resolve_answer(
        AnswerRequest(
            field_semantic="Material declaration",
            options=(option,),
            confirmed_fact=True,
            required=True,
            direct_impact=True,
            declaration=True,
        )
    )

    assert result.action == "review"
    assert result.selected_option is None


def test_chinese_legal_declaration_still_requires_confirmed_fact() -> None:
    result = resolve_answer(
        AnswerRequest(
            field_semantic="利益冲突法律声明",
            options=("是", "否"),
            required=True,
            direct_impact=True,
            declaration=True,
        )
    )

    assert result.action == "review"
    assert result.relation == "contradiction"
    assert result.selected_option is None


def test_other_is_truthful_without_an_explanation_control() -> None:
    result = resolve_answer(
        AnswerRequest(
            field_semantic="Primary project management tool",
            options=("Jira", "Asana", "Other"),
            confirmed_fact="Trello",
            required=True,
        )
    )

    assert result.relation == "broader"
    assert result.selected_option == "Other"
    assert result.value == "Trello"


def test_optional_low_impact_unrepresentable_fact_does_not_review() -> None:
    result = resolve_answer(
        AnswerRequest(
            field_semantic="How did you hear about us?",
            options=("Company website", "Job board"),
            confirmed_fact="LinkedIn",
        )
    )

    assert result.action == "continue_unanswered"
    assert result.relation == "closest_non_equivalent"
