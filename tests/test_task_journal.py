from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest

from applypilot.apply.contracts import TaskResult, TaskSpec
from applypilot.storage import task_journal


def _connection() -> sqlite3.Connection:
    return sqlite3.connect(":memory:")


def test_register_is_idempotent_and_rejects_digest_drift() -> None:
    conn = _connection()
    spec = TaskSpec("task-1", "audit", "Read facts", idempotency_key="same")
    assert task_journal.register(conn, spec).status == "pending"
    assert task_journal.register(conn, spec).task_id == "task-1"
    with pytest.raises(ValueError, match="different task spec"):
        task_journal.register(
            conn,
            TaskSpec("task-1", "audit", "Changed objective", idempotency_key="same"),
        )


def test_new_optional_task_controls_preserve_legacy_canonical_digest() -> None:
    spec = TaskSpec("legacy", "audit", "Read facts", idempotency_key="same")
    legacy = {
        "task_id": "legacy",
        "kind": "audit",
        "objective": "Read facts",
        "inputs": {},
        "depends_on": [],
        "required_results": [],
        "effect_class": "read",
        "resource_claims": [],
        "retry_budget": 0,
        "deadline_at": None,
        "idempotency_key": "same",
        "priority": 0,
        "counts_toward_target": False,
        "resume_cursor": {},
    }
    encoded = json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()

    assert task_journal.spec_digest(spec) == hashlib.sha256(encoded).hexdigest()


def test_pending_legacy_task_does_not_accept_new_control_scope() -> None:
    connection = _connection()
    task_journal.register(connection, TaskSpec("pending", "audit", "Read facts"))

    with pytest.raises(ValueError, match="different task spec"):
        task_journal.register(
            connection,
            TaskSpec(
                "pending",
                "audit",
                "Read facts",
                authority_scope=("read:bounded_snapshot",),
            ),
        )


def test_claim_complete_and_terminal_replay() -> None:
    conn = _connection()
    spec = TaskSpec("task-1", "audit", "Read facts", idempotency_key="same")
    task_journal.register(conn, spec)
    claimed = task_journal.claim(conn, "task-1", "worker-1", lease_seconds=30)
    assert claimed is not None and claimed.status == "running"
    assert task_journal.claim(conn, "task-1", "worker-2") is None
    result = TaskResult(task_id="task-1", status="completed", output={"ready": True})
    completed = task_journal.complete(conn, "task-1", "worker-1", result)
    assert completed.status == "completed"
    assert completed.result["output"] == {"ready": True}
    assert task_journal.register(conn, spec).result == completed.result


def test_only_expired_read_claim_is_recoverable() -> None:
    current = datetime(2026, 8, 29, tzinfo=UTC)
    conn = _connection()
    read = TaskSpec("read", "audit", "Read", effect_class="read")
    write = TaskSpec("write", "submit", "Write", effect_class="submit")
    for spec in (read, write):
        task_journal.register(conn, spec)
        assert task_journal.claim(conn, spec.task_id, "old", lease_seconds=1, now=current)
    later = current + timedelta(seconds=2)
    assert task_journal.claim(conn, "read", "new", now=later).owner_id == "new"
    assert task_journal.claim(conn, "write", "new", now=later) is None


def test_database_schema_path_includes_task_journal(monkeypatch) -> None:
    from applypilot import database

    conn = _connection()
    database.ensure_application_batch_schema(conn)
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "agent_tasks" in names


