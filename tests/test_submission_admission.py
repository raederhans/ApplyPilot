from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from applypilot.apply.submission_admission import (
    evaluate_submission_admission,
    summarize_worker_allocation,
)
from applypilot.apply.submission_surfaces import (
    classify_submission_surface,
    normalize_allowed_submission_surfaces,
)
from applypilot.database import init_db
from applypilot.services.application import count_submission_ready_jobs


def _job(**overrides: object) -> dict[str, object]:
    job: dict[str, object] = {
        "url": "https://careers.example.test/jobs/data",
        "application_url": "https://jobs.lever.co/example/1001",
        "source_site": "official_careers",
        "site": "official_careers",
        "title": "Data Analyst Intern",
        "company_name": "Example Data",
        "full_description": "Use SQL and Python to build dashboards.",
        "fit_score": 8,
        "eligibility_status": "eligible",
        "tailored_resume_path": "resume.pdf",
        "tailor_status": "machine_validated",
        "cover_letter_status": "not_required",
        "apply_attempts": 0,
        "apply_status": None,
        "apply_retry_blocked": 0,
    }
    job.update(overrides)
    return job


def _profile(**policy: object) -> dict[str, object]:
    return {
        "submission_policy": {
            "allowed_submission_surfaces": ["official_ats", "official_company_careers"],
            **policy,
        }
    }


def test_surface_classifier_keeps_linkedin_source_separate_from_target_surface() -> None:
    assert classify_submission_surface(
        _job(
            source_site="linkedin",
            site="linkedin",
            url="https://www.linkedin.com/jobs/view/1001",
            application_url="https://www.linkedin.com/jobs/view/1001",
        )
    ) == "linkedin_apply_entry"
    assert classify_submission_surface(
        _job(source_site="linkedin", site="linkedin")
    ) == "linkedin_to_official_ats"


def test_linkedin_url_is_authoritative_when_source_metadata_is_missing() -> None:
    assert classify_submission_surface(
        _job(
            source_site="",
            site="",
            url="https://www.linkedin.com/jobs/view/1001",
            application_url="https://www.linkedin.com/jobs/view/1001",
        )
    ) == "linkedin_apply_entry"

    result = evaluate_submission_admission(
        _job(
            source_site="",
            site="",
            url="https://www.linkedin.com/jobs/view/1001",
            application_url="https://www.linkedin.com/jobs/view/1001",
            linkedin_easy_apply=True,
        ),
        _profile(allowed_submission_surfaces=["official_company_careers"]),
        minimum_fit_score=6,
    )
    assert result["admitted"] is False
    assert result["reason"] == (
        "submission_surface_not_allowed:linkedin_native_easy_apply"
    )


def test_surface_classifier_covers_official_and_direct_email_routes() -> None:
    assert classify_submission_surface(_job()) == "official_ats"
    assert classify_submission_surface(
        _job(
            source_site="official_careers",
            application_url="https://careers.example.test/jobs/data",
        )
    ) == "official_company_careers"
    assert classify_submission_surface(
        _job(email_application={"route": "direct_email"})
    ) == "official_direct_email"


def test_restricted_and_manual_surfaces_are_explicit() -> None:
    assert classify_submission_surface(
        _job(
            source_site="InternSG",
            site="InternSG",
            url="https://www.internsg.com/job-apply/123",
            application_url="https://www.internsg.com/job-apply/123",
        )
    ) == "restricted_portal_review"
    assert classify_submission_surface(
        _job(
            source_site="TCS",
            site="TCS",
            application_url="https://ibegin.tcsapps.com/candidate/jobs/123",
        )
    ) == "manual_ats"


def test_missing_surface_policy_preserves_normal_channels() -> None:
    job = _job(
        source_site="linkedin",
        site="linkedin",
        url="https://www.linkedin.com/jobs/view/1001",
        application_url="https://www.linkedin.com/jobs/view/1001",
    )
    result = evaluate_submission_admission(job, {}, minimum_fit_score=6)
    assert result["admitted"] is True
    assert result["surface"] == "linkedin_apply_entry"
    assert result["reason"] == "requires_runtime_linkedin_apply_route_resolution"


