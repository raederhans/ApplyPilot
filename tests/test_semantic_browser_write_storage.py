from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from applypilot.storage.semantic_browser_writes import (
    OPERATION_KIND,
    SemanticWriteClaims,
    SemanticWriteCollision,
    SemanticWriteTransitionError,
    begin_operation,
    claim_dispatch,
    ensure_schema,
    get_operation,
    list_attempt_operations,
    mark_effect_observed,
    mark_failed_no_effect,
    mark_verified,
    park_side_effect_unknown,
    park_stale_after_effect,
)


def _claims(**overrides: object) -> SemanticWriteClaims:
    values: dict[str, object] = {
        "operation_id": "semantic-op-1",
        "operation_digest": "a" * 64,
        "actor_id": "application:attempt-1",
        "attempt_id": "attempt-1",
        "provider": "workday",
        "operation_kind": OPERATION_KIND,
        "adapter_version": "resume-upload-driver/v1",
        "application_binding_hash": "e" * 64,
        "page_id": "application:attempt-1",
        "page_lease_id": "lease-1",
        "page_lease_epoch": 2,
        "expected_page_epoch": 3,
        "artifact_sha256": "b" * 64,
        "artifact_size": 42,
        "material_binding_hash": "c" * 64,
        "policy_contract_version": "semantic-browser-write/v1",
        "policy_digest": "d" * 64,
        "expected_postcondition_digest": "f" * 64,
    }
    values.update(overrides)
    return SemanticWriteClaims(**values)  # type: ignore[arg-type]


def test_schema_is_additive_idempotent_and_caller_transaction_owned() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE caller_state(value TEXT)")
    connection.commit()
    ensure_schema(connection)
    ensure_schema(connection)

    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"caller_state", "semantic_browser_writes"} <= tables

    connection.execute("BEGIN")
    connection.execute("INSERT INTO caller_state VALUES('owned-by-caller')")
    begin_operation(connection, _claims())
    assert connection.in_transaction is True
    connection.rollback()
    assert connection.execute("SELECT COUNT(*) FROM caller_state").fetchone()[0] == 0
    assert get_operation(connection, "semantic-op-1") is None


def test_migration_failure_rolls_back_inside_caller_transaction() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE caller_state(value TEXT)")
    connection.execute("BEGIN")
    connection.execute("INSERT INTO caller_state VALUES('preserve')")
    connection.execute(
        "CREATE TABLE semantic_browser_write_schema("
        "component TEXT PRIMARY KEY,version INTEGER NOT NULL)"
    )
    connection.execute(
        "INSERT INTO semantic_browser_write_schema VALUES(?,?)",
        ("semantic_browser_writes", 99),
    )

    with pytest.raises(RuntimeError, match="schema version"):
        ensure_schema(connection)

    assert connection.in_transaction is True
    assert connection.execute("SELECT value FROM caller_state").fetchone()[0] == "preserve"
    connection.rollback()


def test_exact_begin_replays_and_identity_collision_is_rejected() -> None:
    connection = sqlite3.connect(":memory:")
    claims = _claims()
    first = begin_operation(connection, claims)
    replay = begin_operation(connection, claims)

    assert replay == first
    with pytest.raises(SemanticWriteCollision):
        begin_operation(
            connection,
            replace(claims, artifact_size=43, operation_digest="e" * 64),
        )
    with pytest.raises(SemanticWriteCollision):
        begin_operation(
            connection,
            replace(claims, operation_id="semantic-op-2"),
        )


def test_two_connections_only_allow_one_initial_dispatch(tmp_path) -> None:
    path = tmp_path / "semantic-write-race.db"
    setup = sqlite3.connect(path)
    begin_operation(setup, _claims())
    setup.close()
    barrier = threading.Barrier(2)

    def compete() -> bool:
        connection = sqlite3.connect(path, timeout=2)
        barrier.wait()
        claimed = claim_dispatch(
            connection,
            "semantic-op-1",
            expected_dispatch_count=0,
        )
        connection.close()
        return claimed is not None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: compete(), range(2)))

    assert sorted(outcomes) == [False, True]


