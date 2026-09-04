from __future__ import annotations

import sqlite3
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from applypilot.apply.browser_authority import BrowserAuthorityHandle
from applypilot.apply.browser_broker import (
    BrowserContinuityError,
    BrowserLeaseConflict,
    StalePageBinding,
)
from applypilot.apply.contracts import application_actor_id
from applypilot.apply.durable_browser_broker import DurableBrowserBroker


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 4, 1, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


def _broker(path, clock: Clock, *, process=(4321, 987_654), lease_ids=None):
    ids = iter(lease_ids or (f"lease-{index}" for index in range(1, 100)))

    def connect() -> sqlite3.Connection:
        return sqlite3.connect(path, timeout=1)

    return DurableBrowserBroker(
        connect,
        default_ttl_seconds=60,
        clock=clock,
        lease_id_factory=lambda: next(ids),
        process_identity_provider=lambda: process,
        close_connections=True,
    )


def _handle(job, broker, attempt_id="attempt-1") -> BrowserAuthorityHandle:
    return BrowserAuthorityHandle.create(
        job,
        broker=broker,
        browser_generation=7,
        application_session_id="application-session-1",
        actor_id=application_actor_id(attempt_id),
        attempt_id=attempt_id,
    )


def _acquire(handle: BrowserAuthorityHandle):
    return handle.acquire_or_continue(
        profile_id="edge:worker:1",
        page_id="application:attempt-1",
        scope_id="worker:1",
        runtime_id="codex:edge:cdp:9333",
        submit_started=False,
        resume_existing_page=False,
    )


def test_handle_is_the_single_serialized_identity_and_mutation_entry(tmp_path) -> None:
    clock = Clock()
    job: dict[str, object] = {"_attempt_id": "attempt-1"}

    bundle = _acquire(_handle(job, _broker(tmp_path / "handle.db", clock)))

    record = job["_browser_lease_binding"]
    assert isinstance(record, dict)
    assert record["schema_version"] == "2"
    assert record["browser_generation"] == 7
    assert record["application_session_id"] == "application-session-1"
    assert record["actor_id"] == application_actor_id("attempt-1")
    assert record["attempt_id"] == "attempt-1"
    assert record["profile"] == bundle.profile.as_dict()
    assert record["page"] == bundle.page.as_dict()
    assert record["page_binding"] == bundle.page_binding.as_dict()
    assert "submit" not in record
    assert "page_write" not in bundle.page.capabilities


def test_stale_handle_cannot_overwrite_newer_page_epoch(tmp_path) -> None:
    clock = Clock()
    job: dict[str, object] = {"_attempt_id": "attempt-1"}
    broker = _broker(tmp_path / "stale.db", clock)
    _acquire(_handle(job, broker))
    stale = BrowserAuthorityHandle.rebuild(job, broker=broker)
    current = BrowserAuthorityHandle.rebuild(job, broker=broker)

    advanced = current.advance_page(expected_page_epoch=0)

    assert advanced.page_binding.page_epoch == 1
    with pytest.raises(StalePageBinding, match="handle is stale"):
        stale.heartbeat()
    assert BrowserAuthorityHandle.rebuild(job).bundle.page_binding.page_epoch == 1


def test_repair_adoption_rejects_cross_talk_and_stale_binding(tmp_path) -> None:
    clock = Clock()
    job: dict[str, object] = {"_attempt_id": "attempt-1"}
    broker = _broker(tmp_path / "adopt.db", clock)
    _acquire(_handle(job, broker))
    old_candidate = deepcopy(job)
    current = BrowserAuthorityHandle.rebuild(job, broker=broker)
    current.advance_page(expected_page_epoch=0)

    with pytest.raises(StalePageBinding, match="page epoch is stale"):
        BrowserAuthorityHandle.rebuild(job).adopt_from_job(old_candidate)

    cross_talk = deepcopy(job)
    cross_talk["_browser_lease_binding"]["actor_id"] = application_actor_id(  # type: ignore[index]
        "attempt-2"
    )
    cross_talk["_browser_lease_binding"]["attempt_id"] = "attempt-2"  # type: ignore[index]
    with pytest.raises(BrowserContinuityError, match="generation, session, actor"):
        BrowserAuthorityHandle.rebuild(job).adopt_from_job(cross_talk)

    wrong_parent_attempt = deepcopy(job)
    wrong_parent_attempt["_attempt_id"] = "attempt-2"
    with pytest.raises(BrowserContinuityError, match="job attempt"):
        BrowserAuthorityHandle.rebuild(wrong_parent_attempt)