def test_claim_is_committed_and_atomic_across_connections(tmp_path) -> None:
    path = tmp_path / "journal.db"
    first = sqlite3.connect(path, timeout=5)
    task_journal.register(first, TaskSpec("task-1", "audit", "Read", effect_class="read"))
    first.close()
    outcomes: list[str | None] = []

    def claim(owner: str) -> None:
        connection = sqlite3.connect(path, timeout=5)
        entry = task_journal.claim(connection, "task-1", owner, lease_seconds=30)
        outcomes.append(None if entry is None else entry.owner_id)
        connection.close()

    threads = [threading.Thread(target=claim, args=(owner,)) for owner in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(value for value in outcomes if value is not None) in (["a"], ["b"])
    assert outcomes.count(None) == 1
    check = sqlite3.connect(path)
    assert task_journal.load(check, "task-1").status == "running"


def test_load_spec_returns_durable_system_inputs(tmp_path) -> None:
    connection = sqlite3.connect(tmp_path / "journal.db")
    spec = TaskSpec(
        "task-snapshot",
        "ats-fill-plan-v1",
        "Plan",
        inputs={"snapshot_catalog": {"ref": {"schema_version": "v1"}}},
        effect_class="read",
    )
    task_journal.register(connection, spec)

    loaded = task_journal.load_spec(connection, "task-snapshot")

    assert loaded["inputs"] == spec.inputs


def test_additive_migration_is_idempotent_for_legacy_database() -> None:
    connection = _connection()
    connection.execute("""
        CREATE TABLE agent_tasks (
            task_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
            spec_digest TEXT NOT NULL, spec_json TEXT NOT NULL,
            effect_class TEXT NOT NULL, status TEXT NOT NULL, owner_id TEXT,
            lease_expires_at TEXT, result_json TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
    """)

    task_journal.ensure_schema(connection)
    task_journal.ensure_schema(connection)

    columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_tasks)")}
    assert {
        "heartbeat_at",
        "progress_json",
        "cancel_requested",
        "retry_at",
        "worker_id",
        "result_ref",
        "dead_letter_reason",
        "lease_epoch",
    } <= columns


def test_lease_epoch_fences_old_owner_after_reap_and_reclaim() -> None:
    connection = _connection()
    current = datetime(2026, 9, 4, tzinfo=UTC)
    task_journal.register(connection, TaskSpec("read", "audit", "Read"))
    first = task_journal.claim(connection, "read", "same-worker", lease_seconds=1, now=current)
    assert first is not None and first.lease_token is not None
    assert task_journal.reap_expired(
        connection, now=current + timedelta(seconds=2)
    ) == (1, 0)
    second = task_journal.claim(
        connection, "read", "same-worker", lease_seconds=30, now=current + timedelta(seconds=2)
    )
    assert second is not None and second.lease_token is not None
    assert second.lease_epoch == first.lease_epoch + 1

    with pytest.raises(RuntimeError, match="stale"):
        task_journal.heartbeat(connection, first.lease_token, progress={"stage": "late"})
    with pytest.raises(RuntimeError, match="stale"):
        task_journal.complete(
            connection,
            "read",
            first.lease_token,
            TaskResult(task_id="read", status="completed"),
        )
    with pytest.raises(RuntimeError, match="stale"):
        task_journal.fail(
            connection,
            "read",
            first.lease_token,
            TaskResult(task_id="read", status="failed"),
        )
    with pytest.raises(RuntimeError, match="stale"):
        task_journal.acknowledge_cancel(connection, first.lease_token)
    result = TaskResult(task_id="read", status="completed")
    assert task_journal.complete(connection, "read", second.lease_token, result).status == "completed"
    assert task_journal.complete(connection, "read", second.lease_token, result).status == "completed"
    assert [
        event["event_type"] for event in task_journal.list_events(connection, "read")
    ].count("result") == 1
    with pytest.raises(RuntimeError, match="stale"):
        task_journal.complete(connection, "read", first.lease_token, result)


def test_owner_only_compatibility_cannot_mutate_a_reclaimed_task() -> None:
    connection = _connection()
    current = datetime(2026, 9, 4, tzinfo=UTC)
    task_journal.register(connection, TaskSpec("read", "audit", "Read"))
    assert task_journal.claim(connection, "read", "same", lease_seconds=1, now=current)
    task_journal.reap_expired(connection, now=current + timedelta(seconds=2))
    second = task_journal.claim(
        connection, "read", "same", lease_seconds=30, now=current + timedelta(seconds=2)
    )
    assert second is not None

    with pytest.raises(RuntimeError, match="first claim"):
        task_journal.complete(
            connection,
            "read",
            "same",
            TaskResult(task_id="read", status="completed"),
        )
    with pytest.raises(RuntimeError, match="stale"):
        task_journal.complete(
            connection,
            "read",
            "same",
            TaskResult(task_id="read", status="completed"),
            lease_epoch=1,
        )


