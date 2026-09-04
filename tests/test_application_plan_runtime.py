from __future__ import annotations

from copy import deepcopy

import pytest

from applypilot.apply import application_plan_runtime as runtime
from applypilot.apply import launcher
from applypilot.apply.application_plan import HostAuditReceiptIssuer, HostSubmitDenied
from applypilot.apply.browser_broker import BrowserLease, BrowserLeaseBundle
from applypilot.apply.contracts import application_actor_id
from applypilot.apply.page_binding import PageBinding


def _job(*, attempt_id: str = "attempt-plan", page_epoch: int = 3) -> dict[str, object]:
    common = {
        "lease_id": "lease-plan",
        "owner_id": application_actor_id(attempt_id),
        "scope_id": "worker:1",
        "attempt_id": attempt_id,
        "runtime_id": "codex:cdp:9432",
        "epoch": 1,
        "issued_at": 1.0,
        "heartbeat_at": 2.0,
        "expires_at": 9999999999.0,
    }
    profile = BrowserLease(resource_kind="profile", resource_id="profile-plan", **common)
    page = BrowserLease(
        resource_kind="page",
        resource_id=f"application:{attempt_id}",
        **common,
    )
    binding = PageBinding(
        page_id=page.resource_id,
        page_lease_id=page.lease_id,
        page_lease_epoch=page.epoch,
        page_epoch=page_epoch,
        profile_lease_id=profile.lease_id,
        owner_id=page.owner_id,
        attempt_id=attempt_id,
        runtime_id=page.runtime_id,
    )
    return {
        "_attempt_id": attempt_id,
        "_browser_lease_binding": BrowserLeaseBundle(profile, page, binding).as_dict(),
        "_ats_application_binding": {"provider": "greenhouse"},
        "url": "https://boards.greenhouse.io/private-company/jobs/123",
        "application_url": ("https://boards.greenhouse.io/private-company/jobs/123?candidate=private"),
        "company_name": "Private Company",
        "title": "Private Role",
        "tailored_resume_sha256": "b" * 64,
    }


def _profile() -> dict[str, object]:
    return {
        "application_facts": [
            {
                "fact_ref": "profile:preferred-name",
                "key": "preferred_name",
                "value": "Private Candidate Name",
                "source": "profile.json",
                "scope": "global:*",
                "confirmed_at": "2026-09-01T00:00:00Z",
                "sensitivity": "low",
            }
        ]
    }


def _clear_audit() -> dict[str, object]:
    return {
        "status": "clear",
        "disposition": "clear",
        "submission_gate": True,
        "blocking_issues": [],
        "repairable_issues": [],
        "required_unfilled_count": 0,
        "submit_control_count": 1,
        "captcha_token_present": False,
        "assessment_visible": False,
        "verification_visible": False,
        "answer_provenance": {
            "coverage_ratio": 1.0,
            "blocked_count": 0,
            "verified_count": 1,
        },
    }


def test_host_plan_is_deterministic_ref_only_and_has_no_authority() -> None:
    job = _job()

    first = runtime.build_host_application_plan(job, _profile(), runtime_route="browser")
    second = runtime.build_host_application_plan(job, _profile(), runtime_route="browser")

    assert first == second
    assert first.provider == "greenhouse"
    assert first.fact_refs[0].key == "preferred_name"
    assert first.material_refs[0].purpose == "resume"
    rendered = repr(first.as_dict())
    assert "Private Candidate Name" not in rendered
    assert "private-company" not in rendered
    assert "candidate=private" not in rendered
    assert first.as_dict()["host_authority"] is False
    assert first.as_dict()["submit_authority"] is False


def test_host_plan_reuses_equal_content_and_revises_changed_materials() -> None:
    job = _job()
    initial = runtime.build_host_application_plan(job, _profile(), runtime_route="browser")

    replay = runtime.build_host_application_plan(
        job,
        _profile(),
        runtime_route="browser",
        previous=initial,
    )
    changed = dict(job)
    changed["tailored_resume_sha256"] = "c" * 64
    revision = runtime.build_host_application_plan(
        changed,
        _profile(),
        runtime_route="browser",
        previous=initial,
    )

    assert replay is initial
    assert revision.revision == 2
    assert revision.parent_plan_sha256 == initial.digest
    assert revision.material_refs[0].content_sha256 == "c" * 64


