from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

from applypilot.storage import runtime_control

NOW = datetime(2026, 8, 31, 9, 0, tzinfo=UTC)


def _connection(path: object = ":memory:") -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=1)
    connection.row_factory = sqlite3.Row
    return connection


def _acquire(
    connection: sqlite3.Connection,
    *,
    lease_id: str = "lease-1",
    profile_id: str = "profile-1",
    page_target_id: str | None = "page-1",
    owner_id: str = "owner-1",
    expected_lease_epoch: int | None = 0,
    now: datetime = NOW,
) -> runtime_control.BrowserResourceLease:
    return runtime_control.acquire_browser_resource_lease(
        connection,
        lease_id=lease_id,
        resource_kind="browser-profile-page",
        scope_id="scope-1",
        profile_id=profile_id,
        page_target_id=page_target_id,
        owner_id=owner_id,
        actor_id="application:attempt-1",
        attempt_id="attempt-1",
        runtime_id=f"runtime-{owner_id}",
        expected_lease_epoch=expected_lease_epoch,
        page_epoch=4,
        lease_seconds=60,
        process_id=1234,
        process_birth_time=100_000,
        now=now,
    )


def _cas(lease: runtime_control.BrowserResourceLease) -> dict[str, object]:
    return {
        "lease_id": lease.lease_id,
        "owner_id": lease.owner_id,
        "expected_actor_id": lease.actor_id,
        "expected_attempt_id": lease.attempt_id,
        "expected_runtime_id": lease.runtime_id,
        "expected_resource_kind": lease.resource_kind,
        "expected_scope_id": lease.scope_id,
        "expected_profile_id": lease.profile_id,
        "expected_page_target_id": lease.page_target_id,
        "expected_lease_epoch": lease.lease_epoch,
        "expected_page_epoch": lease.page_epoch,
        "expected_process_id": lease.process_id,
        "expected_process_birth_time": lease.process_birth_time,
    }


def _token(
    lease: runtime_control.BrowserResourceLease,
) -> runtime_control.BrowserResourceLeaseToken:
    assert lease.page_target_id is not None
    return runtime_control.BrowserResourceLeaseToken(
        lease_id=lease.lease_id,
        profile_id=lease.profile_id,
        page_target_id=lease.page_target_id,
        lease_epoch=lease.lease_epoch,
        page_epoch=lease.page_epoch,
    )


def _root_turn(
    connection: sqlite3.Connection,
    *,
    turn_id: str = "root",
    submit_started: bool = False,
) -> runtime_control.AgentRuntimeTurn:
    return runtime_control.start_runtime_turn(
        connection,
        turn_id=turn_id,
        actor_id="application:attempt-1",
        attempt_id="attempt-1",
        runtime_id="runtime-1",
        profile_id="profile-1",
        runtime_backend="codex-cli",
        resume_mode="root",
        submit_started=submit_started,
        tool_surface_hash="tools-v1",
        prompt_contract_hash="prompt-v1",
        process_id=1234,
        process_birth_time=100_000,
        started_at=NOW,
    )


def test_new_and_legacy_database_migrate_to_explicit_component_version() -> None:
    new = _connection()
    assert runtime_control.ensure_schema(new) == 1
    assert new.execute(
        "SELECT version FROM agent_control_schema_version"
    ).fetchone()[0] == 1

    legacy = _connection()
    legacy.execute("CREATE TABLE agent_events(event_id TEXT PRIMARY KEY)")
    legacy.execute("INSERT INTO agent_events VALUES('legacy-event')")
    legacy.commit()

    runtime_control.ensure_schema(legacy)

    assert legacy.execute("SELECT event_id FROM agent_events").fetchone()[0] == "legacy-event"
    tables = {
        row[0]
        for row in legacy.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"browser_resource_leases", "agent_runtime_turns"} <= tables
    assert legacy.execute(
        "SELECT version FROM agent_control_schema_version"
    ).fetchone()[0] == runtime_control.CONTROL_PLANE_SCHEMA_VERSION


def test_application_batch_schema_explicitly_wires_runtime_control(tmp_path) -> None:
    from applypilot.database import ensure_application_batch_schema

    connection = _connection(tmp_path / "batch-schema.db")
    ensure_application_batch_schema(connection)

    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"browser_resource_leases", "agent_runtime_turns"} <= tables


def test_repeated_migration_is_a_noop_and_newer_version_fails_closed() -> None:
    connection = _connection()
    runtime_control.ensure_schema(connection)
    objects_before = connection.execute(
        "SELECT type,name,sql FROM sqlite_master "
        "WHERE name LIKE 'browser_resource_%' OR name LIKE 'agent_runtime_%' "
        "ORDER BY type,name"
    ).fetchall()

    assert runtime_control.ensure_schema(connection) == 1
    assert connection.execute(
        "SELECT version FROM agent_control_schema_version"
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT type,name,sql FROM sqlite_master "
        "WHERE name LIKE 'browser_resource_%' OR name LIKE 'agent_runtime_%' "
        "ORDER BY type,name"
    ).fetchall() == objects_before

    connection.execute(
        "UPDATE agent_control_schema_version SET version=99"
    )
    connection.commit()
    with pytest.raises(RuntimeError, match="newer than supported"):
        runtime_control.ensure_schema(connection)
    assert connection.execute(
        "SELECT version FROM agent_control_schema_version"
    ).fetchone()[0] == 99


