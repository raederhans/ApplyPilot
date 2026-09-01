from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from applypilot.apply.browser_broker import (
    BrowserAuthorityDenied,
    BrowserContinuityError,
    BrowserLeaseConflict,
    BrowserLeaseExpired,
    LeaseHeartbeat,
    StalePageBinding,
)
from applypilot.apply.durable_browser_broker import DurableBrowserBroker
from applypilot.storage import runtime_control


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 1, 1, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


def _provider(path, calls: list[tuple[int, sqlite3.Connection]] | None = None):
    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(path, timeout=1)
        if calls is not None:
            calls.append((threading.get_ident(), connection))
        return connection

    return connect


def _broker(path, clock: Clock, *, process=(4321, 987_654), lease_ids=None):
    ids = iter(lease_ids or (f"lease-{index}" for index in range(1, 100)))
    return DurableBrowserBroker(
        _provider(path),
        default_ttl_seconds=60,
        clock=clock,
        lease_id_factory=lambda: next(ids),
        process_identity_provider=lambda: process,
        close_connections=True,
    )


def _acquire(broker: DurableBrowserBroker, **overrides):
    values = {
        "profile_id": "profile-1",
        "page_id": "page-1",
        "owner_id": "owner-1",
        "scope_id": "scope-1",
        "attempt_id": "attempt-1",
        "runtime_id": "runtime-1",
    }
    values.update(overrides)
    return broker.acquire_bundle(**values)


def test_one_row_bundle_exact_replay_and_restart_reconstruction(tmp_path) -> None:
    path = tmp_path / "broker.db"
    clock = Clock()
    first_broker = _broker(path, clock)
    first = _acquire(first_broker)
    clock.value += timedelta(seconds=5)

    restarted = _broker(path, clock, lease_ids=["must-not-be-used"])
    replay = _acquire(restarted)

    assert replay.profile.lease_id == replay.page.lease_id == first.profile.lease_id
    assert replay.profile.epoch == replay.page.epoch == 1
    assert replay.page_binding.profile_lease_id == replay.page_binding.page_lease_id
    assert replay.page_binding.page_epoch == 0
    assert replay.profile.heartbeat_at > first.profile.heartbeat_at
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT COUNT(*) FROM browser_resource_leases"
    ).fetchone()[0] == 1


def test_borrowed_connection_and_caller_transaction_remain_caller_owned(tmp_path) -> None:
    path = tmp_path / "borrowed.db"
    connection = sqlite3.connect(path)
    runtime_control.ensure_schema(connection)
    connection.execute("CREATE TABLE caller_state(value TEXT)")
    connection.commit()
    def caller_row_factory(_cursor, row):
        return tuple(row)

    connection.row_factory = caller_row_factory
    connection.execute("BEGIN")
    connection.execute("INSERT INTO caller_state VALUES('preserved')")
    broker = DurableBrowserBroker(
        lambda: connection,
        default_ttl_seconds=60,
        clock=Clock(),
        lease_id_factory=lambda: "borrowed-lease",
        process_identity_provider=lambda: (4321, 987_654),
    )

    _acquire(broker)

    assert connection.row_factory is caller_row_factory
    assert connection.in_transaction is True
    assert connection.execute("SELECT value FROM caller_state").fetchone()[0] == "preserved"
    assert connection.execute(
        "SELECT status FROM browser_resource_leases"
    ).fetchone()[0] == "active"
    connection.rollback()
    assert connection.execute("SELECT COUNT(*) FROM caller_state").fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM browser_resource_leases"
    ).fetchone()[0] == 0


