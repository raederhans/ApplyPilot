from __future__ import annotations

import pytest

from applypilot.apply import launcher
from applypilot.apply.browser_broker import (
    BrowserAuthorityDenied,
    BrowserBroker,
    BrowserBrokerError,
    BrowserContinuityError,
    BrowserLeaseBundle,
    BrowserLeaseConflict,
    BrowserLeaseExpired,
    StalePageBinding,
)
from applypilot.apply.semantic_browser_ops import SemanticBrowserOps


class Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


def _bundle(
    broker: BrowserBroker,
    *,
    owner_id: str = "application:attempt-1",
    attempt_id: str = "attempt-1",
):
    return broker.acquire_bundle(
        profile_id="edge:worker:0",
        page_id="application:attempt-1",
        owner_id=owner_id,
        scope_id="worker:0",
        attempt_id=attempt_id,
        runtime_id="codex:edge:cdp:9222",
        ttl_seconds=10,
    )


def test_browser_broker_heartbeats_stable_lease_and_rejects_second_writer() -> None:
    clock = Clock()
    broker = BrowserBroker(default_ttl_seconds=10, clock=clock)
    first = _bundle(broker)
    clock.value += 2
    renewed = broker.heartbeat(first, ttl_seconds=10)

    assert renewed.profile.lease_id == first.profile.lease_id
    assert renewed.page.lease_id == first.page.lease_id
    assert renewed.profile.epoch == first.profile.epoch == 1
    assert renewed.profile.expires_at == pytest.approx(112.0)
    assert set(renewed.page.capabilities) == {
        "observe_form",
        "read_page_identity",
    }
    with pytest.raises(BrowserLeaseConflict, match="single writer"):
        _bundle(
            broker,
            owner_id="application:attempt-2",
            attempt_id="attempt-2",
        )


def test_expired_lease_is_replaced_with_higher_epoch_and_old_token_fails_closed() -> None:
    clock = Clock()
    broker = BrowserBroker(default_ttl_seconds=5, clock=clock)
    first = broker.acquire_bundle(
        profile_id="edge:worker:0",
        page_id="application:attempt-1",
        owner_id="application:attempt-1",
        scope_id="worker:0",
        attempt_id="attempt-1",
        runtime_id="codex:edge:cdp:9222",
        ttl_seconds=5,
    )
    clock.value += 6
    second = broker.acquire_bundle(
        profile_id="edge:worker:0",
        page_id="application:attempt-2",
        owner_id="application:attempt-2",
        scope_id="worker:0",
        attempt_id="attempt-2",
        runtime_id="codex:edge:cdp:9222",
        ttl_seconds=5,
    )

    assert second.profile.epoch == 2
    with pytest.raises(BrowserLeaseExpired):
        broker.validate(first.profile)


def test_page_epoch_is_an_optimistic_stale_page_check() -> None:
    broker = BrowserBroker()
    first = _bundle(broker)
    advanced = broker.advance_page(first, expected_page_epoch=0)

    assert advanced.page_binding.page_epoch == 1
    with pytest.raises(StalePageBinding, match="stale page epoch"):
        broker.validate_page(first.page_binding)
    with pytest.raises(StalePageBinding, match="stale page epoch"):
        broker.advance_page(first, expected_page_epoch=0)


def test_serialized_bundle_rejects_mixed_profile_and_page_owners() -> None:
    broker = BrowserBroker()
    bundle = _bundle(broker)
    tampered = bundle.as_dict()
    tampered["page"] = {
        **tampered["page"],
        "owner_id": "application:other-attempt",
    }

    with pytest.raises(BrowserBrokerError, match="different owners"):
        BrowserLeaseBundle.from_mapping(tampered)


def test_semantic_ops_are_observation_only_and_recheck_page_version() -> None:
    broker = BrowserBroker()
    bundle = _bundle(broker)
    ops = SemanticBrowserOps(broker, lambda: {"fields": 3})

    assert ops.observe_form(bundle) == {"fields": 3}
    with pytest.raises(BrowserAuthorityDenied, match="page_write"):
        ops.apply_form_patch(bundle, {"name": "value"})
    with pytest.raises(BrowserAuthorityDenied, match="page_write"):
        ops.upload_artifact(bundle, "resume.pdf")
    with pytest.raises(BrowserAuthorityDenied, match="does not grant"):
        broker.require_operation(bundle.page_binding, "submit")


def test_production_run_job_lease_seam_blocks_submit_runtime_switch_and_stale_page(
    monkeypatch,
) -> None:
    broker = BrowserBroker()
    monkeypatch.setattr(launcher, "_browser_broker", broker)
    job = {
        "url": "https://example.test/jobs/1",
        "application_url": "https://example.test/jobs/1/apply",
        "_attempt_id": "attempt-1",
        "_browser_root_runtime": "edge",
    }
    prepare = launcher._browser_lease_for_agent_turn(
        job,
        worker_id=0,
        port=9222,
        agent_backend="codex",
        actor_id="application:attempt-1",
        attempt_id="attempt-1",
        submission_phase="prepare",
        dry_run=False,
        resume_existing_page=False,
    )
    submit = launcher._browser_lease_for_agent_turn(
        job,
        worker_id=0,
        port=9222,
        agent_backend="codex",
        actor_id="application:attempt-1",
        attempt_id="attempt-1",
        submission_phase="submit",
        dry_run=False,
        resume_existing_page=True,
    )
    assert submit.profile.lease_id == prepare.profile.lease_id

    switched = dict(job)
    switched["_browser_root_runtime"] = "cloak"
    with pytest.raises(BrowserContinuityError, match="submit_started"):
        launcher._browser_lease_for_agent_turn(
            switched,
            worker_id=0,
            port=9222,
            agent_backend="codex",
            actor_id="application:attempt-1",
            attempt_id="attempt-1",
            submission_phase="submit",
            dry_run=False,
            resume_existing_page=True,
        )

    broker.advance_page(submit, expected_page_epoch=0)
    with pytest.raises(StalePageBinding):
        launcher._browser_lease_for_agent_turn(
            job,
            worker_id=0,
            port=9222,
            agent_backend="codex",
            actor_id="application:attempt-1",
            attempt_id="attempt-1",
            submission_phase="submit",
            dry_run=False,
            resume_existing_page=True,
        )


def test_launcher_cleanup_releases_broker_scope(monkeypatch) -> None:
    broker = BrowserBroker()
    monkeypatch.setattr(launcher, "_browser_broker", broker)
    monkeypatch.setattr(launcher, "_cleanup_chrome_worker", lambda *_args: None)
    _bundle(broker)

    launcher.cleanup_worker(0, None)

    replacement = _bundle(
        broker,
        owner_id="application:attempt-2",
        attempt_id="attempt-2",
    )
    assert replacement.profile.epoch == 2
