from __future__ import annotations

import json

from applypilot.apply.authorization import compute_job_fingerprint
from applypilot.database import close_connection, init_db
from applypilot.frontend.contracts import (
    build_discover_item,
    build_discover_summary,
    build_prepare_job,
    build_verify_job,
)
from applypilot.view import collect_dashboard_data, render_dashboard


def _job(**changes: object) -> dict[str, object]:
    job: dict[str, object] = {
        "url": "https://careers.example.test/jobs/data",
        "application_url": "https://careers.example.test/jobs/data/apply",
        "title": "Data Analyst",
        "company_name": "Example",
        "location": "Singapore",
        "full_description": "Use SQL and Python to build decision dashboards.",
        "fit_score": 8,
        "eligibility_status": "eligible",
        "tailored_resume_path": "C:/private/resumes/tailored-data.pdf",
        "tailor_status": "machine_validated",
        "cover_letter_status": "not_required",
    }
    job.update(changes)
    return job


def test_discover_contract_keeps_lineage_bounded_and_non_promotional() -> None:
    item = build_discover_item(
        {
            "kind": "lead",
            "title": "Product Analyst",
            "company_name": "Example",
            "url": "https://social.example.test/post/42",
            "source": "Candidate-reviewed import",
            "provider": "manual",
            "source_count": 1,
            "last_seen_at": "2026-08-27T01:00:00+00:00",
            "payload_json": '{"private":"must not escape"}',
            "content_fingerprint": "a" * 64,
        }
    )
    summary = build_discover_summary([item], [], lineage_available=True)
    serialized = json.dumps(item)

    assert item["state"] == "lead"
    assert item["pipeline"]["state"] == "new"
    assert summary["stats"]["leads"] == 1
    assert "must not escape" not in serialized
    assert "a" * 64 not in serialized


def test_discover_contract_preserves_missing_lineage_and_not_run_source_issue() -> None:
    item = build_discover_item(
        {
            "kind": "listing",
            "title": "Unlinked listing",
            "url": "https://example.test/jobs/unlinked",
            "source_count": 0,
        }
    )
    summary = build_discover_summary(
        [item],
        [{"status": "not_run", "paginationComplete": None, "hasError": False}],
        lineage_available=True,
    )

    assert item["sourceCount"] == 0
    assert summary["stats"]["sourceIssues"] == 1
    assert summary["system"]["state"] == "needs_evidence"