def test_two_facades_racing_for_one_profile_have_one_winner(tmp_path) -> None:
    path = tmp_path / "race.db"
    clock = Clock()
    barrier = threading.Barrier(2)

    def acquire(owner: int) -> str:
        barrier.wait()
        try:
            _acquire(
                _broker(path, clock, lease_ids=[f"lease-{owner}"]),
                owner_id=f"owner-{owner}",
                attempt_id=f"attempt-{owner}",
                runtime_id=f"runtime-{owner}",
            )
            return "acquired"
        except BrowserLeaseConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(acquire, (1, 2)))

    assert sorted(outcomes) == ["acquired", "conflict"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"profile_id": "profile-1", "page_id": "page-2"},
        {"profile_id": "profile-2", "page_id": "page-1"},
        {"owner_id": "owner-2"},
        {"scope_id": "scope-2"},
        {"attempt_id": "attempt-2"},
        {"runtime_id": "runtime-2"},
    ],
)
def test_profile_page_and_authority_contention_fail_closed(tmp_path, overrides) -> None:
    path = tmp_path / "contention.db"
    clock = Clock()
    _acquire(_broker(path, clock))

    with pytest.raises(BrowserLeaseConflict):
        _acquire(_broker(path, clock, process=(4321, 987_654)), **overrides)


def test_process_identity_is_part_of_every_facade_cas(tmp_path) -> None:
    path = tmp_path / "process.db"
    clock = Clock()
    owner = _broker(path, clock)
    bundle = _acquire(owner)
    wrong_process = _broker(path, clock, process=(4322, 987_655))

    with pytest.raises(BrowserLeaseConflict):
        _acquire(wrong_process)
    wrong_process._known[bundle.profile.lease_id] = bundle
    with pytest.raises(BrowserLeaseConflict):
        wrong_process.heartbeat(bundle)
    with pytest.raises(BrowserLeaseConflict):
        wrong_process.release_scope(
            "scope-1",
            owner_id="owner-1",
            attempt_id="attempt-1",
            runtime_id="runtime-1",
        )


def test_double_advance_is_page_epoch_cas_across_connections(tmp_path) -> None:
    path = tmp_path / "advance.db"
    clock = Clock()
    first = _broker(path, clock)
    original = _acquire(first)
    second = _broker(path, clock)
    second_view = _acquire(second)
    clock.value += timedelta(seconds=1)

    advanced = first.advance_page(original, expected_page_epoch=0)

    assert advanced.page_binding.page_epoch == 1
    with pytest.raises(StalePageBinding):
        second.advance_page(second_view, expected_page_epoch=0)
    with pytest.raises(StalePageBinding):
        first.validate_page(original.page_binding)


def test_renewable_timestamps_are_not_part_of_lease_token_identity(tmp_path) -> None:
    path = tmp_path / "renewable.db"
    clock = Clock()
    broker = _broker(path, clock)
    original = _acquire(broker)
    clock.value += timedelta(seconds=5)
    renewed = broker.heartbeat(original)
    clock.value += timedelta(seconds=5)

    assert broker.validate(original.profile) == renewed.profile
    continued = broker.continue_bundle(
        original,
        profile_id="profile-1",
        page_id="page-1",
        owner_id="owner-1",
        scope_id="scope-1",
        attempt_id="attempt-1",
        runtime_id="runtime-1",
        submit_started=True,
        resume_existing_page=True,
    )
    assert continued.profile.lease_id == original.profile.lease_id


def test_release_replay_inactive_replay_and_cross_facade_exact_scope_release(
    tmp_path,
) -> None:
    path = tmp_path / "release.db"
    clock = Clock()
    first = _broker(path, clock, lease_ids=["lease-fixed"])
    bundle = _acquire(first)
    restarted = _broker(path, clock)

    restarted.release_scope(
        "scope-1",
        owner_id="owner-1",
        attempt_id="attempt-1",
        runtime_id="runtime-1",
        expected_bundles=(bundle,),
    )
    first.release_scope("scope-1")
    with pytest.raises(BrowserLeaseExpired):
        first.validate(bundle.profile)
    with pytest.raises(BrowserLeaseExpired):
        _acquire(_broker(path, clock, lease_ids=["lease-fixed"]))


def test_release_wins_against_stale_heartbeat_from_second_facade(tmp_path) -> None:
    path = tmp_path / "release-heartbeat.db"
    clock = Clock()
    first = _broker(path, clock)
    first_view = _acquire(first)
    second = _broker(path, clock)
    second_view = _acquire(second)

    first.release_scope("scope-1")

    with pytest.raises(BrowserLeaseExpired):
        second.heartbeat(second_view)
    with pytest.raises(BrowserLeaseExpired):
        first.validate(first_view.profile)


