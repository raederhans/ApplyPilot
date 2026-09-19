import sqlite3

import pytest

from applypilot import eligibility
from applypilot.database import init_db
from applypilot.scoring import scorer


@pytest.fixture
def scoring_run(tmp_path, monkeypatch):
    db_path = tmp_path / "jobs.db"
    conn = init_db(db_path)
    resume = tmp_path / "resume.txt"
    resume.write_text("Python and SQL project evidence", encoding="utf-8")
    conn.execute(
        "INSERT INTO jobs (url,title,company_name,location,full_description,eligibility_status) "
        "VALUES ('https://example.test/job','Intern','Example','Singapore','Python role','eligible')"
    )
    conn.commit()
    monkeypatch.setattr(eligibility, "refresh_job_eligibility", lambda *_: None)
    monkeypatch.setattr(scorer, "get_connection", lambda: conn)
    monkeypatch.setattr(scorer, "load_profile", dict)
    monkeypatch.setattr(scorer, "select_resume_source", lambda *_: (resume, {}))
    monkeypatch.setattr(scorer, "current_profile_resume_fact_errors", lambda *_: [])
    yield conn, db_path, resume
    conn.close()


def assessment(score=8):
    return {
        "score": score, "keywords": "Python", "reasoning": "Evidence matches",
        "score_evidence": {"review_status": "not_requested"},
    }


@pytest.mark.parametrize("connection_mode", ["same_uncommitted", "other"])
def test_changed_jd_discards_old_score_and_next_run_retries(scoring_run, monkeypatch, connection_mode):
    conn, db_path, _ = scoring_run

    def score_with_jd_change(*_args, **_kwargs):
        writer = conn if connection_mode == "same_uncommitted" else sqlite3.connect(db_path)
        writer.execute("UPDATE jobs SET full_description='SQL role with different requirements'")
        if writer is not conn:
            writer.commit()
            writer.close()
        return assessment()

    monkeypatch.setattr(scorer, "score_job_with_review", score_with_jd_change)
    result = scorer.run_scoring()
    row = conn.execute("SELECT fit_score,score_status,score_evidence_json FROM jobs").fetchone()
    assert tuple(row) == (None, "stale", None)
    assert result["scored"] == 0
    assert result["errors"] == 1

    monkeypatch.setattr(scorer, "score_job_with_review", lambda *_args, **_kwargs: assessment())
    result = scorer.run_scoring()
    assert result["scored"] == 1
    assert result["errors"] == 0
    assert tuple(conn.execute("SELECT fit_score,score_status FROM jobs").fetchone()) == (8, "scored")


def test_source_changed_during_scoring_is_not_written(scoring_run, monkeypatch):
    conn, _, resume = scoring_run

    def score_with_source_change(*_args, **_kwargs):
        resume.write_text("Changed candidate evidence", encoding="utf-8")
        return assessment()

    monkeypatch.setattr(scorer, "score_job_with_review", score_with_source_change)
    result = scorer.run_scoring()
    assert result["scored"] == 0
    assert result["errors"] == 1
    assert conn.execute("SELECT fit_score FROM jobs").fetchone()[0] is None


@pytest.mark.parametrize("old_score", [0, 8])
def test_new_worker_score_survives_older_result(scoring_run, monkeypatch, old_score):
    conn, db_path, _ = scoring_run

    def score_with_concurrent_worker(*_args, **_kwargs):
        with sqlite3.connect(db_path) as writer:
            writer.execute(
                "UPDATE jobs SET fit_score=9,scored_at='newer',score_status='scored',"
                "score_attempts=1,score_evidence_json='{}'"
            )
        return assessment(old_score)

    monkeypatch.setattr(scorer, "score_job_with_review", score_with_concurrent_worker)
    result = scorer.run_scoring()
    assert result["scored"] == 0
    assert result["errors"] == 1
    assert tuple(conn.execute(
        "SELECT fit_score,scored_at,score_status,score_attempts,score_evidence_json FROM jobs"
    ).fetchone()) == (9, "newer", "scored", 1, "{}")


@pytest.mark.parametrize("mode", ["exact", "cover"])
@pytest.mark.parametrize("change", ["jd", "source", "worker"])
def test_exact_entrypoints_reject_changes_during_model_call(scoring_run, monkeypatch, mode, change):
    from applypilot import single_job
    from applypilot.scoring import cover_letter

    conn, db_path, resume = scoring_run
    monkeypatch.setattr(single_job, "get_connection", lambda: conn)
    monkeypatch.setattr(single_job, "refresh_job_eligibility", lambda *_: None)
    monkeypatch.setattr(single_job, "load_profile", dict)
    monkeypatch.setattr(single_job, "load_evidence_sources", lambda _p, _r, text: [{"text": text}])
    monkeypatch.setattr(cover_letter, "load_evidence_sources", lambda _p, _r, text: [{"text": text}])

    class FakeClient:
        def chat(self, *_args, **_kwargs):
            if change == "source":
                resume.write_text("Different candidate evidence", encoding="utf-8")
            else:
                with sqlite3.connect(db_path) as writer:
                    if change == "jd":
                        writer.execute("UPDATE jobs SET full_description='New JD'")
                    else:
                        writer.execute("UPDATE jobs SET fit_score=9,scored_at='newer',score_status='scored'")
            return "SCORE: 8\nKEYWORDS: Python\nREASONING: Evidence matches\nREVIEW: none"

    monkeypatch.setattr(scorer, "get_client", FakeClient)
    expected = {"jd": "JD changed", "source": "resume source changed", "worker": "scoring state changed"}
    with pytest.raises(RuntimeError, match=expected[change]):
        if mode == "exact":
            single_job.score_exact_job_for_url("https://example.test/job", str(resume))
        else:
            single_job.prepare_cover_letter_for_url("https://example.test/job", "Example", resume_path=str(resume))
    row = conn.execute("SELECT fit_score,score_status,scored_at FROM jobs").fetchone()
    if change == "worker":
        assert tuple(row) == (9, "scored", "newer")
    else:
        assert row[0] is None
        assert row[2] is None
        if change == "jd":
            assert row[1] == "stale"
    assert not conn.in_transaction