def test_dashboard_collects_discovery_lineage_with_selects_only(tmp_path) -> None:
    db_path = tmp_path / "discover.db"
    conn = init_db(db_path)
    job_url = "https://careers.example.test/jobs/official-42"
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, location, source_site, site, "
        "discovered_at, last_seen_at, fit_score, eligibility_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job_url,
            "Data Analyst",
            "Example",
            "Singapore",
            "official_careers",
            "official_careers",
            "2026-08-27T00:00:00+00:00",
            "2026-08-27T01:00:00+00:00",
            8,
            "eligible",
        ),
    )
    conn.execute(
        "INSERT INTO radar_sources (source_id, company_id, company_name, source_type, "
        "provider, access_mode, priority_tier, active, registered_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (
            "private-source-id",
            "example",
            "Example careers",
            "official_careers",
            "greenhouse",
            "public_read",
            "p1",
            "2026-08-27T00:00:00+00:00",
            "2026-08-27T01:00:00+00:00",
        ),
    )
    conn.execute(
        "INSERT INTO radar_fetch_runs (run_id, source_id, started_at, finished_at, "
        "status, pagination_complete, normalized_count, new_count, lead_count, error) "
        "VALUES (?, ?, ?, ?, 'partial', 0, 2, 1, 1, ?)",
        (
            "private-run-id",
            "private-source-id",
            "2026-08-27T00:00:00+00:00",
            "2026-08-27T01:00:00+00:00",
            "C:/private/token-bearing-provider-error",
        ),
    )
    conn.execute(
        "INSERT INTO radar_source_observations (observation_key, source_id, source_url, "
        "canonical_url, title, company_name, location, published_at, first_seen_at, "
        "last_seen_at, last_run_id, verification_status, content_fingerprint, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "private-observation-official",
            "private-source-id",
            job_url,
            job_url,
            "Data Analyst",
            "Example",
            "Singapore",
            "2026-08-26T00:00:00+00:00",
            "2026-08-27T00:00:00+00:00",
            "2026-08-27T01:00:00+00:00",
            "private-run-id",
            "verified_official",
            "b" * 64,
            '{"private":"official payload"}',
        ),
    )
    conn.execute(
        "INSERT INTO radar_job_sources (job_url, observation_key, source_id, is_primary, "
        "linked_at) VALUES (?, ?, ?, 1, ?)",
        (job_url, "private-observation-official", "private-source-id", "2026-08-27T01:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO radar_source_observations (observation_key, source_id, source_url, "
        "title, company_name, location, first_seen_at, last_seen_at, last_run_id, "
        "verification_status, content_fingerprint, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'unverified', ?, ?)",
        (
            "private-observation-lead",
            "private-source-id",
            "https://social.example.test/post/7",
            "Product Analyst",
            "Example",
            "Singapore",
            "2026-08-27T00:00:00+00:00",
            "2026-08-27T01:00:00+00:00",
            "private-run-id",
            "c" * 64,
            '{"private":"lead payload"}',
        ),
    )
    conn.execute(
        "INSERT INTO radar_leads (lead_id, observation_key, status, company_id, title, "
        "location, source_url, verification_status, reason, first_seen_at, last_seen_at) "
        "VALUES (?, ?, 'awaiting_official', ?, ?, ?, ?, 'unverified', ?, ?, ?)",
        (
            "private-lead-id",
            "private-observation-lead",
            "example",
            "Product Analyst",
            "Singapore",
            "https://social.example.test/post/7",
            "C:/private/reason-must-not-escape",
            "2026-08-27T00:00:00+00:00",
            "2026-08-27T01:00:00+00:00",
        ),
    )
    conn.commit()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    data = collect_dashboard_data(conn)
    html = render_dashboard(data)

    assert data["discover"]["stats"] == {
        "candidates": 2,
        "verified": 1,
        "leads": 1,
        "sourceIssues": 1,
    }
    assert {item["state"] for item in data["discover"]["items"]} == {"verified", "lead"}
    assert data["discover"]["sources"][0]["status"] == "partial"
    assert "private-source-id" not in html
    assert "token-bearing-provider-error" not in html
    assert "official payload" not in html
    assert "lead payload" not in html
    assert "reason-must-not-escape" not in html
    assert statements
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    close_connection(db_path)


def test_missing_discovery_lineage_degrades_without_migration(tmp_path) -> None:
    db_path = tmp_path / "legacy-discover.db"
    conn = init_db(db_path)
    conn.execute("DROP TABLE radar_leads")
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, source_site, site, discovered_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "https://example.test/jobs/legacy",
            "Legacy Analyst",
            "Example",
            "manual",
            "manual",
            "2026-08-27T00:00:00+00:00",
        ),
    )
    conn.commit()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    data = collect_dashboard_data(conn)

    assert data["discover"]["system"]["state"] == "needs_evidence"
    assert data["discover"]["stats"]["candidates"] == 1
    assert data["discover"]["items"][0]["state"] == "observed"
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='radar_leads'"
    ).fetchone()[0] == 0
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    close_connection(db_path)


def test_prepare_contract_requires_validation_and_hides_local_paths() -> None:
    job = _job()
    assignment = {
        "job_fingerprint": compute_job_fingerprint(job),
        "decision": "reuse_exact",
        "reason": "Current validated artifact covers this subtype.",
        "hard_gaps_json": "[]",
        "recorded_at": "2026-08-27T01:00:00+00:00",
        "artifact_kind": "tailored",
        "artifact_track": "data_bi_decision",
        "artifact_pdf_path": "C:/private/resumes/tailored-data.pdf",
        "artifact_pdf_sha256": "a" * 64,
        "artifact_pdf_size": 12_345,
        "artifact_validation_status": "machine_validated",
        "artifact_validation_report_path": "C:/private/reports/result.json",
        "artifact_validated_at": "2026-08-27T00:59:00+00:00",
    }

    result = build_prepare_job(job, assignment)
    serialized = json.dumps(result)

    assert result["state"] == "ready"
    assert result["route"]["state"] == "current"
    assert result["resume"]["artifactName"] == "tailored-data.pdf"
    assert result["route"]["artifact"]["hasPdfBinding"] is True
    assert "C:/private" not in serialized
    assert "result.json" not in serialized


def test_prepare_contract_never_treats_a_path_as_validation() -> None:
    result = build_prepare_job(_job(tailor_status="pending", cover_letter_status="machine_validated"))

    assert result["state"] == "review"
    assert result["resume"]["state"] == "review"
    assert result["coverLetter"]["state"] == "review"
    assert result["route"]["state"] == "unrecorded"


