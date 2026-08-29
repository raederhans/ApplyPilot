from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from applypilot import config
from applypilot.apply import authorization, launcher
from applypilot.apply.run_progress import RunProgress
from applypilot.database import (
    admit_direct_email_sent_receipt,
    bind_admitted_receipt_to_gate,
    claim_submission_gate,
    has_admitted_submission_receipt,
    init_db,
    reconcile_submission_receipt,
    start_application_attempt,
    update_application_attempt,
    update_batch_submission_status,
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


def _with_causal_apply_attestation(job: dict, audit: dict) -> dict:
    target_id = "causal-target"
    source_target_id = "linkedin-root"
    attestation_id = "attestation-private-id"
    final_url = str(audit.get("page_url") or "")
    source_url = str(job.get("url") or job.get("application_url") or "")
    source_job_id = urlparse(source_url).path.rstrip("/").rsplit("/", 1)[-1]
    job["_linkedin_causal_apply_attestation"] = {
        "version": 1,
        "attestation_id": attestation_id,
        "source_job_id": source_job_id,
        "source_target_id": source_target_id,
        "target_id": target_id,
        "target_id_digest": hashlib.sha256(target_id.encode()).hexdigest(),
        "mode": "new_popup_from_source",
        "initial_url": final_url,
        "final_url": final_url,
        "redirect_lineage": [final_url],
        "lineage_complete": True,
    }
    return {
        **audit,
        "causal_apply_attestation": {
            "version": 1,
            "verified": True,
            "attestation_id_digest": hashlib.sha256(attestation_id.encode()).hexdigest(),
            "source_target_id_digest": hashlib.sha256(source_target_id.encode()).hexdigest(),
            "target_id_digest": hashlib.sha256(target_id.encode()).hexdigest(),
            "mode": "new_popup_from_source",
            "initial_url": final_url,
            "final_url": final_url,
            "redirect_lineage": [final_url],
            "lineage_complete": True,
        },
    }


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


def test_submission_gate_holds_one_writer_until_terminal_state(tmp_path) -> None:
    conn = init_db(tmp_path / "writer.db")
    first_url = "https://jobs.example/1"
    second_url = "https://jobs.example/2"
    first_attempt = _ready_attempt(conn, first_url, "worker-1")
    second_attempt = _ready_attempt(conn, second_url, "worker-2")
    now = datetime.now(UTC)

    first = claim_submission_gate(
        "batch-1",
        first_url,
        2,
        first_attempt,
        hourly_maximum=10,
        minimum_gap_seconds=0,
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
        minimum_gap_seconds=0,
        audit_fingerprint="audit-2",
        conn=conn,
        now=now + timedelta(seconds=30),
    )

    assert first["claimed"] is True
    assert blocked["claimed"] is False
    assert blocked["reason"] == "submit_writer_busy"
    assert conn.execute(
        "SELECT COUNT(*) FROM application_batch_consumptions"
    ).fetchone()[0] == 1

    assert update_submission_gate_state(first_attempt, "applied", conn=conn)
    released = claim_submission_gate(
        "batch-1",
        second_url,
        2,
        second_attempt,
        hourly_maximum=10,
        minimum_gap_seconds=0,
        audit_fingerprint="audit-2",
        conn=conn,
        now=now + timedelta(seconds=31),
    )
    assert released["claimed"] is True


def test_submission_gate_separates_receipt_success_target_from_slot_capacity(tmp_path) -> None:
    conn = init_db(tmp_path / "target.db")
    first_url = "https://jobs.example/uncertain"
    second_url = "https://jobs.example/success"
    third_url = "https://jobs.example/third"
    first_attempt = _ready_attempt(conn, first_url, "worker-1")
    second_attempt = _ready_attempt(conn, second_url, "worker-2")
    third_attempt = _ready_attempt(conn, third_url, "worker-3")
    now = datetime.now(UTC)

    first = claim_submission_gate(
        "batch-target", first_url, 2, first_attempt,
        success_target=1, hourly_maximum=10, minimum_gap_seconds=0,
        conn=conn, now=now,
    )
    assert first["claimed"] is True
    assert update_submission_gate_state(first_attempt, "submission_uncertain", conn=conn)

    second = claim_submission_gate(
        "batch-target", second_url, 2, second_attempt,
        success_target=1, hourly_maximum=10, minimum_gap_seconds=0,
        conn=conn, now=now + timedelta(seconds=1),
    )
    assert second["claimed"] is True
    assert update_submission_gate_state(second_attempt, "applied", conn=conn)
    update_batch_submission_status(
        "batch-target", second_url, "applied", conn=conn
    )

    # Merely marking a gate applied does not satisfy the run target.
    blocked_by_capacity = claim_submission_gate(
        "batch-target", third_url, 2, third_attempt,
        success_target=1, hourly_maximum=10, minimum_gap_seconds=0,
        conn=conn, now=now + timedelta(seconds=2),
    )
    assert blocked_by_capacity == {
        "claimed": False,
        "reason": "authorization_batch_capacity_exhausted",
    }

    conn.execute(
        "INSERT INTO application_receipts "
        "(receipt_source, receipt_id, job_url, admitted_at, receipt_digest) "
        "VALUES ('email', 'receipt-1', ?, ?, 'digest')",
        (second_url, (now + timedelta(seconds=2)).isoformat()),
    )
    conn.commit()
    assert bind_admitted_receipt_to_gate(
        "email",
        "receipt-1",
        second["gate_id"],
        "batch-target",
        second_url,
        second_attempt,
        bound_at=now + timedelta(seconds=2),
        conn=conn,
    )
    conn.commit()
    reached = claim_submission_gate(
        "batch-target", third_url, 3, third_attempt,
        success_target=1, hourly_maximum=10, minimum_gap_seconds=0,
        conn=conn, now=now + timedelta(seconds=3),
    )
    assert reached == {"claimed": False, "reason": "run_success_target_reached"}


def test_durable_receipt_is_required_for_run_success_and_next_gate_stop(tmp_path) -> None:
    conn = init_db(tmp_path / "durable-target.db")
    first_url = "https://jobs.example/durable-1"
    second_url = "https://jobs.example/durable-2"
    first_attempt = _ready_attempt(conn, first_url, "worker-1")
    second_attempt = _ready_attempt(conn, second_url, "worker-2")
    now = datetime.now(UTC)
    first = claim_submission_gate(
        "batch-durable", first_url, 2, first_attempt,
        success_target=1, hourly_maximum=10, minimum_gap_seconds=0,
        conn=conn, now=now,
    )
    assert first["claimed"] is True
    update_batch_submission_status(
        "batch-durable", first_url, "applied", conn=conn
    )
    assert update_submission_gate_state(first_attempt, "applied", conn=conn)

    progress_without_receipt = RunProgress(
        dry_run=False, success_target=1, preview_target=1,
        authorization_slot_cap=2,
    )
    admitted = has_admitted_submission_receipt(
        "batch-durable", first_url, first_attempt, conn=conn
    )
    assert admitted is False
    progress_without_receipt.record_terminal(
        first_url, "applied", receipt_confirmed=admitted
    )
    assert progress_without_receipt.snapshot()["receipt_confirmed_successes"] == 0

    conn.execute(
        "INSERT INTO application_receipts "
        "(receipt_source, receipt_id, job_url, admitted_at, receipt_digest) "
        "VALUES ('browser', 'durable-1', ?, ?, 'digest')",
        (first_url, now.isoformat()),
    )
    conn.commit()
    # A later receipt for the same job remains ineligible until its admission
    # explicitly binds it to this exact gate identity.
    admitted = has_admitted_submission_receipt(
        "batch-durable", first_url, first_attempt, conn=conn
    )
    assert admitted is False
    assert bind_admitted_receipt_to_gate(
        "browser",
        "durable-1",
        first["gate_id"],
        "batch-durable",
        first_url,
        first_attempt,
        bound_at=now + timedelta(milliseconds=1),
        conn=conn,
    )
    conn.commit()
    admitted = has_admitted_submission_receipt(
        "batch-durable", first_url, first_attempt, conn=conn
    )
    assert admitted is True
    progress_with_receipt = RunProgress(
        dry_run=False, success_target=1, preview_target=1,
        authorization_slot_cap=2,
    )
    progress_with_receipt.record_terminal(
        first_url, "applied", receipt_confirmed=admitted
    )
    assert progress_with_receipt.snapshot()["receipt_confirmed_successes"] == 1

    stopped = claim_submission_gate(
        "batch-durable", second_url, 2, second_attempt,
        success_target=1, hourly_maximum=10, minimum_gap_seconds=0,
        conn=conn, now=now + timedelta(seconds=1),
    )
    assert stopped == {"claimed": False, "reason": "run_success_target_reached"}


def test_receipt_binding_rejects_other_attempt_for_same_job(tmp_path) -> None:
    conn = init_db(tmp_path / "other-attempt.db")
    job_url = "https://jobs.example/exact-attempt"
    attempt_id = _ready_attempt(conn, job_url, "worker-1")
    now = datetime.now(UTC)
    claim = claim_submission_gate(
        "batch-exact", job_url, 2, attempt_id,
        success_target=1, hourly_maximum=10, minimum_gap_seconds=0,
        conn=conn, now=now,
    )
    conn.execute(
        "INSERT INTO application_receipts "
        "(receipt_source, receipt_id, job_url, admitted_at, receipt_digest) "
        "VALUES ('browser', 'exact-attempt', ?, ?, 'digest')",
        (job_url, now.isoformat()),
    )
    assert bind_admitted_receipt_to_gate(
        "browser", "exact-attempt", claim["gate_id"], "batch-exact",
        job_url, attempt_id, bound_at=now + timedelta(milliseconds=1), conn=conn,
    )
    conn.commit()

    assert has_admitted_submission_receipt(
        "batch-exact", job_url, attempt_id, conn=conn
    )
    assert not has_admitted_submission_receipt(
        "batch-exact", job_url, "different-attempt", conn=conn
    )


def test_receipt_binding_compares_offset_times_by_utc_epoch(tmp_path) -> None:
    conn = init_db(tmp_path / "offset-binding.db")
    job_url = "https://jobs.example/offset"
    attempt_id = _ready_attempt(conn, job_url, "worker-1")
    claimed_at = datetime.fromisoformat("2026-08-29T10:00:00+08:00")
    claim = claim_submission_gate(
        "batch-offset", job_url, 1, attempt_id,
        success_target=1, hourly_maximum=10, minimum_gap_seconds=0,
        conn=conn, now=claimed_at,
    )
    conn.execute(
        "INSERT INTO application_receipts "
        "(receipt_source, receipt_id, job_url, admitted_at, receipt_digest) "
        "VALUES ('browser', 'offset-receipt', ?, ?, 'digest')",
        (job_url, claimed_at.isoformat()),
    )

    assert not bind_admitted_receipt_to_gate(
        "browser", "offset-receipt", claim["gate_id"], "batch-offset",
        job_url, attempt_id,
        bound_at=datetime.fromisoformat("2026-08-28T21:59:59-04:00"), conn=conn,
    )
    assert bind_admitted_receipt_to_gate(
        "browser", "offset-receipt", claim["gate_id"], "batch-offset",
        job_url, attempt_id,
        bound_at=datetime.fromisoformat("2026-08-28T22:00:01-04:00"), conn=conn,
    )
    conn.commit()
    assert has_admitted_submission_receipt(
        "batch-offset", job_url, attempt_id, conn=conn
    )


def test_receipt_binding_fails_closed_for_invalid_or_missing_claim_time(tmp_path) -> None:
    conn = init_db(tmp_path / "invalid-claim-time.db")
    job_url = "https://jobs.example/invalid-claim-time"
    attempt_id = _ready_attempt(conn, job_url, "worker-1")
    now = datetime.now(UTC)
    claim = claim_submission_gate(
        "batch-invalid-time", job_url, 1, attempt_id,
        success_target=1, hourly_maximum=10, minimum_gap_seconds=0,
        conn=conn, now=now,
    )
    conn.execute(
        "INSERT INTO application_receipts "
        "(receipt_source, receipt_id, job_url, admitted_at, receipt_digest) "
        "VALUES ('browser', 'invalid-time', ?, ?, 'digest')",
        (job_url, now.isoformat()),
    )
    conn.execute(
        "UPDATE application_submission_gates "
        "SET claimed_at='not-a-time', claimed_at_epoch=NULL WHERE gate_id=?",
        (claim["gate_id"],),
    )
    conn.commit()

    assert not bind_admitted_receipt_to_gate(
        "browser", "invalid-time", claim["gate_id"], "batch-invalid-time",
        job_url, attempt_id, bound_at=now + timedelta(seconds=1), conn=conn,
    )
    assert not has_admitted_submission_receipt(
        "batch-invalid-time", job_url, attempt_id, conn=conn
    )
    assert not bind_admitted_receipt_to_gate(
        "browser", "invalid-time", "missing-gate", "batch-invalid-time",
        job_url, attempt_id, bound_at=now + timedelta(seconds=1), conn=conn,
    )


def test_direct_email_admission_atomically_writes_exact_gate_binding(tmp_path) -> None:
    conn = init_db(tmp_path / "direct-email-binding.db")
    job_url = "https://jobs.example/direct-email"
    attempt_id = _ready_attempt(conn, job_url, "worker-1")
    now = datetime.now(UTC)
    claim = claim_submission_gate(
        "batch-email", job_url, 1, attempt_id,
        success_target=1, hourly_maximum=10, minimum_gap_seconds=0,
        conn=conn, now=now,
    )
    result = admit_direct_email_sent_receipt(
        job_url,
        {
            "folder": "sent",
            "provider_message_id": "provider-message-1",
            "recipient": "jobs@example.test",
            "subject": "Application for role",
            "attachment_names": ["resume.pdf"],
            "body_sha256": "a" * 64,
        },
        conn=conn,
        gate_binding={
            "gate_id": claim["gate_id"],
            "batch_id": "batch-email",
            "attempt_id": attempt_id,
        },
    )

    assert result == {"status": "admitted", "job_url": job_url, "gate_bound": True}
    assert has_admitted_submission_receipt(
        "batch-email", job_url, attempt_id, conn=conn
    )


def test_reconcile_requires_explicit_gate_identity_to_advance_current_run(tmp_path) -> None:
    conn = init_db(tmp_path / "reconcile-binding.db")
    job_url = "https://jobs.example/reconcile"
    second_url = "https://jobs.example/reconcile-next"
    attempt_id = _ready_attempt(conn, job_url, "worker-1")
    second_attempt = _ready_attempt(conn, second_url, "worker-2")
    now = datetime.now(UTC)
    claim = claim_submission_gate(
        "batch-reconcile", job_url, 2, attempt_id,
        success_target=1, hourly_maximum=10, minimum_gap_seconds=0,
        conn=conn, now=now,
    )
    conn.execute(
        "UPDATE jobs SET apply_status='submission_uncertain', "
        "company_name='Example', title='Automation Intern' WHERE url=?",
        (job_url,),
    )
    assert update_submission_gate_state(
        attempt_id, "submission_uncertain", conn=conn
    )
    update_batch_submission_status(
        "batch-reconcile", job_url, "submission_uncertain", conn=conn
    )
    conn.commit()
    evidence = {
        "source": "browser_receipt",
        "receipt_id": "reconcile-exact-1",
        "job_url": job_url,
        "company_name": "Example",
        "job_title": "Automation Intern",
        "confirmation_text": "Application successfully submitted",
    }

    legacy = reconcile_submission_receipt(evidence, conn=conn)
    assert legacy["status"] == "applied"
    assert not has_admitted_submission_receipt(
        "batch-reconcile", job_url, attempt_id, conn=conn
    )
    legacy_states = conn.execute(
        "SELECT g.state, c.status FROM application_submission_gates g "
        "JOIN application_batch_consumptions c "
        "ON c.batch_id=g.batch_id AND c.job_url=g.job_url WHERE g.gate_id=?",
        (claim["gate_id"],),
    ).fetchone()
    assert tuple(legacy_states) == ("submission_uncertain", "submission_uncertain")

    exact = reconcile_submission_receipt(
        {
            **evidence,
            "gate_id": claim["gate_id"],
            "batch_id": "batch-reconcile",
            "attempt_id": attempt_id,
        },
        conn=conn,
    )
    assert exact == {
        "status": "applied",
        "job_url": job_url,
        "changed": False,
        "gate_bound": True,
    }
    assert has_admitted_submission_receipt(
        "batch-reconcile", job_url, attempt_id, conn=conn
    )
    exact_states = conn.execute(
        "SELECT g.state, c.status FROM application_submission_gates g "
        "JOIN application_batch_consumptions c "
        "ON c.batch_id=g.batch_id AND c.job_url=g.job_url WHERE g.gate_id=?",
        (claim["gate_id"],),
    ).fetchone()
    assert tuple(exact_states) == ("applied", "applied")
    stopped = claim_submission_gate(
        "batch-reconcile", second_url, 2, second_attempt,
        success_target=1, hourly_maximum=10, minimum_gap_seconds=0,
        conn=conn, now=now + timedelta(seconds=1),
    )
    assert stopped == {"claimed": False, "reason": "run_success_target_reached"}


def test_exact_reconcile_rolls_back_when_gate_is_not_uncertain(tmp_path) -> None:
    conn = init_db(tmp_path / "reconcile-invalid-transition.db")
    job_url = "https://jobs.example/reconcile-invalid"
    attempt_id = _ready_attempt(conn, job_url, "worker-1")
    claim = claim_submission_gate(
        "batch-reconcile-invalid", job_url, 1, attempt_id,
        success_target=1, hourly_maximum=10, minimum_gap_seconds=0,
        conn=conn, now=datetime.now(UTC),
    )
    conn.execute(
        "UPDATE jobs SET apply_status='submission_uncertain', "
        "company_name='Example', title='Automation Intern' WHERE url=?",
        (job_url,),
    )
    conn.commit()

    result = reconcile_submission_receipt(
        {
            "source": "browser_receipt",
            "receipt_id": "invalid-transition-1",
            "job_url": job_url,
            "company_name": "Example",
            "job_title": "Automation Intern",
            "confirmation_text": "Application successfully submitted",
            "gate_id": claim["gate_id"],
            "batch_id": "batch-reconcile-invalid",
            "attempt_id": attempt_id,
        },
        conn=conn,
    )

    assert result == {
        "status": "rejected",
        "reason": "submission_gate_transition_invalid",
        "job_url": job_url,
    }
    assert conn.execute(
        "SELECT apply_status FROM jobs WHERE url=?", (job_url,)
    ).fetchone()[0] == "submission_uncertain"
    assert conn.execute(
        "SELECT 1 FROM application_receipts WHERE receipt_id='invalid-transition-1'"
    ).fetchone() is None
    states = conn.execute(
        "SELECT g.state, c.status FROM application_submission_gates g "
        "JOIN application_batch_consumptions c "
        "ON c.batch_id=g.batch_id AND c.job_url=g.job_url WHERE g.gate_id=?",
        (claim["gate_id"],),
    ).fetchone()
    assert tuple(states) == ("claimed", "reserved")


def test_submission_gate_reports_job_reservation_before_remaining_capacity(tmp_path) -> None:
    conn = init_db(tmp_path / "job-reserved.db")
    url = "https://jobs.example/same"
    first_attempt = _ready_attempt(conn, url, "worker-1")
    second_attempt = start_application_attempt(url, "worker-2", conn=conn)
    assert update_application_attempt(
        second_attempt, phase="reservation", submit_started=False, conn=conn
    )
    conn.commit()
    now = datetime.now(UTC)

    first = claim_submission_gate(
        "batch-reserved", url, 2, first_attempt,
        success_target=2, hourly_maximum=10, minimum_gap_seconds=0,
        conn=conn, now=now,
    )
    assert first["claimed"] is True
    assert update_submission_gate_state(first_attempt, "failed", conn=conn)

    duplicate = claim_submission_gate(
        "batch-reserved", url, 2, second_attempt,
        success_target=2, hourly_maximum=10, minimum_gap_seconds=0,
        conn=conn, now=now + timedelta(seconds=1),
    )
    assert duplicate == {"claimed": False, "reason": "job_already_reserved"}


def test_submission_gate_without_success_target_preserves_legacy_denial_reason(
    tmp_path,
) -> None:
    conn = init_db(tmp_path / "legacy-denial.db")
    first_url = "https://jobs.example/legacy-1"
    second_url = "https://jobs.example/legacy-2"
    first_attempt = _ready_attempt(conn, first_url, "worker-1")
    duplicate_attempt = start_application_attempt(first_url, "worker-2", conn=conn)
    assert update_application_attempt(
        duplicate_attempt, phase="reservation", submit_started=False, conn=conn
    )
    second_attempt = _ready_attempt(conn, second_url, "worker-3")
    now = datetime.now(UTC)

    assert claim_submission_gate(
        "batch-legacy", first_url, 1, first_attempt,
        hourly_maximum=10, minimum_gap_seconds=0, conn=conn, now=now,
    )["claimed"] is True
    assert update_submission_gate_state(first_attempt, "failed", conn=conn)

    duplicate = claim_submission_gate(
        "batch-legacy", first_url, 2, duplicate_attempt,
        hourly_maximum=10, minimum_gap_seconds=0,
        conn=conn, now=now + timedelta(seconds=1),
    )
    exhausted = claim_submission_gate(
        "batch-legacy", second_url, 1, second_attempt,
        hourly_maximum=10, minimum_gap_seconds=0,
        conn=conn, now=now + timedelta(seconds=2),
    )

    assert duplicate == {
        "claimed": False,
        "reason": "authorization_batch_reservation_denied",
    }
    assert exhausted == {
        "claimed": False,
        "reason": "authorization_batch_reservation_denied",
    }


def test_submission_gate_serializes_race_for_last_authorization_slot(tmp_path) -> None:
    db_path = tmp_path / "gate-race.db"
    conn = init_db(db_path)
    urls = ("https://jobs.example/race-1", "https://jobs.example/race-2")
    attempts = tuple(
        _ready_attempt(conn, url, f"worker-{index}")
        for index, url in enumerate(urls, start=1)
    )
    conn.commit()
    barrier = threading.Barrier(2, timeout=5)
    now = datetime.now(UTC)

    def compete(candidate: tuple[str, str]) -> dict[str, object]:
        url, attempt_id = candidate
        worker_conn = sqlite3.connect(db_path, timeout=5)
        worker_conn.row_factory = sqlite3.Row
        try:
            barrier.wait()
            return claim_submission_gate(
                "batch-race", url, 1, attempt_id,
                success_target=2, hourly_maximum=10, minimum_gap_seconds=0,
                conn=worker_conn, now=now,
            )
        finally:
            worker_conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, zip(urls, attempts, strict=True)))

    assert sum(result["claimed"] is True for result in results) == 1
    assert sum(
        result.get("reason") == "authorization_batch_capacity_exhausted"
        for result in results
    ) == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM application_batch_consumptions WHERE batch_id='batch-race'"
    ).fetchone()[0] == 1


