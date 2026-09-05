"""No-network contracts for manifest-authorised browser submission."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from applypilot import database as database_mod
from applypilot.apply import authorization, launcher
from applypilot.apply.performance_attribution import (
    attribution_snapshot,
    bind_attempt_route,
    safe_record_job_span,
    safe_route_binding_snapshot,
)
from applypilot.cli import (
    _build_standing_authorization_manifest,
    _standing_auto_authorization_enabled,
    app,
)
from applypilot.commands import apply as apply_command
from applypilot.database import (
    admit_direct_email_sent_receipt,
    finalize_application_attempt,
    init_db,
    prune_application_runtime_history,
    reconcile_submission_receipt,
    record_application_attempt_performance,
    record_unanswered_questions,
    recover_stale_application_attempts,
    reserve_batch_submission,
    start_application_attempt,
    update_application_attempt,
    update_batch_submission_status,
)

FIXTURES = Path(__file__).parent / "fixtures" / "apply"
NOW = datetime(2026, 8, 24, 9, 30, tzinfo=UTC)


def _write_resume_pdf(path: Path, text: str) -> None:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
    })
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = stream
    writer.write(path)


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


def test_standing_authorization_rejects_stale_profile_resume_without_mutating_job(
    tmp_path: Path,
) -> None:
    conn = init_db(tmp_path / "standing-stale-profile.db")
    job = _job()
    resume = tmp_path / "stale.pdf"
    _write_resume_pdf(resume, "University of Pennsylvania, GPA: 3.6")
    resume.with_suffix(".txt").write_text(
        "University of Pennsylvania, GPA: 3.46",
        encoding="utf-8",
    )
    _insert_ready_job(conn, job, resume)
    profile = {
        "education": [
            {
                "institution": "University of Pennsylvania",
                "gpa": "3.46/4.0",
                "gpa_may_be_disclosed": True,
            }
        ],
        "submission_policy": {
            "authorization_granted": True,
            "standing_auto_authorize_ready_jobs": True,
            "batch_authorization_required": False,
        },
    }

    with pytest.raises(ValueError, match="stale_profile_fact"):
        _build_standing_authorization_manifest(
            conn,
            profile=profile,
            target_url=job["url"],
            requested_limit=1,
            min_score=6,
        )

    stored = conn.execute(
        "SELECT tailored_resume_path, tailor_status FROM jobs WHERE url=?", (job["url"],)
    ).fetchone()
    assert stored["tailored_resume_path"] == str(resume)
    assert stored["tailor_status"] == "machine_validated"


def test_authorize_batch_rejects_stale_profile_resume_without_mutating_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = init_db(tmp_path / "authorize-stale-profile.db")
    job = _job()
    resume = tmp_path / "stale.pdf"
    _write_resume_pdf(resume, "University of Pennsylvania, GPA: 3.6")
    resume.with_suffix(".txt").write_text(
        "University of Pennsylvania, GPA: 3.46",
        encoding="utf-8",
    )
    _insert_ready_job(conn, job, resume)
    profile = {
        "education": [
            {
                "institution": "University of Pennsylvania",
                "gpa": "3.46/4.0",
                "gpa_may_be_disclosed": True,
            }
        ],
        "submission_policy": {},
    }
    monkeypatch.setattr("applypilot.cli._bootstrap", lambda: None)
    monkeypatch.setattr("applypilot.config.APP_DIR", tmp_path / "workspace")
    monkeypatch.setattr("applypilot.config.load_profile", lambda: profile)
    monkeypatch.setattr("applypilot.database.get_connection", lambda: conn)

    result = CliRunner().invoke(app, ["authorize-batch", "--url", job["url"]])

    assert result.exit_code == 2
    assert "stale_profile_fact:" in result.output
    stored = conn.execute(
        "SELECT tailored_resume_path, tailor_status FROM jobs WHERE url=?", (job["url"],)
    ).fetchone()
    assert stored["tailored_resume_path"] == str(resume)
    assert stored["tailor_status"] == "machine_validated"


def test_example_profile_declares_but_does_not_grant_standing_submission() -> None:
    example_profile = Path(__file__).parents[1] / "profile.example.json"
    profile = json.loads(example_profile.read_text(encoding="utf-8"))
    policy = profile["submission_policy"]

    assert policy["mode"] == "auto_submit_when_preflight_passes"
    assert policy["authorization_granted"] is False
    assert policy["standing_auto_authorize_ready_jobs"] is False
    assert policy["batch_authorization_required"] is False
    assert policy["batch_final_authorization_required"] is False
    assert _standing_auto_authorization_enabled(profile) is False

    explicitly_authorized = dict(profile)
    explicitly_authorized["submission_policy"] = {
        **policy,
        "authorization_granted": True,
        "standing_auto_authorize_ready_jobs": True,
    }
    assert _standing_auto_authorization_enabled(explicitly_authorized) is True


def test_profile_enables_manual_captcha_relay_without_repeating_cli_flag() -> None:
    profile = {"submission_policy": {"manual_captcha_relay": True}}

    assert apply_command._manual_captcha_relay_enabled(False, profile) is True
    assert apply_command._manual_captcha_relay_enabled(True, {}) is True
    assert apply_command._manual_captcha_relay_enabled(False, {}) is False


@pytest.mark.parametrize(
    "mode",
    [
        {"dry_run": True},
        {"continuous": True},
        {"headless": True},
        {"workers": 2},
        {"limit": 2},
    ],
)
def test_profile_manual_captcha_relay_does_not_escape_bounded_visible_submission(
    mode: dict[str, object],
) -> None:
    profile = {"submission_policy": {"manual_captcha_relay": True}}

    assert apply_command._manual_captcha_relay_enabled(False, profile, **mode) is False


@pytest.mark.parametrize(
    "mode",
    [
        {"dry_run": True},
        {"continuous": True},
        {"headless": True},
    ],
)
def test_explicit_manual_captcha_relay_remains_enabled_for_mode_rejection(
    mode: dict[str, object],
) -> None:
    assert apply_command._manual_captcha_relay_enabled(True, {}, **mode) is True


def test_profile_manual_captcha_relay_is_disabled_for_dry_run_before_launch(
    tmp_path: Path, monkeypatch
) -> None:
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps({"submission_policy": {"manual_captcha_relay": True}}),
        encoding="utf-8",
    )
    launch_calls: list[dict[str, object]] = []
    monkeypatch.setattr("applypilot.cli._bootstrap", lambda: None)
    monkeypatch.setattr("applypilot.config.PROFILE_PATH", profile)
    monkeypatch.setattr(
        "applypilot.services.application.count_submission_ready_jobs",
        lambda *_args, **_kwargs: 1,
    )
    monkeypatch.setattr(
        "applypilot.services.application.resolve_apply_backend",
        lambda *_args, **_kwargs: "codex",
    )
    monkeypatch.setattr(
        "applypilot.services.application.resolve_apply_model",
        lambda *_args, **_kwargs: "test-model",
    )
    monkeypatch.setattr("applypilot.database.get_connection", lambda: object())
    monkeypatch.setattr("applypilot.apply.chrome.get_browser_executable", lambda *_: "edge")
    monkeypatch.setattr("applypilot.apply.chrome.resolve_browser_backend", lambda *_: "edge")
    monkeypatch.setattr("applypilot.apply.router.resolve_interaction_mode", lambda *_: "playwright")
    monkeypatch.setattr("shutil.which", lambda *_: "codex")
    monkeypatch.setattr(
        "applypilot.apply.launcher.main",
        lambda **kwargs: launch_calls.append(kwargs),
    )

    result = CliRunner().invoke(
        app,
        ["apply", "--dry-run", "--url", "https://careers.example.test/jobs/data"],
    )

    assert result.exit_code == 0, result.output
    assert "CAPTCHA:  stop on blocker" in result.output
    assert launch_calls == [
        {
            "limit": 1,
            "target_url": "https://careers.example.test/jobs/data",
            "min_score": 6,
            "headless": False,
            "model": "test-model",
            "dry_run": True,
            "continuous": False,
            "workers": 1,
            "agent_backend": "codex",
            "manual_captcha_relay": False,
            "browser_backend": "edge",
            "interaction_mode": "playwright",
            "authorization_manifest": None,
        }
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        ["--dry-run"],
        ["--continuous"],
        ["--headless"],
        ["--workers", "2"],
        ["--limit", "2"],
    ],
)
def test_explicit_manual_captcha_relay_rejects_incompatible_mode(
    tmp_path: Path, monkeypatch, arguments: list[str]
) -> None:
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"submission_policy": {}}), encoding="utf-8")
    monkeypatch.setattr("applypilot.cli._bootstrap", lambda: None)
    monkeypatch.setattr("applypilot.config.PROFILE_PATH", profile)

    result = CliRunner().invoke(app, ["apply", "--manual-captcha-relay", *arguments])

    assert result.exit_code == 1
    assert "Manual CAPTCHA relay requires bounded visible submission mode" in result.output


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

    claimed = launcher.acquire_job(worker_id="worker-1", authorization_manifest=manifest)
    second_claim = launcher.acquire_job(worker_id=1, authorization_manifest=manifest)

    assert claimed is not None
    assert claimed["url"] == authorized["url"]
    assert claimed["_authorization_entry"] == manifest["jobs"][0]
    assert second_claim is None


@pytest.mark.parametrize("exact_url", [False, True])
@pytest.mark.parametrize("consumption_status", ["failed", "submission_uncertain"])
def test_acquisition_does_not_prepare_an_already_consumed_batch_job(
    tmp_path: Path, monkeypatch, exact_url: bool, consumption_status: str
) -> None:
    conn = init_db(tmp_path / "consumed-jobs.db")
    job = _job()
    manifest = authorization.load_manifest(
        _manifest_path(tmp_path, job, expires_at=datetime.now(UTC) + timedelta(minutes=5)),
        now=NOW,
    )
    _insert_ready_job(conn, job, Path(manifest["jobs"][0]["resume_path"]), status="failed")
    batch_id = manifest["batch_id"]
    assert reserve_batch_submission(batch_id, job["url"], 1, conn)
    update_batch_submission_status(batch_id, job["url"], consumption_status, conn=conn)
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    acquired = launcher.acquire_job(
        worker_id="worker-1", authorization_manifest=manifest,
        target_url=job["url"] if exact_url else None,
    )
    assert acquired is None
    assert conn.execute("SELECT COUNT(*) FROM application_attempts").fetchone()[0] == 0
    assert conn.execute("SELECT apply_status FROM jobs WHERE url=?", (job["url"],)).fetchone()[0] == "failed"


def test_manifest_queue_preserves_fallback_application_url_for_material_binding(
    tmp_path: Path, monkeypatch
) -> None:
    from applypilot.apply.material_readiness import evaluate_material_readiness

    conn = init_db(tmp_path / "fallback-application-url.db")
    job = _job("https://job-boards.greenhouse.io/example/jobs/1002")
    job["application_url"] = None
    resume = tmp_path / "fallback-resume.pdf"
    resume.write_bytes(b"%PDF-fallback-application-url")
    _insert_ready_job(conn, job, resume)
    manifest_job = dict(job, application_url=job["url"], tailored_resume_path=str(resume))
    conn.execute(
        "UPDATE jobs SET application_readiness_fingerprint=? WHERE url=?",
        (authorization.compute_job_fingerprint(manifest_job), job["url"]),
    )
    conn.commit()
    manifest = authorization.build_bound_manifest(
        [manifest_job], now=datetime.now(UTC), ttl_minutes=30
    )
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)

    claimed = launcher.acquire_job(
        target_url=job["url"],
        worker_id="worker-1",
        authorization_manifest=manifest,
    )

    assert claimed is not None
    assert claimed["application_url"] == job["url"]
    assert evaluate_material_readiness(claimed)["ready"] is True


def test_manifest_queue_uses_profile_attempt_ceiling(
    tmp_path: Path, monkeypatch
) -> None:
    conn = init_db(tmp_path / "attempt-ceiling.db")
    job = _job("https://careers.example.test/jobs/attempt-ceiling")
    manifest = authorization.load_manifest(_manifest_path(tmp_path, job), now=NOW)
    _insert_ready_job(conn, job, Path(manifest["jobs"][0]["resume_path"]))
    conn.execute("UPDATE jobs SET apply_attempts=1 WHERE url=?", (job["url"],))
    conn.commit()
    monkeypatch.setattr(launcher, "get_connection", lambda: conn)
    monkeypatch.setattr(
        launcher.config,
        "load_profile",
        lambda: {"submission_policy": {"maximum_apply_attempts": 1}},
    )

    assert launcher.acquire_job(worker_id="worker-1", authorization_manifest=manifest) is None


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

    claimed = launcher.acquire_job(worker_id="worker-1", authorization_manifest=manifest)

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
        worker_id="worker-1",
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

    assert launcher.acquire_job(worker_id="worker-1", authorization_manifest=manifest) is None


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


def test_browser_receipt_reconciles_manual_frontend_submission(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "manual-browser-receipt.db")
    job_url = "https://jobs.ashbyhq.com/example/software-intern"
    conn.execute(
        "INSERT INTO jobs (url, title, company_name) VALUES (?, ?, ?)",
        (job_url, "Software Engineer Intern", "Example"),
    )
    conn.commit()

    result = reconcile_submission_receipt(
        {
            "job_url": job_url,
            "source": "browser_receipt",
            "receipt_id": "ashby-example-20260827",
            "company_name": "Example",
            "job_title": "Software Engineer Intern",
            "confirmation_text": "Your application was successfully submitted.",
        },
        conn,
    )
    stored = conn.execute(
        "SELECT apply_status, application_evidence, verification_confidence "
        "FROM jobs WHERE url = ?",
        (job_url,),
    ).fetchone()

    assert result["status"] == "applied" and result["changed"] is True
    assert stored["apply_status"] == "applied"
    assert stored["application_evidence"] == "browser_receipt:ashby-example-20260827"
    assert stored["verification_confidence"] == "durable_receipt_reconciled"


def test_confirmation_email_reconciles_manual_submission_from_resume_receipt(
    tmp_path: Path,
) -> None:
    conn = init_db(tmp_path / "manual-email-receipt.db")
    job_url = "https://example.applytojob.com/apply/ai-intern"
    conn.execute(
        "INSERT INTO jobs (url, title, company_name) VALUES (?, ?, ?)",
        (job_url, "AI Workflow Intern", "Example Climate"),
    )
    conn.commit()

    result = reconcile_submission_receipt(
        {
            "job_url": job_url,
            "source": "confirmation_email",
            "receipt_id": "gmail-example-resume-received",
            "company_name": "Example Climate",
            "job_title": "AI Workflow Intern",
            "confirmation_text": "Qiushi, we've received your resume.",
        },
        conn,
    )
    stored = conn.execute(
        "SELECT apply_status, application_evidence, verification_confidence "
        "FROM jobs WHERE url = ?",
        (job_url,),
    ).fetchone()

    assert result["status"] == "applied" and result["changed"] is True
    assert stored["apply_status"] == "applied"
    assert stored["application_evidence"] == "confirmation_email:gmail-example-resume-received"
    assert stored["verification_confidence"] == "durable_receipt_reconciled"


def test_internsg_submission_record_email_is_a_durable_receipt(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "internsg-submission-record.db")
    job_url = "https://www.internsg.com/job/example-data-engineer-intern/"
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, apply_status, apply_error) "
        "VALUES (?, ?, ?, 'failed', 'captcha submission failed')",
        (job_url, "Data Engineer Intern", "Example Data Pte. Ltd."),
    )
    conn.commit()

    result = reconcile_submission_receipt(
        {
            "job_url": job_url,
            "source": "confirmation_email",
            "receipt_id": "gmail-internsg-submission-record",
            "company_name": "Example Data Pte. Ltd.",
            "job_title": "Data Engineer Intern",
            "confirmation_text": (
                "This email is a submission record for your application to an "
                "InternSG listing."
            ),
        },
        conn,
    )

    assert result["status"] == "applied" and result["changed"] is True
    stored = conn.execute(
        "SELECT apply_status, apply_error, verification_confidence, application_evidence "
        "FROM jobs WHERE url=?",
        (job_url,),
    ).fetchone()
    assert dict(stored) == {
        "apply_status": "applied",
        "apply_error": None,
        "verification_confidence": "durable_receipt_reconciled",
        "application_evidence": "confirmation_email:gmail-internsg-submission-record",
    }


def test_direct_email_provider_message_id_is_unique_across_jobs(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "direct-email-receipts.db")
    first_url = "https://careers.example.test/jobs/data"
    second_url = "https://careers.example.test/jobs/data-copy"
    conn.executemany(
        "INSERT INTO jobs (url, title, company_name) VALUES (?, ?, ?)",
        [
            (first_url, "Data Analyst Intern", "Example"),
            (second_url, "Data Analyst Intern", "Example"),
        ],
    )
    conn.commit()
    receipt = {
        "folder": "sent",
        "recipient": "jobs@example.test",
        "subject": "Application - Data Analyst Intern",
        "attachment_names": ["Candidate_Resume.pdf"],
        "body_sha256": "a" * 64,
        "provider_message_id": "provider-message-1",
    }

    first = admit_direct_email_sent_receipt(first_url, receipt, conn)
    replay = admit_direct_email_sent_receipt(first_url, receipt, conn)
    conflict = admit_direct_email_sent_receipt(second_url, receipt, conn)

    assert first["status"] == "admitted"
    assert replay["status"] == "already_admitted"
    assert conflict["reason"] == "receipt_replay_conflict"


def test_browser_receipt_overrides_prior_cli_upload_failure(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "failed-cli-browser-success.db")
    job_url = "https://jobs.ashbyhq.com/example/product-intern"
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, apply_status, apply_error) "
        "VALUES (?, ?, ?, 'failed', 'resume_upload')",
        (job_url, "Product Intern", "Example"),
    )
    conn.commit()

    result = reconcile_submission_receipt(
        {
            "job_url": job_url,
            "source": "browser_receipt",
            "receipt_id": "ashby-example-browser-success",
            "company_name": "Example",
            "job_title": "Product Intern",
            "confirmation_text": "Your application was successfully submitted.",
        },
        conn,
    )
    stored = conn.execute(
        "SELECT apply_status, application_evidence, verification_confidence "
        "FROM jobs WHERE url = ?",
        (job_url,),
    ).fetchone()

    assert result["status"] == "applied" and result["changed"] is True
    assert stored["apply_status"] == "applied"
    assert stored["application_evidence"] == "browser_receipt:ashby-example-browser-success"
    assert stored["verification_confidence"] == "durable_receipt_reconciled"


def test_standing_authorization_missing_exact_job_explains_import_flow(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "missing-exact-job.db")
    profile = {
        "submission_policy": {
            "authorization_granted": True,
            "standing_auto_authorize_ready_jobs": True,
            "batch_authorization_required": False,
        }
    }

    with pytest.raises(ValueError, match="Register it first with `applypilot import-job`"):
        _build_standing_authorization_manifest(
            conn,
            profile=profile,
            target_url="https://jobs.example.test/company/role",
            requested_limit=1,
            min_score=7,
        )


def test_standing_authorization_reports_exact_registered_job_gate_and_next_step(
    tmp_path: Path,
) -> None:
    conn = init_db(tmp_path / "registered-not-ready.db")
    job_url = "https://jobs.example.test/company/role"
    conn.execute(
        "INSERT INTO jobs (url, title, company_name) VALUES (?, ?, ?)",
        (job_url, "AI Intern", "Example"),
    )
    conn.commit()
    profile = {
        "submission_policy": {
            "authorization_granted": True,
            "standing_auto_authorize_ready_jobs": True,
            "batch_authorization_required": False,
        }
    }

    with pytest.raises(ValueError) as error:
        _build_standing_authorization_manifest(
            conn,
            profile=profile,
            target_url=job_url,
            requested_limit=1,
            min_score=7,
        )

    message = str(error.value)
    assert "Exact job is registered but not ready" in message
    assert "Job fit score is missing" in message
    assert f'applypilot import-job --url "{job_url}"' in message
    assert "--description-file <official-job-description.txt>" in message


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


def test_attempt_lease_recovery_distinguishes_pre_and_post_submit(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "attempts.db")
    conn.execute("INSERT INTO jobs (url, apply_status) VALUES (?, 'in_progress')", ("job:pre",))
    pre_id = start_application_attempt("job:pre", "worker-1", conn=conn)
    conn.execute(
        "UPDATE application_attempts SET lease_expires_at=? WHERE attempt_id=?",
        ((NOW - timedelta(minutes=1)).isoformat(), pre_id),
    )
    conn.execute("UPDATE jobs SET apply_task_id=? WHERE url='job:pre'", (pre_id,))

    conn.execute("INSERT INTO jobs (url, apply_status) VALUES (?, 'in_progress')", ("job:post",))
    post_id = start_application_attempt("job:post", "worker-2", conn=conn)
    update_application_attempt(
        post_id,
        phase="submit",
        submit_started=True,
        conn=conn,
    )
    conn.execute(
        "UPDATE application_attempts SET lease_expires_at=? WHERE attempt_id=?",
        ((NOW - timedelta(minutes=1)).isoformat(), post_id),
    )
    conn.execute("UPDATE jobs SET apply_task_id=? WHERE url='job:post'", (post_id,))
    conn.commit()

    recovered = recover_stale_application_attempts(conn, now=NOW)

    assert recovered == {"pre_submit": 1, "submission_uncertain": 1}
    states = {
        row["url"]: (row["apply_status"], row["apply_retry_blocked"])
        for row in conn.execute(
            "SELECT url, apply_status, apply_retry_blocked FROM jobs"
        ).fetchall()
    }
    assert states["job:pre"] == ("failed", 0)
    assert states["job:post"] == ("submission_uncertain", 1)
    assert finalize_application_attempt(post_id, "applied", conn=conn) is False


def test_terminal_attempt_performance_merge_is_bounded_and_preserves_evidence(
    tmp_path: Path,
) -> None:
    conn = init_db(tmp_path / "attempt-performance.db")
    attempt_id = start_application_attempt(
        "https://acme.myworkdayjobs.com/job/123", "worker-1", conn=conn
    )
    attribution_job = {
        "application_url": "https://acme.myworkdayjobs.com/job/123",
    }
    bind_attempt_route(
        attribution_job,
        provider="workday",
        target_url=attribution_job["application_url"],
        worker_application_index=1,
        worker_id="worker-1",
    )
    safe_record_job_span(attribution_job, "agent.turn", 120)
    safe_record_job_span(attribution_job, "browser.prepare", 120)
    safe_record_job_span(attribution_job, "audit.pre_submit", 20)
    attribution = attribution_snapshot(attribution_job)
    assert attribution is not None
    route = safe_route_binding_snapshot(attribution_job)
    assert route is not None
    assert finalize_application_attempt(
        attempt_id,
        "applied",
        evidence={
            "receipt": {"confirmed": True},
            "orchestration_performance": {"attribution_route": route},
        },
        conn=conn,
    )

    recorded = record_application_attempt_performance(
        attempt_id,
        {
            "version": 1,
            "metrics": {
                "submit_lane_wait_ms": 20,
                "submit_lane_hold_ms": 220,
                "unknown": "drop",
            },
            "acquisition": {
                "candidate_rows": 4,
                "worker_call_ms": 12.5,
                "raw_url": "drop",
            },
            "attribution": attribution,
        },
        conn=conn,
    )

    stored = conn.execute(
        "SELECT status, evidence_json FROM application_attempts WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    evidence = json.loads(stored["evidence_json"])
    assert recorded is True
    assert stored["status"] == "applied"
    assert evidence["receipt"] == {"confirmed": True}
    assert evidence["orchestration_performance"] == {
        "version": 1,
        "metrics": {
            "submit_lane_wait_ms": 20.0,
            "submit_lane_hold_ms": 220.0,
        },
        "acquisition": {
            "candidate_rows": 4.0,
            "worker_call_ms": 12.5,
        },
        "attribution": attribution,
    }


def test_terminal_attempt_performance_accepts_audited_direct_email_attribution(
    tmp_path: Path,
) -> None:
    conn = init_db(tmp_path / "direct-email-attribution.db")
    target_url = "https://acme.myworkdayjobs.com/job/123"
    attempt_id = start_application_attempt(target_url, "worker-1", conn=conn)
    attribution_job = {"application_url": target_url}
    bind_attempt_route(
        attribution_job,
        provider="direct_email",
        target_url=target_url,
        worker_application_index=1,
        worker_id="worker-1",
    )
    safe_record_job_span(attribution_job, "agent.turn", 120)
    attribution = attribution_snapshot(attribution_job)
    assert attribution is not None
    route = safe_route_binding_snapshot(attribution_job)
    assert route is not None
    assert route["provider"] == "direct_email"
    assert finalize_application_attempt(
        attempt_id,
        "applied",
        evidence={"orchestration_performance": {"attribution_route": route}},
        conn=conn,
    )

    assert record_application_attempt_performance(
        attempt_id,
        {"version": 1, "metrics": {}, "acquisition": {}, "attribution": attribution},
        conn=conn,
    )
    stored = conn.execute(
        "SELECT evidence_json FROM application_attempts WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    evidence = json.loads(stored["evidence_json"])
    assert evidence["orchestration_performance"]["attribution"] == attribution


def test_prune_history_is_preview_first_and_preserves_uncertainty(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "prune.db")
    old = (NOW - timedelta(days=200)).isoformat()
    for attempt_id, status in (("old-failed", "failed"), ("old-uncertain", "submission_uncertain")):
        conn.execute(
            "INSERT INTO application_attempts "
            "(attempt_id, job_url, worker_id, started_at, lease_expires_at, phase, "
            "submit_started, status, updated_at) VALUES (?, ?, 'worker', ?, ?, 'done', 1, ?, ?)",
            (attempt_id, f"job:{attempt_id}", old, old, status, old),
        )
    conn.commit()

    preview = prune_application_runtime_history(
        retention_days=180, execute=False, conn=conn, now=NOW
    )
    executed = prune_application_runtime_history(
        retention_days=180, execute=True, conn=conn, now=NOW
    )

    assert preview["eligible_attempts"] == 1
    assert executed["deleted_attempts"] == 1
    remaining = {
        row[0] for row in conn.execute("SELECT attempt_id FROM application_attempts")
    }
    assert remaining == {"old-uncertain"}


def test_ledger_facade_preserves_an_existing_thread_local_transaction(
    monkeypatch, tmp_path: Path
) -> None:
    conn = init_db(tmp_path / "transaction-ownership.db")
    conn.execute(
        "INSERT INTO jobs (url, title) VALUES ('job:outer', 'outer transaction')"
    )
    monkeypatch.setattr(database_mod, "get_connection", lambda: conn)

    attempt_id = start_application_attempt("job:attempt", "worker-1")

    assert attempt_id
    assert conn.in_transaction is True
    conn.rollback()
    assert conn.execute("SELECT 1 FROM jobs WHERE url='job:outer'").fetchone() is None


def test_prune_execute_does_not_commit_an_outer_transaction(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "prune-outer-transaction.db")
    old = (NOW - timedelta(days=200)).isoformat()
    conn.execute(
        "INSERT INTO application_attempts "
        "(attempt_id, job_url, worker_id, started_at, lease_expires_at, phase, "
        "submit_started, status, updated_at) "
        "VALUES ('old-failed', 'job:failed', 'worker', ?, ?, 'done', 0, 'failed', ?)",
        (old, old, old),
    )
    conn.commit()
    conn.execute("INSERT INTO jobs (url, title) VALUES ('job:outer', 'outer')")

    result = prune_application_runtime_history(
        retention_days=180, execute=True, conn=conn, now=NOW
    )

    assert result["deleted_attempts"] == 1
    assert conn.in_transaction is True
    conn.rollback()
    assert conn.execute(
        "SELECT 1 FROM application_attempts WHERE attempt_id='old-failed'"
    ).fetchone() is not None
    assert conn.execute("SELECT 1 FROM jobs WHERE url='job:outer'").fetchone() is None


def test_prune_preview_does_not_create_a_missing_database(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "missing.db"
    monkeypatch.setattr(database_mod, "DB_PATH", db_path)

    result = CliRunner().invoke(app, ["prune-application-history"])

    assert result.exit_code == 0, result.output
    assert "Eligible: 0" in result.output
    assert not db_path.exists()


def test_prune_preview_does_not_migrate_an_old_database(monkeypatch, tmp_path: Path) -> None:
    import sqlite3

    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    before = db_path.read_bytes()
    monkeypatch.setattr(database_mod, "DB_PATH", db_path)

    result = CliRunner().invoke(app, ["prune-application-history"])

    assert result.exit_code == 0, result.output
    assert db_path.read_bytes() == before
    check = sqlite3.connect(db_path)
    tables = {row[0] for row in check.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    check.close()
    assert tables == {"jobs"}


def test_receipt_id_cannot_be_replayed_for_a_second_job(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "receipts.db")
    for url in ("https://jobs.example/one", "https://jobs.example/two"):
        conn.execute(
            "INSERT INTO jobs (url, title, company_name, apply_status) "
            "VALUES (?, 'Data Intern', 'Example Co', 'submission_uncertain')",
            (url,),
        )
    conn.commit()
    evidence = {
        "source": "confirmation_email",
        "receipt_id": "message-123",
        "job_url": "https://jobs.example/one",
        "company_name": "Example Co",
        "job_title": "Data Intern",
        "confirmation_text": "We have received your application.",
    }

    assert reconcile_submission_receipt(evidence, conn)["status"] == "applied"
    conflicting = dict(evidence, job_url="https://jobs.example/two")
    result = reconcile_submission_receipt(conflicting, conn)

    assert result["reason"] == "receipt_replay_conflict"
    assert conn.execute(
        "SELECT apply_status FROM jobs WHERE url=?", (conflicting["job_url"],)
    ).fetchone()[0] == "submission_uncertain"


def test_profile_fact_binding_keeps_raw_values_out_and_tracks_categories() -> None:
    profile = {
        "personal": {
            "full_name": "Sensitive Name",
            "email": "private@example.com",
            "address": "Private Street",
        },
        "work_authorization": {"legally_authorized_to_work": "Yes"},
        "availability": {"earliest_start_date": "2027-01-01"},
    }

    binding = authorization.build_profile_fact_binding(profile)
    serialized = json.dumps(binding)

    assert set(binding) == {"version", "raw_values_stored", "stable", "sensitive", "temporary"}
    assert binding["raw_values_stored"] is False
    assert "Sensitive Name" not in serialized
    assert "private@example.com" not in serialized
    assert "Private Street" not in serialized


def test_final_material_freeze_rejects_staged_resume_byte_drift(tmp_path: Path) -> None:
    resume = tmp_path / "resume.pdf"
    staged = tmp_path / "staged-resume.pdf"
    resume.write_bytes(b"authorized-resume")
    staged.write_bytes(b"different-staged-resume")
    job = _job()
    job.update(
        {
            "tailored_resume_path": str(resume),
            "tailor_status": "machine_validated",
            "cover_letter_status": "not_required",
            "_staged_resume_path": str(staged),
        }
    )

    with pytest.raises(ValueError, match="Staged resume bytes differ"):
        authorization.freeze_submission_materials(job, {})


def test_default_non_dry_apply_without_url_or_manifest_is_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps({"submission_policy": {}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("applypilot.cli._bootstrap", lambda: None)
    monkeypatch.setattr("applypilot.config.PROFILE_PATH", profile)
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