def test_adoption_does_not_duplicate_or_mutate_submit_authority(tmp_path) -> None:
    clock = Clock()
    gate = {"gate_id": "gate-1", "attempt_id": "attempt-1", "consumed": True}
    job: dict[str, object] = {
        "_attempt_id": "attempt-1",
        "_submission_gate_binding": deepcopy(gate),
    }
    broker = _broker(tmp_path / "submit.db", clock)
    _acquire(_handle(job, broker))
    candidate = deepcopy(job)

    BrowserAuthorityHandle.rebuild(job).adopt_from_job(candidate)

    assert job["_submission_gate_binding"] == gate
    record = job["_browser_lease_binding"]
    assert isinstance(record, dict)
    assert set(record).isdisjoint(
        {"submission_gate", "submit_authority", "submit_started"}
    )


def test_new_broker_facade_rebuilds_same_durable_binding_after_restart(tmp_path) -> None:
    path = tmp_path / "restart.db"
    clock = Clock()
    job: dict[str, object] = {"_attempt_id": "attempt-1"}
    original = _acquire(_handle(job, _broker(path, clock, lease_ids=["lease-fixed"])))
    clock.value += timedelta(seconds=5)

    restarted = BrowserAuthorityHandle.rebuild(job, broker=_broker(path, clock))
    rebuilt = restarted.reacquire_current()

    assert rebuilt.profile.lease_id == original.profile.lease_id == "lease-fixed"
    assert rebuilt.page_binding.page_epoch == original.page_binding.page_epoch
    assert restarted.identity.browser_generation == 7
    assert restarted.identity.application_session_id == "application-session-1"
    assert job["_browser_lease_binding"]["schema_version"] == "2"  # type: ignore[index]


def test_process_change_fails_closed_without_rewriting_job_binding(tmp_path) -> None:
    path = tmp_path / "process.db"
    clock = Clock()
    job: dict[str, object] = {"_attempt_id": "attempt-1"}
    _acquire(_handle(job, _broker(path, clock)))
    before = deepcopy(job["_browser_lease_binding"])
    restarted = BrowserAuthorityHandle.rebuild(
        job,
        broker=_broker(path, clock, process=(4322, 987_655)),
    )

    with pytest.raises(BrowserLeaseConflict):
        restarted.reacquire_current()
    assert job["_browser_lease_binding"] == before


@pytest.mark.parametrize(
    "mutation",
    [
        lambda record: record.update({"schema_version": "2"}),
        lambda record: record.update(
            {"schema_version": "2", "attempt_id": "attempt-1"}
        ),
    ],
    ids=("all_identity_missing", "partial_identity"),
)
def test_v2_requires_every_identity_field(tmp_path, mutation) -> None:
    clock = Clock()
    source_job: dict[str, object] = {"_attempt_id": "attempt-1"}
    bundle = _acquire(_handle(source_job, _broker(tmp_path / "v2.db", clock)))
    record = bundle.as_dict()
    mutation(record)
    job: dict[str, object] = {
        "_attempt_id": "attempt-1",
        "_browser_lease_binding": record,
    }

    with pytest.raises(BrowserContinuityError, match="identity is incomplete"):
        BrowserAuthorityHandle.rebuild(job)


def test_unknown_schema_version_is_rejected(tmp_path) -> None:
    clock = Clock()
    source_job: dict[str, object] = {"_attempt_id": "attempt-1"}
    bundle = _acquire(_handle(source_job, _broker(tmp_path / "unknown.db", clock)))
    record = bundle.as_dict()
    record["schema_version"] = "99"

    with pytest.raises(BrowserContinuityError, match="unsupported.*schema"):
        BrowserAuthorityHandle.rebuild(
            {
                "_attempt_id": "attempt-1",
                "_browser_lease_binding": record,
            }
        )


def test_legacy_bundle_cannot_rebind_to_new_session_or_generation(tmp_path) -> None:
    clock = Clock()
    source_job: dict[str, object] = {"_attempt_id": "attempt-1"}
    broker = _broker(tmp_path / "legacy-rebind.db", clock)
    bundle = _acquire(_handle(source_job, broker))
    legacy_job: dict[str, object] = {
        "_attempt_id": "attempt-1",
        "_application_context_bundle": {
            "browser_generation": 4,
            "application_session_id": "old-session",
            "actor_id": application_actor_id("attempt-1"),
            "attempt_id": "attempt-1",
        },
        "_browser_lease_binding": bundle.as_dict(),
    }

    with pytest.raises(BrowserContinuityError, match="generation.*session"):
        BrowserAuthorityHandle.create(
            legacy_job,
            broker=broker,
            browser_generation=5,
            application_session_id="new-session",
            actor_id=application_actor_id("attempt-1"),
            attempt_id="attempt-1",
        )


