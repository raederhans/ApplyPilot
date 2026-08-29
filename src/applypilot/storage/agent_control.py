"""Durable, provider-neutral agent control records.

The application jobs and application ledger remain authoritative.  Checkpoints
only allow an agent runtime to resume its own work and never mutate apply state.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from applypilot.apply.contracts import (
    AgentCheckpoint,
    ApplicationEvent,
    HumanRequest,
    ensure_persistable,
)


def _json_text(value: object) -> str:
    return json.dumps(ensure_persistable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Create control-plane tables without changing application state tables."""
    was_in_transaction = connection.in_transaction
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_events (
            event_id         TEXT PRIMARY KEY,
            attempt_id       TEXT NOT NULL,
            run_id           TEXT NOT NULL,
            phase            TEXT NOT NULL,
            actor            TEXT NOT NULL,
            event_type       TEXT NOT NULL,
            payload_json     TEXT NOT NULL,
            evidence_json    TEXT NOT NULL,
            idempotency_key  TEXT,
            occurred_at      TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_events_attempt
            ON agent_events(attempt_id, occurred_at, event_id)
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_events_run
            ON agent_events(run_id, occurred_at, event_id)
    """)
    connection.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_events_idempotency
            ON agent_events(idempotency_key)
            WHERE idempotency_key IS NOT NULL
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_checkpoints (
            checkpoint_id TEXT PRIMARY KEY,
            run_id         TEXT NOT NULL,
            attempt_id     TEXT NOT NULL,
            phase          TEXT NOT NULL,
            sequence       INTEGER NOT NULL CHECK(sequence >= 0),
            state_json     TEXT NOT NULL,
            created_at     TEXT NOT NULL,
            UNIQUE(run_id, sequence)
        )
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_checkpoints_latest
            ON agent_checkpoints(run_id, sequence DESC)
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_human_requests (
            request_id    TEXT PRIMARY KEY,
            run_id        TEXT NOT NULL,
            attempt_id    TEXT NOT NULL,
            request_type  TEXT NOT NULL,
            prompt        TEXT NOT NULL,
            context_json  TEXT NOT NULL,
            status        TEXT NOT NULL,
            created_at    TEXT NOT NULL,
            resolved_at   TEXT
        )
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_human_requests_open
            ON agent_human_requests(attempt_id, status, created_at)
    """)
    # Durable history is immutable even if a future caller accidentally issues
    # an UPDATE or DELETE directly. Human requests are intentionally mutable so
    # their resolution can be recorded without changing application state.
    connection.execute("""
        CREATE TRIGGER IF NOT EXISTS agent_events_no_update
        BEFORE UPDATE ON agent_events BEGIN
            SELECT RAISE(ABORT, 'agent_events is append-only');
        END
    """)
    connection.execute("""
        CREATE TRIGGER IF NOT EXISTS agent_events_no_delete
        BEFORE DELETE ON agent_events BEGIN
            SELECT RAISE(ABORT, 'agent_events is append-only');
        END
    """)
    connection.execute("""
        CREATE TRIGGER IF NOT EXISTS agent_checkpoints_no_update
        BEFORE UPDATE ON agent_checkpoints BEGIN
            SELECT RAISE(ABORT, 'agent_checkpoints is append-only');
        END
    """)
    connection.execute("""
        CREATE TRIGGER IF NOT EXISTS agent_checkpoints_no_delete
        BEFORE DELETE ON agent_checkpoints BEGIN
            SELECT RAISE(ABORT, 'agent_checkpoints is append-only');
        END
    """)
    if not was_in_transaction:
        connection.commit()


def append_event(connection: sqlite3.Connection, event: ApplicationEvent) -> bool:
    """Append an event; exact replay is a no-op and conflicting reuse is rejected."""
    ensure_schema(connection)
    values = (
        event.event_id,
        event.attempt_id,
        event.run_id,
        event.phase,
        event.actor,
        event.event_type,
        _json_text(event.payload),
        _json_text(event.evidence_refs),
        event.idempotency_key,
        event.occurred_at.isoformat(),
    )
    try:
        connection.execute(
            "INSERT INTO agent_events "
            "(event_id, attempt_id, run_id, phase, actor, event_type, payload_json, "
            "evidence_json, idempotency_key, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        return True
    except sqlite3.IntegrityError:
        existing = connection.execute(
            "SELECT event_id, attempt_id, run_id, phase, actor, event_type, payload_json, "
            "evidence_json, idempotency_key, occurred_at FROM agent_events WHERE event_id=?",
            (event.event_id,),
        ).fetchone()
        if existing is not None and tuple(existing) == values:
            return False
        if event.idempotency_key is not None:
            replay = connection.execute(
                "SELECT event_id, attempt_id, run_id, phase, actor, event_type, payload_json, "
                "evidence_json, idempotency_key, occurred_at FROM agent_events "
                "WHERE idempotency_key=?",
                (event.idempotency_key,),
            ).fetchone()
            if replay is not None:
                if tuple(replay)[1:9] == values[1:9]:
                    return False
                raise ValueError(
                    f"idempotency_key collision: {event.idempotency_key}"
                ) from None
        raise ValueError(f"event_id collision: {event.event_id}") from None


def list_events(
    connection: sqlite3.Connection,
    *,
    attempt_id: str | None = None,
    run_id: str | None = None,
    event_type: str | None = None,
    limit: int | None = None,
) -> list[ApplicationEvent]:
    """Search durable events using optional workflow-neutral filters."""
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
    ):
        raise ValueError("limit must be a positive integer")
    ensure_schema(connection)
    clauses: list[str] = []
    params: list[object] = []
    for column, value in (
        ("attempt_id", attempt_id),
        ("run_id", run_id),
        ("event_type", event_type),
    ):
        if value is not None:
            clauses.append(f"{column}=?")
            params.append(value)
    sql = (
        "SELECT event_id, attempt_id, run_id, phase, actor, event_type, payload_json, "
        "evidence_json, idempotency_key, occurred_at FROM agent_events"
    )
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY occurred_at, event_id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [
        ApplicationEvent(
            event_id=row[0],
            attempt_id=row[1],
            run_id=row[2],
            phase=row[3],
            actor=row[4],
            event_type=row[5],
            payload=json.loads(row[6]),
            evidence_refs=tuple(json.loads(row[7])),
            idempotency_key=row[8],
            occurred_at=datetime.fromisoformat(row[9]),
        )
        for row in connection.execute(sql, tuple(params)).fetchall()
    ]