def test_explicit_surface_policy_and_email_authorization_are_enforced() -> None:
    linkedin = _job(
        source_site="linkedin",
        site="linkedin",
        url="https://www.linkedin.com/jobs/view/1001",
        application_url="https://www.linkedin.com/jobs/view/1001",
    )
    blocked = evaluate_submission_admission(
        linkedin, _profile(), minimum_fit_score=6
    )
    assert blocked["admitted"] is False
    assert blocked["reason"] == "submission_surface_not_allowed:linkedin_apply_entry"

    email = _job(email_application={"route": "direct_email"})
    email_blocked = evaluate_submission_admission(
        email,
        _profile(allowed_submission_surfaces=["official_direct_email"]),
        minimum_fit_score=6,
    )
    assert email_blocked["admitted"] is False
    assert email_blocked["reason"] == "direct_email_requires_independent_authorization"
    email_allowed = evaluate_submission_admission(
        email,
        _profile(
            allowed_submission_surfaces=["official_direct_email"],
            direct_email_application_authorized=True,
        ),
        minimum_fit_score=6,
    )
    assert email_allowed["admitted"] is True


def test_malformed_explicit_surface_policy_fails_closed() -> None:
    profile = _profile(allowed_submission_surfaces={"official_ats": True})
    assert normalize_allowed_submission_surfaces(profile) == frozenset()
    result = evaluate_submission_admission(_job(), profile, minimum_fit_score=6)
    assert result["admitted"] is False
    assert result["reason"] == "submission_surface_not_allowed:official_ats"


def test_admission_rejects_exhausted_attempts() -> None:
    result = evaluate_submission_admission(
        _job(apply_attempts=3), _profile(), minimum_fit_score=6
    )
    assert result["admitted"] is False
    assert result["reason"] == "maximum application attempts reached"


def test_admission_uses_profile_attempt_ceiling() -> None:
    result = evaluate_submission_admission(
        _job(apply_attempts=1),
        _profile(maximum_apply_attempts=1),
        minimum_fit_score=6,
    )
    assert result["admitted"] is False
    assert result["reason"] == "maximum application attempts reached"


def test_invalid_profile_attempt_ceiling_fails_closed() -> None:
    result = evaluate_submission_admission(
        _job(), _profile(maximum_apply_attempts=0), minimum_fit_score=6
    )
    assert result["admitted"] is False
    assert result["reason"] == (
        "submission_policy.maximum_apply_attempts must be a positive integer"
    )


def test_unknown_retry_block_is_always_review_blocked() -> None:
    result = evaluate_submission_admission(
        _job(apply_retry_blocked=1, apply_retry_reason="future_unknown_reason"),
        _profile(),
        minimum_fit_score=6,
    )
    assert result["admitted"] is False
    assert result["reason"] == "apply_retry_blocked_requires_review"


def test_linkedin_external_target_requires_verification_or_trust() -> None:
    generic = _job(
        source_site="linkedin",
        site="linkedin",
        url="https://www.linkedin.com/jobs/view/1001",
        application_url="https://careers.acme.example/jobs/data",
    )
    profile = _profile(
        allowed_submission_surfaces=["linkedin_to_official_ats"],
    )
    blocked = evaluate_submission_admission(generic, profile, minimum_fit_score=6)
    assert blocked["admitted"] is False
    assert blocked["reason"] == "unverified_linkedin_external_target"

    greenhouse = dict(generic, application_url="https://boards.greenhouse.io/acme/jobs/1")
    allowed_ats = evaluate_submission_admission(greenhouse, profile, minimum_fit_score=6)
    assert allowed_ats["admitted"] is True
    assert allowed_ats["metadata"]["target_verification"] == "recognized_ats"

    trusted = evaluate_submission_admission(
        generic,
        _profile(
            allowed_submission_surfaces=["linkedin_to_official_ats"],
            trusted_external_application_hosts=["careers.acme.example"],
        ),
        minimum_fit_score=6,
    )
    assert trusted["admitted"] is True
    assert trusted["metadata"]["target_verification"] == "explicitly_trusted_external_host"


def test_linkedin_native_admission_requires_runtime_verification() -> None:
    native = _job(
        source_site="linkedin",
        site="linkedin",
        url="https://www.linkedin.com/jobs/view/1001",
        application_url="https://www.linkedin.com/jobs/view/1001",
        linkedin_easy_apply=True,
    )
    result = evaluate_submission_admission(native, {}, minimum_fit_score=6)
    assert result["admitted"] is True
    assert result["reason"] == "requires_runtime_easy_apply_verification"
    assert result["metadata"]["requires_runtime_easy_apply_verification"] is True


