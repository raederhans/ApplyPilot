import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from applypilot.discovery.company_priority import import_events, load_events, rank_with_company_priority

START = datetime(2026, 9, 21, tzinfo=UTC)


def event(kind, day, app=None, **extra):
    return {"event_id": f"{kind}-{day}-{app}", "kind": kind,
            "company": "Acme", "scope": "sg:data-intern",
            "occurred_at": (START + timedelta(days=day)).isoformat(),
            "evidence": "reviewed fixture evidence", "application_id": app, **extra}


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY, company_name TEXT, apply_status TEXT, applied_at TEXT)")
    yield connection
    connection.close()


def seed(conn, events):
    for e in events:
        if e["kind"] == "submitted":
            e["job_url"] = "https://jobs.example/" + e["application_id"]
            conn.execute("INSERT OR IGNORE INTO jobs VALUES (?, ?, 'applied', ?)",
                         (e["job_url"], e["company"], e["occurred_at"]))
    import_events(conn, events, now=START + timedelta(days=100))


def rejected_batch():
    return [event("submitted", -3, "a"), event("submitted", -2, "b"),
            event("submitted", -1, "c"), event("rejected", 0, "a"), event("rejected", 0, "b")]


def rank(conn, day=0, scope="sg:data-intern"):
    return rank_with_company_priority(conn, [
        {"url": "new", "company_name": "Acme", "fit_score": 9, "priority_scope": scope},
        {"url": "other", "company_name": "Other", "fit_score": 8},
        {"url": "low", "company_name": "Third", "fit_score": 6},
    ], now=START + timedelta(days=day))


@pytest.mark.parametrize("day,penalty", [(0, .25), (7, .25), (10.5, .125), (14, 0), (40, 0)])
def test_two_week_decay_and_rank_retains_fit_and_candidates(conn, day, penalty):
    seed(conn, rejected_batch())
    result = rank(conn, day)
    acme = next(j for j in result if j["url"] == "new")
    assert acme["company_priority"]["penalty"] == penalty
    assert acme["fit_score"] == 9
    assert len(result) == 3
    if day == 0:
        assert [j["url"] for j in result] == ["other", "new", "low"]


def test_progress_releases_and_consumes_old_evidence(conn):
    seed(conn, rejected_batch() + [event("progress", 2), event("rejected", 3, "a")])
    assert next(j for j in rank(conn, 4) if j["url"] == "new")["company_priority"]["penalty"] == 0


def test_assessment_halves_only_once(conn):
    seed(conn, rejected_batch() + [event("assessment", 1, "c"), event("assessment", 2, "c")])
    assert next(j for j in rank(conn, 3) if j["url"] == "new")["company_priority"]["penalty"] == .125


def test_unknown_scope_half_and_unrelated_scope_unaffected(conn):
    seed(conn, rejected_batch())
    assert next(j for j in rank(conn, scope="") if j["url"] == "new")["company_priority"]["penalty"] == .125
    assert next(j for j in rank(conn, scope="uk:design") if j["url"] == "new")["company_priority"]["penalty"] == 0


def test_silence_requires_review_and_old_evidence_never_retriggers(conn):
    seed(conn, [event("submitted", -45 + i, str(i)) for i in range(4)])
    assert rank(conn)[0]["url"] == "new"
    seed(conn, [event("coverage", 0, complete=True, covered_since=(START - timedelta(days=60)).isoformat())])
    assert next(j for j in rank(conn) if j["url"] == "new")["company_priority"]["penalty"] == .2
    seed(conn, [event("coverage", 14, complete=True, covered_since=(START - timedelta(days=46)).isoformat())])
    assert rank(conn, 14)[0]["company_priority"]["penalty"] == 0


def test_recent_submissions_do_not_count_as_silence(conn):
    seed(conn, [event("submitted", -14 + i, str(i)) for i in range(4)] + [
        event("coverage", 0, complete=True, covered_since=(START - timedelta(days=60)).isoformat())])
    assert rank(conn)[0]["company_priority"]["penalty"] == 0


def test_repeated_rejection_of_one_application_is_not_two_rejections(conn):
    events = rejected_batch()[:-1] + [event("rejected", 1, "a")]
    seed(conn, events)
    assert rank(conn, 2)[0]["company_priority"]["penalty"] == 0


def test_idempotent_import_and_conflicts_are_atomic(conn):
    events = rejected_batch()
    seed(conn, events)
    assert import_events(conn, events, now=START) == 0
    before = load_events(conn)
    with pytest.raises(ValueError):
        import_events(conn, [event("progress", 0), {**events[0], "evidence": "changed"}], now=START)
    assert load_events(conn) == before


def test_unconfirmed_attempts_cannot_be_imported(conn):
    with pytest.raises(ValueError, match="confirmed applied"):
        import_events(conn, [event("submitted", 0, "a", job_url="not-applied")], now=START)
    assert load_events(conn) == []


