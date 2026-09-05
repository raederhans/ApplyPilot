"""Opportunity-first eligibility and fit-scoring policy contracts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import ClassVar

import pytest

from applypilot.database import init_db
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


@pytest.mark.parametrize(
    ("response", "expected_score"),
    [
        ("SCORE: 6\nKEYWORDS: Python\nREASONING: Direct evidence.", 6),
        ("SCORE: 6/10\nKEYWORDS: Python\nREASONING: Direct evidence.", 6),
        ('{"score": 6, "keywords": "Python", "reasoning": "Direct evidence."}', 6),
        ("SCORE: 99\nKEYWORDS: Python\nREASONING: Invalid range.", 0),
        ("SCORE: -3\nKEYWORDS: Python\nREASONING: Invalid range.", 0),
        ('{"score": true, "keywords": "Python", "reasoning": "Invalid type."}', 0),
        ('{"score": 6.5, "keywords": "Python", "reasoning": "Invalid type."}', 0),
        ('{"score": "6", "keywords": "Python", "reasoning": "Invalid type."}', 0),
    ],
)
def test_score_parser_accepts_only_real_integer_scores_in_the_declared_range(
    response: str, expected_score: int
) -> None:
    result = scorer._parse_score_response(response)

    assert result["score"] == expected_score
    assert result["keywords"] == "Python"
    assert result["reasoning"]


def _insert_pending_score_job(conn, url: str, *, source_path: Path | None = None) -> None:
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, site, source_site, full_description, "
        "eligibility_status, tailor_source_resume_path) "
        "VALUES (?, 'Data Analyst', 'Example Data', 'lever', 'official_careers', "
        "'Use SQL and Python to build decision dashboards.', 'eligible', ?)",
        (url, str(source_path) if source_path else None),
    )
    conn.commit()


def test_conflicting_explicit_resume_source_is_reselected_or_fails_before_llm(
    tmp_path: Path, monkeypatch
) -> None:
    """A stale stored override must not silently influence a new assessment."""
    conn = init_db(tmp_path / "jobs.db")
    stale = tmp_path / "stale.txt"
    current = tmp_path / "current.txt"
    stale.write_text("STALE GPA: 2.0", encoding="utf-8")
    current.write_text("CURRENT GPA: 3.8", encoding="utf-8")
    invalid_url = "https://careers.example.test/jobs/no-current-source"
    reselected_url = "https://careers.example.test/jobs/reselected-source"
    _insert_pending_score_job(conn, invalid_url, source_path=stale)
    _insert_pending_score_job(conn, reselected_url, source_path=stale)
    profile = {"submission_policy": {"minimum_fit_score": 6}}
    selected_jobs: list[tuple[str, bool]] = []

    def select_source(job: dict, _profile: dict) -> tuple[Path, dict]:
        has_override = bool(job.get("tailor_source_resume_path"))
        selected_jobs.append((job["url"], has_override))
        if has_override:
            return stale, {"method": "job_override", "track": "explicit", "score": None}
        if job["url"] == reselected_url:
            return current, {"method": "fixture_reselection", "track": "data", "score": 1}
        return stale, {"method": "fixture_no_valid_source", "track": "data", "score": 0}

    class FakeClient:
        calls = 0
        last_response_meta: ClassVar[dict] = {}

        def chat(self, _messages, **_kwargs):
            type(self).calls += 1
            return "SCORE: 8\nKEYWORDS: SQL\nREASONING: Current source matches."

    monkeypatch.setattr(scorer, "get_connection", lambda: conn)
    monkeypatch.setattr(scorer, "load_profile", lambda: profile)
    monkeypatch.setattr(scorer, "select_resume_source", select_source)
    monkeypatch.setattr(scorer, "current_profile_resume_fact_errors", lambda text, _profile: ["stale GPA"] if "STALE" in text else [])
    monkeypatch.setattr(scorer, "get_client", lambda: FakeClient())

    result = scorer.run_scoring(limit=2)

    assert result["scored"] == 2
    assert FakeClient.calls == 1
    assert (invalid_url, False) in selected_jobs
    assert (reselected_url, False) in selected_jobs
    stored = {
        row[0]: row[1:]
        for row in conn.execute(
            "SELECT url, fit_score, score_error, score_evidence_json, tailor_source_resume_path "
            "FROM jobs"
        ).fetchall()
    }
    invalid = stored[invalid_url]
    reselected = stored[reselected_url]
    assert invalid[0] is None
    assert "source" in invalid[1].casefold() and "conflict" in invalid[1].casefold()
    assert reselected[0] == 8
    assert Path(reselected[3]) == current.resolve()
    invalid_evidence = json.loads(invalid[2])
    reselected_evidence = json.loads(reselected[2])
    assert invalid_evidence["review_status"] == "not_requested"
    assert "source" in invalid_evidence["initial_assessment"]["reasoning"].casefold()
    assert reselected_evidence["review_status"] == "not_requested"
    assert Path(reselected_evidence["source_resume_path"]) == current.resolve()
    assert reselected_evidence["resume_routing"]["method"] == "fixture_reselection"


def test_batch_reviews_only_ambiguous_passing_boundary_scores_and_adopts_review(
    tmp_path: Path, monkeypatch
) -> None:
    conn = init_db(tmp_path / "jobs.db")
    urls = [
        "https://careers.example.test/jobs/review-lower",
        "https://careers.example.test/jobs/review-second",
        "https://careers.example.test/jobs/budget-exhausted",
        "https://careers.example.test/jobs/not-boundary",
        "https://careers.example.test/jobs/no-doubt",
    ]
    for url in urls:
        _insert_pending_score_job(conn, url)
    source = tmp_path / "source.txt"
    source.write_text("Current candidate facts", encoding="utf-8")
    profile = {"submission_policy": {"minimum_fit_score": 6}}
    calls: list[tuple[str, bool]] = []

    def select_source(_job: dict, _profile: dict) -> tuple[Path, dict]:
        return source, {"method": "fixture", "track": "data", "score": 1}

    def score(resume_text: str, job: dict, *, profile: dict | None = None, review_of: dict | None = None) -> dict:
        assert resume_text == "Current candidate facts"
        assert profile is not None
        calls.append((job["url"], review_of is not None))
        if review_of is not None:
            return {
                "score": {urls[0]: 4, urls[1]: 5}[job["url"]],
                "keywords": "SQL",
                "reasoning": "Review found the gap is material.",
                "review_reason": "",
            }
        score_by_url = {urls[0]: 5, urls[1]: 6, urls[2]: 6, urls[3]: 7, urls[4]: 5}
        return {
            "score": score_by_url[job["url"]],
            "keywords": "SQL",
            "reasoning": "Initial assessment.",
            "review_reason": "conflicting evidence" if job["url"] != urls[4] else "",
        }

    monkeypatch.setattr(scorer, "get_connection", lambda: conn)
    monkeypatch.setattr(scorer, "load_profile", lambda: profile)
    monkeypatch.setattr(scorer, "select_resume_source", select_source)
    monkeypatch.setattr(scorer, "score_job", score)

    result = scorer.run_scoring(limit=5, review_limit=99)

    assert result["reviewed"] == 2
    assert sum(is_review for _, is_review in calls) == 2
    rows = {
        row[0]: row[1:]
        for row in conn.execute(
            "SELECT url, fit_score, score_evidence_json FROM jobs ORDER BY url"
        ).fetchall()
    }
    assert rows[urls[0]][0] == 4
    assert rows[urls[1]][0] == 5
    assert rows[urls[2]][0] == 6
    assert rows[urls[3]][0] == 7
    assert rows[urls[4]][0] == 5
    assert json.loads(rows[urls[0]][1])["review_status"] == "completed"
    assert json.loads(rows[urls[2]][1])["review_status"] == "budget_exhausted"
    assert json.loads(rows[urls[3]][1])["review_status"] == "not_borderline"
    assert json.loads(rows[urls[4]][1])["review_status"] == "not_requested"


@pytest.mark.parametrize("review_mode", ["error", "zero"])
def test_failed_boundary_review_keeps_initial_assessment_and_records_failure(
    tmp_path: Path, monkeypatch, review_mode: str
) -> None:
    conn = init_db(tmp_path / "jobs.db")
    url = "https://careers.example.test/jobs/review-failure"
    _insert_pending_score_job(conn, url)
    source = tmp_path / "source.txt"
    source.write_text("Current candidate facts", encoding="utf-8")
    profile = {"submission_policy": {"minimum_fit_score": 6}}
    calls = 0

    def select_source(_job: dict, _profile: dict) -> tuple[Path, dict]:
        return source, {"method": "fixture", "track": "data", "score": 1}

    def score(_resume_text: str, _job: dict, *, profile: dict | None = None, review_of: dict | None = None) -> dict:
        nonlocal calls
        calls += 1
        assert profile is not None
        if review_of is not None:
            if review_mode == "error":
                raise RuntimeError("review provider unavailable")
            return {
                "score": 0,
                "keywords": "",
                "reasoning": "Review response is unusable.",
                "review_reason": "",
            }
        return {
            "score": 6,
            "keywords": "SQL",
            "reasoning": "Initial assessment.",
            "review_reason": "years evidence conflicts",
        }

    monkeypatch.setattr(scorer, "get_connection", lambda: conn)
    monkeypatch.setattr(scorer, "load_profile", lambda: profile)
    monkeypatch.setattr(scorer, "select_resume_source", select_source)
    monkeypatch.setattr(scorer, "score_job", score)

    result = scorer.run_scoring(limit=1, review_limit=2)

    assert result["reviewed"] == 1
    assert calls == 2
    row = conn.execute(
        "SELECT fit_score, score_evidence_json FROM jobs WHERE url = ?", (url,)
    ).fetchone()
    assert row[0] == 6
    evidence = json.loads(row[1])
    assert evidence["review_status"] == "failed"
    assert evidence["review_error"]
    if review_mode == "error":
        assert evidence.get("review_assessment") is None
        assert "provider unavailable" in evidence["review_error"]
    else:
        assert evidence["review_assessment"]["score"] == 0


def test_score_job_projects_only_confirmed_qualification_facts_to_the_model(
    monkeypatch,
) -> None:
    captured: dict = {}

    class FakeClient:
        last_response_meta: ClassVar[dict] = {}
        model = "synthetic-score-model"

        def chat(self, messages, **_kwargs):
            captured["messages"] = messages
            return "SCORE: 7\nKEYWORDS: SQL\nREASONING: Synthetic evidence."

    profile = {
        "personal": {
            "email": "candidate@example.test",
            "password": "personal-secret",
            "permitID": "personal-permit",
        },
        "availability": {
            "earliest_start_date": "2027-06-01",
            "available_for_full_time": True,
            "private_calendar": "calendar-secret",
        },
        "work_authorization": {
            "permitID": "work-auth-secret",
            "form_answer_policy": {
                "programme_credit_bearing_internship": {
                    "legally_authorized": True,
                    "requires_sponsorship": False,
                    "status": "eligible",
                    "permitID": "role-secret",
                }
            },
        },
        "education": [
            {
                "institution": "Synthetic University",
                "degree": "BSc",
                "field": "Computer Science",
                "status": "undergraduate",
                "graduation": "2027-05-31",
                "expected_graduation": "2027-05-31",
                "gpa": "3.8",
                "private_note": "education-secret",
            }
        ],
    }
    monkeypatch.setattr(scorer, "get_client", lambda: FakeClient())

    result = scorer.score_job(
        "Synthetic University candidate evidence.",
        {"title": "Data Analyst", "full_description": "Use SQL."},
        profile=profile,
    )

    assert result["score"] == 7
    prompt_content = captured["messages"][1]["content"]
    facts = json.loads(prompt_content.split("CONFIRMED PROFILE FACTS:\n", maxsplit=1)[1])
    assert facts == {
        "availability": {
            "earliest_start_date": "2027-06-01",
            "available_for_full_time": True,
        },
        "work_authorization_by_role": {
            "programme_credit_bearing_internship": {
                "legally_authorized": True,
                "requires_sponsorship": False,
                "status": "eligible",
            }
        },
        "education": [
            {
                "institution": "Synthetic University",
                "degree": "BSc",
                "field": "Computer Science",
                "status": "undergraduate",
                "graduation": "2027-05-31",
                "expected_graduation": "2027-05-31",
            }
        ],
    }
    for forbidden in (
        "candidate@example.test",
        "personal-secret",
        "personal-permit",
        "work-auth-secret",
        "role-secret",
        "calendar-secret",
        "education-secret",
    ):
        assert forbidden not in prompt_content


def test_score_evidence_column_is_added_to_legacy_jobs_without_losing_score(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-jobs.db"
    legacy = sqlite3.connect(database_path)
    legacy.execute(
        "CREATE TABLE jobs (url TEXT PRIMARY KEY, fit_score INTEGER, score_reasoning TEXT)"
    )
    legacy.execute(
        "INSERT INTO jobs (url, fit_score, score_reasoning) VALUES (?, 7, 'legacy score')",
        ("https://careers.example.test/jobs/legacy",),
    )
    legacy.commit()
    legacy.close()

    migrated = init_db(database_path)

    columns = {row[1] for row in migrated.execute("PRAGMA table_info(jobs)").fetchall()}
    row = migrated.execute(
        "SELECT fit_score, score_reasoning, score_evidence_json FROM jobs WHERE url = ?",
        ("https://careers.example.test/jobs/legacy",),
    ).fetchone()
    assert "score_evidence_json" in columns
    assert tuple(row) == (7, "legacy score", None)


@pytest.mark.parametrize("review_response", [
    '{"score": 5}', '{"score": 5, "reasoning": null}', 'SCORE: 5', 'SCORE: 5\nREASONING:',
])
def test_incomplete_review_does_not_replace_supported_initial_score(monkeypatch, review_response):
    responses = iter([
        (
            "SCORE: 6\nREASONING: Transferable evidence supports a moderate fit.\n"
            "REVIEW: A preferred condition may have been interpreted as required."
        ),
        review_response,
    ])

    class FakeClient:
        def chat(self, messages, **kwargs):
            return next(responses)

    monkeypatch.setattr(scorer, "get_client", lambda: FakeClient())
    result = scorer.score_job_with_review(
        "Python tools", {"title": "Tools Intern", "full_description": "Python or C++; Linux preferred."},
        profile={"submission_policy": {"minimum_fit_score": 6}},
    )
    assert result["score"] == 6
    assert "Transferable evidence" in result["reasoning"]
    assert result["score_evidence"]["review_status"] == "failed"
    assert "reasoning" in result["score_evidence"]["review_error"]