def test_prepare_contract_requires_a_current_gap_free_bound_route() -> None:
    job = _job()
    current = {
        "job_fingerprint": compute_job_fingerprint(job),
        "decision": "reuse_exact",
        "reason": "Persisted route.",
        "hard_gaps_json": "[]",
        "artifact_validation_status": "machine_validated",
        "artifact_pdf_path": "C:/private/resume.pdf",
        "artifact_pdf_sha256": "a" * 64,
        "artifact_pdf_size": 12_345,
    }

    assert build_prepare_job(job)["state"] == "review"
    assert build_prepare_job(job, {**current, "artifact_pdf_sha256": None})["state"] == "review"
    assert build_prepare_job(job, {**current, "hard_gaps_json": '["unsupported"]'})["state"] == "review"
    assert build_prepare_job(job, current)["state"] == "ready"


def test_prepare_contract_marks_old_fingerprint_route_as_stale() -> None:
    job = _job()
    assignment = {
        "job_fingerprint": compute_job_fingerprint({**job, "full_description": "Old copy"}),
        "decision": "reuse_exact",
        "reason": "This must not be presented as current.",
        "hard_gaps_json": '["stale gap"]',
        "recorded_at": "2026-08-26T01:00:00+00:00",
    }

    result = build_prepare_job(job, assignment)

    assert result["state"] == "review"
    assert result["route"]["state"] == "stale"
    assert result["route"]["gaps"] == []
    assert "older version" in result["route"]["reason"]


def test_dashboard_collects_current_prepare_evidence_with_selects_only(tmp_path) -> None:
    db_path = tmp_path / "prepare.db"
    conn = init_db(db_path)
    job = _job()
    conn.execute(
        "INSERT INTO jobs (url, application_url, title, company_name, location, "
        "full_description, fit_score, eligibility_status, tailored_resume_path, "
        "tailor_status, cover_letter_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        tuple(job.values()),
    )
    fingerprint = compute_job_fingerprint(dict(conn.execute("SELECT * FROM jobs").fetchone()))
    conn.execute(
        "INSERT INTO resume_artifacts (artifact_id, content_sha256, kind, track, "
        "text_path, pdf_path, pdf_sha256, pdf_size, validation_status, "
        "validation_report_path, validated_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "resume:test",
            "a" * 64,
            "tailored",
            "data_bi_decision",
            "C:/private/resumes/tailored-data.txt",
            "C:/private/resumes/tailored-data.pdf",
            "a" * 64,
            12_345,
            "machine_validated",
            "C:/private/reports/result.json",
            "2026-08-27T01:00:00+00:00",
            "2026-08-27T01:00:00+00:00",
            "2026-08-27T01:00:00+00:00",
        ),
    )
    conn.execute(
        "INSERT INTO job_resume_assignments (assignment_id, job_url, job_fingerprint, "
        "artifact_id, decision, hard_gaps_json, components_json, reason, policy_version, "
        "recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "assignment:test",
            job["url"],
            fingerprint,
            "resume:test",
            "reuse_exact",
            "[]",
            "{}",
            "Validated artifact covers this role.",
            "test",
            "2026-08-27T01:00:00+00:00",
        ),
    )
    conn.commit()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    data = collect_dashboard_data(conn)
    html = render_dashboard(data)

    assert data["prepare"]["stats"]["ready"] == 1
    assert data["discover"]["items"][0]["sourceCount"] == 0
    assert data["jobs"][0]["prepare"]["route"]["state"] == "current"
    assert data["jobs"][0]["prepare"]["route"]["artifact"]["hasPdfBinding"] is True
    assert "tailored-data.pdf" in html
    assert "No linked source" in html
    assert "C:/private" not in html
    assert "a" * 64 not in html
    assert 'document.querySelectorAll("#decide-panel .filter")' in html
    assert statements
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    close_connection(db_path)