def test_legacy_context_actor_and_attempt_must_match_bundle(tmp_path) -> None:
    clock = Clock()
    source_job: dict[str, object] = {"_attempt_id": "attempt-1"}
    bundle = _acquire(
        _handle(source_job, _broker(tmp_path / "legacy-context.db", clock))
    )
    legacy_job: dict[str, object] = {
        "_attempt_id": "attempt-1",
        "_application_context_bundle": {
            "actor_id": application_actor_id("attempt-2"),
            "attempt_id": "attempt-2",
        },
        "_browser_lease_binding": bundle.as_dict(),
    }

    with pytest.raises(BrowserContinuityError, match="legacy context actor_id"):
        BrowserAuthorityHandle.rebuild(legacy_job)


def test_new_process_rebuilds_only_after_old_process_lease_expires(tmp_path) -> None:
    path = tmp_path / "process-recovery.db"
    clock = Clock()
    job: dict[str, object] = {"_attempt_id": "attempt-1"}
    original = _acquire(
        _handle(job, _broker(path, clock, lease_ids=["lease-old"]))
    )
    clock.value += timedelta(seconds=61)
    restarted = BrowserAuthorityHandle.rebuild(
        job,
        broker=_broker(
            path,
            clock,
            process=(4322, 987_655),
            lease_ids=["lease-new"],
        ),
    )

    recovered = restarted.reacquire_current()

    assert recovered.profile.lease_id == "lease-new"
    assert recovered.profile.epoch == original.profile.epoch + 1
    assert restarted.identity.browser_generation == 7
    assert restarted.identity.application_session_id == "application-session-1"
    assert job["_browser_lease_binding"]["profile"]["lease_id"] == "lease-new"  # type: ignore[index]


def test_legacy_bundle_rebuilds_and_upgrades_on_next_heartbeat(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    clock = Clock()
    broker = _broker(path, clock)
    source_job: dict[str, object] = {"_attempt_id": "attempt-1"}
    bundle = _acquire(_handle(source_job, broker))
    legacy_job: dict[str, object] = {
        "_attempt_id": "attempt-1",
        "_application_context_bundle": {
            "browser_generation": 4,
            "application_session_id": "recovered-session",
            "actor_id": application_actor_id("attempt-1"),
            "attempt_id": "attempt-1",
        },
        "_browser_lease_binding": bundle.as_dict(),
    }
    clock.value += timedelta(seconds=1)

    recovered = BrowserAuthorityHandle.rebuild(legacy_job, broker=broker)
    recovered.heartbeat()

    record = legacy_job["_browser_lease_binding"]
    assert isinstance(record, dict)
    assert record["schema_version"] == "2"
    assert record["browser_generation"] == 4
    assert record["application_session_id"] == "recovered-session"


def test_missing_legacy_schema_version_is_compatibly_upgraded(tmp_path) -> None:
    path = tmp_path / "legacy-missing-version.db"
    clock = Clock()
    broker = _broker(path, clock)
    source_job: dict[str, object] = {"_attempt_id": "attempt-1"}
    bundle = _acquire(_handle(source_job, broker))
    record = bundle.as_dict()
    record.pop("schema_version")
    legacy_job: dict[str, object] = {
        "_attempt_id": "attempt-1",
        "_browser_lease_binding": record,
    }

    BrowserAuthorityHandle.rebuild(legacy_job, broker=broker).heartbeat()

    upgraded = legacy_job["_browser_lease_binding"]
    assert isinstance(upgraded, dict)
    assert upgraded["schema_version"] == "2"
    assert upgraded["application_session_id"] == "legacy:attempt-1"


def test_exact_release_removes_only_owned_binding(tmp_path) -> None:
    path = tmp_path / "release.db"
    clock = Clock()
    job: dict[str, object] = {"_attempt_id": "attempt-1"}
    handle = _handle(job, _broker(path, clock))
    _acquire(handle)

    handle.release()

    assert "_browser_lease_binding" not in job
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT status FROM browser_resource_leases"
    ).fetchone() == ("released",)
