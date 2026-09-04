"""Durable journal for bounded read-only background tasks.

The journal never owns browser, application-ledger, or submit authority.
Expired reads may be recovered; every other effect class is fail-closed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from applypilot.apply.contracts import TaskResult, TaskSpec, contract_json, ensure_persistable

_TERMINAL = {"completed", "failed", "blocked", "cancelled", "timed_out", "dead_letter"}
_MAX_STATE_BYTES = 16 * 1024
_PROGRESS_KEYS = {"stage", "current", "total", "completed", "percent", "checkpoint_ref", "kind"}
_P2_OPTIONAL_SPEC_FIELDS = {
    "authority_scope", "retry_categories", "deadline_at", "cancellation_mode",
    "partial_allowed", "conflict_keys",
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


def _bounded_json(value: object) -> str:
    encoded = _json(value)
    if len(encoded.encode("utf-8")) > _MAX_STATE_BYTES:
        raise ValueError("journal state exceeds the bounded size limit")
    return encoded


def spec_digest(spec: TaskSpec) -> str:
    return hashlib.sha256(_json(contract_json(spec)).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class LeaseToken:
    """Immutable fencing token for one exact claim."""

    task_id: str
    owner_id: str
    lease_epoch: int

    def __post_init__(self) -> None:
        if not self.task_id.strip() or not self.owner_id.strip() or self.lease_epoch < 1:
            raise ValueError("a lease token requires task, owner, and positive epoch")


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
    heartbeat_at: str | None
    progress: dict[str, object] | None
    cancel_requested: bool
    retry_at: str | None
    worker_id: str | None
    result_ref: str | None
    dead_letter_reason: str | None
    lease_epoch: int

    @property
    def lease_token(self) -> LeaseToken | None:
        if self.status != "running" or self.owner_id is None:
            return None
        return LeaseToken(self.task_id, self.owner_id, self.lease_epoch)


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Create or additively migrate the task journal."""
    was_in_transaction = connection.in_transaction
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_tasks (
            task_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
            attempt_id TEXT, workflow_id TEXT, proposal_id TEXT,
            spec_digest TEXT NOT NULL, spec_json TEXT NOT NULL,
            effect_class TEXT NOT NULL, status TEXT NOT NULL, owner_id TEXT,
            lease_expires_at TEXT, result_json TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            heartbeat_at TEXT, progress_json TEXT,
            cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
            retry_at TEXT, worker_id TEXT, result_ref TEXT, dead_letter_reason TEXT,
            lease_epoch INTEGER NOT NULL DEFAULT 0 CHECK(lease_epoch >= 0)
        )
    """)
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(agent_tasks)")}
    migrations = {
        "attempt_id": "TEXT", "workflow_id": "TEXT", "proposal_id": "TEXT",
        "heartbeat_at": "TEXT", "progress_json": "TEXT",
        "cancel_requested": "INTEGER NOT NULL DEFAULT 0", "retry_at": "TEXT",
        "worker_id": "TEXT", "result_ref": "TEXT", "dead_letter_reason": "TEXT",
        "lease_epoch": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, declaration in migrations.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE agent_tasks ADD COLUMN {name} {declaration}")
    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_tasks_claimable
        ON agent_tasks(status, retry_at, lease_expires_at, effect_class)
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_tasks_workflow
        ON agent_tasks(workflow_id, attempt_id, proposal_id)
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS task_runtime_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
            lease_epoch INTEGER NOT NULL, event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL, occurred_at TEXT NOT NULL,
            UNIQUE(task_id, lease_epoch, event_type, payload_json)
        )
    """)
    if not was_in_transaction:
        connection.commit()


_SELECT = (
    "SELECT task_id,idempotency_key,spec_digest,effect_class,status,owner_id,"
    "lease_expires_at,result_json,attempt_count,attempt_id,workflow_id,proposal_id,"
    "heartbeat_at,progress_json,cancel_requested,retry_at,worker_id,result_ref,"
    "dead_letter_reason,lease_epoch FROM agent_tasks"
)


