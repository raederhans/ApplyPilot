"""No-network contracts for manifest-authorised browser submission."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from applypilot.apply import authorization, launcher
from applypilot.cli import _build_standing_authorization_manifest, app
from applypilot.database import (
    init_db,
    reconcile_submission_receipt,
    record_unanswered_questions,
    reserve_batch_submission,
    update_batch_submission_status,
)

FIXTURES = Path(__file__).parent / "fixtures" / "apply"
NOW = datetime(2026, 8, 24, 9, 30, tzinfo=UTC)


def _job(url: str = "https://careers.example.test/jobs/data") -> dict:
    return {
        "url": url,
        "application_url": "https://jobs.lever.co/example/1002",
        "title": "Data Analyst",
        "company_name": "Example Data",
        "full_description": "Use SQL and Python to build decision dashboards.",
        "eligibility_status": "eligible",
        "fit_score": 8,
    }


def _manifest_path(
    tmp_path: Path,
    job: dict,
    *,
    expires_at: datetime | None = None,
    target_host: str = "jobs.lever.co",
    fingerprint: str | None = None,
    extra_jobs: list[dict] | None = None,
) -> Path:
    resume = tmp_path / "data-resume.pdf"
    resume.write_bytes(b"%PDF-fixture")
    resume_sha256, resume_size = authorization.compute_file_binding(resume)
    entry = {
        "url": job["url"],
        "application_url": job["application_url"],
        "target_host": target_host,
        "resume_path": str(resume.resolve()),
        "resume_sha256": resume_sha256,
        "resume_size": resume_size,
        "job_fingerprint": fingerprint or authorization.compute_job_fingerprint(job),
    }
    manifest = {
        "version": 1,
        "batch_id": "fixture-batch-001",
        "authorized_at": NOW.isoformat(),
        "expires_at": (expires_at or (NOW + timedelta(hours=1))).isoformat(),
        "max_submissions": 1,
        "jobs": [entry, *(extra_jobs or [])],
    }
    path = tmp_path / "batch.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _insert_ready_job(conn, job: dict, resume_path: Path, *, status: str | None = None,
                      error: str | None = None) -> None:
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, source_site, site, application_url, "
        "full_description, tailored_resume_path, tailor_status, cover_letter_status, "
        "eligibility_status, fit_score, apply_status, apply_error, "
        "application_readiness_status, application_readiness_reason, "
        "application_readiness_reviewed_at, application_readiness_reviewed_by, "
        "application_readiness_fingerprint) "
        "VALUES (?, ?, ?, 'official_careers', 'lever', ?, ?, ?, 'machine_validated', "
        "'not_required', 'eligible', 8, ?, ?, 'confirmed', "
        "'Verified eligibility and availability.', ?, 'agent', ?)",
        (
            job["url"], job["title"], job["company_name"], job["application_url"],
            job["full_description"], str(resume_path), status, error, NOW.isoformat(),
            authorization.compute_job_fingerprint(job),
        ),
    )
    conn.commit()


def test_manifest_accepts_only_current_exact_https_job_and_resume(tmp_path: Path) -> None:
    job = _job()
    path = _manifest_path(tmp_path, job)

    manifest = authorization.load_manifest(path, now=NOW)
    job["tailored_resume_path"] = manifest["jobs"][0]["resume_path"]
    entry = authorization.authorize_job(manifest, job)

    assert manifest["version"] == 1
    assert manifest["batch_id"] == "fixture-batch-001"
    assert entry["url"] == job["url"]
    assert entry["target_host"] == "jobs.lever.co"


def test_authorization_binds_the_uploaded_pdf_not_the_source_text(tmp_path: Path) -> None:
    source = tmp_path / "tailored.txt"
    pdf = tmp_path / "tailored.pdf"
    source.write_text("validated source text", encoding="utf-8")
    pdf.write_bytes(b"%PDF-uploaded-bytes")

    resolved = authorization.resolve_resume_attachment(
        {"tailored_resume_path": str(source)}
    )

    assert resolved == pdf.resolve()
    assert authorization.compute_file_binding(resolved) == authorization.compute_file_binding(pdf)


def test_standing_manifest_keeps_exact_job_and_resume_bindings(tmp_path: Path) -> None:
    job = _job()
    resume = tmp_path / "data-resume.pdf"
    resume.write_bytes(b"%PDF-standing")
    job["tailored_resume_path"] = str(resume)

    manifest = authorization.build_bound_manifest([job], now=NOW, ttl_minutes=30)

    assert manifest["max_submissions"] == 1
    assert manifest["batch_id"].startswith("standing-")
    assert authorization.authorize_job(manifest, job) == manifest["jobs"][0]

    changed = dict(job, full_description="A materially different role")
    assert authorization.authorize_job(manifest, changed) is None


def test_standing_manifest_can_bind_replacements_without_expanding_submission_quota(
    tmp_path: Path,
) -> None:
    first = _job("https://careers.example.test/jobs/data-1")
    second = _job("https://careers.example.test/jobs/data-2")
    second["application_url"] = "https://jobs.lever.co/example/1003"
    resume = tmp_path / "replacement-pool.pdf"
    resume.write_bytes(b"%PDF-replacement-pool")
    first["tailored_resume_path"] = str(resume)
    second["tailored_resume_path"] = str(resume)

    manifest = authorization.build_bound_manifest(
        [first, second],
        now=NOW,
        ttl_minutes=30,
        max_submissions=1,
    )

    assert len(manifest["jobs"]) == 2
    assert manifest["max_submissions"] == 1


def test_final_batch_authorization_binds_the_exact_initial_manifest(tmp_path: Path) -> None:
    job = _job()
    manifest_path = _manifest_path(tmp_path, job)
    final_authorization = authorization.build_final_authorization(
        manifest_path,
        now=NOW + timedelta(minutes=1),
        ttl_minutes=30,
    )
    final_path = tmp_path / "batch.final.json"
    final_path.write_text(json.dumps(final_authorization), encoding="utf-8")

    loaded = authorization.load_final_authorization(
        final_path,
        manifest_path,
        now=NOW + timedelta(minutes=2),
    )

    assert loaded["batch_id"] == "fixture-batch-001"
    assert loaded["_final_submission_authorized"] is True

    manifest_path.write_text(manifest_path.read_text(encoding="utf-8") + "\n")
    with pytest.raises(ValueError, match="changed after final authorization"):
        authorization.load_final_authorization(
            final_path,
            manifest_path,
            now=NOW + timedelta(minutes=2),
        )


def test_standing_authorization_selects_only_ready_exact_jobs(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "standing.db")
    job = _job()
    resume = tmp_path / "ready.pdf"
    resume.write_bytes(b"%PDF-ready")
    _insert_ready_job(conn, job, resume)
    conn.execute(
        "UPDATE jobs SET application_readiness_status='confirmed', "
        "application_readiness_reason='Verified role eligibility.', "
        "application_readiness_reviewed_at=?, application_readiness_reviewed_by='agent', "
        "application_readiness_fingerprint=? WHERE url=?",
        (NOW.isoformat(), authorization.compute_job_fingerprint(job), job["url"]),
    )
    conn.commit()
    profile = {
        "submission_policy": {
            "authorization_granted": True,
            "standing_auto_authorize_ready_jobs": True,
            "batch_authorization_required": False,
            "maximum_auto_authorized_submissions_per_run": 3,
            "standing_authorization_ttl_minutes": 30,
        }
    }

    manifest = _build_standing_authorization_manifest(
        conn,
        profile=profile,
        target_url=job["url"],
        requested_limit=1,
        min_score=6,
    )

    assert manifest["max_submissions"] == 1
    assert manifest["jobs"][0]["url"] == job["url"]


def test_standing_authorization_binds_a_bounded_replacement_pool(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "standing-replacements.db")
    first = _job("https://careers.example.test/jobs/data-1")
    second = _job("https://careers.example.test/jobs/data-2")
    second["application_url"] = "https://jobs.lever.co/example/1003"
    first_resume = tmp_path / "ready-1.pdf"
    second_resume = tmp_path / "ready-2.pdf"
    first_resume.write_bytes(b"%PDF-ready-1")
    second_resume.write_bytes(b"%PDF-ready-2")
    _insert_ready_job(conn, first, first_resume)
    _insert_ready_job(conn, second, second_resume)
    profile = {
        "submission_policy": {
            "authorization_granted": True,
            "standing_auto_authorize_ready_jobs": True,
            "batch_authorization_required": False,
            "maximum_auto_authorized_submissions_per_run": 3,
            "maximum_auto_authorized_candidates_per_run": 2,
            "standing_authorization_ttl_minutes": 30,
        }
    }

    manifest = _build_standing_authorization_manifest(
        conn,
        profile=profile,
        target_url=None,
        requested_limit=1,
        min_score=6,
    )

    assert len(manifest["jobs"]) == 2
    assert manifest["max_submissions"] == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "expired",
        "non_https_application",
        "lookalike_target_host",
        "missing_resume",
        "duplicate_url",
    ],
)
def test_manifest_rejects_unsafe_or_ambiguous_entries(
    tmp_path: Path, mutation: str
) -> None:
    job = _job()
    path = _manifest_path(tmp_path, job)
    raw = json.loads(path.read_text(encoding="utf-8"))
    entry = raw["jobs"][0]
    if mutation == "expired":
        raw["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
    elif mutation == "non_https_application":
        entry["application_url"] = "http://jobs.lever.co/example/1002"
    elif mutation == "lookalike_target_host":
        entry["target_host"] = "jobs.lever.co.evil.example"
    elif mutation == "missing_resume":
        entry["resume_path"] = str(tmp_path / "does-not-exist.pdf")
    elif mutation == "duplicate_url":
        raw["jobs"].append(dict(entry))
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError):
        authorization.load_manifest(path, now=NOW)


def test_manifest_refuses_job_content_that_changed_after_authorization(tmp_path: Path) -> None:
    job = _job()
    manifest = authorization.load_manifest(_manifest_path(tmp_path, job), now=NOW)
    changed = dict(
        job,
        full_description="Different role after the user authorised this batch.",
        tailored_resume_path=manifest["jobs"][0]["resume_path"],
    )

    assert authorization.authorize_job(manifest, changed) is None


def test_manifest_refuses_resume_overwritten_at_same_path(tmp_path: Path) -> None:
    job = _job()
    manifest = authorization.load_manifest(_manifest_path(tmp_path, job), now=NOW)
    resume = Path(manifest["jobs"][0]["resume_path"])
    resume.write_bytes(b"%PDF-replaced-with-different-bytes")
    job["tailored_resume_path"] = str(resume)

    assert authorization.authorize_job(manifest, job) is None


def test_manifest_refuses_host_or_application_url_drift_after_authorization(tmp_path: Path) -> None:
    job = _job()
    manifest = authorization.load_manifest(_manifest_path(tmp_path, job), now=NOW)

    assert authorization.authorize_job(
        manifest,
        dict(
            job,
            application_url="https://jobs.lever.co.evil.example/example/1002",
            tailored_resume_path=manifest["jobs"][0]["resume_path"],
        ),
    ) is None


def test_manifest_queue_claims_only_authorized_job_and_is_idempotent(
    tmp_path: Path, monkeypatch
) -> None:
    conn = init_db(tmp_path / "jobs.db")
    authorized = _job("https://careers.example.test/jobs/authorized")
    unrelated = _job("https://careers.example.test/jobs/unrelated")
    unrelated["application_url"] = "https://jobs.lever.co/example/9999"
    path = _manifest_path(
        tmp_path,
        authorized,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    manifest = authorization.load_manifest(path, now=NOW)
    resume_path = Path(manifest["jobs"][0]["resume_path"])
    _insert_ready_job(conn, authorized, resume_path)
    _insert_ready_job(conn, unrelated, resume_path)
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    claimed = launcher.acquire_job(worker_id=0, authorization_manifest=manifest)
    second_claim = launcher.acquire_job(worker_id=1, authorization_manifest=manifest)

    assert claimed is not None
    assert claimed["url"] == authorized["url"]
    assert second_claim is None


def test_optional_unanswered_question_does_not_block_authorized_acquisition(
    tmp_path: Path, monkeypatch
) -> None:
    conn = init_db(tmp_path / "jobs.db")
    job = _job("https://careers.example.test/jobs/optional-question")
    manifest = authorization.load_manifest(
        _manifest_path(
            tmp_path,
            job,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
        now=NOW,
    )
    _insert_ready_job(conn, job, Path(manifest["jobs"][0]["resume_path"]))
    record_unanswered_questions(
        job["url"],
        [{"question": "What is your GPA?", "required": False}],
        conn,
    )
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    claimed = launcher.acquire_job(worker_id=0, authorization_manifest=manifest)

    assert claimed is not None
    assert claimed["url"] == job["url"]


def test_batch_exclusion_prevents_retrying_one_deferred_job_in_the_same_run(
    tmp_path: Path, monkeypatch
) -> None:
    conn = init_db(tmp_path / "jobs.db")
    job = _job("https://careers.example.test/jobs/deferred-question")
    manifest = authorization.load_manifest(
        _manifest_path(
            tmp_path,
            job,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
        now=NOW,
    )
    _insert_ready_job(conn, job, Path(manifest["jobs"][0]["resume_path"]))
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    claimed = launcher.acquire_job(
        worker_id=0,
        authorization_manifest=manifest,
        exclude_urls={job["url"]},
    )

    assert claimed is None


@pytest.mark.parametrize(
    ("status", "error", "has_question"),
    [
        (None, None, True),
        ("submission_uncertain", None, False),
        ("failed", "assessment_required", False),
        ("failed", "captcha", False),
        ("failed", "manual_review_required:salary", False),
    ],
)
def test_manifest_queue_never_auto_retries_human_or_submission_stops(
    tmp_path: Path, monkeypatch, status: str | None, error: str | None, has_question: bool
) -> None:
    conn = init_db(tmp_path / "jobs.db")
    job = _job()
    manifest = authorization.load_manifest(_manifest_path(tmp_path, job), now=NOW)
    _insert_ready_job(conn, job, Path(manifest["jobs"][0]["resume_path"]), status=status, error=error)
    if has_question:
        record_unanswered_questions(
            job["url"],
            [{"question": "Do you have work authorisation?", "required": True}],
            conn,
        )
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    assert launcher.acquire_job(worker_id=0, authorization_manifest=manifest) is None


@pytest.mark.parametrize(("limit", "continuous"), [(2, False), (1, True)])
def test_non_dry_run_batch_or_continuous_worker_requires_manifest(
    monkeypatch, limit: int, continuous: bool
) -> None:
    # A pre-set stop event makes an accidental legacy loop harmless while this
    # contract requires the implementation to reject before it starts.
    launcher._stop_event.set()
    monkeypatch.setattr(launcher, "update_state", lambda *args, **kwargs: None)
    try:
        with pytest.raises(ValueError, match="manifest"):
            launcher.main(
                limit=limit,
                continuous=continuous,
                dry_run=False,
                authorization_manifest=None,
            )
    finally:
        launcher._stop_event.clear()


def test_non_dry_single_url_core_launcher_also_requires_manifest() -> None:
    with pytest.raises(ValueError, match="Every real submission requires"):
        launcher.main(
            limit=1,
            target_url="https://jobs.example.test/role",
            dry_run=False,
            authorization_manifest=None,
        )


def test_applied_requires_structured_visible_submission_evidence() -> None:
    outputs = json.loads((FIXTURES / "agent_results.json").read_text(encoding="utf-8"))

    evidence = launcher._validate_submission_evidence(outputs["applied_with_receipt"])
    assert evidence == {
        "receipt_visible": True,
        "applied_badge_visible": False,
        "confirmation_text": "Your application has been submitted",
        "confirmation_url": "https://jobs.lever.co/example/1002/confirmation",
    }
    assert launcher._validate_submission_evidence(outputs["applied_without_receipt"]) is None


def test_durable_confirmation_email_reconciles_uncertain_submission_idempotently(
    tmp_path: Path,
) -> None:
    conn = init_db(tmp_path / "receipts.db")
    job_url = "https://careers.example.test/jobs/data"
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, apply_status, "
        "apply_retry_blocked, apply_retry_reason) VALUES (?, ?, ?, ?, 1, ?)",
        (
            job_url,
            "Data Analyst Intern",
            "Example Data",
            "submission_uncertain",
            "submission_uncertain_requires_review",
        ),
    )
    conn.commit()
    evidence = {
        "job_url": job_url,
        "source": "confirmation_email",
        "receipt_id": "gmail-message-123",
        "company_name": "Example Data Pte Ltd",
        "job_title": "Data Analyst Internship",
        "confirmation_text": "We have received your application.",
        "observed_at": "2026-08-24T10:00:00+08:00",
    }

    first = reconcile_submission_receipt(evidence, conn)
    second = reconcile_submission_receipt(evidence, conn)
    stored = conn.execute(
        "SELECT apply_status, apply_retry_blocked, verification_confidence "
        "FROM jobs WHERE url = ?",
        (job_url,),
    ).fetchone()

    assert first["status"] == "applied" and first["changed"] is True
    assert second["status"] == "applied" and second["changed"] is False
    assert stored["apply_status"] == "applied"
    assert stored["apply_retry_blocked"] == 0
    assert stored["verification_confidence"] == "durable_receipt_reconciled"


def test_receipt_reconciliation_rejects_cross_job_or_weak_evidence(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "receipts-rejected.db")
    job_url = "https://careers.example.test/jobs/data"
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, apply_status) VALUES (?, ?, ?, ?)",
        (job_url, "Data Analyst Intern", "Example Data", "submission_uncertain"),
    )
    conn.commit()
    base = {
        "job_url": job_url,
        "source": "confirmation_email",
        "receipt_id": "gmail-message-456",
        "company_name": "Example Data",
        "job_title": "Product Manager",
        "confirmation_text": "We have received your application.",
    }

    mismatch = reconcile_submission_receipt(base, conn)
    weak = reconcile_submission_receipt(
        {**base, "job_title": "Data Analyst Intern", "confirmation_text": "Thanks for your interest."},
        conn,
    )

    assert mismatch["reason"] == "job_title_mismatch"
    assert weak["reason"] == "no_decisive_submission_signal"
    assert conn.execute(
        "SELECT apply_status FROM jobs WHERE url = ?", (job_url,)
    ).fetchone()["apply_status"] == "submission_uncertain"


def test_durable_receipt_enriches_existing_applied_record_without_losing_evidence(
    tmp_path: Path,
) -> None:
    conn = init_db(tmp_path / "receipt-enrichment.db")
    job_url = "https://careers.example.test/jobs/automation"
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, apply_status, application_evidence) "
        "VALUES (?, ?, ?, 'applied', ?)",
        (job_url, "Workflow Automation Intern", "Example", "browser_receipt"),
    )
    conn.commit()
    evidence = {
        "job_url": job_url,
        "source": "confirmation_email",
        "receipt_id": "gmail-message-789",
        "company_name": "Example Pte Ltd",
        "job_title": "Workflow Automation Internship",
        "confirmation_text": "Thank you for submitting your application to Example.",
    }

    result = reconcile_submission_receipt(evidence, conn)
    stored = conn.execute(
        "SELECT application_evidence, verification_confidence, submission_observation_json "
        "FROM jobs WHERE url = ?",
        (job_url,),
    ).fetchone()

    assert result["changed"] is True
    assert stored["application_evidence"] == "browser_receipt"
    assert stored["verification_confidence"] == "durable_receipt_reconciled"
    assert json.loads(stored["submission_observation_json"])["receipt_id"] == "gmail-message-789"


def test_assessment_is_permanent_but_answerable_questions_are_batch_deferred() -> None:
    assert launcher._is_permanent_failure("captcha") is False
    assert launcher._is_permanent_failure("failed:assessment_required") is True
    assert launcher._is_permanent_failure(
        "failed:manual_review_required:work_authorization"
    ) is False
    assert launcher._is_permanent_failure(
        "failed:manual_review_required:unknown_required_question"
    ) is False
    assert launcher._is_permanent_failure("login_issue") is False
    assert launcher._is_permanent_failure(
        "failed:unsafe_verification:required_video_despite_optional_label"
    ) is True


def test_repairable_browser_failures_are_not_permanent() -> None:
    assert launcher._is_permanent_failure("failed:resume_not_uploaded") is False
    assert launcher._is_permanent_failure("failed:required_unfilled") is False
    assert launcher._is_permanent_failure("failed:browser_mcp_unavailable") is False


def test_batch_consumption_is_one_shot_and_enforces_global_quota(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    first = init_db(db_path)
    assert reserve_batch_submission("batch", "https://jobs.example/1", 1, first) is True
    update_batch_submission_status(
        "batch", "https://jobs.example/1", "submission_uncertain", {"seen": False}, first
    )

    # A fresh connection simulates a later process/run using the same durable ledger.
    from applypilot.database import close_connection

    close_connection(db_path)
    second = init_db(db_path)
    assert reserve_batch_submission("batch", "https://jobs.example/1", 1, second) is False
    assert reserve_batch_submission("batch", "https://jobs.example/2", 1, second) is False
    stored = second.execute(
        "SELECT status, evidence_json FROM application_batch_consumptions"
    ).fetchone()
    assert stored["status"] == "submission_uncertain"
    assert json.loads(stored["evidence_json"]) == {"seen": False}


def test_default_non_dry_apply_without_url_or_manifest_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr("applypilot.cli._bootstrap", lambda: None)
    monkeypatch.setenv("APPLYPILOT_AUTO_SUBMIT", "1")

    result = CliRunner().invoke(app, ["apply"])

    assert result.exit_code == 2
    assert "requires one exact --url" in result.output


def test_profile_can_require_manifest_even_for_exact_url(tmp_path: Path, monkeypatch) -> None:
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps({"submission_policy": {"batch_authorization_required": True}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("applypilot.cli._bootstrap", lambda: None)
    monkeypatch.setattr("applypilot.config.PROFILE_PATH", profile)
    monkeypatch.setenv("APPLYPILOT_AUTO_SUBMIT", "1")

    result = CliRunner().invoke(
        app,
        ["apply", "--url", "https://careers.example.test/jobs/data"],
    )

    assert result.exit_code == 2
    assert "requires --authorization-file" in result.output


def test_profile_can_require_one_final_authorization_for_the_whole_batch(
    tmp_path: Path, monkeypatch
) -> None:
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "submission_policy": {
                    "batch_authorization_required": True,
                    "batch_final_authorization_required": True,
                }
            }
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "batch.json"
    manifest.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("applypilot.cli._bootstrap", lambda: None)
    monkeypatch.setattr("applypilot.config.PROFILE_PATH", profile)

    result = CliRunner().invoke(
        app,
        ["apply", "--authorization-file", str(manifest)],
    )

    assert result.exit_code == 2
    assert "one final batch authorization" in result.output
