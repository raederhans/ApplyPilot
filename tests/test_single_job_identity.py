"""Exact-job import identity contracts."""

from __future__ import annotations

from applypilot import single_job
from applypilot.database import init_db


def test_import_exact_job_reuses_legacy_linkedin_identity_and_preserves_status(
    tmp_path,
    monkeypatch,
) -> None:
    conn = init_db(tmp_path / "jobs.db")
    existing_url = "https://www.linkedin.com/jobs/view/123456789"
    conn.execute(
        """
        INSERT INTO jobs (
            url, application_url, title, company_name, apply_status, applied_at,
            apply_retry_blocked, apply_retry_reason, application_evidence
        ) VALUES (?, ?, 'Earlier title', 'Example', 'submission_uncertain',
                  '2026-09-05T00:00:00+00:00', 1,
                  'submission_uncertain_requires_review', 'independent_observer_pending')
        """,
        (existing_url, existing_url),
    )
    conn.execute(
        """
        INSERT INTO application_receipts (
            receipt_source, receipt_id, job_url, admitted_at, receipt_digest
        ) VALUES ('mailbox', 'receipt-legacy-linkedin', ?, '2026-09-05T00:01:00+00:00', ?)
        """,
        (existing_url, "a" * 64),
    )
    conn.execute(
        """
        INSERT INTO application_attempts (
            attempt_id, job_url, worker_id, started_at, lease_expires_at,
            phase, submit_started, status, updated_at
        ) VALUES ('attempt-legacy-linkedin', ?, 'worker-test',
                  '2026-09-05T00:00:00+00:00', '2026-09-05T01:00:00+00:00',
                  'submit', 1, 'submission_uncertain', '2026-09-05T00:01:00+00:00')
        """,
        (existing_url,),
    )
    conn.commit()
    monkeypatch.setattr(single_job, "get_connection", lambda: conn)

    matching = single_job.import_exact_job(
        "https://www.linkedin.com/jobs/view/data-analyst-intern-123456789?trk=public_jobs",
        "Data Analyst Intern",
        "Example",
        description="Required: SQL.",
    )

    assert matching["url"] == existing_url
    rows = conn.execute(
        """
        SELECT url, platform_job_id, canonical_job_url, apply_status, applied_at,
               apply_retry_blocked, apply_retry_reason, application_evidence
        FROM jobs ORDER BY url
        """
    ).fetchall()
    assert len(rows) == 1
    assert dict(rows[0]) == {
        "url": existing_url,
        "platform_job_id": "linkedin:123456789",
        "canonical_job_url": existing_url,
        "apply_status": "submission_uncertain",
        "applied_at": "2026-09-05T00:00:00+00:00",
        "apply_retry_blocked": 1,
        "apply_retry_reason": "submission_uncertain_requires_review",
        "application_evidence": "independent_observer_pending",
    }
    assert conn.execute(
        "SELECT job_url FROM application_receipts WHERE receipt_id='receipt-legacy-linkedin'"
    ).fetchone()["job_url"] == existing_url
    assert conn.execute(
        "SELECT job_url FROM application_attempts WHERE attempt_id='attempt-legacy-linkedin'"
    ).fetchone()["job_url"] == existing_url

    distinct = single_job.import_exact_job(
        "https://www.linkedin.com/jobs/view/data-analyst-intern-987654321?trk=public_jobs",
        "Data Analyst Intern",
        "Example",
        description="Required: SQL.",
    )

    assert distinct["url"] != existing_url
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