def test_failed_migration_rolls_back_schema_and_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _connection()

    def broken_migration(candidate: sqlite3.Connection) -> None:
        candidate.execute("CREATE TABLE must_rollback(value TEXT)")
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(
        runtime_control,
        "_MIGRATIONS",
        (broken_migration,),
    )

    with pytest.raises(RuntimeError, match="injected migration failure"):
        runtime_control.ensure_schema(connection)

    assert connection.in_transaction is False
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name IN "
        "('must_rollback','agent_control_schema_version')"
    ).fetchone()[0] == 0


def test_failed_nested_migration_rolls_back_only_its_savepoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _connection()
    connection.execute("CREATE TABLE caller_state(value TEXT)")
    connection.commit()
    connection.execute("BEGIN")
    connection.execute("INSERT INTO caller_state VALUES('preserved')")

    def broken_migration(candidate: sqlite3.Connection) -> None:
        candidate.execute("CREATE TABLE nested_must_rollback(value TEXT)")
        raise RuntimeError("nested migration failure")

    monkeypatch.setattr(runtime_control, "_MIGRATIONS", (broken_migration,))
    with pytest.raises(RuntimeError, match="nested migration failure"):
        runtime_control.ensure_schema(connection)

    assert connection.in_transaction is True
    assert connection.execute("SELECT value FROM caller_state").fetchone()[0] == "preserved"
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name IN "
        "('nested_must_rollback','agent_control_schema_version')"
    ).fetchone()[0] == 0
    connection.rollback()


@pytest.mark.parametrize(
    ("second_profile", "second_page"),
    [("profile-1", "page-2"), ("profile-2", "page-1")],
)
def test_two_connections_cannot_lease_the_same_profile_or_page(
    tmp_path, second_profile: str, second_page: str
) -> None:
    path = tmp_path / "durable-control.db"
    first = _connection(path)
    second = _connection(path)

    acquired = _acquire(first)
    with pytest.raises(runtime_control.ResourceLeaseConflictError):
        _acquire(
            second,
            lease_id="lease-2",
            profile_id=second_profile,
            page_target_id=second_page,
            owner_id="owner-2",
            expected_lease_epoch=None,
        )

    assert acquired.status == "active"
    assert second.execute(
        "SELECT COUNT(*) FROM browser_resource_leases WHERE status='active'"
    ).fetchone()[0] == 1


def test_lease_rejects_noncanonical_actor_and_unpaired_process_identity() -> None:
    connection = _connection()
    with pytest.raises(ValueError, match="canonical"):
        runtime_control.acquire_browser_resource_lease(
            connection,
            lease_id="bad-actor",
            resource_kind="browser-profile-page",
            scope_id="scope-1",
            profile_id="profile-1",
            page_target_id="page-1",
            owner_id="owner-1",
            actor_id="application:other",
            attempt_id="attempt-1",
            runtime_id="runtime-1",
        )
    with pytest.raises(ValueError, match="supplied together"):
        runtime_control.acquire_browser_resource_lease(
            connection,
            lease_id="bad-process",
            resource_kind="browser-profile-page",
            scope_id="scope-1",
            profile_id="profile-1",
            page_target_id="page-1",
            owner_id="owner-1",
            actor_id="application:attempt-1",
            attempt_id="attempt-1",
            runtime_id="runtime-1",
            process_id=1234,
        )


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("expected_actor_id", "application:other"),
        ("owner_id", "other-owner"),
        ("expected_attempt_id", "attempt-other"),
        ("expected_runtime_id", "runtime-other"),
        ("expected_resource_kind", "other-kind"),
        ("expected_scope_id", "other-scope"),
        ("expected_profile_id", "other-profile"),
        ("expected_page_target_id", "other-page"),
        ("expected_process_id", 9999),
        ("expected_process_birth_time", 999_999),
    ],
)
def test_lease_validation_requires_the_full_authority_binding(
    field: str,
    wrong: object,
) -> None:
    connection = _connection()
    acquired = _acquire(connection)
    binding = _cas(acquired)
    binding[field] = wrong

    with pytest.raises(
        (runtime_control.ResourceLeaseConflictError, ValueError),
        match="binding mismatch|canonical",
    ):
        runtime_control.validate_browser_resource_lease(
            connection,
            **binding,
            now=NOW + timedelta(seconds=1),
        )


def test_outer_transaction_stale_acquire_rolls_back_local_expiry_sweep() -> None:
    connection = _connection()
    acquired = _acquire(connection)
    connection.execute("CREATE TABLE caller_state(value TEXT)")
    connection.commit()
    connection.execute("BEGIN")
    connection.execute("INSERT INTO caller_state VALUES('keep')")

    with pytest.raises(runtime_control.StaleResourceLeaseError):
        _acquire(
            connection,
            lease_id="stale-acquire",
            expected_lease_epoch=0,
            now=NOW + timedelta(seconds=61),
        )

    assert connection.in_transaction is True
    assert connection.execute("SELECT value FROM caller_state").fetchone()[0] == "keep"
    assert connection.execute(
        "SELECT status FROM browser_resource_leases WHERE lease_id=?",
        (acquired.lease_id,),
    ).fetchone()[0] == "active"
    assert connection.execute(
        "SELECT COUNT(*) FROM browser_resource_leases WHERE lease_id='stale-acquire'"
    ).fetchone()[0] == 0
    connection.rollback()


