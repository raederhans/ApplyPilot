from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from applypilot import config
from applypilot.apply import launcher
from applypilot.apply.browser_broker import BrowserBroker
from applypilot.apply.page_binding import PageBinding
from applypilot.apply.resume_authorization import (
    ResumeAuthorization,
    consume_resume_authorization,
    issue_resume_authorization,
    latest_open_resume_authorization,
    store_resume_authorization,
    validate_resume_authorization,
)


def page_binding(*, page_epoch: int = 2) -> PageBinding:
    return PageBinding(
        page_id="application:attempt-1",
        page_lease_id="page-lease-1",
        page_lease_epoch=1,
        page_epoch=page_epoch,
        profile_lease_id="profile-lease-1",
        owner_id="application-actor:attempt-1",
        attempt_id="attempt-1",
        runtime_id="codex:edge:cdp:9432",
    )


def test_resume_authorization_binds_attempt_application_page_and_trigger() -> None:
    now = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)
    authorization = issue_resume_authorization(
        attempt_id="attempt-1",
        application_id="https://jobs.example.test/role",
        page_binding=page_binding(),
        trigger="captcha_cleared",
        submit_started=False,
        ttl_seconds=120,
        now=now,
    )

    validate_resume_authorization(
        authorization,
        attempt_id="attempt-1",
        application_id="https://jobs.example.test/role",
        page_binding=page_binding(),
        trigger="captcha_cleared",
        submit_started=False,
        now=now + timedelta(seconds=10),
    )
    assert authorization.allowed_transition == "awaiting_captcha_clearance -> prepare"
    assert authorization.submit_authority is False
    assert authorization.page_write_authority is False
    assert ResumeAuthorization.from_mapping(authorization.as_dict()) == authorization


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"attempt_id": "attempt-2"}, "attempt mismatch"),
        ({"application_id": "https://jobs.example.test/other"}, "application mismatch"),
        ({"page_binding": page_binding(page_epoch=3)}, "page binding mismatch"),
        ({"trigger": "login_completed"}, "trigger mismatch"),
        ({"submit_started": True}, "submit state mismatch"),
    ],
)
def test_resume_authorization_rejects_cross_scope_resume(changes, match) -> None:
    now = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)
    authorization = issue_resume_authorization(
        attempt_id="attempt-1",
        application_id="https://jobs.example.test/role",
        page_binding=page_binding(),
        trigger="captcha_cleared",
        submit_started=False,
        ttl_seconds=120,
        now=now,
    )
    values = {
        "attempt_id": "attempt-1",
        "application_id": "https://jobs.example.test/role",
        "page_binding": page_binding(),
        "trigger": "captcha_cleared",
        "submit_started": False,
        "now": now + timedelta(seconds=10),
        **changes,
    }

    with pytest.raises(ValueError, match=match):
        validate_resume_authorization(authorization, **values)


def test_post_submit_resume_is_observation_only_and_consumed_once() -> None:
    connection = sqlite3.connect(":memory:")
    now = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)
    authorization = issue_resume_authorization(
        attempt_id="attempt-1",
        application_id="https://jobs.example.test/role",
        page_binding=page_binding(),
        trigger="captcha_cleared",
        submit_started=True,
        ttl_seconds=120,
        now=now,
    )

    assert authorization.allowed_transition == "awaiting_captcha_clearance -> verify"
    assert store_resume_authorization(connection, authorization) is True
    assert store_resume_authorization(connection, authorization) is False
    assert latest_open_resume_authorization(
        connection, "attempt-1", now=now + timedelta(seconds=5)
    ) == authorization
    assert consume_resume_authorization(
        connection, authorization, consumed_at=now + timedelta(seconds=10)
    ) is True
    assert consume_resume_authorization(
        connection, authorization, consumed_at=now + timedelta(seconds=11)
    ) is False
    assert latest_open_resume_authorization(
        connection, "attempt-1", now=now + timedelta(seconds=12)
    ) is None


def test_expired_resume_authorization_fails_closed() -> None:
    now = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)
    authorization = issue_resume_authorization(
        attempt_id="attempt-1",
        application_id="https://jobs.example.test/role",
        page_binding=page_binding(),
        trigger="captcha_cleared",
        submit_started=False,
        ttl_seconds=30,
        now=now,
    )

    with pytest.raises(ValueError, match="expired"):
        validate_resume_authorization(
            authorization,
            attempt_id="attempt-1",
            application_id="https://jobs.example.test/role",
            page_binding=page_binding(),
            trigger="captcha_cleared",
            submit_started=False,
            now=now + timedelta(seconds=30),
        )


def test_launcher_issues_and_consumes_exact_resume_authorization_once(
    monkeypatch,
) -> None:
    connection = sqlite3.connect(":memory:")
    broker = BrowserBroker()
    bundle = broker.acquire_bundle(
        profile_id="edge:worker:0",
        page_id="application:attempt-1",
        owner_id="application:attempt-1",
        scope_id="worker:0",
        attempt_id="attempt-1",
        runtime_id="codex:edge:cdp:9432",
        ttl_seconds=60,
    )
    job = {
        "url": "https://jobs.example.test/role",
        "_attempt_id": "attempt-1",
        "_browser_lease_binding": bundle.as_dict(),
    }
    monkeypatch.setattr(launcher, "get_connection", lambda: connection)
    monkeypatch.setattr(
        config,
        "load_profile",
        lambda: {"submission_policy": {"manual_intervention_timeout_seconds": 120}},
    )

    authorization = launcher._issue_manual_resume_authorization(
        job,
        submit_started=False,
    )

    assert authorization is not None
    assert authorization["allowed_transition"] == (
        "awaiting_captcha_clearance -> prepare"
    )
    assert authorization["submit_authority"] is False
    assert launcher._consume_manual_resume_authorization(
        job,
        authorization,
        submit_started=False,
    )
    assert not launcher._consume_manual_resume_authorization(
        job,
        authorization,
        submit_started=False,
    )