def test_heartbeat_cancel_and_result_events_share_durable_state() -> None:
    connection = _connection()
    task_journal.register(connection, TaskSpec("read", "audit", "Read"))
    claimed = task_journal.claim(connection, "read", "worker")
    assert claimed is not None and claimed.lease_token is not None
    token = claimed.lease_token

    updated = task_journal.heartbeat(connection, token, progress={"stage": "half"})
    assert updated.progress == {"stage": "half"}
    assert updated.heartbeat_at is not None
    assert task_journal.request_cancel(connection, "read").cancel_requested is True
    assert task_journal.cancellation_requested(connection, token) is True
    assert task_journal.acknowledge_cancel(connection, token).status == "cancelled"

    assert [event["event_type"] for event in task_journal.list_events(connection, "read")] == [
        "claimed",
        "progress",
        "cancel_requested",
        "result",
    ]


def test_progress_rejects_unbounded_or_payload_shaped_content() -> None:
    connection = _connection()
    task_journal.register(connection, TaskSpec("read", "audit", "Read"))
    claimed = task_journal.claim(connection, "read", "worker")
    assert claimed is not None and claimed.lease_token is not None

    with pytest.raises(ValueError, match="non-sensitive"):
        task_journal.heartbeat(
            connection,
            claimed.lease_token,
            progress={"candidate_email": "private@example.test"},
        )


def test_reaper_never_replays_expired_effectful_work() -> None:
    connection = _connection()
    current = datetime(2026, 9, 4, tzinfo=UTC)
    task_journal.register(
        connection, TaskSpec("submit", "submit", "Submit", effect_class="submit")
    )
    assert task_journal.claim(connection, "submit", "worker", lease_seconds=1, now=current)

    assert task_journal.reap_expired(
        connection, now=current + timedelta(seconds=2)
    ) == (0, 1)
    entry = task_journal.load(connection, "submit")
    assert entry is not None and entry.status == "dead_letter"
    assert entry.dead_letter_reason == "expired_non_read_lease_requires_manual_review"


def test_cancel_request_blocks_complete_fail_and_retry_until_ack() -> None:
    connection = _connection()
    task_journal.register(connection, TaskSpec("read", "audit", "Read"))
    claimed = task_journal.claim(connection, "read", "worker")
    assert claimed is not None and claimed.lease_token is not None
    token = claimed.lease_token
    task_journal.request_cancel(connection, "read")

    with pytest.raises(RuntimeError, match="stale"):
        task_journal.complete(
            connection,
            "read",
            token,
            TaskResult(task_id="read", status="completed"),
        )
    with pytest.raises(RuntimeError, match="stale"):
        task_journal.fail(
            connection,
            "read",
            token,
            TaskResult(task_id="read", status="failed"),
        )
    with pytest.raises(RuntimeError, match="stale|replayable"):
        task_journal.schedule_retry(
            connection,
            token,
            retry_at=datetime.now(UTC) + timedelta(seconds=1),
            failure_category="transient",
        )
    assert task_journal.acknowledge_cancel(connection, token).status == "cancelled"


def test_claim_next_skips_expired_non_read_so_pending_read_is_not_starved() -> None:
    connection = _connection()
    current = datetime(2026, 9, 4, tzinfo=UTC)
    task_journal.register(
        connection, TaskSpec("unsafe", "submit", "Submit", effect_class="submit")
    )
    task_journal.register(connection, TaskSpec("read", "audit", "Read"))
    assert task_journal.claim(connection, "unsafe", "old", lease_seconds=1, now=current)

    claimed = task_journal.claim_next(
        connection, "reader", lease_seconds=30, now=current + timedelta(seconds=2)
    )
    assert claimed is not None and claimed.task_id == "read"


def test_retry_beyond_durable_deadline_becomes_timed_out() -> None:
    connection = _connection()
    current = datetime(2026, 9, 4, tzinfo=UTC)
    task_journal.register(
        connection,
        TaskSpec(
            "read",
            "audit",
            "Read",
            deadline_at=current + timedelta(seconds=4),
        ),
    )
    claimed = task_journal.claim(connection, "read", "worker", now=current)
    assert claimed is not None and claimed.lease_token is not None

    result = task_journal.schedule_retry(
        connection,
        claimed.lease_token,
        retry_at=current + timedelta(seconds=5),
        failure_category="transient",
        now=current,
    )
    assert result.status == "timed_out"
    assert result.result["failure_category"] == "retry_exceeds_deadline"