def test_lease_heartbeat_page_cas_release_and_reacquire() -> None:
    connection = _connection()
    acquired = _acquire(connection)

    heartbeat = runtime_control.heartbeat_browser_resource_lease(
        connection,
        **_cas(acquired),
        lease_seconds=120,
        now=NOW + timedelta(seconds=10),
    )
    assert heartbeat.expires_at == (NOW + timedelta(seconds=130)).isoformat()

    advanced = runtime_control.advance_browser_page_epoch(
        connection,
        **_cas(acquired),
        now=NOW + timedelta(seconds=20),
    )
    assert advanced.page_epoch == 5
    with pytest.raises(runtime_control.StaleResourceLeaseError):
        runtime_control.validate_browser_resource_lease(
            connection,
            **_cas(acquired),
            now=NOW + timedelta(seconds=21),
        )

    released = runtime_control.release_browser_resource_lease(
        connection,
        **_cas(advanced),
        now=NOW + timedelta(seconds=30),
    )
    assert released.status == "released"
    assert runtime_control.release_browser_resource_lease(
        connection,
        **_cas(advanced),
        now=NOW + timedelta(seconds=31),
    ) == released

    reacquired = _acquire(
        connection,
        lease_id="lease-2",
        owner_id="owner-2",
        expected_lease_epoch=1,
        now=NOW + timedelta(seconds=32),
    )
    assert reacquired.lease_epoch == 2


def test_late_heartbeat_and_advance_cannot_rewind_the_lease_timeline() -> None:
    connection = _connection()
    acquired = _acquire(connection)
    renewed = runtime_control.heartbeat_browser_resource_lease(
        connection,
        **_cas(acquired),
        lease_seconds=300,
        now=NOW + timedelta(seconds=50),
    )
    assert renewed.heartbeat_at == (NOW + timedelta(seconds=50)).isoformat()
    assert renewed.expires_at == (NOW + timedelta(seconds=350)).isoformat()

    for operation in (
        runtime_control.heartbeat_browser_resource_lease,
        runtime_control.advance_browser_page_epoch,
    ):
        with pytest.raises(runtime_control.StaleResourceLeaseError, match="timeline"):
            operation(
                connection,
                **_cas(renewed),
                lease_seconds=1,
                now=NOW + timedelta(seconds=10),
            )
        assert runtime_control.validate_browser_resource_lease(
            connection,
            **_cas(renewed),
            now=NOW + timedelta(seconds=51),
        ) == renewed


def test_expired_lease_invalidates_old_epoch_and_can_be_replaced() -> None:
    connection = _connection()
    acquired = _acquire(connection)
    expired_at = NOW + timedelta(seconds=61)

    with pytest.raises(runtime_control.ResourceLeaseExpiredError):
        runtime_control.validate_browser_resource_lease(
            connection,
            **_cas(acquired),
            now=expired_at,
        )
    assert connection.execute(
        "SELECT status FROM browser_resource_leases WHERE lease_id='lease-1'"
    ).fetchone()[0] == "expired"

    replacement = _acquire(
        connection,
        lease_id="lease-2",
        owner_id="owner-2",
        expected_lease_epoch=1,
        now=expired_at,
    )
    assert replacement.lease_epoch == 2
    with pytest.raises(runtime_control.StaleResourceLeaseError):
        runtime_control.heartbeat_browser_resource_lease(
            connection,
            **{
                **_cas(replacement),
                "expected_lease_epoch": 1,
            },
            now=expired_at + timedelta(seconds=1),
        )


@pytest.mark.parametrize("terminal_state", ["released", "expired"])
def test_acquire_replay_requires_the_existing_lease_to_remain_active(
    terminal_state: str,
) -> None:
    connection = _connection()
    acquired = _acquire(connection)
    if terminal_state == "released":
        runtime_control.release_browser_resource_lease(
            connection,
            **_cas(acquired),
            now=NOW + timedelta(seconds=1),
        )
        replay_at = NOW + timedelta(seconds=2)
    else:
        replay_at = NOW + timedelta(seconds=61)

    with pytest.raises(runtime_control.ResourceLeaseExpiredError, match="not active"):
        _acquire(connection, now=replay_at)
    assert connection.execute(
        "SELECT status FROM browser_resource_leases WHERE lease_id='lease-1'"
    ).fetchone()[0] == terminal_state