def test_linkedin_external_apply_handoff_is_rebound_before_submission(
    tmp_path, monkeypatch
) -> None:
    conn = init_db(tmp_path / "linkedin-handoff.db")
    linkedin_url = "https://www.linkedin.com/jobs/view/1001"
    attempt_id = _ready_attempt(conn, linkedin_url, "worker-1")
    conn.execute(
        "UPDATE jobs SET apply_error='stale pre-handoff failure' WHERE url=?",
        (linkedin_url,),
    )
    conn.commit()
    job = {
        "url": linkedin_url,
        "application_url": linkedin_url,
        "source_site": "linkedin",
        "site": "linkedin",
        "title": "Data Engineering Intern",
        "company_name": "Acme",
        "_attempt_id": attempt_id,
    }
    manifest = {
        "batch_id": "batch-linkedin",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "max_submissions": 1,
    }
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(
        launcher.config,
        "load_profile",
        lambda: {
            "submission_policy": {
                "allowed_submission_surfaces": [
                    "linkedin_apply_entry",
                    "linkedin_native_easy_apply",
                    "linkedin_to_official_ats",
                ]
            }
        },
    )
    monkeypatch.setattr(
        "applypilot.apply.authorization.authorize_job",
        lambda _manifest, _job: {"url": linkedin_url},
    )

    reserved = launcher._reserve_manifest_submission(
        manifest,
        job,
        _with_causal_apply_attestation(job, {
            "disposition": "linkedin_external_handoff",
            "page_url": "https://boards.greenhouse.io/acme/jobs/123?source=linkedin",
            "page_identity": {
                "version": 1,
                "page_title": "Data Engineering Intern | Acme Careers",
                "primary_headings": ["Data Engineering Intern", "Acme"],
            },
            "submit_control_count": 1,
        }),
    )

    assert reserved == (False, "linkedin_external_handoff_reauthorized")
    stored = conn.execute(
        "SELECT application_url, apply_error FROM jobs WHERE url=?",
        (linkedin_url,),
    ).fetchone()
    assert stored["application_url"] == "https://boards.greenhouse.io/acme/jobs/123"
    assert stored["apply_error"] is None
    assert job["_discovered_application_url"] == stored["application_url"]
    assert conn.execute(
        "SELECT COUNT(*) FROM application_batch_consumptions"
    ).fetchone()[0] == 0