def _entry(row: sqlite3.Row | tuple[object, ...] | None) -> JournalEntry | None:
    if row is None:
        return None
    value = tuple(row)
    return JournalEntry(
        task_id=str(value[0]), idempotency_key=str(value[1]), spec_digest=str(value[2]),
        effect_class=str(value[3]), status=str(value[4]),
        owner_id=None if value[5] is None else str(value[5]),
        lease_expires_at=None if value[6] is None else str(value[6]),
        result=None if value[7] is None else json.loads(str(value[7])),
        attempt_count=int(value[8]), attempt_id=None if value[9] is None else str(value[9]),
        workflow_id=None if value[10] is None else str(value[10]),
        proposal_id=None if value[11] is None else str(value[11]),
        heartbeat_at=None if value[12] is None else str(value[12]),
        progress=None if value[13] is None else json.loads(str(value[13])),
        cancel_requested=bool(value[14]), retry_at=None if value[15] is None else str(value[15]),
        worker_id=None if value[16] is None else str(value[16]),
        result_ref=None if value[17] is None else str(value[17]),
        dead_letter_reason=None if value[18] is None else str(value[18]), lease_epoch=int(value[19]),
    )


@contextmanager
def _write_transaction(connection: sqlite3.Connection):
    owns = not connection.in_transaction
    if owns:
        connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        if owns:
            connection.commit()
    except Exception:
        if owns and connection.in_transaction:
            connection.rollback()
        raise


def _event(
    connection: sqlite3.Connection,
    task_id: str,
    lease_epoch: int,
    event_type: str,
    payload: dict[str, object],
    occurred_at: datetime,
) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO task_runtime_events"
        "(task_id,lease_epoch,event_type,payload_json,occurred_at) VALUES(?,?,?,?,?)",
        (task_id, lease_epoch, event_type, _bounded_json(payload), _iso(occurred_at)),
    )


def load(connection: sqlite3.Connection, task_id: str) -> JournalEntry | None:
    ensure_schema(connection)
    return _entry(connection.execute(f"{_SELECT} WHERE task_id=?", (task_id,)).fetchone())


def load_spec(connection: sqlite3.Connection, task_id: str) -> dict[str, object]:
    ensure_schema(connection)
    row = connection.execute("SELECT spec_json FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        raise KeyError(f"task does not exist: {task_id}")
    value = json.loads(str(row[0]))
    if not isinstance(value, dict):
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
                (spec.task_id, key, attempt_id, workflow_id, proposal_id, digest,
                 _json(contract_json(spec)), spec.effect_class, now, now),
            )
        except sqlite3.IntegrityError:
            existing = _entry(connection.execute(
                f"{_SELECT} WHERE task_id=? OR idempotency_key=?", (spec.task_id, key)
            ).fetchone())
            legacy_match = False
            if (existing is not None and existing.task_id == spec.task_id
                    and existing.idempotency_key == key and existing.status == "completed"
                    and existing.effect_class == spec.effect_class == "read"):
                row = connection.execute(
                    "SELECT spec_json FROM agent_tasks WHERE task_id=?", (existing.task_id,)
                ).fetchone()
                legacy = json.loads(str(row[0])) if row is not None else None
                current = contract_json(spec)
                if isinstance(legacy, dict):
                    for field in _P2_OPTIONAL_SPEC_FIELDS:
                        if field not in legacy:
                            current.pop(field, None)
                    legacy_match = legacy == current
            if existing is None or existing.task_id != spec.task_id or (
                existing.spec_digest != digest and not legacy_match
            ):
                raise ValueError("task idempotency key was reused for a different task spec") from None
            return existing
        registered = _entry(connection.execute(f"{_SELECT} WHERE task_id=?", (spec.task_id,)).fetchone())
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
    if not owner_id.strip() or lease_seconds < 1:
        raise ValueError("owner_id and a positive lease are required")
    ensure_schema(connection)
    current = now or _now()
    current_text = _iso(current)
    with _write_transaction(connection):
        cursor = connection.execute(
            "UPDATE agent_tasks SET status='running',owner_id=?,worker_id=?,lease_expires_at=?,"
            "heartbeat_at=?,attempt_count=attempt_count+1,lease_epoch=lease_epoch+1,"
            "retry_at=NULL,updated_at=? WHERE task_id=? AND "
            "((status='pending' AND (retry_at IS NULL OR retry_at<=?)) OR "
            "(status='running' AND effect_class='read' AND lease_expires_at IS NOT NULL "
            "AND lease_expires_at<=?))",
            (owner_id, owner_id, _iso(current + timedelta(seconds=lease_seconds)), current_text,
             current_text, task_id, current_text, current_text),
        )
        if cursor.rowcount != 1:
            return None
        claimed = _entry(connection.execute(f"{_SELECT} WHERE task_id=?", (task_id,)).fetchone())
        assert claimed is not None
        _event(connection, task_id, claimed.lease_epoch, "claimed", {"worker_id": owner_id}, current)
    return claimed