def test_scope_release_is_one_exact_batch_and_rolls_back_on_stale_member() -> None:
    connection = _connection()
    first = _acquire(connection)
    second = _acquire(
        connection,
        lease_id="lease-2",
        profile_id="profile-2",
        page_target_id="page-2",
    )
    original_tokens = (_token(first), _token(second))
    advanced = runtime_control.advance_browser_page_epoch(
        connection,
        **_cas(second),
        now=NOW + timedelta(seconds=1),
    )

    with pytest.raises(runtime_control.StaleResourceLeaseError, match="token set"):
        runtime_control.release_browser_resource_scope(
            connection,
            scope_id="scope-1",
            owner_id="owner-1",
            expected_actor_id="application:attempt-1",
            expected_attempt_id="attempt-1",
            expected_runtime_id="runtime-owner-1",
            expected_process_id=1234,
            expected_process_birth_time=100_000,
            expected_tokens=original_tokens,
            now=NOW + timedelta(seconds=2),
        )

    assert [
        row[0]
        for row in connection.execute(
            "SELECT status FROM browser_resource_leases ORDER BY lease_id"
        )
    ] == ["active", "active"]
    released = runtime_control.release_browser_resource_scope(
        connection,
        scope_id="scope-1",
        owner_id="owner-1",
        expected_actor_id="application:attempt-1",
        expected_attempt_id="attempt-1",
        expected_runtime_id="runtime-owner-1",
        expected_process_id=1234,
        expected_process_birth_time=100_000,
        expected_tokens=(_token(first), _token(advanced)),
        now=NOW + timedelta(seconds=2),
    )
    assert {lease.status for lease in released} == {"released"}


def test_two_connections_allow_only_one_running_root_for_an_actor(tmp_path) -> None:
    path = tmp_path / "runtime-root-contention.db"
    setup = _connection(path)
    runtime_control.ensure_schema(setup)
    setup.close()
    barrier = Barrier(2)

    def start(turn_id: str) -> str:
        connection = _connection(path)
        barrier.wait()
        try:
            _root_turn(connection, turn_id=turn_id)
            return "started"
        except runtime_control.RuntimeTurnConflictError:
            return "conflict"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(start, ("root-a", "root-b")))

    assert sorted(outcomes) == ["conflict", "started"]


def test_two_connections_reject_second_running_root_and_sibling(tmp_path) -> None:
    path = tmp_path / "runtime-lineage-contention.db"
    first = _connection(path)
    second = _connection(path)
    parent = _root_turn(first, turn_id="root")

    with pytest.raises(runtime_control.RuntimeTurnConflictError, match="running turn"):
        _root_turn(second, turn_id="second-root")
    assert _root_turn(second, turn_id="root") == parent

    parent = runtime_control.mark_runtime_turn_terminal(
        first,
        turn_id=parent.turn_id,
        status="closed",
        exit_code=0,
        terminal_at=NOW + timedelta(seconds=1),
    )
    child_args = {
        "actor_id": parent.actor_id,
        "attempt_id": parent.attempt_id,
        "parent_turn_id": parent.turn_id,
        "checkpoint_id": "checkpoint-1",
        "runtime_id": "runtime-2",
        "profile_id": "profile-2",
        "runtime_backend": "codex-cli",
        "resume_mode": "resume",
        "submit_started": False,
        "tool_surface_hash": "tools-v1",
        "prompt_contract_hash": "prompt-v1",
    }
    first_child = runtime_control.start_runtime_turn(
        first,
        turn_id="child-a",
        **child_args,
    )
    with pytest.raises(runtime_control.RuntimeTurnConflictError, match="running turn"):
        runtime_control.start_runtime_turn(
            second,
            turn_id="child-b",
            **{**child_args, "checkpoint_id": "checkpoint-2"},
        )
    assert runtime_control.start_runtime_turn(
        second,
        turn_id="child-a",
        **child_args,
    ) == first_child