def test_started_crash_cannot_replay_without_observed_no_effect() -> None:
    connection = sqlite3.connect(":memory:")
    begin_operation(connection, _claims())
    assert claim_dispatch(
        connection, "semantic-op-1", expected_dispatch_count=0
    ) is not None

    replay = claim_dispatch(
        connection,
        "semantic-op-1",
        expected_dispatch_count=1,
        allow_replay=True,
    )

    assert replay is None
    persisted = get_operation(connection, "semantic-op-1")
    assert persisted is not None
    assert persisted.state == "started"
    assert persisted.dispatch_count == 1


def test_failed_no_effect_can_be_replayed_once_but_effect_cannot() -> None:
    connection = sqlite3.connect(":memory:")
    begin_operation(connection, _claims())
    claim_dispatch(connection, "semantic-op-1", expected_dispatch_count=0)
    failed = mark_failed_no_effect(
        connection,
        "semantic-op-1",
        reason_code="driver_rejected_before_effect",
    )
    assert failed.effect_observed is False
    assert claim_dispatch(
        connection,
        "semantic-op-1",
        expected_dispatch_count=1,
        allow_replay=True,
    ) is not None
    effect = mark_effect_observed(connection, "semantic-op-1")
    assert effect.effect_observed is True
    assert claim_dispatch(
        connection,
        "semantic-op-1",
        expected_dispatch_count=1,
        allow_replay=True,
    ) is None


def test_effect_to_verified_requires_exact_next_page_epoch() -> None:
    connection = sqlite3.connect(":memory:")
    begin_operation(connection, _claims())
    claim_dispatch(connection, "semantic-op-1", expected_dispatch_count=0)
    mark_effect_observed(connection, "semantic-op-1")

    with pytest.raises(ValueError, match="plus one"):
        mark_verified(connection, "semantic-op-1", resulting_page_epoch=7)
    verified = mark_verified(connection, "semantic-op-1", resulting_page_epoch=4)
    assert verified.state == "verified"
    assert verified.resulting_page_epoch == 4
    assert mark_verified(
        connection, "semantic-op-1", resulting_page_epoch=4
    ) == verified
    with pytest.raises(SemanticWriteTransitionError):
        mark_effect_observed(connection, "semantic-op-1")


@pytest.mark.parametrize(
    ("park", "expected_state"),
    [
        (park_side_effect_unknown, "parked_side_effect_unknown"),
        (park_stale_after_effect, "parked_stale_after_effect"),
    ],
)
def test_parked_states_are_terminal_and_cannot_dispatch(
    park,
    expected_state: str,
) -> None:
    connection = sqlite3.connect(":memory:")
    begin_operation(connection, _claims())
    claim_dispatch(connection, "semantic-op-1", expected_dispatch_count=0)
    if park is park_stale_after_effect:
        mark_effect_observed(connection, "semantic-op-1")
    parked = park(
        connection,
        "semantic-op-1",
        reason_code="page_identity_unprovable",
    )

    assert parked.state == expected_state
    assert claim_dispatch(
        connection,
        "semantic-op-1",
        expected_dispatch_count=1,
        allow_replay=True,
    ) is None


def test_reason_is_machine_bounded_and_schema_has_no_sensitive_columns() -> None:
    connection = sqlite3.connect(":memory:")
    begin_operation(connection, _claims())
    claim_dispatch(connection, "semantic-op-1", expected_dispatch_count=0)

    for unsafe in (
        "C:/private/resume.pdf",
        "candidate@example.test",
        "x" * 121,
        "contains spaces",
    ):
        with pytest.raises(ValueError, match="reason_code"):
            mark_failed_no_effect(
                connection,
                "semantic-op-1",
                reason_code=unsafe,
            )

    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(semantic_browser_writes)")
    }
    assert not columns & {
        "path",
        "artifact_path",
        "raw_value",
        "dom",
        "dom_text",
        "nonce",
        "token",
        "authority",
    }
    assert list_attempt_operations(connection, "attempt-1")[0].artifact_size == 42
