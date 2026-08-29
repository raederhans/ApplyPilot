"""Opportunity-first eligibility and fit-scoring policy contracts."""

from __future__ import annotations

from typing import ClassVar

from applypilot.eligibility import evaluate_job_eligibility
from applypilot.scoring import scorer


def test_ordinary_qualification_requirements_are_not_hard_ineligible() -> None:
    requirements = (
        "Applicants must be Singapore citizens or permanent residents.\n"
        "This senior role requires five years of experience and a master's degree."
    )

    assert evaluate_job_eligibility({"full_description": requirements}) == ("eligible", None)


def test_explicit_do_not_apply_instruction_is_hard_ineligible() -> None:
    status, reason = evaluate_job_eligibility(
        {
            "full_description": (
                "If you are not a Singapore citizen, please do not apply for this position."
            )
        },
        profile={"personal": {"nationality": "China"}},
    )

    assert status == "ineligible"
    assert reason and "do-not-apply" in reason.casefold()


def test_explicit_exclusion_without_a_confirmed_candidate_conflict_is_not_hard() -> None:
    assert evaluate_job_eligibility(
        {"full_description": "Applications from recruitment agencies will not be considered."},
        profile={"personal": {"nationality": "China"}},
    ) == ("eligible", None)
    assert evaluate_job_eligibility(
        {"full_description": "If you require sponsorship, do not apply."},
        profile={"work_authorization": {"requires_sponsorship": False}},
    ) == ("eligible", None)
    assert evaluate_job_eligibility(
        {
            "full_description": (
                "Join our Singapore team. If you cannot travel occasionally, do not apply."
            )
        },
        profile={"personal": {"nationality": "China"}},
    ) == ("eligible", None)


def test_confirmed_sponsorship_conflict_honors_explicit_do_not_apply() -> None:
    status, _ = evaluate_job_eligibility(
        {"full_description": "Applicants requiring sponsorship will not be considered."},
        profile={"work_authorization": {"requires_sponsorship": True}},
    )

    assert status == "ineligible"


def test_scoring_prompt_uses_configurable_explainable_qualification_penalties(
    monkeypatch,
) -> None:
    captured: dict = {}

    class FakeClient:
        last_response_meta: ClassVar[dict] = {}

        def chat(self, messages, **kwargs):
            captured["prompt"] = messages[0]["content"]
            return (
                "SCORE: 6\nKEYWORDS: Python\n"
                "REASONING: Base match 8; applied a two-point experience-gap penalty."
            )

    monkeypatch.setenv("APPLYPILOT_SCORE_SENIOR_TITLE_PENALTY", "2")
    monkeypatch.setenv("APPLYPILOT_SCORE_YEARS_GAP_PER_POINT", "3")
    monkeypatch.setenv("APPLYPILOT_SCORE_YEARS_GAP_MAX_PENALTY", "4")
    monkeypatch.setattr(scorer, "get_client", lambda: FakeClient())

    result = scorer.score_job(
        "Built Python workflows.",
        {
            "title": "Senior Python Engineer",
            "full_description": "Requires six years of Python experience.",
        },
    )

    assert result["score"] == 6
    assert "at most 2 point(s)" in captured["prompt"]
    assert "per 3 missing year(s), capped at 4 point(s)" in captured["prompt"]
    assert "not automatic rejection" in captured["prompt"]
    assert "do not double-count" in captured["prompt"]
