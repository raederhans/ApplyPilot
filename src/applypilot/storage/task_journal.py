"""Durable execution journal for bounded orchestration tasks.

The journal is advisory: it never mutates the application ledger or job state.
Only read-only leases may be recovered after expiry; effectful work requires a
new explicit task so a crashed submit can never be replayed automatically.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from applypilot.apply.contracts import TaskResult, TaskSpec, contract_json, ensure_persistable

_TERMINAL = {"completed", "failed", "blocked", "cancelled", "timed_out"}
_P2_OPTIONAL_SPEC_FIELDS = {
    "authority_scope",
    "retry_categories",
    "deadline_at",
    "cancellation_mode",
    "partial_allowed",
    "conflict_keys",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("journal timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _json(value: object) -> str:
    return json.dumps(
        ensure_persistable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def spec_digest(spec: TaskSpec) -> str:
    encoded = _json(contract_json(spec)).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class JournalEntry:
    task_id: str
    idempotency_key: str
    spec_digest: str
    effect_class: str
    status: str
    owner_id: str | None
    lease_expires_at: str | None
    result: dict[str, object] | None
    attempt_count: int
    attempt_id: str | None
    workflow_id: str | None
    proposal_id: str | None


def ensure_schema(connection: sqlite3.Connection) -> None:
    was_in_transaction = connection.in_transaction
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_tasks (
            task_id          TEXT PRIMARY KEY,
            idempotency_key  TEXT NOT NULL UNIQUE,
            attempt_id       TEXT,
            workflow_id      TEXT,
            proposal_id      TEXT,
            spec_digest      TEXT NOT NULL,
            spec_json        TEXT NOT NULL,
            effect_class     TEXT NOT NULL,
            status           TEXT NOT NULL,
            owner_id         TEXT,
            lease_expires_at TEXT,
            result_json      TEXT,
            attempt_count    INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_tasks_claimable
        ON agent_tasks(status, lease_expires_at, effect_class)
    """)
    existing_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(agent_tasks)")
    }
    for name in ("attempt_id", "workflow_id", "proposal_id"):
        if name not in existing_columns:
            connection.execute(f"ALTER TABLE agent_tasks ADD COLUMN {name} TEXT")
    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_tasks_workflow
        ON agent_tasks(workflow_id, attempt_id, proposal_id)
    """)
    if not was_in_transaction:
        connection.commit()


def _entry(row: sqlite3.Row | tuple[object, ...] | None) -> JournalEntry | None:
    if row is None:
        return None
    values = tuple(row)
    return JournalEntry(
        task_id=str(values[0]),
        idempotency_key=str(values[1]),
        spec_digest=str(values[2]),
        effect_class=str(values[3]),
        status=str(values[4]),
        owner_id=None if values[5] is None else str(values[5]),
        lease_expires_at=None if values[6] is None else str(values[6]),
        result=None if values[7] is None else json.loads(str(values[7])),
        attempt_count=int(values[8]),
        attempt_id=None if values[9] is None else str(values[9]),
        workflow_id=None if values[10] is None else str(values[10]),
        proposal_id=None if values[11] is None else str(values[11]),
    )


_SELECT = (
    "SELECT task_id,idempotency_key,spec_digest,effect_class,status,owner_id,"
    "lease_expires_at,result_json,attempt_count,attempt_id,workflow_id,proposal_id "
    "FROM agent_tasks"
)


@contextmanager
def _write_transaction(connection: sqlite3.Connection):
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        if owns_transaction:
            connection.commit()
    except Exception:
        if owns_transaction and connection.in_transaction:
            connection.rollback()
        raise


def load(connection: sqlite3.Connection, task_id: str) -> JournalEntry | None:
    ensure_schema(connection)
    return _entry(connection.execute(f"{_SELECT} WHERE task_id=?", (task_id,)).fetchone())


def load_spec(connection: sqlite3.Connection, task_id: str) -> dict[str, object]:
    """Load the immutable durable spec used to recover read-only work."""
    ensure_schema(connection)
    row = connection.execute(
        "SELECT spec_json FROM agent_tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"task does not exist: {task_id}")
    value = json.loads(str(row[0]))
    if not isinstance(value, dict):  # pragma: no cover - register always stores an object
        raise TypeError("durable task spec is not an object")
    return value


def register(
    connection: sqlite3.Connection,
    spec: TaskSpec,
    *,
    attempt_id: str | None = None,
    workflow_id: str | None = None,
    proposal_id: str | None = None,
) -> JournalEntry:
    """Register exactly one spec, returning its terminal replay when present."""
    ensure_schema(connection)
    key = spec.idempotency_key or spec.task_id
    digest = spec_digest(spec)
    now = _iso(_now())
    with _write_transaction(connection):
        try:
            connection.execute(
                "INSERT INTO agent_tasks(task_id,idempotency_key,attempt_id,workflow_id,"
                "proposal_id,spec_digest,spec_json,effect_class,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,'pending',?,?)",
                (
                    spec.task_id,
                    key,
                    attempt_id,
                    workflow_id,
                    proposal_id,
                    digest,
                    _json(contract_json(spec)),
                    spec.effect_class,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            existing = _entry(connection.execute(
                f"{_SELECT} WHERE task_id=? OR idempotency_key=?",
                (spec.task_id, key),
            ).fetchone())
            legacy_completed_read_match = False
            if (
                existing is not None
                and existing.task_id == spec.task_id
                and existing.idempotency_key == key
                and existing.status == "completed"
                and existing.effect_class == "read"
                and spec.effect_class == "read"
            ):
                row = connection.execute(
                    "SELECT spec_json FROM agent_tasks WHERE task_id=?",
                    (existing.task_id,),
                ).fetchone()
                legacy = json.loads(str(row[0])) if row is not None else None
                current = contract_json(spec)
                if isinstance(legacy, dict):
                    for field in _P2_OPTIONAL_SPEC_FIELDS:
                        if field not in legacy:
                            current.pop(field, None)
                    legacy_completed_read_match = legacy == current
            if (
                existing is None
                or existing.task_id != spec.task_id
                or (
                    existing.spec_digest != digest
                    and not legacy_completed_read_match
                )
            ):
                raise ValueError(
                    "task idempotency key was reused for a different task spec"
                ) from None
            return existing
        registered = _entry(connection.execute(
            f"{_SELECT} WHERE task_id=?", (spec.task_id,)
        ).fetchone())
    assert registered is not None
    return registered


def claim(
    connection: sqlite3.Connection,
    task_id: str,
    owner_id: str,
    *,
    lease_seconds: int = 300,
    now: datetime | None = None,
) -> JournalEntry | None:
    """Atomically claim pending work or an expired read-only lease."""
    if not owner_id.strip() or lease_seconds < 1:
        raise ValueError("owner_id and a positive lease are required")
    ensure_schema(connection)
    current = now or _now()
    current_text = _iso(current)
    expires = _iso(current + timedelta(seconds=lease_seconds))
    with _write_transaction(connection):
        cursor = connection.execute(
            "UPDATE agent_tasks SET status='running',owner_id=?,lease_expires_at=?,"
            "attempt_count=attempt_count+1,updated_at=? WHERE task_id=? AND "
            "(status='pending' OR (status='running' AND effect_class='read' "
            "AND lease_expires_at IS NOT NULL AND lease_expires_at<=?))",
            (owner_id, expires, current_text, task_id, current_text),
        )
        if cursor.rowcount != 1:
            return None
        claimed = _entry(connection.execute(
            f"{_SELECT} WHERE task_id=?", (task_id,)
        ).fetchone())
    return claimed


def _finish(
    connection: sqlite3.Connection,
    task_id: str,
    owner_id: str,
    result: TaskResult,
) -> JournalEntry:
    if result.task_id != task_id or result.status.casefold() not in _TERMINAL:
        raise ValueError("a matching terminal TaskResult is required")
    encoded = _json(contract_json(result))
    with _write_transaction(connection):
        cursor = connection.execute(
            "UPDATE agent_tasks SET status=?,result_json=?,owner_id=NULL,lease_expires_at=NULL,"
            "updated_at=? WHERE task_id=? AND status='running' AND owner_id=?",
            (result.status.casefold(), encoded, _iso(result.completed_at), task_id, owner_id),
        )
        if cursor.rowcount != 1:
            existing = _entry(connection.execute(
                f"{_SELECT} WHERE task_id=?", (task_id,)
            ).fetchone())
            if (
                existing is not None
                and existing.status in _TERMINAL
                and existing.result == json.loads(encoded)
            ):
                return existing
            raise RuntimeError("task is not claimed by this owner")
        finished = _entry(connection.execute(
            f"{_SELECT} WHERE task_id=?", (task_id,)
        ).fetchone())
    assert finished is not None
    return finished


def complete(connection: sqlite3.Connection, task_id: str, owner_id: str, result: TaskResult) -> JournalEntry:
    if not result.succeeded:
        raise ValueError("complete requires a successful TaskResult")
    return _finish(connection, task_id, owner_id, result)


def fail(connection: sqlite3.Connection, task_id: str, owner_id: str, result: TaskResult) -> JournalEntry:
    if result.succeeded:
        raise ValueError("fail requires a non-successful TaskResult")
    return _finish(connection, task_id, owner_id, result)