def test_ready_count_uses_attempt_and_retry_admission_gates(tmp_path: Path) -> None:
    connection = init_db(tmp_path / "ready-count.db")
    base = _job()
    for job in (
        base,
        dict(base, url="https://careers.example.test/jobs/exhausted", apply_attempts=3),
        dict(
            base,
            url="https://careers.example.test/jobs/retry-blocked",
            apply_retry_blocked=1,
            apply_retry_reason="future_unknown_reason",
        ),
    ):
        connection.execute(
            "INSERT INTO jobs (url, application_url, source_site, site, title, company_name, "
            "full_description, fit_score, eligibility_status, tailored_resume_path, tailor_status, "
            "cover_letter_status, apply_attempts, apply_retry_blocked, apply_retry_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job["url"], job["application_url"], job["source_site"], job["site"],
                job["title"], job["company_name"], job["full_description"], job["fit_score"],
                job["eligibility_status"], job["tailored_resume_path"], job["tailor_status"],
                job["cover_letter_status"], job["apply_attempts"], job["apply_retry_blocked"],
                job.get("apply_retry_reason"),
            ),
        )
    connection.commit()
    assert count_submission_ready_jobs(
        connection,
        dry_run=True,
        profile=_profile(),
        minimum_fit_score=6,
    ) == 1


def test_status_distinguishes_raw_prepared_from_canonical_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from applypilot import cli, config, database
    from applypilot.services import application

    stats = {
        "total": 8,
        "excluded_ineligible": 0,
        "with_description": 8,
        "pending_detail": 0,
        "detail_errors": 0,
        "scored": 8,
        "unscored": 0,
        "tailored": 8,
        "untailored_eligible": 0,
        "with_cover_letter": 0,
        "ready_to_apply": 8,
        "applied": 0,
        "apply_errors": 0,
        "score_distribution": [],
        "by_site": [],
    }
    connection = object()
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr(database, "get_stats", lambda: stats)
    monkeypatch.setattr(database, "get_connection", lambda: connection)
    monkeypatch.setattr(config, "load_profile", lambda: {"submission_policy": {}})

    def _count(conn, *, dry_run, profile):
        assert conn is connection
        assert dry_run is False
        assert profile == {"submission_policy": {}}
        return 1

    monkeypatch.setattr(application, "count_submission_ready_jobs", _count)

    result = CliRunner().invoke(cli.app, ["status"])

    assert result.exit_code == 0
    assert "Prepared candidates (raw)" in result.output
    assert "8" in result.output
    assert "Admission-ready (pre-manifest)" in result.output
    assert "1" in result.output
    assert "exact authorization manifest" in result.output


def test_worker_summary_counts_manifest_bound_and_executable_jobs(tmp_path: Path) -> None:
    connection = init_db(tmp_path / "jobs.db")
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-test")
    job = _job(tailored_resume_path=str(resume))
    connection.execute(
        "INSERT INTO jobs (url, application_url, source_site, site, title, company_name, "
        "full_description, fit_score, eligibility_status, tailored_resume_path, tailor_status, "
        "cover_letter_status, apply_attempts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job["url"], job["application_url"], job["source_site"], job["site"],
            job["title"], job["company_name"], job["full_description"], job["fit_score"],
            job["eligibility_status"], str(resume), job["tailor_status"],
            job["cover_letter_status"], 0,
        ),
    )
    connection.commit()
    from applypilot.apply.authorization import build_bound_manifest

    manifest = build_bound_manifest([job])
    summary = summarize_worker_allocation(
        connection,
        _profile(maximum_workers=2),
        manifest,
        requested_workers=2,
        minimum_fit_score=6,
    )
    assert summary == {
        "requested_workers": 2,
        "bound_candidates": 1,
        "executable_candidates": 1,
        "blocked_candidates": 0,
        "effective_workers": 1,
    }


def test_worker_summary_requires_manifest(tmp_path: Path) -> None:
    connection = init_db(tmp_path / "no-manifest.db")
    with pytest.raises(ValueError, match="authorization manifest"):
        summarize_worker_allocation(
            connection,
            _profile(),
            None,
            requested_workers=1,
            minimum_fit_score=6,
        )
