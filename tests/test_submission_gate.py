from __future__ import annotations

from datetime import UTC, datetime, timedelta

from applypilot import config
from applypilot.apply import launcher
from applypilot.database import (
    claim_submission_gate,
    init_db,
    start_application_attempt,
    update_application_attempt,
    update_submission_gate_state,
)


def _ready_attempt(conn, url: str, worker: str) -> str:
    conn.execute(
        "INSERT INTO jobs (url, apply_status) VALUES (?, 'in_progress')",
        (url,),
    )
    attempt_id = start_application_attempt(url, worker, conn=conn)
    assert update_application_attempt(
        attempt_id,
        phase="reservation",
        submit_started=False,
        conn=conn,
    )
    conn.commit()
    return attempt_id


def test_submission_gate_is_atomic_and_idempotent_for_one_attempt(tmp_path) -> None:
    conn = init_db(tmp_path / "gate.db")
    url = "https://jobs.example/1"
    attempt_id = _ready_attempt(conn, url, "worker-1")
    now = datetime(2026, 8, 29, 1, 0, tzinfo=UTC)

    first = claim_submission_gate(
        "batch-1",
        url,
        2,
        attempt_id,
        hourly_maximum=10,
        minimum_gap_seconds=0,
        audit_fingerprint="audit-1",
        conn=conn,
        now=now,
    )
    replay = claim_submission_gate(
        "batch-1",
        url,
        2,
        attempt_id,
        hourly_maximum=10,
        minimum_gap_seconds=0,
        audit_fingerprint="audit-1",
        conn=conn,
        now=now + timedelta(seconds=1),
    )

    assert first["claimed"] is True
    assert replay["claimed"] is True
    assert replay["replay"] is True
    assert replay["gate_id"] == first["gate_id"]
    assert conn.execute(
        "SELECT COUNT(*) FROM application_batch_consumptions"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM application_submission_gates"
    ).fetchone()[0] == 1


def test_submission_gate_enforces_global_gap_without_consuming_batch_slot(tmp_path) -> None:
    conn = init_db(tmp_path / "gap.db")
    first_url = "https://jobs.example/1"
    second_url = "https://jobs.example/2"
    first_attempt = _ready_attempt(conn, first_url, "worker-1")
    second_attempt = _ready_attempt(conn, second_url, "worker-2")
    now = datetime(2026, 8, 29, 1, 0, tzinfo=UTC)

    first = claim_submission_gate(
        "batch-1",
        first_url,
        2,
        first_attempt,
        hourly_maximum=10,
        minimum_gap_seconds=20,
        audit_fingerprint="audit-1",
        conn=conn,
        now=now,
    )
    blocked = claim_submission_gate(
        "batch-1",
        second_url,
        2,
        second_attempt,
        hourly_maximum=10,
        minimum_gap_seconds=20,
        audit_fingerprint="audit-2",
        conn=conn,
        now=now + timedelta(seconds=5),
    )

    assert first["claimed"] is True
    assert blocked["claimed"] is False
    assert blocked["reason"] == "minimum_submission_gap"
    assert 14 <= float(blocked["retry_after_seconds"]) <= 15
    assert conn.execute(
        "SELECT COUNT(*) FROM application_batch_consumptions"
    ).fetchone()[0] == 1


def test_submission_gate_requires_live_matching_attempt_and_tracks_outcome(tmp_path) -> None:
    conn = init_db(tmp_path / "attempt.db")
    url = "https://jobs.example/1"
    attempt_id = _ready_attempt(conn, url, "worker-1")
    now = datetime.now(UTC)

    mismatch = claim_submission_gate(
        "batch-1",
        "https://jobs.example/other",
        1,
        attempt_id,
        hourly_maximum=0,
        minimum_gap_seconds=0,
        conn=conn,
        now=now,
    )
    assert mismatch == {"claimed": False, "reason": "submission_gate_job_mismatch"}

    claimed = claim_submission_gate(
        "batch-1",
        url,
        1,
        attempt_id,
        hourly_maximum=0,
        minimum_gap_seconds=0,
        audit_fingerprint="audit-1",
        conn=conn,
        now=now,
    )
    assert claimed["claimed"] is True
    assert update_submission_gate_state(
        attempt_id,
        "submission_uncertain",
        {"submit_started": True},
        conn=conn,
    )
    stored = conn.execute(
        "SELECT state, evidence_json FROM application_submission_gates "
        "WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    assert stored["state"] == "submission_uncertain"
    assert "submit_started" in stored["evidence_json"]


def test_manifest_gate_revalidates_duplicate_in_the_same_transaction(
    tmp_path, monkeypatch
) -> None:
    conn = init_db(tmp_path / "duplicate-gate.db")
    url = "https://jobs.example/current"
    duplicate_url = "https://jobs.example/already-applied"
    attempt_id = _ready_attempt(conn, url, "worker-1")
    conn.execute(
        "UPDATE jobs SET canonical_job_url=? WHERE url=?",
        ("https://jobs.example/canonical/1", url),
    )
    conn.execute(
        "INSERT INTO jobs (url, canonical_job_url, apply_status) VALUES (?, ?, 'applied')",
        (duplicate_url, "https://jobs.example/canonical/1"),
    )
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(
        "applypilot.apply.authorization.authorize_job",
        lambda _manifest, supplied_job: {"url": supplied_job["url"]},
    )
    monkeypatch.setattr(
        "applypilot.apply.authorization.freeze_submission_materials",
        lambda _job, _profile: {"version": 1, "materials": []},
    )
    monkeypatch.setattr(
        config,
        "load_profile",
        lambda: {
            "submission_policy": {
                "maximum_verified_submissions_per_rolling_hour": 10,
                "minimum_seconds_between_verified_submissions": 0,
            }
        },
    )
    manifest = {
        "batch_id": "batch-1",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "max_submissions": 1,
    }
    job = {"url": url, "_attempt_id": attempt_id}

    claimed = launcher._reserve_manifest_submission(
        manifest,
        job,
        {"disposition": "clear", "submit_control_count": 1},
    )

    assert claimed == (False, "duplicate_submission_identity")
    assert job["_duplicate_revalidation"]["matched_job_url"] == duplicate_url
    assert conn.in_transaction is False
    assert conn.execute(
        "SELECT COUNT(*) FROM application_batch_consumptions"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM application_submission_gates"
    ).fetchone()[0] == 0