def test_linkedin_runtime_gate_uses_landing_host_without_source_metadata() -> None:
    allowed, reason = launcher._runtime_linkedin_route_gate(
        {
            "url": "https://www.linkedin.com/jobs/view/1001",
            "application_url": "https://www.linkedin.com/jobs/view/1001",
        },
        {
            "page_url": "https://www.linkedin.com/jobs/view/1001/apply",
            "submit_control_count": 1,
        },
        {
            "submission_policy": {
                "allowed_submission_surfaces": ["official_company_careers"]
            }
        },
    )

    assert allowed is False
    assert reason == "submission_surface_not_allowed:linkedin_native_easy_apply"


def test_linkedin_preview_handoff_verifies_without_persisting(tmp_path, monkeypatch) -> None:
    conn = init_db(tmp_path / "linkedin-preview-handoff.db")
    linkedin_url = "https://www.linkedin.com/jobs/view/1002"
    _ready_attempt(conn, linkedin_url, "worker-1")
    job = {
        "url": linkedin_url,
        "application_url": linkedin_url,
        "source_site": "linkedin",
        "title": "Data Engineering Intern",
        "company_name": "Acme",
    }
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    allowed, reason = launcher._runtime_linkedin_route_gate(
        job,
        _with_causal_apply_attestation(job, {
            "page_url": "https://boards.greenhouse.io/acme/jobs/456?source=linkedin",
            "disposition": "linkedin_external_handoff",
            "page_identity": {
                "version": 1,
                "page_title": "Data Engineering Intern | Acme Careers",
                "primary_headings": ["Data Engineering Intern", "Acme"],
            },
            "submit_control_count": 0,
        }),
        {
            "submission_policy": {
                "allowed_submission_surfaces": ["linkedin_to_official_ats"]
            }
        },
        persist_external_handoff=False,
    )

    assert allowed is False
    assert reason == "linkedin_external_handoff_preview_verified"
    assert conn.execute(
        "SELECT application_url FROM jobs WHERE url=?", (linkedin_url,)
    ).fetchone()["application_url"] is None
    assert job["_discovered_application_url"] == (
        "https://boards.greenhouse.io/acme/jobs/456"
    )