def test_scope_release_rejects_mixed_authority_without_partial_release(tmp_path) -> None:
    path = tmp_path / "mixed-scope.db"
    clock = Clock()
    first = _broker(path, clock)
    _acquire(first)
    _acquire(
        _broker(path, clock, process=(4322, 987_655), lease_ids=["lease-2"]),
        profile_id="profile-2",
        page_id="page-2",
        owner_id="owner-2",
        attempt_id="attempt-2",
        runtime_id="runtime-2",
    )

    with pytest.raises(BrowserLeaseConflict):
        first.release_scope("scope-1")

    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT COUNT(*) FROM browser_resource_leases WHERE status='active'"
    ).fetchone()[0] == 2


def test_scope_release_vs_advance_is_all_released_or_all_active(tmp_path) -> None:
    path = tmp_path / "scope-race.db"
    clock = Clock()
    owner = _broker(path, clock, lease_ids=["lease-1", "lease-2"])
    _acquire(owner)
    _acquire(owner, profile_id="profile-2", page_id="page-2")
    racer = _broker(path, clock)
    racer_view = _acquire(racer)
    _acquire(racer, profile_id="profile-2", page_id="page-2")
    barrier = threading.Barrier(2)

    def release() -> str:
        barrier.wait()
        try:
            owner.release_scope("scope-1")
            return "released"
        except StalePageBinding:
            return "stale"

    def advance() -> str:
        barrier.wait()
        try:
            racer.advance_page(racer_view, expected_page_epoch=0)
            return "advanced"
        except BrowserLeaseExpired:
            return "expired"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [pool.submit(release), pool.submit(advance)]
        results = {future.result() for future in outcomes}

    connection = sqlite3.connect(path)
    statuses = [
        row[0]
        for row in connection.execute(
            "SELECT status FROM browser_resource_leases ORDER BY lease_id"
        )
    ]
    assert statuses in (["released", "released"], ["active", "active"])
    assert results in ({"released", "expired"}, {"stale", "advanced"})


def test_stale_previous_bundle_cannot_release_new_page_epoch(tmp_path) -> None:
    path = tmp_path / "stale-continuation.db"
    clock = Clock()
    first = _broker(path, clock)
    previous = _acquire(first)
    restarted = _broker(path, clock)
    current = _acquire(restarted)
    clock.value += timedelta(seconds=1)
    restarted.advance_page(current, expected_page_epoch=0)

    with pytest.raises(StalePageBinding):
        first.continue_bundle(
            previous,
            profile_id="profile-2",
            page_id="page-2",
            owner_id="owner-1",
            scope_id="scope-1",
            attempt_id="attempt-1",
            runtime_id="runtime-2",
            submit_started=False,
            resume_existing_page=False,
        )

    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT status,page_epoch FROM browser_resource_leases"
    ).fetchone() == ("active", 1)


def test_ownerless_scope_release_is_instance_local_and_close_preserves_row(tmp_path) -> None:
    path = tmp_path / "close.db"
    clock = Clock()
    owner = _broker(path, clock)
    bundle = _acquire(owner)
    unrelated = _broker(path, clock)

    unrelated.release_scope("scope-1")
    unrelated.close()
    assert owner.validate(bundle.profile).lease_id == bundle.profile.lease_id
    owner.close()
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT status FROM browser_resource_leases"
    ).fetchone()[0] == "active"


def test_continue_contract_and_observation_only_authority(tmp_path) -> None:
    path = tmp_path / "continue.db"
    clock = Clock()
    broker = _broker(path, clock)
    bundle = _acquire(broker)

    with pytest.raises(BrowserContinuityError, match="submit_started"):
        broker.continue_bundle(
            bundle,
            profile_id="profile-2",
            page_id="page-2",
            owner_id="owner-1",
            scope_id="scope-1",
            attempt_id="attempt-1",
            runtime_id="runtime-2",
            submit_started=True,
            resume_existing_page=True,
        )
    assert broker.require_operation(bundle.page_binding, "observe_form")
    with pytest.raises(BrowserAuthorityDenied):
        broker.require_operation(bundle.page_binding, "submit")