def test_runtime_turn_parent_terminal_and_idempotency_contracts() -> None:
    connection = _connection()
    parent = runtime_control.start_runtime_turn(
        connection,
        turn_id="turn-parent",
        actor_id="application:attempt-1",
        attempt_id="attempt-1",
        runtime_id="runtime-1",
        profile_id="profile-1",
        runtime_backend="codex-cli",
        resume_mode="root",
        submit_started=False,
        model="gpt-test",
        provider_session_id="provider-session-optional",
        process_id=1234,
        process_birth_time=100_000,
        tool_surface_hash="tools-v1",
        prompt_contract_hash="prompt-v1",
        idempotency_key="runtime-start-parent",
        started_at=NOW,
    )
    assert runtime_control.start_runtime_turn(
        connection,
        turn_id="turn-parent",
        actor_id="application:attempt-1",
        attempt_id="attempt-1",
        runtime_id="runtime-1",
        profile_id="profile-1",
        runtime_backend="codex-cli",
        resume_mode="root",
        submit_started=False,
        model="gpt-test",
        provider_session_id="provider-session-optional",
        process_id=1234,
        process_birth_time=100_000,
        tool_surface_hash="tools-v1",
        prompt_contract_hash="prompt-v1",
        idempotency_key="runtime-start-parent",
        started_at=NOW + timedelta(seconds=1),
    ) == parent

    parent = runtime_control.mark_runtime_turn_terminal(
        connection,
        turn_id=parent.turn_id,
        status="closed",
        exit_code=0,
        terminal_at=NOW + timedelta(seconds=1),
    )

    child = runtime_control.start_runtime_turn(
        connection,
        turn_id="turn-child",
        actor_id=parent.actor_id,
        attempt_id=parent.attempt_id,
        parent_turn_id=parent.turn_id,
        checkpoint_id="checkpoint-1",
        runtime_id="runtime-2",
        profile_id="profile-2",
        runtime_backend="fresh-runtime",
        resume_mode="resume",
        submit_started=False,
        tool_surface_hash="tools-v2",
        prompt_contract_hash="prompt-v1",
        # A restarted launcher can reuse a coarse or frozen clock value.  The
        # causal child is still the newest persisted row even when its turn id
        # sorts before the parent.
        started_at=NOW,
    )
    assert runtime_control.parent_runtime_turn(connection, child.turn_id) == parent
    assert runtime_control.get_runtime_turn(connection, child.turn_id) == child

    terminal = runtime_control.mark_runtime_turn_terminal(
        connection,
        token=runtime_control.token_from_turn(child),
        status="failed",
        failure_code="RUNTIME_EXITED",
        exit_code=17,
        terminal_at=NOW + timedelta(seconds=3),
    )
    assert terminal.status == "failed"
    assert runtime_control.mark_runtime_turn_terminal(
        connection,
        token=runtime_control.token_from_turn(child),
        status="failed",
        failure_code="RUNTIME_EXITED",
        exit_code=17,
        terminal_at=NOW + timedelta(seconds=4),
    ) == terminal
    with pytest.raises(RuntimeError, match="already terminal"):
        runtime_control.mark_runtime_turn_terminal(
            connection,
            token=runtime_control.token_from_turn(child),
            status="completed",
            terminal_at=NOW + timedelta(seconds=5),
        )


def test_runtime_turn_rejects_unknown_or_cross_actor_parent_and_key_collision() -> None:
    connection = _connection()
    with pytest.raises(ValueError, match="parent turn is unknown"):
        runtime_control.start_runtime_turn(
            connection,
            turn_id="orphan",
            actor_id="application:attempt-1",
            attempt_id="attempt-1",
            parent_turn_id="missing",
            checkpoint_id="checkpoint-missing",
            runtime_id="runtime-1",
            profile_id="profile-1",
            runtime_backend="codex-cli",
            resume_mode="resume",
            submit_started=False,
            tool_surface_hash="tools-v1",
            prompt_contract_hash="prompt-v1",
        )

    parent = runtime_control.start_runtime_turn(
        connection,
        turn_id="parent",
        actor_id="application:attempt-1",
        attempt_id="attempt-1",
        runtime_id="runtime-1",
        profile_id="profile-1",
        runtime_backend="codex-cli",
        resume_mode="root",
        submit_started=False,
        tool_surface_hash="tools-v1",
        prompt_contract_hash="prompt-v1",
        idempotency_key="one-start",
    )
    with pytest.raises(ValueError, match="same actor and attempt"):
        runtime_control.start_runtime_turn(
            connection,
            turn_id="wrong-child",
            actor_id="application:attempt-2",
            attempt_id="attempt-2",
            parent_turn_id=parent.turn_id,
            checkpoint_id="checkpoint-2",
            runtime_id="runtime-2",
            profile_id="profile-2",
            runtime_backend="codex-cli",
            resume_mode="resume",
            submit_started=False,
            tool_surface_hash="tools-v1",
            prompt_contract_hash="prompt-v1",
        )
    with pytest.raises(ValueError, match="idempotency collision"):
        runtime_control.start_runtime_turn(
            connection,
            turn_id="different-turn",
            actor_id=parent.actor_id,
            attempt_id=parent.attempt_id,
            runtime_id="runtime-other",
            profile_id="profile-other",
            runtime_backend="different-runtime",
            resume_mode="root",
            submit_started=False,
            tool_surface_hash="tools-v1",
            prompt_contract_hash="prompt-v1",
            idempotency_key="one-start",
        )


def test_runtime_child_rejects_running_parent_then_accepts_unknown_parent() -> None:
    connection = _connection()
    parent = _root_turn(connection)
    child_args = {
        "turn_id": "child",
        "actor_id": parent.actor_id,
        "attempt_id": parent.attempt_id,
        "parent_turn_id": parent.turn_id,
        "checkpoint_id": "checkpoint-1",
        "runtime_id": "runtime-2",
        "profile_id": "profile-2",
        "runtime_backend": "codex-cli",
        "resume_mode": "resume",
        "submit_started": False,
        "tool_surface_hash": "tools-v1",
        "prompt_contract_hash": "prompt-v1",
    }
    with pytest.raises(ValueError, match="terminal or unknown"):
        runtime_control.start_runtime_turn(connection, **child_args)

    parent = runtime_control.mark_runtime_turn_terminal(
        connection,
        turn_id=parent.turn_id,
        status="unknown",
        failure_code="PROCESS_DISAPPEARED",
        exit_code=255,
        terminal_at=NOW + timedelta(seconds=1),
    )
    child = runtime_control.start_runtime_turn(connection, **child_args)

    assert parent.status == "unknown"
    assert parent.exit_code == 255
    assert runtime_control.parent_runtime_turn(connection, child.turn_id) == parent