def test_missing_resume_library_tables_degrade_without_migration(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    conn = init_db(db_path)
    conn.execute("DROP TABLE job_resume_assignments")
    job = _job(tailored_resume_path=None, tailor_status=None)
    conn.execute(
        "INSERT INTO jobs (url, application_url, title, company_name, location, "
        "full_description, fit_score, eligibility_status, cover_letter_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        tuple(value for key, value in job.items() if key not in {"tailored_resume_path", "tailor_status"}),
    )
    conn.commit()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    data = collect_dashboard_data(conn)

    assert data["prepare"]["system"]["state"] == "needs_evidence"
    assert data["jobs"][0]["prepare"]["route"]["state"] == "unrecorded"
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    close_connection(db_path)


def test_verify_contract_separates_reservation_observation_and_receipt() -> None:
    ledger = {
        "batch_id": "standing-1234567890-secret-tail",
        "status": "reserved",
        "reserved_at": "2026-08-27T01:00:00+00:00",
        "updated_at": "2026-08-27T01:00:00+00:00",
    }
    result = build_verify_job(
        {
            "apply_status": "submission_uncertain",
            "apply_retry_blocked": 0,
            "submission_observation_json": json.dumps(
                {
                    "submit_clicked": True,
                    "receipt_visible": False,
                    "receipt_id": "gmail-secret-123",
                    "confirmation_text": "private receipt body",
                    "page_url": "https://private.example.test/confirmation",
                }
            ),
            "submission_observed_at": "2026-08-27T01:01:00+00:00",
        },
        [ledger],
    )
    serialized = json.dumps(result)

    assert result["state"] == "action_needed"
    assert result["batch"]["state"] == "recorded"
    assert result["authorization"]["state"] == "reservation_recorded"
    assert result["observation"]["submitClicked"] is True
    assert result["receipt"]["state"] == "pending"
    assert "standing-1" not in serialized
    assert "ret-tail" not in serialized
    assert "gmail-secret-123" not in serialized
    assert "private receipt body" not in serialized
    assert "private.example.test" not in serialized


def test_verify_contract_only_calls_reconciled_receipts_durable() -> None:
    durable = build_verify_job(
        {
            "apply_status": "applied",
            "verification_confidence": "durable_receipt_reconciled",
        }
    )
    browser = build_verify_job(
        {
            "apply_status": "applied",
            "verification_confidence": "visible_confirmation",
        }
    )
    export = build_verify_job(
        {
            "apply_status": "applied",
            "verification_confidence": "platform_export",
        }
    )
    unclassified = build_verify_job({"apply_status": "applied"})

    assert durable["state"] == "reconciled"
    assert durable["receipt"]["state"] == "durable"
    assert browser["state"] == "confirmed"
    assert browser["receipt"]["state"] == "confirmed"
    assert export["state"] == "reported"
    assert export["receipt"]["state"] == "reported"
    assert unclassified["state"] == "action_needed"
    assert unclassified["receipt"]["state"] == "unclassified"


def test_dashboard_collects_verify_evidence_without_raw_receipts_or_writes(tmp_path) -> None:
    db_path = tmp_path / "verify.db"
    conn = init_db(db_path)
    job_url = "https://careers.example.test/jobs/verified"
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, apply_status, applied_at, "
        "verification_confidence, application_recorded_at, submission_observation_json, "
        "submission_observed_at) VALUES (?, ?, ?, 'applied', ?, ?, ?, ?, ?)",
        (
            job_url,
            "Data Analyst",
            "Example",
            "2026-08-27T01:00:00+00:00",
            "durable_receipt_reconciled",
            "2026-08-27T01:01:00+00:00",
            json.dumps(
                {
                    "source": "confirmation_email",
                    "receipt_id": "gmail-private-789",
                    "confirmation_text": "We received your private application.",
                }
            ),
            "2026-08-27T01:01:00+00:00",
        ),
    )
    conn.execute(
        "INSERT INTO application_batch_consumptions "
        "(batch_id, job_url, reserved_at, status, updated_at, evidence_json) "
        "VALUES (?, ?, ?, 'applied', ?, ?)",
        (
            "batch-private-1234567890",
            job_url,
            "2026-08-27T00:59:00+00:00",
            "2026-08-27T01:01:00+00:00",
            json.dumps({"confirmation_text": "secret ledger evidence"}),
        ),
    )
    conn.commit()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    data = collect_dashboard_data(conn)
    html = render_dashboard(data)

    assert data["verify"]["stats"]["reconciled"] == 1
    assert data["verify"]["jobs"][0]["verify"]["state"] == "reconciled"
    assert "gmail-private-789" not in html
    assert "private application" not in html
    assert "secret ledger evidence" not in html
    assert statements
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    close_connection(db_path)


def test_missing_batch_ledger_degrades_without_migration(tmp_path) -> None:
    db_path = tmp_path / "legacy-verify.db"
    conn = init_db(db_path)
    conn.execute("DROP TABLE application_batch_consumptions")
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, apply_status, "
        "verification_confidence) VALUES (?, ?, ?, 'applied', 'visible_confirmation')",
        ("https://example.test/legacy", "Analyst", "Example"),
    )
    conn.commit()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    data = collect_dashboard_data(conn)

    assert data["verify"]["system"]["state"] == "needs_evidence"
    assert data["verify"]["jobs"][0]["verify"]["state"] == "confirmed"
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    close_connection(db_path)
