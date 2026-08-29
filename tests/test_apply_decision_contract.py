"""Contracts for lightweight screening and one-resume-per-application routing.

All data in this module is synthetic.  These tests deliberately exercise no
browser, network, LLM, or user profile.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from applypilot.apply import decision
from applypilot.apply.authorization import compute_job_fingerprint
from applypilot.cli import app
from applypilot.database import init_db
from applypilot.scoring import scorer, tailor

FIXTURES = Path(__file__).parent / "fixtures" / "apply"


def _ready_job(**changes: object) -> dict:
    job = {
        "url": "https://careers.example.test/jobs/data",
        "application_url": "https://jobs.lever.co/example/1002",
        "title": "Data Analyst",
        "company_name": "Example Data",
        "full_description": "Use SQL and Python to build decision dashboards.",
        "eligibility_status": "eligible",
        "application_readiness_status": "confirmed",
        "application_readiness_reason": (
            "Synthetic evidence confirms authorization, availability, and location."
        ),
        "application_readiness_reviewed_at": "2026-08-24T01:00:00+00:00",
        "application_readiness_reviewed_by": "agent",
        "fit_score": 8,
        "tailored_resume_path": "C:/safe/tailored.pdf",
        "tailor_status": "machine_validated",
        "cover_letter_status": "not_required",
        "apply_status": None,
        "unanswered_questions": [],
    }
    job.update(changes)
    if "application_readiness_fingerprint" not in changes:
        job["application_readiness_fingerprint"] = compute_job_fingerprint(job)
    return job


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, "ready_to_apply"),
        ({"eligibility_status": "needs_review"}, "ready_to_apply"),
        (
            {
                "unanswered_questions": [
                    {
                        "question": "Work authorisation?",
                        "required": True,
                        "direct_impact": True,
                    }
                ]
            },
            "needs_review",
        ),
        ({"unanswered_questions": [{"question": "Application source?"}]}, "ready_to_apply"),
        ({"tailored_resume_path": None}, "needs_review"),
        ({"eligibility_status": "ineligible"}, "ignore"),
        ({"fit_score": 3}, "ignore"),
        ({"apply_status": "applied"}, "ignore"),
        ({"apply_status": "expired"}, "ignore"),
    ],
)
def test_decision_is_exactly_one_of_three_actionable_states(
    changes: dict, expected: str
) -> None:
    result = decision.evaluate(_ready_job(**changes))

    assert result["decision"] == expected
    assert result["decision"] in {"ready_to_apply", "needs_review", "ignore"}
    assert result["reason"]


def test_decision_does_not_block_on_explicitly_optional_unanswered_fields() -> None:
    job = _ready_job(
        unanswered_questions_json=json.dumps(
            [
                {
                    "question": "Current GPA",
                    "field_type": "text",
                    "required": False,
                    "reason": "Applicant may leave this optional field blank",
                }
            ]
        )
    )

    assert decision.evaluate(job)["decision"] == "ready_to_apply"


def test_retryable_historical_errors_do_not_block_a_fresh_application() -> None:
    job = _ready_job(
        apply_status="failed",
        apply_error="captcha encountered in an old session",
        detail_error="login page encountered during an old enrichment attempt",
        apply_retry_blocked=False,
    )

    assert decision.evaluate(job)["decision"] == "ready_to_apply"


def test_structured_retry_block_uses_failure_recoverability() -> None:
    retryable = _ready_job(
        apply_status="failed",
        apply_error="stuck",
        apply_retry_reason="stuck",
        apply_retry_blocked=True,
    )
    boundary = _ready_job(
        apply_status="failed",
        apply_error="assessment_required",
        apply_retry_reason="assessment_required",
        apply_retry_blocked=True,
    )

    assert decision.evaluate(retryable)["decision"] == "ready_to_apply"
    assert decision.evaluate(boundary)["decision"] == "needs_review"


def test_fixture_matrix_covers_multiple_companies_and_role_families() -> None:
    jobs = json.loads((FIXTURES / "jobs.json").read_text(encoding="utf-8"))
    outcomes = {job["id"]: decision.evaluate(job)["decision"] for job in jobs}

    assert outcomes == {
        "greenhouse-product": "needs_review",  # no verified tailored resume yet
        "lever-data": "needs_review",  # no verified tailored resume yet
        "ashby-ai-unknown": "needs_review",
        "workday-presales": "needs_review",  # site policy is an authorization concern
        "spatial-expired": "ignore",
    }


def test_scoring_persists_the_same_selected_resume_for_tailoring(tmp_path: Path, monkeypatch) -> None:
    """A score must never be calculated with the generic fallback resume first."""
    conn = init_db(tmp_path / "jobs.db")
    url = "https://careers.example.test/jobs/data"
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, site, source_site, full_description, "
        "eligibility_status) VALUES (?, 'Data Analyst', 'Example Data', 'lever', "
        "'official_careers', 'Use SQL and Python to build dashboards.', 'eligible')",
        (url,),
    )
    conn.commit()

    generic_resume = tmp_path / "generic.txt"
    data_resume = tmp_path / "data.txt"
    generic_resume.write_text("GENERIC FACTS", encoding="utf-8")
    data_resume.write_text("DATA RESUME FACTS", encoding="utf-8")
    profile = {
        "tailoring": {
            "resume_variants": [
                {"track": "data", "path": str(data_resume), "keywords": ["data analyst", "sql"]}
            ]
        }
    }
    captured: list[str] = []

    def select_data_resume(job: dict, loaded_profile: dict) -> tuple[Path, dict]:
        assert loaded_profile is profile
        assert job["url"] == url
        return data_resume, {"method": "fixture", "track": "data", "score": 1}

    def score_selected_resume(resume_text: str, job: dict) -> dict:
        assert job["url"] == url
        captured.append(resume_text)
        return {"score": 8, "keywords": "SQL", "reasoning": "fixture score"}

    monkeypatch.setattr(scorer, "get_connection", lambda: conn)
    monkeypatch.setattr(scorer, "load_profile", lambda: profile)
    monkeypatch.setattr(scorer, "RESUME_PATH", generic_resume)
    # The production scorer must import this router rather than silently
    # reading RESUME_PATH for every role.
    monkeypatch.setattr(scorer, "select_resume_source", select_data_resume, raising=False)
    monkeypatch.setattr(scorer, "score_job", score_selected_resume)

    result = scorer.run_scoring(limit=1)

    assert result["scored"] == 1
    assert captured == ["DATA RESUME FACTS"]
    stored = conn.execute(
        "SELECT fit_score, tailor_source_resume_path FROM jobs WHERE url=?", (url,)
    ).fetchone()
    assert stored[0] == 8
    assert Path(stored[1]) == data_resume.resolve()

    # Tailoring must respect the persisted score source rather than route the
    # same job a second, potentially different, way.
    selected, routing = tailor.select_resume_source(
        {
            "url": url,
            "title": "Data Analyst",
            "full_description": "Use SQL and Python to build dashboards.",
            "tailor_source_resume_path": stored[1],
        },
        profile,
    )
    assert selected == data_resume.resolve()
    assert routing["method"] == "job_override"


def test_decision_never_promotes_an_unvalidated_tailored_resume() -> None:
    result = decision.evaluate(
        _ready_job(tailor_status="failed_judge", tailored_resume_path="C:/unsafe/draft.pdf")
    )

    assert result["decision"] == "needs_review"
    assert "resume" in result["reason"].casefold()


def test_decision_enforces_configured_fit_floor_and_requires_a_score() -> None:
    assert decision.evaluate(_ready_job(fit_score=5), minimum_fit_score=6)["decision"] == "ignore"
    assert (
        decision.evaluate(_ready_job(fit_score=6), minimum_fit_score=6)["decision"]
        == "ready_to_apply"
    )
    assert decision.evaluate(_ready_job(fit_score=7), minimum_fit_score=8)["decision"] == "ignore"
    missing = decision.evaluate(_ready_job(fit_score=None), minimum_fit_score=8)
    assert missing["decision"] == "needs_review"
    assert "score" in missing["reason"].casefold()


def test_decision_invalidates_readiness_when_job_details_change() -> None:
    job = _ready_job()
    job["full_description"] += " Now requires five years of experience."

    result = decision.evaluate(job, minimum_fit_score=8)

    assert result["decision"] == "ready_to_apply"
    assert "apply with conditions" in result["reason"].casefold()
    assert "changed" in result["reason"].casefold()


@pytest.mark.parametrize(
    "changes",
    [
        {"application_readiness_status": None},
        {"application_readiness_status": "needs_review"},
        {"application_readiness_reason": ""},
        {"application_readiness_reviewed_at": ""},
    ],
)
def test_decision_defers_unconfirmed_application_readiness_to_runtime(changes: dict) -> None:
    result = decision.evaluate(_ready_job(**changes))

    assert result["decision"] == "ready_to_apply"
    assert "apply with conditions" in result["reason"].casefold()


def test_standing_policy_can_defer_unset_readiness_and_cover_to_runtime() -> None:
    job = _ready_job(
        application_readiness_status=None,
        application_readiness_reason=None,
        application_readiness_reviewed_at=None,
        application_readiness_fingerprint=None,
        cover_letter_status=None,
    )

    result = decision.evaluate(
        job,
        allow_runtime_readiness=True,
        allow_runtime_cover_letter=True,
    )

    assert result["decision"] == "ready_to_apply"


def test_explicit_needs_review_enters_application_with_conditions() -> None:
    result = decision.evaluate(
        _ready_job(application_readiness_status="needs_review"),
        allow_runtime_readiness=True,
        allow_runtime_cover_letter=True,
    )

    assert result["decision"] == "ready_to_apply"
    assert "apply with conditions" in result["reason"].casefold()


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("Senior Data Analyst", "Use SQL and Python to build decision dashboards."),
        ("Data Analyst", "Requires at least 5 years of analytics experience."),
    ],
)
def test_seniority_and_experience_do_not_override_a_passing_fit_score(
    title: str, description: str
) -> None:
    job = _ready_job(title=title, full_description=description)
    job["application_readiness_fingerprint"] = compute_job_fingerprint(job)

    assert decision.evaluate(job, minimum_fit_score=8)["decision"] == "ready_to_apply"
    job["fit_score"] = 7
    assert decision.evaluate(job, minimum_fit_score=8)["decision"] == "ignore"


def test_explicit_readiness_contradiction_is_do_not_apply() -> None:
    result = decision.evaluate(
        _ready_job(
            application_readiness_status="explicit_contradiction",
            application_readiness_reason="Candidate cannot meet the mandatory location requirement.",
        )
    )

    assert result["decision"] == "ignore"
    assert "location" in result["reason"].casefold()


def test_jobs_schema_includes_application_readiness_review_fields(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "readiness.db")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}

    assert {
        "application_readiness_status",
        "application_readiness_reason",
        "application_readiness_reviewed_at",
        "application_readiness_reviewed_by",
        "application_readiness_fingerprint",
    } <= columns


def test_review_readiness_records_evidence_review_without_application(
    tmp_path: Path, monkeypatch
) -> None:
    conn = init_db(tmp_path / "review-readiness.db")
    url = "https://careers.example.test/jobs/reviewed"
    conn.execute(
        "INSERT INTO jobs (url, title, company_name) VALUES (?, 'Analyst', 'Example')",
        (url,),
    )
    conn.commit()
    monkeypatch.setattr("applypilot.cli._bootstrap", lambda: None)
    monkeypatch.setattr("applypilot.database.get_connection", lambda: conn)

    result = CliRunner().invoke(
        app,
        [
            "review-readiness",
            "--url",
            url,
            "--status",
            "confirmed",
            "--reason",
            "Verified Singapore work authorization, term availability, and location.",
            "--reviewed-by",
            "agent",
        ],
    )

    assert result.exit_code == 0, result.output
    stored = conn.execute(
        """
        SELECT application_readiness_status, application_readiness_reason,
               application_readiness_reviewed_at, application_readiness_reviewed_by,
               application_readiness_fingerprint,
               apply_status, applied_at
        FROM jobs WHERE url = ?
        """,
        (url,),
    ).fetchone()
    assert stored["application_readiness_status"] == "confirmed"
    assert stored["application_readiness_reason"].startswith("Verified Singapore")
    assert stored["application_readiness_reviewed_at"].endswith("+00:00")
    assert stored["application_readiness_reviewed_by"] == "agent"
    assert stored["application_readiness_fingerprint"]
    assert stored["apply_status"] is None
    assert stored["applied_at"] is None


def test_import_job_accepts_verified_utf8_description_file(
    tmp_path: Path, monkeypatch
) -> None:
    description_file = tmp_path / "job-description.txt"
    description_file.write_text("分析产品数据并使用 SQL。\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_import_exact_job(*args, **kwargs) -> dict:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"status": "imported"}

    monkeypatch.setattr("applypilot.cli._bootstrap", lambda: None)
    monkeypatch.setattr("applypilot.single_job.import_exact_job", fake_import_exact_job)

    result = CliRunner().invoke(
        app,
        [
            "import-job",
            "--url",
            "https://careers.example.test/jobs/data",
            "--title",
            "Data Analyst",
            "--company",
            "Example Data",
            "--description-file",
            str(description_file),
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["kwargs"] == {"description": "分析产品数据并使用 SQL。\n"}
