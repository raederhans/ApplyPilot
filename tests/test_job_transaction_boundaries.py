"""Real SQLite races and rollback behavior, not implementation-shape checks."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from applypilot.apply import application_jobs, job_acquisition, submission_admission
from applypilot.database import close_connection, init_db
from applypilot.storage.runtime_cells import RuntimeCellConflictError
from applypilot.storage.transactions import write_transaction


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "transactions.db"
    connection = init_db(path)
    connection.execute("CREATE TABLE unrelated(value TEXT)")
    connection.commit()
    monkeypatch.setattr(application_jobs.config, "load_profile", dict)
    monkeypatch.setattr(application_jobs.config, "portal_application_gate", lambda *_a, **_k: None)
    monkeypatch.setattr("applypilot.eligibility.refresh_job_eligibility", lambda *_a, **_k: None)
    monkeypatch.setattr(submission_admission, "evaluate_submission_admission", lambda *_a, **_k: {"admitted": True})
    yield connection, path
    close_connection(path)


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=2)
    connection.row_factory = sqlite3.Row
    return connection


def _job(connection: sqlite3.Connection, name: str = "a") -> str:
    url = f"https://{name}.example.test/apply"
    connection.execute(
        "INSERT INTO jobs(url, application_url, title, company_name, fit_score, "
        "tailored_resume_path, tailor_status, cover_letter_status, eligibility_status) "
        "VALUES(?, ?, 'Analyst', 'Example', 9, 'resume.txt', 'machine_validated', 'not_required', 'eligible')",
        (url, url),
    )
    connection.commit()
    return url


def _acquire(connection: sqlite3.Connection, **kwargs):
    return application_jobs.acquire_job(
        connection, load_blocked=lambda: ([], []), application_lease_minutes=15,
        **kwargs,
    )


def test_admission_allows_an_independent_writer(database, monkeypatch):
    connection, path = database
    url = _job(connection)

    def evaluate(*_args, **_kwargs):
        assert not connection.in_transaction
        other = _connect(path)
        try:
            other.execute("INSERT INTO unrelated VALUES ('during-admission')")
            other.commit()
        finally:
            other.close()
        return {"admitted": True}

    monkeypatch.setattr(submission_admission, "evaluate_submission_admission", evaluate)
    result = _acquire(connection, target_url=url)
    assert result is not None
    assert connection.execute("SELECT value FROM unrelated").fetchone()[0] == "during-admission"
    assert not connection.in_transaction
    assert result["_acquisition_performance"]["transaction_hold_ms"] >= 0


def test_two_workers_cannot_claim_the_same_snapshot(database, monkeypatch):
    connection, path = database
    url = _job(connection)
    barrier = threading.Barrier(2)

    def evaluate(*_args, **_kwargs):
        barrier.wait(timeout=5)
        return {"admitted": True}

    monkeypatch.setattr(submission_admission, "evaluate_submission_admission", evaluate)

    def claim(worker):
        own = _connect(path)
        try:
            return _acquire(own, target_url=url, worker_id=worker)
        finally:
            own.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, [1, 2]))
    assert sum(result is not None for result in results) == 1
    assert connection.execute("SELECT COUNT(*) FROM application_attempts").fetchone()[0] == 1
    winner = next(result for result in results if result is not None)
    assert connection.execute("SELECT apply_task_id FROM jobs WHERE url=?", (url,)).fetchone()[0] == winner["_attempt_id"]


@pytest.mark.parametrize("change", ["full_description", "tailored_resume_path", "apply_status"])
def test_changed_snapshot_is_never_claimed(database, monkeypatch, change):
    connection, path = database
    url = _job(connection)

    def evaluate(*_args, **_kwargs):
        other = _connect(path)
        try:
            # Identifiers come only from the test's fixed parameter list.
            value = "applied" if change == "apply_status" else "new-evidence"
            other.execute(f"UPDATE jobs SET {change}=? WHERE url=?", (value, url))
            other.commit()
        finally:
            other.close()
        return {"admitted": True}

    monkeypatch.setattr(submission_admission, "evaluate_submission_admission", evaluate)
    metrics = {}
    assert _acquire(connection, target_url=url, performance_sink=metrics) is None
    assert metrics["stale_candidates_skipped"] == 1
    assert connection.execute("SELECT COUNT(*) FROM application_attempts").fetchone()[0] == 0
    assert not connection.in_transaction


def test_stale_material_observation_cannot_erase_a_corrected_resume(database, monkeypatch):
    connection, path = database
    url = _job(connection)

    def evaluate(*_args, **_kwargs):
        other = _connect(path)
        try:
            other.execute("UPDATE jobs SET tailored_resume_path='corrected.txt' WHERE url=?", (url,))
            other.commit()
        finally:
            other.close()
        return {
            "admitted": False, "reason": "old profile fact",
            "metadata": {"profile_resume_fact_freshness": {"state": "stale_profile_fact"}},
        }

    monkeypatch.setattr(submission_admission, "evaluate_submission_admission", evaluate)
    assert _acquire(connection, target_url=url) is None
    assert connection.execute("SELECT tailored_resume_path FROM jobs WHERE url=?", (url,)).fetchone()[0] == "corrected.txt"


def test_authorization_expiry_is_checked_again_at_claim(database, monkeypatch):
    connection, _ = database
    url = _job(connection)
    start = datetime.now(UTC)
    current = [start]
    monkeypatch.setattr("applypilot.apply.authorization.authorize_job", lambda *_a: {"url": url})

    def evaluate(*_args, **_kwargs):
        current[0] = start + timedelta(minutes=2)
        return {"admitted": True}

    monkeypatch.setattr(submission_admission, "evaluate_submission_admission", evaluate)
    result = job_acquisition.acquire_job(
        connection, target_url=url,
        authorization_manifest={"expires_at": (start + timedelta(minutes=1)).isoformat()},
        load_blocked=lambda: ([], []), application_lease_minutes=15,
        profile_loader=dict, clock=lambda: current[0], environ={},
    )
    assert result is None
    assert connection.execute("SELECT COUNT(*) FROM application_attempts").fetchone()[0] == 0


def test_conflicting_cell_claim_rolls_back_both_attempt_and_callback_write(database):
    connection, _ = database
    first = _job(connection, "a")
    second = _job(connection, "b")

    def claim(conn, job, attempt_id):
        assert conn.in_transaction
        conn.execute("INSERT INTO unrelated VALUES (?)", (job["url"],))
        if job["url"] == first:
            raise RuntimeCellConflictError("domain busy")
        return {"attempt": attempt_id}

    result = _acquire(connection, runtime_cell_claim=claim)
    assert result is not None and result["url"] == second
    assert [row[0] for row in connection.execute("SELECT value FROM unrelated")] == [second]
    assert [row[0] for row in connection.execute("SELECT job_url FROM application_attempts")] == [second]


def test_blocking_one_candidate_does_not_end_the_next_claim_transaction(database, monkeypatch):
    connection, _ = database
    first = _job(connection, "a")
    second = _job(connection, "b")
    monkeypatch.setattr(
        application_jobs.config, "portal_application_gate",
        lambda url, **_kwargs: "manual gate" if url == first else None,
    )

    def claim(conn, _job, _attempt_id):
        assert conn.in_transaction
        return {"lease": "next-job"}

    result = _acquire(connection, runtime_cell_claim=claim)
    assert result is not None and result["url"] == second
    assert connection.execute("SELECT apply_status FROM jobs WHERE url=?", (first,)).fetchone()[0] == "manual"
    assert not connection.in_transaction


def test_acquisition_rejects_a_callers_transaction_without_committing_it(database):
    connection, path = database
    url = _job(connection)
    connection.execute("INSERT INTO unrelated VALUES ('caller')")
    with pytest.raises(RuntimeError, match="without an active transaction"):
        _acquire(connection, target_url=url)
    assert connection.in_transaction
    other = _connect(path)
    try:
        assert other.execute("SELECT COUNT(*) FROM unrelated").fetchone()[0] == 0
    finally:
        other.close()
    connection.rollback()


def test_result_failure_rolls_back_job_and_attempt_together(database, monkeypatch):
    connection, _ = database
    url = _job(connection)
    result = _acquire(connection, target_url=url)
    before = dict(connection.execute("SELECT * FROM jobs WHERE url=?", (url,)).fetchone())

    def fail(*_args, **_kwargs):
        raise RuntimeError("injected ledger failure")

    monkeypatch.setattr("applypilot.database.finalize_application_attempt", fail)
    with pytest.raises(RuntimeError, match="injected ledger failure"):
        application_jobs.mark_result(connection, url, "failed", task_id=result["_attempt_id"])
    assert not connection.in_transaction
    assert dict(connection.execute("SELECT * FROM jobs WHERE url=?", (url,)).fetchone()) == before
    assert connection.execute("SELECT status FROM application_attempts").fetchone()[0] == "in_progress"


def test_nested_result_failure_keeps_callers_prior_work(database, monkeypatch):
    connection, path = database
    url = _job(connection)
    result = _acquire(connection, target_url=url)
    connection.execute("INSERT INTO unrelated VALUES ('caller')")

    def fail(*_args, **_kwargs):
        raise RuntimeError("injected nested failure")

    monkeypatch.setattr("applypilot.database.finalize_application_attempt", fail)
    with pytest.raises(RuntimeError, match="injected nested failure"):
        application_jobs.mark_result(connection, url, "failed", task_id=result["_attempt_id"])
    assert connection.in_transaction
    assert connection.execute("SELECT value FROM unrelated").fetchone()[0] == "caller"
    assert connection.execute("SELECT apply_status FROM jobs WHERE url=?", (url,)).fetchone()[0] == "in_progress"
    other = _connect(path)
    try:
        assert other.execute("SELECT COUNT(*) FROM unrelated").fetchone()[0] == 0
    finally:
        other.close()
    connection.rollback()


def test_nested_result_success_is_not_committed_until_caller_commits(database):
    connection, path = database
    url = _job(connection)
    result = _acquire(connection, target_url=url)
    connection.execute("BEGIN IMMEDIATE")
    application_jobs.mark_result(connection, url, "failed", task_id=result["_attempt_id"])
    assert connection.in_transaction
    other = _connect(path)
    try:
        assert other.execute("SELECT apply_status FROM jobs WHERE url=?", (url,)).fetchone()[0] == "in_progress"
        connection.commit()
        assert other.execute("SELECT apply_status FROM jobs WHERE url=?", (url,)).fetchone()[0] == "failed"
    finally:
        other.close()


@pytest.mark.parametrize("nested", [False, True])
def test_transaction_cancellation_rolls_back_only_its_unit(database, nested):
    connection, _ = database
    if nested:
        connection.execute("INSERT INTO unrelated VALUES ('outer')")
    with pytest.raises(KeyboardInterrupt):
        with write_transaction(connection):
            connection.execute("INSERT INTO unrelated VALUES ('cancelled')")
            raise KeyboardInterrupt
    assert bool(connection.in_transaction) is nested
    assert [row[0] for row in connection.execute("SELECT value FROM unrelated")] == (["outer"] if nested else [])
    if nested:
        connection.rollback()


def test_stale_attempt_cannot_change_a_newer_job_owner(database):
    connection, _ = database
    url = _job(connection)
    result = _acquire(connection, target_url=url)
    connection.execute("UPDATE jobs SET apply_task_id='new-owner' WHERE url=?", (url,))
    connection.commit()
    with pytest.raises(RuntimeError, match="stale application attempt"):
        application_jobs.mark_result(connection, url, "failed", task_id=result["_attempt_id"])
    assert connection.execute("SELECT apply_task_id FROM jobs WHERE url=?", (url,)).fetchone()[0] == "new-owner"
    assert not connection.in_transaction