def test_runtime_turn_shape_actor_and_process_identity_fail_closed() -> None:
    connection = _connection()
    base = {
        "turn_id": "bad-root",
        "actor_id": "application:attempt-1",
        "attempt_id": "attempt-1",
        "runtime_id": "runtime-1",
        "profile_id": "profile-1",
        "runtime_backend": "codex-cli",
        "resume_mode": "root",
        "submit_started": False,
        "tool_surface_hash": "tools-v1",
        "prompt_contract_hash": "prompt-v1",
    }
    with pytest.raises(ValueError, match="canonical"):
        runtime_control.start_runtime_turn(
            connection,
            **{**base, "actor_id": "application:other"},
        )
    with pytest.raises(ValueError, match="supplied together"):
        runtime_control.start_runtime_turn(connection, **base, process_id=1234)
    with pytest.raises(ValueError, match="unsupported resume_mode"):
        runtime_control.start_runtime_turn(
            connection,
            **{**base, "resume_mode": "automatic"},
        )
    with pytest.raises(ValueError, match="root turn requires"):
        runtime_control.start_runtime_turn(
            connection,
            **{**base, "checkpoint_id": "checkpoint-not-root"},
        )


def test_post_submit_child_preserves_submit_runtime_profile_and_receipt_mode() -> None:
    connection = _connection()
    parent = _root_turn(connection, submit_started=True)
    parent = runtime_control.mark_runtime_turn_terminal(
        connection,
        turn_id=parent.turn_id,
        status="closed",
        exit_code=0,
        terminal_at=NOW + timedelta(seconds=1),
    )
    base = {
        "turn_id": "receipt-child",
        "actor_id": parent.actor_id,
        "attempt_id": parent.attempt_id,
        "parent_turn_id": parent.turn_id,
        "checkpoint_id": "checkpoint-submit",
        "runtime_id": parent.runtime_id,
        "profile_id": parent.profile_id,
        "runtime_backend": "codex-cli",
        "resume_mode": "receipt_only",
        "submit_started": True,
        "tool_surface_hash": "tools-v1",
        "prompt_contract_hash": "prompt-v1",
    }
    with pytest.raises(ValueError, match="cannot be downgraded"):
        runtime_control.start_runtime_turn(
            connection,
            **{**base, "turn_id": "downgrade", "submit_started": False},
        )
    with pytest.raises(ValueError, match="runtime/profile cannot change"):
        runtime_control.start_runtime_turn(
            connection,
            **{**base, "turn_id": "switch-runtime", "runtime_id": "runtime-2"},
        )
    with pytest.raises(ValueError, match="receipt_only"):
        runtime_control.start_runtime_turn(
            connection,
            **{**base, "turn_id": "wrong-mode", "resume_mode": "resume"},
        )

    child = runtime_control.start_runtime_turn(connection, **base)
    assert child.submit_started == 1
    assert (child.runtime_id, child.profile_id, child.resume_mode) == (
        parent.runtime_id,
        parent.profile_id,
        "receipt_only",
    )


def test_prepare_to_submit_child_requires_exact_runtime_profile_and_resume_mode() -> None:
    connection = _connection()
    parent = _root_turn(connection, submit_started=False)
    parent = runtime_control.mark_runtime_turn_terminal(
        connection,
        turn_id=parent.turn_id,
        status="closed",
        exit_code=0,
        terminal_at=NOW + timedelta(seconds=1),
    )
    base = {
        "turn_id": "submit-child",
        "actor_id": parent.actor_id,
        "attempt_id": parent.attempt_id,
        "parent_turn_id": parent.turn_id,
        "checkpoint_id": "checkpoint-prepare",
        "runtime_id": parent.runtime_id,
        "profile_id": parent.profile_id,
        "runtime_backend": "codex-cli",
        "resume_mode": "resume",
        "submit_started": True,
        "tool_surface_hash": "tools-v1",
        "prompt_contract_hash": "prompt-v1",
    }

    with pytest.raises(ValueError, match="runtime/profile cannot change"):
        runtime_control.start_runtime_turn(
            connection,
            **{**base, "turn_id": "switch-runtime", "runtime_id": "runtime-2"},
        )
    with pytest.raises(ValueError, match="runtime/profile cannot change"):
        runtime_control.start_runtime_turn(
            connection,
            **{**base, "turn_id": "switch-profile", "profile_id": "profile-2"},
        )
    with pytest.raises(ValueError, match="require resume mode"):
        runtime_control.start_runtime_turn(
            connection,
            **{**base, "turn_id": "wrong-mode", "resume_mode": "receipt_only"},
        )

    child = runtime_control.start_runtime_turn(connection, **base)
    assert child.submit_started == 1
    assert (child.runtime_id, child.profile_id, child.resume_mode) == (
        parent.runtime_id,
        parent.profile_id,
        "resume",
    )