def append_checkpoint(connection: sqlite3.Connection, checkpoint: AgentCheckpoint) -> bool:
    """Append a resumable runtime checkpoint without mutating business state."""
    ensure_schema(connection)
    values = (
        checkpoint.checkpoint_id,
        checkpoint.run_id,
        checkpoint.attempt_id,
        checkpoint.phase,
        checkpoint.sequence,
        _json_text(checkpoint.state),
        checkpoint.created_at.isoformat(),
    )
    try:
        connection.execute(
            "INSERT INTO agent_checkpoints "
            "(checkpoint_id, run_id, attempt_id, phase, sequence, state_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        return True
    except sqlite3.IntegrityError:
        existing = connection.execute(
            "SELECT checkpoint_id, run_id, attempt_id, phase, sequence, state_json, created_at "
            "FROM agent_checkpoints WHERE checkpoint_id=?",
            (checkpoint.checkpoint_id,),
        ).fetchone()
        if existing is not None and tuple(existing) == values:
            return False
        raise ValueError("checkpoint id or run sequence collision") from None


def latest_checkpoint(connection: sqlite3.Connection, run_id: str) -> AgentCheckpoint | None:
    """Load the newest checkpoint for one runtime run."""
    ensure_schema(connection)
    row = connection.execute(
        "SELECT checkpoint_id, run_id, attempt_id, phase, sequence, state_json, created_at "
        "FROM agent_checkpoints WHERE run_id=? ORDER BY sequence DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    return AgentCheckpoint(
        checkpoint_id=row[0],
        run_id=row[1],
        attempt_id=row[2],
        phase=row[3],
        sequence=int(row[4]),
        state=json.loads(row[5]),
        created_at=datetime.fromisoformat(row[6]),
    )


def create_human_request(connection: sqlite3.Connection, request: HumanRequest) -> bool:
    """Persist a human handoff request; exact replay is idempotent."""
    ensure_schema(connection)
    values = (
        request.request_id,
        request.run_id,
        request.attempt_id,
        request.request_type,
        request.prompt,
        _json_text(request.context),
        request.status,
        request.created_at.isoformat(),
        None if request.resolved_at is None else request.resolved_at.isoformat(),
    )
    try:
        connection.execute(
            "INSERT INTO agent_human_requests "
            "(request_id, run_id, attempt_id, request_type, prompt, context_json, status, "
            "created_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        return True
    except sqlite3.IntegrityError:
        existing = connection.execute(
            "SELECT request_id, run_id, attempt_id, request_type, prompt, context_json, status, "
            "created_at, resolved_at FROM agent_human_requests WHERE request_id=?",
            (request.request_id,),
        ).fetchone()
        if existing is not None and tuple(existing) == values:
            return False
        raise ValueError(f"human request id collision: {request.request_id}") from None


def resolve_human_request(
    connection: sqlite3.Connection,
    request_id: str,
    *,
    status: str = "resolved",
    resolved_at: datetime | None = None,
) -> bool:
    """Resolve one open request without touching the jobs or application ledger."""
    if not request_id or not status:
        raise ValueError("request_id and status are required")
    when = resolved_at or datetime.now(UTC)
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("resolved_at must be timezone-aware")
    ensure_schema(connection)
    cursor = connection.execute(
        "UPDATE agent_human_requests SET status=?, resolved_at=? "
        "WHERE request_id=? AND status='open'",
        (status, when.isoformat(), request_id),
    )
    return cursor.rowcount == 1


def list_open_human_requests(
    connection: sqlite3.Connection,
    *,
    attempt_id: str | None = None,
) -> list[HumanRequest]:
    """Return open requests globally or for one attempt, oldest first."""
    ensure_schema(connection)
    sql = (
        "SELECT request_id, run_id, attempt_id, request_type, prompt, context_json, status, "
        "created_at, resolved_at FROM agent_human_requests WHERE status='open'"
    )
    params: tuple[str, ...] = ()
    if attempt_id is not None:
        sql += " AND attempt_id=?"
        params = (attempt_id,)
    sql += " ORDER BY created_at, request_id"
    return [
        HumanRequest(
            request_id=row[0],
            run_id=row[1],
            attempt_id=row[2],
            request_type=row[3],
            prompt=row[4],
            context=json.loads(row[5]),
            status=row[6],
            created_at=datetime.fromisoformat(row[7]),
            resolved_at=None if row[8] is None else datetime.fromisoformat(row[8]),
        )
        for row in connection.execute(sql, params).fetchall()
    ]