def test_pre_submit_continuation_releases_complete_known_scope_atomically(
    tmp_path,
) -> None:
    path = tmp_path / "complete-continuation.db"
    clock = Clock()
    broker = _broker(path, clock, lease_ids=["lease-1", "lease-2", "lease-3"])
    previous = _acquire(broker)
    _acquire(broker, profile_id="profile-2", page_id="page-2")

    continued = broker.continue_bundle(
        previous,
        profile_id="profile-3",
        page_id="page-3",
        owner_id="owner-1",
        scope_id="scope-1",
        attempt_id="attempt-1",
        runtime_id="runtime-2",
        submit_started=False,
        resume_existing_page=False,
    )

    connection = sqlite3.connect(path)
    rows = connection.execute(
        "SELECT lease_id,status FROM browser_resource_leases ORDER BY lease_id"
    ).fetchall()
    assert rows == [
        ("lease-1", "released"),
        ("lease-2", "released"),
        ("lease-3", "active"),
    ]
    assert continued.profile.lease_id == "lease-3"


def test_pre_submit_continuation_rejects_known_mixed_authority_scope(tmp_path) -> None:
    path = tmp_path / "mixed-continuation.db"
    clock = Clock()
    broker = _broker(path, clock, lease_ids=["lease-1", "lease-2"])
    previous = _acquire(broker)
    _acquire(
        broker,
        profile_id="profile-2",
        page_id="page-2",
        owner_id="owner-2",
        attempt_id="attempt-2",
        runtime_id="runtime-2",
    )

    with pytest.raises(BrowserLeaseConflict, match="mixed authority"):
        broker.continue_bundle(
            previous,
            profile_id="profile-3",
            page_id="page-3",
            owner_id="owner-1",
            scope_id="scope-1",
            attempt_id="attempt-1",
            runtime_id="runtime-3",
            submit_started=False,
            resume_existing_page=False,
        )

    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT COUNT(*) FROM browser_resource_leases WHERE status='active'"
    ).fetchone()[0] == 2


def test_backwards_wall_clock_and_sqlite_fault_are_not_contention(tmp_path, monkeypatch) -> None:
    path = tmp_path / "clock.db"
    clock = Clock()
    broker = _broker(path, clock)
    bundle = _acquire(broker)
    clock.value -= timedelta(seconds=1)

    with pytest.raises(StalePageBinding, match="timeline"):
        broker.heartbeat(bundle)

    def unavailable(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk unavailable")

    monkeypatch.setattr(runtime_control, "inspect_browser_resource_lease_state", unavailable)
    with pytest.raises(sqlite3.OperationalError, match="disk unavailable"):
        _acquire(_broker(tmp_path / "other.db", Clock()))


def test_heartbeat_thread_obtains_its_own_connection(tmp_path) -> None:
    path = tmp_path / "heartbeat.db"
    clock = Clock()
    calls: list[tuple[int, sqlite3.Connection]] = []
    broker = DurableBrowserBroker(
        _provider(path, calls),
        default_ttl_seconds=60,
        clock=clock,
        lease_id_factory=lambda: "lease-heartbeat",
        process_identity_provider=lambda: (4321, 987_654),
        close_connections=True,
    )
    bundle = _acquire(broker)
    main_thread = threading.get_ident()
    heartbeat = LeaseHeartbeat(broker, bundle, interval_seconds=0.01).start()
    threading.Event().wait(0.04)
    heartbeat.stop()
    heartbeat.raise_if_failed()

    assert any(thread_id != main_thread for thread_id, _ in calls)
    assert len({id(connection) for _, connection in calls}) == len(calls)


def test_schema_has_scope_and_exact_binding_index() -> None:
    connection = sqlite3.connect(":memory:")
    runtime_control.ensure_schema(connection)
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(browser_resource_leases)")
    }
    indexes = {
        row[1] for row in connection.execute("PRAGMA index_list(browser_resource_leases)")
    }
    scope_index_columns = [
        row[2]
        for row in connection.execute(
            "PRAGMA index_info(idx_browser_leases_scope_binding)"
        )
    ]

    assert "scope_id" in columns
    assert "idx_browser_leases_scope_binding" in indexes
    assert scope_index_columns == [
        "scope_id",
        "owner_id",
        "actor_id",
        "attempt_id",
        "runtime_id",
        "process_id",
        "process_birth_time",
        "status",
    ]