def test_latest_runtime_turn_for_actor_returns_newest_terminal_child() -> None:
    connection = _connection()
    parent = _root_turn(connection, submit_started=False)
    parent = runtime_control.mark_runtime_turn_terminal(
        connection,
        turn_id=parent.turn_id,
        status="unknown",
        failure_code="PROCESS_DISAPPEARED",
        exit_code=None,
        terminal_at=NOW + timedelta(seconds=1),
    )
    assert runtime_control.latest_runtime_turn_for_actor(
        connection, parent.actor_id
    ) == parent

    child = runtime_control.start_runtime_turn(
        connection,
        turn_id="recovery-child",
        actor_id=parent.actor_id,
        attempt_id=parent.attempt_id,
        parent_turn_id=parent.turn_id,
        checkpoint_id="checkpoint-parent",
        runtime_id=parent.runtime_id,
        profile_id=parent.profile_id,
        runtime_backend="codex-cli",
        resume_mode="resume",
        submit_started=False,
        tool_surface_hash="tools-v1",
        prompt_contract_hash="prompt-v2",
        started_at=NOW + timedelta(seconds=2),
    )
    child = runtime_control.mark_runtime_turn_terminal(
        connection,
        token=runtime_control.token_from_turn(child),
        status="completed",
        exit_code=0,
        terminal_at=NOW + timedelta(seconds=3),
    )

    assert runtime_control.latest_runtime_turn_for_actor(
        connection, parent.actor_id
    ) == child


def test_runtime_reservation_attach_exact_replay_and_pid_birth_cas() -> None:
    connection = _connection()
    reserved = runtime_control.start_runtime_turn(
        connection,
        turn_id="reserved",
        actor_id="application:attempt-1",
        attempt_id="attempt-1",
        runtime_id="runtime-1",
        profile_id="profile-1",
        runtime_backend="codex-cli",
        resume_mode="root",
        submit_started=False,
        tool_surface_hash="tools-v1",
        prompt_contract_hash="prompt-v1",
        started_at=NOW,
    )
    reservation_token = runtime_control.token_from_turn(reserved)

    assert runtime_control.running_runtime_turn_for_actor(
        connection, reserved.actor_id
    ) == reserved
    attached = runtime_control.attach_runtime_turn_process(
        connection,
        token=reservation_token,
        process_id=4321,
        process_birth_time=200_000,
    )
    assert (attached.process_id, attached.process_birth_time) == (4321, 200_000)
    assert runtime_control.attach_runtime_turn_process(
        connection,
        token=reservation_token,
        process_id=4321,
        process_birth_time=200_000,
    ) == attached
    assert runtime_control.attach_runtime_turn_process(
        connection,
        token=runtime_control.token_from_turn(attached),
        process_id=4321,
        process_birth_time=200_000,
    ) == attached

    with pytest.raises(runtime_control.RuntimeTurnConflictError, match="different process"):
        runtime_control.attach_runtime_turn_process(
            connection,
            token=reservation_token,
            process_id=4321,
            process_birth_time=200_001,
        )
    with pytest.raises(runtime_control.RuntimeTurnConflictError, match="identity changed"):
        runtime_control.attach_runtime_turn_process(
            connection,
            token=replace(reservation_token, profile_id="stale-profile"),
            process_id=4321,
            process_birth_time=200_000,
        )
    attached_token = runtime_control.token_from_turn(attached)
    with pytest.raises(runtime_control.RuntimeTurnConflictError, match="token is stale"):
        runtime_control.mark_runtime_turn_terminal(
            connection,
            token=replace(attached_token, process_birth_time=200_001),
            status="closed",
            exit_code=0,
        )
    assert runtime_control.mark_runtime_turn_terminal(
        connection,
        token=attached_token,
        status="closed",
        exit_code=0,
        terminal_at=NOW + timedelta(seconds=1),
    ).status == "closed"


def test_unbound_terminal_requires_exact_null_token_and_replays_exactly() -> None:
    connection = _connection()
    reserved = runtime_control.start_runtime_turn(
        connection,
        turn_id="spawn-failed",
        actor_id="application:attempt-1",
        attempt_id="attempt-1",
        runtime_id="runtime-1",
        profile_id="profile-1",
        runtime_backend="codex-cli",
        resume_mode="root",
        submit_started=False,
        tool_surface_hash="tools-v1",
        prompt_contract_hash="prompt-v1",
        started_at=NOW,
    )
    with pytest.raises(ValueError, match="exact NULL-process token"):
        runtime_control.mark_runtime_turn_terminal(
            connection,
            turn_id=reserved.turn_id,
            status="failed",
            failure_code="SPAWN_FAILED",
        )

    token = runtime_control.token_from_turn(reserved)
    terminal = runtime_control.mark_runtime_turn_terminal(
        connection,
        token=token,
        status="failed",
        failure_code="SPAWN_FAILED",
        terminal_at=NOW + timedelta(seconds=1),
    )
    assert terminal.status == "failed"
    assert runtime_control.mark_runtime_turn_terminal(
        connection,
        token=token,
        status="failed",
        failure_code="SPAWN_FAILED",
        terminal_at=NOW + timedelta(seconds=2),
    ) == terminal