def test_launcher_installs_host_plan_and_preserves_delta_parent() -> None:
    job = _job()
    initial = launcher._install_application_plan_context(
        job,
        _profile(),
        runtime_route="browser",
    )
    job["tailored_resume_sha256"] = "c" * 64

    revision = launcher._install_application_plan_context(
        job,
        _profile(),
        runtime_route="browser",
    )

    assert job["_application_plan"] is revision
    assert job["_previous_application_plan"] is initial
    assert revision.parent_plan_sha256 == initial.digest


def test_page_lease_drift_cannot_retarget_an_existing_plan() -> None:
    job = _job()
    plan = runtime.build_host_application_plan(job, _profile(), runtime_route="browser")
    drifted = deepcopy(job)
    drifted["_browser_lease_binding"]["page_binding"]["page_lease_id"] = (  # type: ignore[index]
        "replacement-page-lease"
    )
    drifted["_browser_lease_binding"]["page"]["lease_id"] = (  # type: ignore[index]
        "replacement-page-lease"
    )

    with pytest.raises(ValueError, match="immutable target identity changed"):
        runtime.build_host_application_plan(
            drifted,
            _profile(),
            runtime_route="browser",
            previous=plan,
        )


def test_deterministic_host_audit_issues_receipt_without_submit_authority() -> None:
    job = _job()
    plan = runtime.build_host_application_plan(job, _profile(), runtime_route="browser")
    issuer = HostAuditReceiptIssuer()

    receipt = runtime.verify_host_application_plan_audit(
        plan,
        job,
        _profile(),
        _clear_audit(),
        issuer=issuer,
    )
    result = runtime.application_plan_shadow_result(
        plan,
        job,
        _profile(),
        _clear_audit(),
        issuer=issuer,
    )

    issuer.validate(receipt, plan)
    assert result["status"] == "verified"
    assert result["submit_executor"] == "host_submit_executor"
    assert result["submit_executor_enabled"] is False
    assert result["durable_submission_gate"] == "authoritative"
    assert result["submit_authority"] is False
    assert "Private Candidate Name" not in repr(result)
    assert "private-company" not in repr(result)


def test_deterministic_host_audit_rejects_stale_fact_refs() -> None:
    job = _job()
    plan = runtime.build_host_application_plan(job, _profile(), runtime_route="browser")
    changed_profile = _profile()
    changed_profile["application_facts"][0]["value"] = (  # type: ignore[index]
        "Changed Candidate Name"
    )

    with pytest.raises(HostSubmitDenied, match="host references drifted"):
        runtime.verify_host_application_plan_audit(
            plan,
            job,
            changed_profile,
            _clear_audit(),
            issuer=HostAuditReceiptIssuer(),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("disposition", "proceed_with_advisories"),
        ("required_unfilled_count", 1),
        ("submit_control_count", 0),
        ("assessment_visible", True),
    ),
)
def test_deterministic_host_audit_fails_closed(field: str, value: object) -> None:
    job = _job()
    plan = runtime.build_host_application_plan(job, _profile(), runtime_route="browser")
    audit = _clear_audit()
    audit[field] = value

    with pytest.raises(HostSubmitDenied, match="not clear"):
        runtime.verify_host_application_plan_audit(
            plan,
            job,
            _profile(),
            audit,
            issuer=HostAuditReceiptIssuer(),
        )


def test_direct_email_plan_stays_mailbox_owned() -> None:
    job = _job()
    plan = runtime.build_host_application_plan(
        job,
        _profile(),
        runtime_route="direct_email",
    )

    result = runtime.application_plan_shadow_result(
        plan,
        job,
        _profile(),
        {},
        issuer=HostAuditReceiptIssuer(),
    )

    assert plan.route == "direct_email"
    assert result["status"] == "mailbox_owned"
    assert result["submit_authority"] is False
    assert result["reason_code"] == "DIRECT_EMAIL_REMAINS_MAILBOX_OWNED"
