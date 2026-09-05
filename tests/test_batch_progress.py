"""Contracts for restart-safe, read-only application batch progress."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from applypilot.apply import authorization
from applypilot.apply.batch_progress import (
    batch_progress,
    consumed_batch_job_urls,
    open_read_only_database,
)
from applypilot.cli import app
from applypilot.commands.batches import run_next
from applypilot.database import init_db

NOW = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)


def _profile() -> dict:
    return {
        "submission_policy": {
            "authorization_granted": True,
            "standing_auto_authorize_ready_jobs": True,
            "maximum_apply_attempts": 3,
        }
    }


def _ready_jobs(connection, tmp_path: Path, count: int) -> tuple[list[dict], dict]:
    resume = tmp_path / "batch-resume.pdf"
    resume.write_bytes(b"%PDF-batch-progress")
    jobs: list[dict] = []
    for index in range(count):
        job = {
            "url": f"https://careers.example.test/jobs/{index}",
            "application_url": f"https://jobs.lever.co/example/{index}",
            "title": f"Data Intern {index}",
            "company_name": "Example Data",
            "location": "Singapore",
            "full_description": "Use SQL and Python to build decision dashboards.",
            "source_site": "official_careers",
            "site": "lever",
            "tailored_resume_path": str(resume),
            "tailor_status": "machine_validated",
            "cover_letter_status": "not_required",
            "eligibility_status": "eligible",
            "fit_score": 8,
            "apply_attempts": 0,
            "application_readiness_status": "confirmed",
            "application_readiness_reason": "Verified role eligibility.",
            "application_readiness_reviewed_at": NOW.isoformat(),
            "application_readiness_reviewed_by": "agent",
        }
        connection.execute(
            "INSERT INTO jobs (url, application_url, title, company_name, location, "
            "full_description, source_site, site, tailored_resume_path, tailor_status, "
            "cover_letter_status, eligibility_status, fit_score, apply_attempts, "
            "application_readiness_status, application_readiness_reason, "
            "application_readiness_reviewed_at, application_readiness_reviewed_by, "
            "application_readiness_fingerprint) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job["url"], job["application_url"], job["title"], job["company_name"],
                job["location"], job["full_description"], job["source_site"], job["site"],
                job["tailored_resume_path"], job["tailor_status"],
                job["cover_letter_status"], job["eligibility_status"], job["fit_score"],
                job["apply_attempts"], job["application_readiness_status"],
                job["application_readiness_reason"], job["application_readiness_reviewed_at"],
                job["application_readiness_reviewed_by"],
                authorization.compute_job_fingerprint(job),
            ),
        )
        jobs.append(job)
    connection.commit()
    manifest = authorization.build_bound_manifest(
        jobs,
        now=NOW,
        ttl_minutes=240,
        batch_id="batch-progress",
        max_submissions=count,
    )
    return jobs, manifest


def _attempt(connection, job_url: str, index: int, *, active: bool, submit_started: bool) -> str:
    attempt_id = f"attempt-{index}"
    connection.execute(
        "INSERT INTO application_attempts "
        "(attempt_id, job_url, batch_id, worker_id, started_at, lease_expires_at, "
        "phase, submit_started, status, updated_at) VALUES (?, ?, 'batch-progress', "
        "'worker', ?, ?, 'submit', ?, 'in_progress', ?)",
        (
            attempt_id,
            job_url,
            NOW.isoformat(),
            (NOW + timedelta(minutes=5) if active else NOW - timedelta(minutes=5)).isoformat(),
            int(submit_started),
            NOW.isoformat(),
        ),
    )
    return attempt_id


def _consume(connection, job_url: str, status: str) -> None:
    connection.execute(
        "INSERT INTO application_batch_consumptions "
        "(batch_id, job_url, reserved_at, status, updated_at) "
        "VALUES ('batch-progress', ?, ?, ?, ?)",
        (job_url, NOW.isoformat(), status, NOW.isoformat()),
    )


def test_mixed_batch_is_bounded_and_never_recommends_consumed_or_unknown(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "progress.db"
    connection = init_db(database_path)
    jobs, manifest = _ready_jobs(connection, tmp_path, 10)

    # One exact admitted receipt.
    confirmed_attempt = _attempt(connection, jobs[0]["url"], 0, active=False, submit_started=True)
    _consume(connection, jobs[0]["url"], "applied")
    connection.execute(
        "INSERT INTO application_submission_gates "
        "(gate_id, attempt_id, batch_id, job_url, claimed_at, claimed_at_epoch, state, "
        "updated_at, idempotency_key) VALUES ('gate-0', ?, 'batch-progress', ?, ?, ?, "
        "'applied', ?, 'key-0')",
        (confirmed_attempt, jobs[0]["url"], NOW.isoformat(), NOW.timestamp(), NOW.isoformat()),
    )
    connection.execute(
        "INSERT INTO application_receipts "
        "(receipt_source, receipt_id, job_url, admitted_at, receipt_digest) "
        "VALUES ('browser', 'receipt-0', ?, ?, 'digest')",
        (jobs[0]["url"], NOW.isoformat()),
    )
    connection.execute(
        "INSERT INTO application_receipt_gate_bindings "
        "(receipt_source, receipt_id, gate_id, batch_id, job_url, attempt_id, bound_at_epoch) "
        "VALUES ('browser', 'receipt-0', 'gate-0', 'batch-progress', ?, ?, ?)",
        (jobs[0]["url"], confirmed_attempt, NOW.timestamp() + 1),
    )

    # Unknown and consumed-failed both permanently occupy their exact slots.
    uncertain_attempt = _attempt(connection, jobs[1]["url"], 1, active=False, submit_started=True)
    _consume(connection, jobs[1]["url"], "submission_uncertain")
    connection.execute(
        "INSERT INTO application_submission_gates "
        "(gate_id, attempt_id, batch_id, job_url, claimed_at, claimed_at_epoch, state, "
        "updated_at, idempotency_key) VALUES ('gate-1', ?, 'batch-progress', ?, ?, ?, "
        "'submission_uncertain', ?, 'key-1')",
        (uncertain_attempt, jobs[1]["url"], NOW.isoformat(), NOW.timestamp(), NOW.isoformat()),
    )
    _consume(connection, jobs[2]["url"], "failed")
    # A same-URL receipt without an exact gate binding must not manufacture success.
    connection.execute(
        "INSERT INTO application_receipts "
        "(receipt_source, receipt_id, job_url, admitted_at, receipt_digest) "
        "VALUES ('mailbox', 'unbound', ?, ?, 'digest')",
        (jobs[2]["url"], NOW.isoformat()),
    )
    _attempt(connection, jobs[3]["url"], 3, active=True, submit_started=False)
    # Other-batch consumption must not contaminate this manifest.
    connection.execute(
        "INSERT INTO application_batch_consumptions "
        "(batch_id, job_url, reserved_at, status, updated_at) "
        "VALUES ('other-batch', ?, ?, 'failed', ?)",
        (jobs[4]["url"], NOW.isoformat(), NOW.isoformat()),
    )
    connection.commit()

    first = batch_progress(connection, manifest, _profile(), limit=5, now=NOW)
    connection.close()
    reopened = open_read_only_database(database_path)
    try:
        second = batch_progress(reopened, manifest, _profile(), limit=5, now=NOW)
    finally:
        assert reopened is not None
        reopened.close()

    assert first == second
    assert first["consumed"] == 3
    assert first["counts"] == {
        "receipt_confirmed": 1,
        "uncertain": 1,
        "consumed_without_receipt": 1,
        "in_progress": 1,
        "ready": 6,
        "blocked": 0,
    }
    assert len(first["next"]) == 5
    assert {item["job_url"] for item in first["next"]}.isdisjoint(
        {jobs[0]["url"], jobs[1]["url"], jobs[2]["url"], jobs[3]["url"]}
    )
    assert first["page"]["next_offset"] == 5


def test_partial_manifest_cannot_reclaim_capacity_consumed_elsewhere_in_same_batch(
    tmp_path: Path,
) -> None:
    connection = init_db(tmp_path / "partial-batch.db")
    jobs, manifest = _ready_jobs(connection, tmp_path, 5)
    _consume(connection, "https://jobs.example.test/outside-current-view", "failed")
    connection.commit()
    manifest["max_submissions"] = 1
    result = batch_progress(connection, manifest, _profile(), now=NOW)
    assert result["consumed"] == 1
    assert result["remaining_capacity"] == 0
    assert result["next"] == []
    assert result["counts"]["receipt_confirmed"] == 0
    assert result["authorized_jobs"] == len(jobs)
    connection.close()


def test_consumed_helper_and_missing_storage_are_read_only(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    assert open_read_only_database(missing) is None
    assert not missing.exists()
    assert consumed_batch_job_urls(None, "batch") == set()

    connection = __import__("sqlite3").connect(tmp_path / "legacy.db")
    try:
        assert consumed_batch_job_urls(connection, "batch") == set()
    finally:
        connection.close()


@pytest.mark.parametrize("job_count", [5, 10, 100])
def test_progress_output_never_exceeds_ten_candidates(job_count: int) -> None:
    manifest = {
        "batch_id": "compatibility",
        "max_submissions": job_count,
        "jobs": [
            {
                "url": f"https://careers.example.test/jobs/{index}",
                "application_url": f"https://jobs.example.test/{index}",
            }
            for index in range(job_count)
        ],
    }
    result = batch_progress(None, manifest, _profile(), limit=10, now=NOW)

    assert len(result["next"]) <= 10
    assert sum(result["counts"].values()) == job_count


def test_cli_body_has_one_bounded_real_caller(tmp_path: Path, monkeypatch) -> None:
    connection = init_db(tmp_path / "applypilot.db")
    _, manifest = _ready_jobs(connection, tmp_path, 5)
    connection.close()
    manifest["authorized_at"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    manifest["expires_at"] = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
    manifest_path = tmp_path / "batch.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(_profile()), encoding="utf-8")

    from applypilot import config

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "applypilot.db")
    monkeypatch.setattr(config, "PROFILE_PATH", profile_path)

    class Console:
        data: dict | None = None

        def print_json(self, *, data: object) -> None:
            self.data = data  # type: ignore[assignment]

        def print(self, *objects: object, **kwargs: object) -> None:
            raise AssertionError((objects, kwargs))

    console = Console()
    run_next(console, authorization_file=manifest_path, limit=5)

    assert console.data is not None
    assert len(console.data["next"]) == 5
    assert console.data["continuation"].endswith("--limit 5")


def test_apply_reports_same_durable_progress_before_launch(
    tmp_path: Path, monkeypatch
) -> None:
    connection = init_db(tmp_path / "applypilot.db")
    _, manifest = _ready_jobs(connection, tmp_path, 5)
    manifest["authorized_at"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    manifest["expires_at"] = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
    manifest_path = tmp_path / "batch.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(_profile()), encoding="utf-8")
    launches: list[dict[str, object]] = []

    monkeypatch.setattr("applypilot.cli._bootstrap", lambda: None)
    monkeypatch.setattr("applypilot.config.PROFILE_PATH", profile_path)
    monkeypatch.setattr("applypilot.database.get_connection", lambda: connection)
    monkeypatch.setattr(
        "applypilot.services.application.count_submission_ready_jobs",
        lambda *_args, **_kwargs: 5,
    )
    monkeypatch.setattr(
        "applypilot.services.application.resolve_apply_backend",
        lambda *_args, **_kwargs: "codex",
    )
    monkeypatch.setattr(
        "applypilot.services.application.resolve_apply_model",
        lambda *_args, **_kwargs: "test-model",
    )
    monkeypatch.setattr("applypilot.apply.chrome.get_browser_executable", lambda *_: "edge")
    monkeypatch.setattr("applypilot.apply.chrome.resolve_browser_backend", lambda *_: "edge")
    monkeypatch.setattr(
        "applypilot.apply.router.resolve_interaction_mode", lambda *_: "playwright"
    )
    monkeypatch.setattr("shutil.which", lambda *_: "codex")
    monkeypatch.setattr(
        "applypilot.apply.submission_admission.summarize_worker_allocation",
        lambda *_args, **_kwargs: {
            "requested_workers": 1,
            "bound_candidates": 5,
            "executable_candidates": 5,
            "blocked_candidates": 0,
            "effective_workers": 1,
        },
    )
    monkeypatch.setattr(
        "applypilot.apply.launcher.main", lambda **kwargs: launches.append(kwargs)
    )

    result = CliRunner().invoke(
        app,
        ["apply", "--authorization-file", str(manifest_path), "--limit", "5"],
    )
    connection.close()

    assert result.exit_code == 0, result.output
    assert "Batch progress:" in result.output
    assert "consumed=0" in result.output
    assert "ready=5" in result.output
    assert len(launches) == 1