def test_linkedin_runtime_route_reuses_exact_source_authorization_for_same_attempt(
    tmp_path, monkeypatch
) -> None:
    conn = init_db(tmp_path / "linkedin-runtime-route.db")
    linkedin_url = "https://www.linkedin.com/jobs/view/1003"
    workday_url = "https://example.wd5.myworkdayjobs.com/External/job/role"
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-runtime-route")
    attempt_id = _ready_attempt(conn, linkedin_url, "worker-1")
    job = {
        "url": linkedin_url,
        "application_url": linkedin_url,
        "title": "Data Engineering Intern",
        "company_name": "Example",
        "location": "Singapore",
        "full_description": "Build data pipelines and applied AI systems.",
        "tailored_resume_path": str(resume),
        "tailor_status": "machine_validated",
        "cover_letter_status": "not_required",
        "_attempt_id": attempt_id,
    }
    manifest = authorization.build_bound_manifest([job], max_submissions=1)
    profile = {
        "submission_policy": {
            "allowed_submission_surfaces": ["linkedin_to_official_ats"]
        }
    }
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    allowed, reason = launcher._runtime_linkedin_route_gate(
        job,
        _with_causal_apply_attestation(job, {
            "disposition": "linkedin_external_handoff",
            "page_url": workday_url,
            "page_identity": {
                "version": 1,
                "page_title": "Data Engineering Internship | Example Careers",
                "primary_headings": ["Data Engineering Intern", "Example"],
            },
            "submit_control_count": 0,
        }),
        profile,
    )

    assert allowed is False
    assert reason == "linkedin_external_handoff_reauthorized"
    job["application_url"] = job["_discovered_application_url"]
    route_allowed, route_reason = launcher._authorize_linkedin_runtime_route(
        manifest,
        job,
        profile,
    )
    assert route_allowed is True
    assert route_reason == "linkedin_runtime_route_authorized"
    assert manifest["jobs"][0]["application_url"] == linkedin_url
    assert job["_linkedin_runtime_route_binding"]["attempt_id"] == attempt_id
    attempt = conn.execute(
        "SELECT phase, evidence_json FROM application_attempts WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    assert attempt["phase"] == "route_handoff"
    assert json.loads(attempt["evidence_json"])["linkedin_runtime_route_binding"][
        "target_application_url"
    ] == workday_url
    persisted_binding = json.loads(attempt["evidence_json"])[
        "linkedin_runtime_route_binding"
    ]
    assert persisted_binding["causal_apply_attestation"]["verified"] is True
    assert "target_id" not in persisted_binding["causal_apply_attestation"]

    tampered = dict(job)
    tampered["_attempt_id"] = "different-attempt"
    assert launcher._authorize_linkedin_runtime_route(
        manifest,
        tampered,
        profile,
    ) == (False, "linkedin_runtime_route_attempt_mismatch")

    attestation_tampered = dict(job)
    attestation_tampered["_linkedin_runtime_route_binding"] = {
        **job["_linkedin_runtime_route_binding"],
        "causal_apply_attestation": {
            **job["_linkedin_runtime_route_binding"]["causal_apply_attestation"],
            "target_id_digest": "c" * 64,
        },
    }
    assert launcher._authorize_linkedin_runtime_route(
        manifest,
        attestation_tampered,
        profile,
    ) == (False, "linkedin_runtime_route_causal_attestation_mismatch")


def test_linkedin_runtime_route_preserves_identity_query_and_fragment(
    tmp_path, monkeypatch
) -> None:
    conn = init_db(tmp_path / "linkedin-query-route.db")
    linkedin_url = "https://www.linkedin.com/jobs/view/1004"
    _ready_attempt(conn, linkedin_url, "worker-1")
    job = {
        "url": linkedin_url,
        "application_url": linkedin_url,
        "title": "Machine Learning Intern",
        "company_name": "Acme",
    }
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    allowed, reason = launcher._runtime_linkedin_route_gate(
        job,
        _with_causal_apply_attestation(job, {
            "disposition": "linkedin_external_handoff",
            "page_url": (
                "https://jobs.smartrecruiters.com/acme/apply?jobId=123"
                "&utm_source=linkedin&source=linkedin#job/123"
            ),
            "page_identity": {
                "version": 1,
                "page_title": "Machine Learning Intern | Acme",
                "primary_headings": ["Machine Learning Intern", "Acme"],
            },
        }),
        {
            "submission_policy": {
                "allowed_submission_surfaces": ["linkedin_to_official_ats"]
            }
        },
        persist_external_handoff=False,
    )

    assert allowed is False
    assert reason == "linkedin_external_handoff_preview_verified"
    assert job["_discovered_application_url"] == (
        "https://jobs.smartrecruiters.com/acme/apply?jobId=123#job/123"
    )


def test_linkedin_runtime_route_rejects_wrong_job_on_shared_ats() -> None:
    job = {
        "url": "https://www.linkedin.com/jobs/view/1005",
        "application_url": "https://www.linkedin.com/jobs/view/1005",
        "title": "Data Engineering Intern",
        "company_name": "Acme",
    }

    allowed, reason = launcher._runtime_linkedin_route_gate(
        job,
        _with_causal_apply_attestation(job, {
            "disposition": "linkedin_external_handoff",
            "page_url": "https://boards.greenhouse.io/acme/jobs/999",
            "page_identity": {
                "version": 1,
                "page_title": "Product Marketing Manager | Acme",
                "primary_headings": ["Product Marketing Manager", "Acme"],
            },
        }),
        {
            "submission_policy": {
                "allowed_submission_surfaces": ["linkedin_to_official_ats"]
            }
        },
        persist_external_handoff=False,
    )

    assert allowed is False
    assert reason == "linkedin_external_job_identity_unverified"
    assert "_linkedin_runtime_route_binding" not in job


def test_linkedin_runtime_route_rejects_matching_title_company_without_causal_attestation() -> None:
    job = {
        "url": "https://www.linkedin.com/jobs/view/1007",
        "application_url": "https://www.linkedin.com/jobs/view/1007",
        "title": "Data Engineering Intern",
        "company_name": "Acme",
    }

    allowed, reason = launcher._runtime_linkedin_route_gate(
        job,
        {
            "disposition": "linkedin_external_handoff",
            "page_url": "https://boards.greenhouse.io/acme/jobs/other-requisition",
            "page_identity": {
                "version": 1,
                "page_title": "Data Engineering Intern | Acme",
                "primary_headings": ["Data Engineering Intern", "Acme"],
            },
        },
        {
            "submission_policy": {
                "allowed_submission_surfaces": ["linkedin_to_official_ats"]
            }
        },
        persist_external_handoff=False,
    )

    assert allowed is False
    assert reason == "linkedin_external_causal_apply_attestation_required"
    assert "_linkedin_runtime_route_binding" not in job


def test_linkedin_runtime_route_accepts_verified_causal_attestation(
    tmp_path, monkeypatch
) -> None:
    conn = init_db(tmp_path / "linkedin-causal-attestation.db")
    linkedin_url = "https://www.linkedin.com/jobs/view/1008"
    _ready_attempt(conn, linkedin_url, "worker-1")
    job = {
        "url": linkedin_url,
        "application_url": linkedin_url,
        "title": "Data Engineering Intern",
        "company_name": "Acme",
    }
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    allowed, reason = launcher._runtime_linkedin_route_gate(
        job,
        _with_causal_apply_attestation(job, {
            "disposition": "linkedin_external_handoff",
            "page_url": "https://boards.greenhouse.io/acme/jobs/1008",
            "page_identity": {
                "version": 1,
                "page_title": "Data Engineering Intern | Acme",
                "primary_headings": ["Data Engineering Intern", "Acme"],
            },
        }),
        {
            "submission_policy": {
                "allowed_submission_surfaces": ["linkedin_to_official_ats"]
            }
        },
        persist_external_handoff=False,
    )

    assert allowed is False
    assert reason == "linkedin_external_handoff_preview_verified"
    assert job["_linkedin_runtime_route_binding"]["causal_apply_attestation"][
        "verified"
    ] is True


def test_linkedin_runtime_route_authorization_rechecks_identity_evidence(
    tmp_path, monkeypatch
) -> None:
    conn = init_db(tmp_path / "linkedin-route-identity.db")
    linkedin_url = "https://www.linkedin.com/jobs/view/1006"
    attempt_id = _ready_attempt(conn, linkedin_url, "worker-1")
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-runtime-route-identity")
    job = {
        "url": linkedin_url,
        "application_url": linkedin_url,
        "title": "Data Engineering Intern",
        "company_name": "Acme",
        "tailored_resume_path": str(resume),
        "_attempt_id": attempt_id,
    }
    manifest = authorization.build_bound_manifest([job], max_submissions=1)
    profile = {
        "submission_policy": {
            "allowed_submission_surfaces": ["linkedin_to_official_ats"]
        }
    }
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    launcher._runtime_linkedin_route_gate(
        job,
        _with_causal_apply_attestation(job, {
            "disposition": "linkedin_external_handoff",
            "page_url": "https://boards.greenhouse.io/acme/jobs/123",
            "page_identity": {
                "version": 1,
                "page_title": "Data Engineering Intern | Acme",
                "primary_headings": ["Data Engineering Intern", "Acme"],
            },
        }),
        profile,
    )
    job["application_url"] = job["_discovered_application_url"]
    job["_linkedin_runtime_route_binding"]["identity_evidence"]["page_title"] = (
        "Product Marketing Manager | Acme"
    )

    assert launcher._authorize_linkedin_runtime_route(manifest, job, profile) == (
        False,
        "linkedin_runtime_route_identity_evidence_mismatch",
    )


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
