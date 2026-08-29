from __future__ import annotations

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