def claim_next(
    connection: sqlite3.Connection,
    owner_id: str,
    *,
    lease_seconds: int = 300,
    now: datetime | None = None,
) -> JournalEntry | None:
    ensure_schema(connection)
    current = now or _now()
    current_text = _iso(current)
    row = connection.execute(
        "SELECT task_id FROM agent_tasks WHERE "
        "((status='pending' AND (retry_at IS NULL OR retry_at<=?)) OR "
        "(status='running' AND effect_class='read' AND lease_expires_at IS NOT NULL "
        "AND lease_expires_at<=?)) "
        "ORDER BY created_at,task_id LIMIT 1", (current_text, current_text)
    ).fetchone()
    return None if row is None else claim(
        connection, str(row[0]), owner_id, lease_seconds=lease_seconds, now=current
    )


def heartbeat(
    connection: sqlite3.Connection,
    token: LeaseToken,
    *,
    progress: dict[str, object] | None = None,
    lease_seconds: int = 300,
    now: datetime | None = None,
) -> JournalEntry:
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")
    ensure_schema(connection)
    current = now or _now()
    if progress is not None and (
        not set(progress) <= _PROGRESS_KEYS
        or any(
            isinstance(value, (dict, list, tuple))
            or (isinstance(value, str) and len(value) > 200)
            for value in progress.values()
        )
    ):
        raise ValueError("progress must contain only bounded non-sensitive control fields")
    encoded = None if progress is None else _bounded_json(progress)
    with _write_transaction(connection):
        cursor = connection.execute(
            "UPDATE agent_tasks SET heartbeat_at=?,progress_json=COALESCE(?,progress_json),"
            "lease_expires_at=?,updated_at=? WHERE task_id=? AND status='running' "
            "AND owner_id=? AND lease_epoch=?",
            (_iso(current), encoded, _iso(current + timedelta(seconds=lease_seconds)), _iso(current),
             token.task_id, token.owner_id, token.lease_epoch),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("task lease token is stale")
        if progress is not None:
            _event(connection, token.task_id, token.lease_epoch, "progress", {"progress": progress}, current)
        updated = _entry(connection.execute(f"{_SELECT} WHERE task_id=?", (token.task_id,)).fetchone())
    assert updated is not None
    return updated


def cancellation_requested(connection: sqlite3.Connection, token: LeaseToken) -> bool:
    ensure_schema(connection)
    row = connection.execute(
        "SELECT cancel_requested FROM agent_tasks WHERE task_id=? AND status='running' "
        "AND owner_id=? AND lease_epoch=?", (token.task_id, token.owner_id, token.lease_epoch)
    ).fetchone()
    if row is None:
        raise RuntimeError("task lease token is stale")
    return bool(row[0])


def request_cancel(
    connection: sqlite3.Connection, task_id: str, *, now: datetime | None = None
) -> JournalEntry:
    ensure_schema(connection)
    current = now or _now()
    with _write_transaction(connection):
        row = connection.execute(
            "SELECT status,lease_epoch FROM agent_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"task does not exist: {task_id}")
        if str(row[0]) == "pending":
            connection.execute(
                "UPDATE agent_tasks SET status='cancelled',cancel_requested=1,updated_at=? "
                "WHERE task_id=? AND status='pending'", (_iso(current), task_id)
            )
        elif str(row[0]) == "running":
            connection.execute(
                "UPDATE agent_tasks SET cancel_requested=1,updated_at=? WHERE task_id=? "
                "AND status='running'", (_iso(current), task_id)
            )
        _event(connection, task_id, int(row[1]), "cancel_requested", {}, current)
        updated = _entry(connection.execute(f"{_SELECT} WHERE task_id=?", (task_id,)).fetchone())
    assert updated is not None
    return updated


def _coerce_token(
    connection: sqlite3.Connection,
    task_id: str,
    owner: str | LeaseToken,
    lease_epoch: int | None,
) -> LeaseToken:
    if isinstance(owner, LeaseToken):
        if owner.task_id != task_id:
            raise ValueError("lease token belongs to another task")
        return owner
    # Compatibility for existing synchronous specialist callers. They have an
    # immediate claim/finish lifecycle; new background workers always retain the token.
    if lease_epoch is None:
        row = connection.execute(
            "SELECT lease_epoch,attempt_count FROM agent_tasks WHERE task_id=? "
            "AND status='running' AND owner_id=?",
            (task_id, owner),
        ).fetchone()
        if row is None or int(row[0]) != 1 or int(row[1]) != 1:
            raise RuntimeError("owner-only mutation is limited to the first claim")
        lease_epoch = int(row[0])
    return LeaseToken(task_id, owner, lease_epoch)


def _finish(
    connection: sqlite3.Connection,
    task_id: str,
    owner: str | LeaseToken,
    result: TaskResult,
    *,
    lease_epoch: int | None = None,
    result_ref: str | None = None,
) -> JournalEntry:
    if result.task_id != task_id or result.status.casefold() not in _TERMINAL:
        raise ValueError("a matching terminal TaskResult is required")
    if result_ref is not None and (not result_ref.strip() or len(result_ref) > 500):
        raise ValueError("result_ref must be a bounded non-empty reference")
    ensure_schema(connection)
    encoded = _json(contract_json(result))
    with _write_transaction(connection):
        token = _coerce_token(connection, task_id, owner, lease_epoch)
        cursor = connection.execute(
            "UPDATE agent_tasks SET status=?,result_json=?,result_ref=?,owner_id=NULL,worker_id=NULL,"
            "lease_expires_at=NULL,updated_at=? WHERE task_id=? AND status='running' "
            "AND owner_id=? AND lease_epoch=? AND cancel_requested=0",
            (result.status.casefold(), encoded, result_ref, _iso(result.completed_at), task_id,
             token.owner_id, token.lease_epoch),
        )
        if cursor.rowcount != 1:
            existing = _entry(connection.execute(f"{_SELECT} WHERE task_id=?", (task_id,)).fetchone())
            if (
                existing is not None
                and existing.status in _TERMINAL
                and existing.lease_epoch == token.lease_epoch
                and existing.result == json.loads(encoded)
            ):
                return existing
            raise RuntimeError("task lease token is stale")
        _event(connection, task_id, token.lease_epoch, "result",
               {"status": result.status.casefold(), "result_ref": result_ref}, result.completed_at)
        finished = _entry(connection.execute(f"{_SELECT} WHERE task_id=?", (task_id,)).fetchone())
    assert finished is not None
    return finished


def complete(
    connection: sqlite3.Connection, task_id: str, owner: str | LeaseToken, result: TaskResult,
    *, lease_epoch: int | None = None, result_ref: str | None = None,
) -> JournalEntry:
    if not result.succeeded:
        raise ValueError("complete requires a successful TaskResult")
    return _finish(connection, task_id, owner, result, lease_epoch=lease_epoch, result_ref=result_ref)


def fail(
    connection: sqlite3.Connection, task_id: str, owner: str | LeaseToken, result: TaskResult,
    *, lease_epoch: int | None = None, result_ref: str | None = None,
) -> JournalEntry:
    if result.succeeded:
        raise ValueError("fail requires a non-successful TaskResult")
    return _finish(connection, task_id, owner, result, lease_epoch=lease_epoch, result_ref=result_ref)


def acknowledge_cancel(
    connection: sqlite3.Connection, token: LeaseToken, *, now: datetime | None = None
) -> JournalEntry:
    ensure_schema(connection)
    current = now or _now()
    result = TaskResult(task_id=token.task_id, status="cancelled", completed_at=current)
    encoded = _json(contract_json(result))
    with _write_transaction(connection):
        cursor = connection.execute(
            "UPDATE agent_tasks SET status='cancelled',result_json=?,owner_id=NULL,worker_id=NULL,"
            "lease_expires_at=NULL,updated_at=? WHERE task_id=? AND status='running' "
            "AND owner_id=? AND lease_epoch=? AND cancel_requested=1",
            (encoded, _iso(current), token.task_id, token.owner_id, token.lease_epoch),
        )
        if cursor.rowcount != 1:
            existing = _entry(
                connection.execute(f"{_SELECT} WHERE task_id=?", (token.task_id,)).fetchone()
            )
            if (
                existing is not None
                and existing.status == "cancelled"
                and existing.lease_epoch == token.lease_epoch
            ):
                return existing
            raise RuntimeError("task lease token is stale or cancellation was not requested")
        _event(
            connection,
            token.task_id,
            token.lease_epoch,
            "result",
            {"status": "cancelled", "result_ref": None},
            current,
        )
        finished = _entry(
            connection.execute(f"{_SELECT} WHERE task_id=?", (token.task_id,)).fetchone()
        )
    assert finished is not None
    return finished


def schedule_retry(
    connection: sqlite3.Connection,
    token: LeaseToken,
    *,
    retry_at: datetime,
    failure_category: str,
    now: datetime | None = None,
) -> JournalEntry:
    if not failure_category.strip():
        raise ValueError("failure_category is required")
    current = now or _now()
    durable = load_spec(connection, token.task_id)
    deadline_value = durable.get("deadline_at")
    if deadline_value is not None:
        if not isinstance(deadline_value, str):
            raise TypeError("durable task deadline must be an ISO timestamp")
        deadline = datetime.fromisoformat(deadline_value)
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("durable task deadline must be timezone-aware")
        if retry_at >= deadline.astimezone(UTC):
            return fail(
                connection,
                token.task_id,
                token,
                TaskResult(
                    task_id=token.task_id,
                    status="timed_out",
                    failure_category="retry_exceeds_deadline",
                    completed_at=current,
                ),
            )
    with _write_transaction(connection):
        cursor = connection.execute(
            "UPDATE agent_tasks SET status='pending',owner_id=NULL,worker_id=NULL,lease_expires_at=NULL,"
            "retry_at=?,updated_at=? WHERE task_id=? AND status='running' AND effect_class='read' "
            "AND owner_id=? AND lease_epoch=? AND cancel_requested=0",
            (_iso(retry_at), _iso(current), token.task_id, token.owner_id, token.lease_epoch),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("task lease token is stale or task is not replayable")
        _event(connection, token.task_id, token.lease_epoch, "retry_scheduled",
               {"failure_category": failure_category, "retry_at": _iso(retry_at)}, current)
        updated = _entry(connection.execute(f"{_SELECT} WHERE task_id=?", (token.task_id,)).fetchone())
    assert updated is not None
    return updated


def dead_letter(
    connection: sqlite3.Connection,
    token: LeaseToken,
    *,
    reason: str,
    now: datetime | None = None,
) -> JournalEntry:
    if not reason.strip():
        raise ValueError("dead-letter reason is required")
    current = now or _now()
    with _write_transaction(connection):
        cursor = connection.execute(
            "UPDATE agent_tasks SET status='dead_letter',owner_id=NULL,worker_id=NULL,"
            "lease_expires_at=NULL,dead_letter_reason=?,updated_at=? WHERE task_id=? "
            "AND status='running' AND owner_id=? AND lease_epoch=?",
            (reason[:500], _iso(current), token.task_id, token.owner_id, token.lease_epoch),
        )
        if cursor.rowcount != 1:
            existing = _entry(
                connection.execute(f"{_SELECT} WHERE task_id=?", (token.task_id,)).fetchone()
            )
            if (
                existing is not None
                and existing.status == "dead_letter"
                and existing.lease_epoch == token.lease_epoch
                and existing.dead_letter_reason == reason[:500]
            ):
                return existing
            raise RuntimeError("task lease token is stale")
        _event(connection, token.task_id, token.lease_epoch, "dead_lettered", {"reason": reason[:500]}, current)
        updated = _entry(connection.execute(f"{_SELECT} WHERE task_id=?", (token.task_id,)).fetchone())
    assert updated is not None
    return updated


def reap_expired(
    connection: sqlite3.Connection, *, now: datetime | None = None
) -> tuple[int, int]:
    ensure_schema(connection)
    current = now or _now()
    current_text = _iso(current)
    requeued = dead_count = 0
    with _write_transaction(connection):
        rows = connection.execute(
            "SELECT task_id,effect_class,lease_epoch FROM agent_tasks WHERE status='running' "
            "AND lease_expires_at IS NOT NULL AND lease_expires_at<=?", (current_text,)
        ).fetchall()
        for task_id, effect_class, epoch in rows:
            if str(effect_class) == "read":
                cursor = connection.execute(
                    "UPDATE agent_tasks SET status='pending',owner_id=NULL,worker_id=NULL,"
                    "lease_expires_at=NULL,retry_at=?,updated_at=? WHERE task_id=? "
                    "AND status='running' AND lease_epoch=?",
                    (current_text, current_text, str(task_id), int(epoch)),
                )
                requeued += cursor.rowcount
                event_type, payload = "lease_requeued", {}
            else:
                reason = "expired_non_read_lease_requires_manual_review"
                cursor = connection.execute(
                    "UPDATE agent_tasks SET status='dead_letter',owner_id=NULL,worker_id=NULL,"
                    "lease_expires_at=NULL,dead_letter_reason=?,updated_at=? WHERE task_id=? "
                    "AND status='running' AND lease_epoch=?",
                    (reason, current_text, str(task_id), int(epoch)),
                )
                dead_count += cursor.rowcount
                event_type, payload = "dead_lettered", {"reason": reason}
            if cursor.rowcount:
                _event(connection, str(task_id), int(epoch), event_type, payload, current)
    return requeued, dead_count


def list_events(connection: sqlite3.Connection, task_id: str) -> list[dict[str, object]]:
    ensure_schema(connection)
    rows = connection.execute(
        "SELECT lease_epoch,event_type,payload_json,occurred_at FROM task_runtime_events "
        "WHERE task_id=? ORDER BY event_id", (task_id,)
    ).fetchall()
    return [{"lease_epoch": int(row[0]), "event_type": str(row[1]),
             "payload": json.loads(str(row[2])), "occurred_at": str(row[3])} for row in rows]