def test_new_evidence_after_expiry_can_retrigger(conn):
    events = rejected_batch() + [event("submitted", 15 + i, "new" + str(i)) for i in range(3)]
    events += [event("rejected", 18, "new0"), event("rejected", 18, "new1")]
    seed(conn, events)
    assert next(j for j in rank(conn, 18) if j["url"] == "new")["company_priority"]["penalty"] == .25


def test_exact_invitation_exemption_expires(conn):
    seed(conn, rejected_batch())
    conn.execute("INSERT INTO jobs VALUES ('new', 'Acme', NULL, NULL)")
    seed(conn, [event("invited", 1, "new", job_url="new")])
    assert rank(conn, 2)[0]["company_priority"]["details"]["reason"] == "explicit_job_invitation"


def test_empty_database_read_is_nonmutating(conn):
    before = conn.execute("SELECT name FROM sqlite_master").fetchall()
    assert rank(conn)[0]["url"] == "new"
    assert conn.execute("SELECT name FROM sqlite_master").fetchall() == before


def test_same_timestamp_submission_and_rejection_order(conn):
    seed(conn, [event("submitted", 0, str(i)) for i in range(3)] +
         [event("rejected", 0, str(i)) for i in range(2)])
    assert next(j for j in rank(conn) if j["url"] == "new")["company_priority"]["penalty"] == .25


def test_scope_event_applies_to_new_candidate(conn):
    seed(conn, rejected_batch())
    conn.execute("INSERT INTO jobs VALUES ('new', 'Acme', NULL, NULL)")
    seed(conn, [event("scope", 0, job_url="new")])
    assert next(j for j in rank(conn, scope="") if j["url"] == "new")["company_priority"]["penalty"] == .25


def test_incomplete_coverage_rejected_and_no_partial_import(conn):
    with pytest.raises(ValueError, match="complete feedback"):
        import_events(conn, [event("reset", 0), event("coverage", 0, complete=False)], now=START)
    assert load_events(conn) == []


@pytest.mark.parametrize("entry", ["authorization", "worker"])
def test_real_selection_entrypoints_use_adjusted_score(tmp_path, monkeypatch, entry):
    from applypilot import database
    from applypilot.apply import application_jobs, authorization, submission_admission
    from applypilot.cli import _build_standing_authorization_manifest

    conn = database.init_db(tmp_path / "test.db")
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-fixture")
    for url, company, score in [("new", "Acme", 9), ("other", "Other", 8)]:
        conn.execute("INSERT INTO jobs (url, application_url, company_name, fit_score, "
                     "eligibility_status, full_description, tailored_resume_path, tailor_status, "
                     "cover_letter_status) VALUES (?, ?, ?, ?, 'eligible', 'Description', ?, "
                     "'machine_validated', 'not_required')", (url, url, company, score, str(resume)))
    current = datetime.now(UTC)
    observations = rejected_batch()
    for e in observations:
        e["occurred_at"] = (current + (datetime.fromisoformat(e["occurred_at"]) - START)).isoformat()
        if e["kind"] == "submitted":
            e["job_url"] = "history-" + e["application_id"]
            conn.execute("INSERT INTO jobs (url, company_name, apply_status, applied_at) "
                         "VALUES (?, 'Acme', 'applied', ?)", (e["job_url"], e["occurred_at"]))
    observations.append({**event("scope", 0, job_url="new"), "occurred_at": current.isoformat()})
    import_events(conn, observations)
    monkeypatch.setattr(submission_admission, "evaluate_submission_admission", lambda *a, **k: {"admitted": True})
    if entry == "authorization":
        monkeypatch.setattr(authorization, "build_bound_manifest", lambda jobs, **kw: {"jobs": jobs})
        profile = {"submission_policy": {"authorization_granted": True,
                   "standing_auto_authorize_ready_jobs": True, "batch_authorization_required": False,
                   "maximum_auto_authorized_submissions_per_run": 2,
                   "maximum_auto_authorized_candidates_per_run": 2}}
        result = _build_standing_authorization_manifest(conn, profile=profile, target_url=None,
                                                        requested_limit=2, min_score=8)
        assert [j["url"] for j in result["jobs"]] == ["other", "new"]
        assert result["jobs"][1]["fit_score"] == 9
    else:
        monkeypatch.setattr(application_jobs.config, "load_profile", dict)
        monkeypatch.setattr(application_jobs.config, "portal_application_gate", lambda *a, **k: None)
        monkeypatch.setattr("applypilot.eligibility.refresh_job_eligibility", lambda *a, **k: None)
        result = application_jobs.acquire_job(conn, min_score=8, preview_only=True,
                    load_blocked=lambda: ([], []), application_lease_minutes=15)
        assert result["url"] == "other"
    conn.close()
