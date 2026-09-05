import sqlite3
from datetime import UTC, datetime, timedelta

from applypilot import database as database_mod
from applypilot.apply import application_jobs, authorization, submission_admission
from applypilot.cli import _build_standing_authorization_manifest
from applypilot.discovery.diversity import (
    rank_company_diverse,
    recent_handled_companies,
)


def _job(name: str, company: str | None, score: int | None) -> dict:
    return {"title": name, "company_name": company, "fit_score": score}


def _titles(jobs: list[dict]) -> list[str]:
    return [job["title"] for job in jobs]


def test_different_scores_keep_fit_score_priority_and_missing_scores_sort_last():
    jobs = [
        _job("lower-new", "New Co", 7),
        _job("higher-recent", "Recent Co", 9),
        _job("unknown-score", "Another Co", None),
        _job("middle-new", "Third Co", 8),
    ]

    ranked = rank_company_diverse(jobs, recent_companies=["Recent Co"])

    assert _titles(ranked) == [
        "higher-recent",
        "middle-new",
        "lower-new",
        "unknown-score",
    ]


def test_equal_score_prefers_company_not_processed_recently():
    jobs = [
        _job("recent", "  ACME   Labs ", 8),
        _job("new", "Beta", 8),
    ]

    ranked = rank_company_diverse(jobs, recent_companies=["acme labs"])

    assert _titles(ranked) == ["new", "recent"]


def test_equal_score_rotates_companies_and_preserves_each_company_order():
    jobs = [
        _job("acme-one", "Acme", 8),
        _job("acme-two", "ACME", 8),
        _job("beta-one", "Beta", 8),
        _job("beta-two", "Beta", 8),
        _job("gamma-one", "Gamma", 8),
    ]

    ranked = rank_company_diverse(jobs)

    assert _titles(ranked) == [
        "acme-one",
        "beta-one",
        "gamma-one",
        "acme-two",
        "beta-two",
    ]


def test_ranking_retains_every_job_without_copying_or_changing_scores():
    jobs = [
        _job("acme-one", "Acme", 8),
        _job("acme-two", "Acme", 8),
        _job("beta", "Beta", 8),
    ]
    snapshots = [job.copy() for job in jobs]

    ranked = rank_company_diverse(jobs)

    assert len(ranked) == len(jobs)
    assert {id(job) for job in ranked} == {id(job) for job in jobs}
    assert jobs == snapshots
    assert [job["fit_score"] for job in ranked] == [8, 8, 8]


def test_unknown_companies_are_independent_and_brand_aliases_are_not_inferred():
    jobs = [
        _job("unknown-one", None, 8),
        _job("unknown-two", "  ", 8),
        _job("legal-name", "Example Pte Ltd", 8),
        _job("short-name-one", "Example", 8),
        _job("short-name-two", "Example", 8),
    ]

    ranked = rank_company_diverse(jobs)

    assert _titles(ranked) == [
        "unknown-one",
        "unknown-two",
        "legal-name",
        "short-name-one",
        "short-name-two",
    ]


def test_recent_handled_companies_requires_a_recent_attempt_or_application_date():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE jobs (company_name TEXT, apply_attempts INTEGER, "
        "last_attempted_at TEXT, applied_at TEXT)"
    )
    now = datetime.now(UTC)
    rows = [
        ("Attempted Co", 1, (now - timedelta(days=2)).isoformat(), None),
        ("Applied Co", 0, None, (now - timedelta(days=3)).isoformat()),
        ("No Attempt", 0, (now - timedelta(days=1)).isoformat(), None),
        ("No Date", 2, None, None),
        ("Old Co", 1, (now - timedelta(days=15)).isoformat(), None),
        ("Status Only", 0, None, None),
    ]
    conn.executemany("INSERT INTO jobs VALUES (?, ?, ?, ?)", rows)

    companies = recent_handled_companies(conn, days=14)

    assert companies == {"attempted co", "applied co"}


def test_standing_candidates_apply_diversity_after_all_existing_filters(
    tmp_path, monkeypatch
):
    conn = database_mod.init_db(tmp_path / "standing-diversity.db")
    now = datetime.now(UTC).isoformat()
    conn.executemany(
        "INSERT INTO jobs (url, application_url, company_name, fit_score, "
        "eligibility_status, apply_status, applied_at) VALUES (?, ?, ?, 8, 'eligible', ?, ?)",
        [
            ("https://jobs.test/a-recent", "https://apply.test/a", "Recent Co", None, None),
            ("https://jobs.test/b-new", "https://apply.test/b", "New Co", None, None),
            ("https://jobs.test/history", "https://apply.test/history", "Recent Co", "applied", now),
        ],
    )
    conn.commit()
    monkeypatch.setattr(
        submission_admission,
        "evaluate_submission_admission",
        lambda *args, **kwargs: {"admitted": True},
    )
    monkeypatch.setattr(
        authorization,
        "build_bound_manifest",
        lambda jobs, **kwargs: {"jobs": jobs},
    )
    profile = {
        "submission_policy": {
            "authorization_granted": True,
            "standing_auto_authorize_ready_jobs": True,
            "batch_authorization_required": False,
            "maximum_auto_authorized_submissions_per_run": 2,
            "maximum_auto_authorized_candidates_per_run": 2,
        }
    }

    manifest = _build_standing_authorization_manifest(
        conn,
        profile=profile,
        target_url=None,
        requested_limit=2,
        min_score=6,
    )

    assert [job["url"] for job in manifest["jobs"]] == [
        "https://jobs.test/b-new",
        "https://jobs.test/a-recent",
    ]


def test_batch_acquisition_uses_diversity_for_equal_score_candidates(
    tmp_path, monkeypatch
):
    conn = database_mod.init_db(tmp_path / "batch-diversity.db")
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-test")
    now = datetime.now(UTC).isoformat()
    conn.executemany(
        "INSERT INTO jobs (url, application_url, company_name, full_description, fit_score, "
        "eligibility_status, tailored_resume_path, tailor_status, cover_letter_status, "
        "apply_status, applied_at) VALUES (?, ?, ?, 'Description', 8, 'eligible', ?, "
        "'machine_validated', 'not_required', ?, ?)",
        [
            ("https://jobs.test/a-recent", "https://apply.test/a", "Recent Co", str(resume), None, None),
            ("https://jobs.test/b-new", "https://apply.test/b", "New Co", str(resume), None, None),
            (
                "https://jobs.test/history",
                "https://apply.test/history",
                "Recent Co",
                str(resume),
                "applied",
                now,
            ),
        ],
    )
    conn.commit()
    monkeypatch.setattr(application_jobs.config, "load_profile", dict)
    monkeypatch.setattr(
        application_jobs.config, "portal_application_gate", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        submission_admission,
        "evaluate_submission_admission",
        lambda *args, **kwargs: {"admitted": True},
    )
    monkeypatch.setattr("applypilot.eligibility.refresh_job_eligibility", lambda *args, **kwargs: None)

    acquired = application_jobs.acquire_job(
        conn,
        min_score=6,
        preview_only=True,
        load_blocked=lambda: ([], []),
        application_lease_minutes=15,
    )

    assert acquired is not None
    assert acquired["url"] == "https://jobs.test/b-new"