def test_runtime_attach_and_terminal_respect_nested_savepoint_rollback() -> None:
    connection = _connection()
    reserved = runtime_control.start_runtime_turn(
        connection,
        turn_id="nested",
        actor_id="application:attempt-1",
        attempt_id="attempt-1",
        runtime_id="runtime-1",
        profile_id="profile-1",
        runtime_backend="codex-cli",
        resume_mode="root",
        submit_started=False,
        tool_surface_hash="tools-v1",
        prompt_contract_hash="prompt-v1",
        started_at=NOW,
    )
    reservation_token = runtime_control.token_from_turn(reserved)

    connection.execute("BEGIN")
    runtime_control.attach_runtime_turn_process(
        connection,
        token=reservation_token,
        process_id=4321,
        process_birth_time=200_000,
    )
    connection.rollback()
    assert runtime_control.get_runtime_turn(connection, reserved.turn_id) == reserved

    attached = runtime_control.attach_runtime_turn_process(
        connection,
        token=reservation_token,
        process_id=4321,
        process_birth_time=200_000,
    )
    attached_token = runtime_control.token_from_turn(attached)
    connection.execute("BEGIN")
    runtime_control.mark_runtime_turn_terminal(
        connection,
        token=attached_token,
        status="closed",
        exit_code=0,
        terminal_at=NOW + timedelta(seconds=1),
    )
    connection.rollback()
    assert runtime_control.get_runtime_turn(connection, reserved.turn_id) == attached


def test_two_connections_only_one_process_can_attach_to_reservation(tmp_path) -> None:
    path = tmp_path / "runtime-attach-contention.db"
    setup = _connection(path)
    reserved = runtime_control.start_runtime_turn(
        setup,
        turn_id="contended",
        actor_id="application:attempt-1",
        attempt_id="attempt-1",
        runtime_id="runtime-1",
        profile_id="profile-1",
        runtime_backend="codex-cli",
        resume_mode="root",
        submit_started=False,
        tool_surface_hash="tools-v1",
        prompt_contract_hash="prompt-v1",
        started_at=NOW,
    )
    token = runtime_control.token_from_turn(reserved)
    barrier = Barrier(2)

    def attach(process: tuple[int, int]) -> str:
        connection = _connection(path)
        try:
            barrier.wait()
            try:
                runtime_control.attach_runtime_turn_process(
                    connection,
                    token=token,
                    process_id=process[0],
                    process_birth_time=process[1],
                )
                return "attached"
            except runtime_control.RuntimeTurnConflictError:
                return "conflict"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attach, ((4321, 200_000), (4321, 200_001))))

    assert sorted(outcomes) == ["attached", "conflict"]
    persisted = runtime_control.get_runtime_turn(setup, reserved.turn_id)
    assert persisted is not None
    assert (persisted.process_id, persisted.process_birth_time) in {
        (4321, 200_000),
        (4321, 200_001),
    }


def test_require_new_rejects_exact_old_unbound_reservation() -> None:
    connection = _connection()
    arguments = {
        "turn_id": "old-reservation",
        "actor_id": "application:attempt-1",
        "attempt_id": "attempt-1",
        "runtime_id": "runtime-1",
        "profile_id": "profile-1",
        "runtime_backend": "codex-cli",
        "resume_mode": "root",
        "submit_started": False,
        "tool_surface_hash": "tools-v1",
        "prompt_contract_hash": "prompt-v1",
        "idempotency_key": "old-reservation-key",
        "started_at": NOW,
    }
    reserved = runtime_control.start_runtime_turn(connection, **arguments)
    assert reserved.process_id is None
    assert runtime_control.start_runtime_turn(connection, **arguments) == reserved

    with pytest.raises(
        runtime_control.RuntimeTurnConflictError,
        match="reservation already exists",
    ):
        runtime_control.start_runtime_turn(
            connection,
            **arguments,
            require_new=True,
        )


def test_two_connections_only_one_gets_new_runtime_reservation(tmp_path) -> None:
    path = tmp_path / "runtime-reservation-contention.db"
    setup = _connection(path)
    runtime_control.ensure_schema(setup)
    setup.close()
    barrier = Barrier(2)
    arguments = {
        "turn_id": "one-new-reservation",
        "actor_id": "application:attempt-1",
        "attempt_id": "attempt-1",
        "runtime_id": "runtime-1",
        "profile_id": "profile-1",
        "runtime_backend": "codex-cli",
        "resume_mode": "root",
        "submit_started": False,
        "tool_surface_hash": "tools-v1",
        "prompt_contract_hash": "prompt-v1",
        "idempotency_key": "one-new-reservation-key",
        "started_at": NOW,
        "require_new": True,
    }

    def reserve(_: int) -> str:
        connection = _connection(path)
        try:
            barrier.wait()
            try:
                runtime_control.start_runtime_turn(connection, **arguments)
                return "reserved"
            except runtime_control.RuntimeTurnConflictError:
                return "conflict"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve, (1, 2)))

    assert sorted(outcomes) == ["conflict", "reserved"]
    inspect = _connection(path)
    persisted = runtime_control.running_runtime_turn_for_actor(
        inspect, "application:attempt-1"
    )
    assert persisted is not None
    assert persisted.turn_id == "one-new-reservation"
