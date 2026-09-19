import json
import sqlite3

import pytest

from applypilot import single_job
from applypilot.apply.authorization import compute_job_fingerprint
from applypilot.database import init_db
from applypilot.resume_versions import text_digest
from applypilot.scoring import cover_letter, scorer


@pytest.fixture
def scored_job(tmp_path):
    conn = init_db(tmp_path / "jobs.db")
    resume = tmp_path / "resume.txt"
    resume.write_text("Python and SQL project evidence", encoding="utf-8")
    conn.execute(
        "INSERT INTO jobs (url,title,company_name,location,full_description,"
        "fit_score,scored_at,score_status,score_evidence_json,tailored_resume_path,"
        "tailored_at,tailor_status,tailor_report_path,tailor_source_resume_path) "
        "VALUES ('https://example.test/job','Intern','Example','Singapore','Python role',"
        "8,'then','scored','{}',?,'then','machine_validated','history.json',?)",
        (str(resume), str(resume)),
    )
    conn.commit()
    yield conn, resume
    conn.close()


@pytest.mark.parametrize("field", ["url", "application_url", "title", "company_name", "location", "full_description"])
def test_changed_job_invalidates_current_projection(scored_job, field):
    conn, resume = scored_job
    conn.execute("UPDATE jobs SET cover_letter_path='letter.md', cover_letter_status='approved', "
                 "cover_letter_approved_at='then', cover_letter_approved_by='reviewer', "
                 "application_readiness_status='ready', application_readiness_fingerprint='old'")
    conn.execute(f"UPDATE jobs SET {field}=?", ("changed",))
    job = dict(conn.execute("SELECT * FROM jobs").fetchone())
    for key in ("fit_score", "scored_at", "score_evidence_json", "tailored_resume_path", "tailored_at"):
        assert job[key] is None
    assert job["score_status"] == job["tailor_status"] == "stale"
    assert job["tailor_report_path"] == "history.json"
    assert job["tailor_source_resume_path"] == str(resume)
    assert job["cover_letter_status"] == "stale"
    for key in ("cover_letter_path", "cover_letter_approved_at", "cover_letter_approved_by",
                "application_readiness_status", "application_readiness_fingerprint"):
        assert job[key] is None
    assert resume.is_file()


def test_unchanged_and_unrelated_updates_preserve_score(scored_job):
    conn, _ = scored_job
    conn.execute("UPDATE jobs SET full_description=full_description, application_url=NULL, last_seen_at='now'")
    assert conn.execute("SELECT fit_score,score_status FROM jobs").fetchone()[:] == (8, "scored")


@pytest.mark.parametrize("status,applied_at", [(None, "then"), ("applied", None), ("submitted", None), ("submission_uncertain", None)])
def test_submitted_or_uncertain_jobs_are_protected(scored_job, status, applied_at):
    conn, resume = scored_job
    conn.execute("UPDATE jobs SET apply_status=?,applied_at=?", (status, applied_at))
    conn.execute("UPDATE jobs SET full_description='changed'")
    assert conn.execute("SELECT fit_score,tailored_resume_path FROM jobs").fetchone()[:] == (8, str(resume))


@pytest.mark.parametrize("status", ["in_progress", "submission_uncertain"])
def test_submit_started_ledger_protects_job(scored_job, status):
    conn, _ = scored_job
    conn.execute(
        "INSERT INTO application_attempts (attempt_id,job_url,worker_id,started_at,lease_expires_at,phase,submit_started,status,updated_at) "
        "VALUES ('attempt','https://example.test/job','worker','then','later','submit',1,?,'then')",
        (status,),
    )
    conn.execute("UPDATE jobs SET full_description='changed'")
    assert conn.execute("SELECT fit_score FROM jobs").fetchone()[0] == 8


@pytest.mark.parametrize("mode", ["batch", "exact", "cover"])
def test_scoring_persists_input_binding(scored_job, monkeypatch, mode):
    conn, resume = scored_job
    from applypilot import eligibility

    monkeypatch.setattr(eligibility, "refresh_job_eligibility", lambda *_: None)
    result = {"score": 8, "keywords": "Python", "reasoning": "Evidence matches", "score_evidence": {"review_status": "not_requested"}}
    if mode == "batch":
        monkeypatch.setattr(scorer, "get_connection", lambda: conn)
        monkeypatch.setattr(scorer, "load_profile", dict)
        monkeypatch.setattr(scorer, "select_resume_source", lambda *_: (resume, {}))
        monkeypatch.setattr(scorer, "current_profile_resume_fact_errors", lambda *_: [])
        monkeypatch.setattr(scorer, "score_job_with_review", lambda *_args, **_kwargs: result)
        conn.execute("UPDATE jobs SET eligibility_status='eligible'")
        scorer.run_scoring(rescore=True)
    else:
        monkeypatch.setattr(single_job, "get_connection", lambda: conn)
        monkeypatch.setattr(single_job, "refresh_job_eligibility", lambda *_: None)
        monkeypatch.setattr(single_job, "load_profile", dict)
        monkeypatch.setattr(single_job, "load_evidence_sources", lambda _p, _r, text: [{"text": text}])
        monkeypatch.setattr(cover_letter, "load_evidence_sources", lambda _p, _r, text: [{"text": text}])
        monkeypatch.setattr(single_job, "score_job", lambda *_args, **_kwargs: result)
        if mode == "cover":
            def stop_after_score(*_args, **_kwargs):
                raise RuntimeError("stop after score persistence")

            monkeypatch.setattr(single_job, "generate_cover_letter_document", stop_after_score)
            with pytest.raises(RuntimeError, match="stop after score persistence"):
                single_job.prepare_cover_letter_for_url("https://example.test/job", "New employer", resume_path=str(resume))
        else:
            single_job.score_exact_job_for_url("https://example.test/job", str(resume))
            conn = sqlite3.connect(resume.parent / "jobs.db")
            conn.row_factory = sqlite3.Row
    job = dict(conn.execute("SELECT * FROM jobs").fetchone())
    binding = json.loads(job["score_evidence_json"])["input_binding"]
    assert job["fit_score"] == 8
    expected = {
        "job_fingerprint": compute_job_fingerprint(job),
        "source_path": str(resume.resolve()),
        "source_text_digest": text_digest(resume.read_text(encoding="utf-8")),
        "prompt_revision": scorer.PROMPT_REVISION,
    }
    assert all(binding[key] == value for key, value in expected.items())
    assert "profile_facts_digest" in binding
    if mode != "batch":
        assert binding["context_mode"] == "registered_evidence_sources"
        assert binding["context_text_digest"] == text_digest(resume.read_text(encoding="utf-8"))
    if mode == "exact":
        conn.close()


def test_long_description_tail_requirements_reach_model(monkeypatch):
    description = "Role context. " * 600 + "MANDATORY: Must hold a professional engineering license."
    observed = []

    class FakeClient:
        last_response_meta = None

        def chat(self, messages, **_kwargs):
            observed.append(messages[1]["content"])
            return "SCORE: 5\nKEYWORDS: engineering\nREASONING: License is missing.\nREVIEW: none"

    monkeypatch.setattr(scorer, "get_client", FakeClient)
    monkeypatch.setattr(scorer, "current_profile_resume_fact_errors", lambda *_: [])
    result = scorer.score_job_with_review("Candidate project evidence", {"title": "Engineer", "full_description": description})
    assert result["score"] == 5
    assert len(observed) == 1
    assert description in observed[0]
    assert result["score_evidence"]["jd_sent_chars"] == len(description) > 6000
