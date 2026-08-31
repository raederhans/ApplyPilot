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
    ApplicationException,
    HumanRequest,
    RecoveryExecutionResult,
    ensure_persistable,
)


class StaleCheckpointError(ValueError):
    """The caller's expected actor sequence is no longer current."""


def _json_text(value: object) -> str:
    return json.dumps(ensure_persistable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _ensure_columns(
    connection: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
) -> None:
    existing = {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    for name, definition in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Create control-plane tables without changing application state tables."""
    was_in_transaction = connection.in_transaction
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_events (
            event_id         TEXT PRIMARY KEY,
            attempt_id       TEXT NOT NULL,
            run_id           TEXT NOT NULL,
            actor_id         TEXT,
            turn_id          TEXT,
            phase            TEXT NOT NULL,
            actor            TEXT NOT NULL,
            event_type       TEXT NOT NULL,
            payload_json     TEXT NOT NULL,
            evidence_json    TEXT NOT NULL,
            idempotency_key  TEXT,
            schema_version   TEXT NOT NULL DEFAULT '1',
            occurred_at      TEXT NOT NULL
        )
    """)
    _ensure_columns(
        connection,
        "agent_events",
        {
            "actor_id": "TEXT",
            "turn_id": "TEXT",
            "schema_version": "TEXT NOT NULL DEFAULT '1'",
        },
    )
    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_events_attempt
            ON agent_events(attempt_id, occurred_at, event_id)
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_events_run
            ON agent_events(run_id, occurred_at, event_id)
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_events_actor
            ON agent_events(actor_id, occurred_at, event_id)
            WHERE actor_id IS NOT NULL
    """)
    connection.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_events_idempotency
            ON agent_events(idempotency_key)
            WHERE idempotency_key IS NOT NULL
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_checkpoints (
            checkpoint_id               TEXT PRIMARY KEY,
            run_id                       TEXT NOT NULL,
            attempt_id                   TEXT NOT NULL,
            actor_id                     TEXT,
            turn_id                      TEXT,
            phase                        TEXT NOT NULL,
            sequence                     INTEGER NOT NULL CHECK(sequence >= 0),
            expected_sequence            INTEGER,
            state_json                   TEXT NOT NULL,
            idempotency_key              TEXT,
            fresh_turn_resume_authorized INTEGER NOT NULL DEFAULT 0,
            schema_version               TEXT NOT NULL DEFAULT '1',
            created_at                   TEXT NOT NULL,
            UNIQUE(run_id, sequence)
        )
    """)
    _ensure_columns(
        connection,
        "agent_checkpoints",
        {
            "actor_id": "TEXT",
            "turn_id": "TEXT",
            "expected_sequence": "INTEGER",
            "idempotency_key": "TEXT",
            "fresh_turn_resume_authorized": "INTEGER NOT NULL DEFAULT 0",
            "schema_version": "TEXT NOT NULL DEFAULT '1'",
        },
    )
    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_checkpoints_latest
            ON agent_checkpoints(run_id, sequence DESC)
    """)
    connection.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_checkpoints_actor_sequence_v2
            ON agent_checkpoints(actor_id, sequence)
            WHERE schema_version = '2' AND actor_id IS NOT NULL
    """)
    connection.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_checkpoints_idempotency
            ON agent_checkpoints(idempotency_key)
            WHERE idempotency_key IS NOT NULL
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_checkpoints_actor_latest
            ON agent_checkpoints(actor_id, sequence DESC)
            WHERE schema_version = '2' AND actor_id IS NOT NULL
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
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_recovery_results (
            result_id      TEXT PRIMARY KEY,
            command_id     TEXT NOT NULL,
            run_id         TEXT NOT NULL,
            attempt_id     TEXT NOT NULL,
            actor_id       TEXT NOT NULL,
            turn_id        TEXT NOT NULL,
            stage          TEXT NOT NULL,
            outcome        TEXT NOT NULL,
            terminal_status TEXT,
            details_json   TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            occurred_at    TEXT NOT NULL,
            UNIQUE(command_id, stage)
        )
    """)
    _ensure_columns(
        connection,
        "agent_recovery_results",
        {"terminal_status": "TEXT"},
    )
    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_recovery_results_attempt
            ON agent_recovery_results(attempt_id, occurred_at, result_id)
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_exception_queue (
            exception_id     TEXT PRIMARY KEY,
            command_id       TEXT NOT NULL UNIQUE,
            run_id           TEXT NOT NULL,
            attempt_id       TEXT NOT NULL,
            actor_id         TEXT NOT NULL,
            turn_id          TEXT NOT NULL,
            queue_kind       TEXT NOT NULL,
            failure_category TEXT NOT NULL,
            next_action      TEXT NOT NULL,
            status           TEXT NOT NULL,
            context_json     TEXT NOT NULL,
            created_at       TEXT NOT NULL,
            resolved_at      TEXT
        )
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_exception_queue_open
            ON agent_exception_queue(status, queue_kind, created_at, exception_id)
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
    connection.execute("""
        CREATE TRIGGER IF NOT EXISTS agent_recovery_results_no_update
        BEFORE UPDATE ON agent_recovery_results BEGIN
            SELECT RAISE(ABORT, 'agent_recovery_results is append-only');
        END
    """)
    connection.execute("""
        CREATE TRIGGER IF NOT EXISTS agent_recovery_results_no_delete
        BEFORE DELETE ON agent_recovery_results BEGIN
            SELECT RAISE(ABORT, 'agent_recovery_results is append-only');
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
        event.actor_id,
        event.turn_id,
        event.phase,
        event.actor,
        event.event_type,
        _json_text(event.payload),
        _json_text(event.evidence_refs),
        event.idempotency_key,
        event.schema_version,
        event.occurred_at.isoformat(),
    )
    try:
        connection.execute(
            "INSERT INTO agent_events "
            "(event_id, attempt_id, run_id, actor_id, turn_id, phase, actor, event_type, "
            "payload_json, evidence_json, idempotency_key, schema_version, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        return True
    except sqlite3.IntegrityError:
        existing = connection.execute(
            "SELECT event_id, attempt_id, run_id, actor_id, turn_id, phase, actor, event_type, "
            "payload_json, evidence_json, idempotency_key, schema_version, occurred_at "
            "FROM agent_events WHERE event_id=?",
            (event.event_id,),
        ).fetchone()
        if existing is not None and tuple(existing) == values:
            return False
        if event.idempotency_key is not None:
            replay = connection.execute(
                "SELECT event_id, attempt_id, run_id, actor_id, turn_id, phase, actor, "
                "event_type, payload_json, evidence_json, idempotency_key, schema_version, "
                "occurred_at FROM agent_events WHERE idempotency_key=?",
                (event.idempotency_key,),
            ).fetchone()
            if replay is not None:
                if tuple(replay)[1:12] == values[1:12]:
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
        "SELECT event_id, attempt_id, run_id, actor_id, turn_id, phase, actor, event_type, "
        "payload_json, evidence_json, idempotency_key, schema_version, occurred_at "
        "FROM agent_events"
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
            actor_id=row[3],
            turn_id=row[4],
            phase=row[5],
            actor=row[6],
            event_type=row[7],
            payload=json.loads(row[8]),
            evidence_refs=tuple(json.loads(row[9])),
            idempotency_key=row[10],
            schema_version=row[11],
            occurred_at=datetime.fromisoformat(row[12]),
        )
        for row in connection.execute(sql, tuple(params)).fetchall()
    ]


_CHECKPOINT_COLUMNS = (
    "checkpoint_id, run_id, attempt_id, actor_id, turn_id, phase, sequence, "
    "expected_sequence, state_json, idempotency_key, fresh_turn_resume_authorized, "
    "schema_version, created_at"
)


def _checkpoint_from_row(row: sqlite3.Row | tuple[object, ...]) -> AgentCheckpoint:
    return AgentCheckpoint(
        checkpoint_id=str(row[0]),
        run_id=str(row[1]),
        attempt_id=str(row[2]),
        actor_id=None if row[3] is None else str(row[3]),
        turn_id=None if row[4] is None else str(row[4]),
        phase=str(row[5]),
        sequence=int(row[6]),
        expected_sequence=None if row[7] is None else int(row[7]),
        state=json.loads(str(row[8])),
        idempotency_key=None if row[9] is None else str(row[9]),
        fresh_turn_resume_authorized=bool(row[10]),
        schema_version=str(row[11]),
        created_at=datetime.fromisoformat(str(row[12])),
    )


def _checkpoint_semantics(row: tuple[object, ...]) -> tuple[object, ...]:
    """Return completion semantics, excluding generated CAS/time coordinates."""
    return (
        row[1],
        row[2],
        row[3],
        row[4],
        row[5],
        row[8],
        row[9],
        row[10],
        row[11],
    )


def append_checkpoint(connection: sqlite3.Connection, checkpoint: AgentCheckpoint) -> bool:
    """CAS-append one v2 actor checkpoint; semantic replay is a no-op."""
    if checkpoint.schema_version != "2":
        raise ValueError("legacy v1 checkpoints are read-only")
    ensure_schema(connection)
    values = (
        checkpoint.checkpoint_id,
        checkpoint.run_id,
        checkpoint.attempt_id,
        checkpoint.actor_id,
        checkpoint.turn_id,
        checkpoint.phase,
        checkpoint.sequence,
        checkpoint.expected_sequence,
        _json_text(checkpoint.state),
        checkpoint.idempotency_key,
        int(checkpoint.fresh_turn_resume_authorized),
        checkpoint.schema_version,
        checkpoint.created_at.isoformat(),
    )
    replay = connection.execute(
        f"SELECT {_CHECKPOINT_COLUMNS} FROM agent_checkpoints WHERE idempotency_key=?",
        (checkpoint.idempotency_key,),
    ).fetchone()
    if replay is not None:
        if _checkpoint_semantics(tuple(replay)) == _checkpoint_semantics(values):
            return False
        raise ValueError(
            f"checkpoint idempotency_key collision: {checkpoint.idempotency_key}"
        )

    current_sequence = current_actor_sequence(connection, str(checkpoint.actor_id))
    if checkpoint.expected_sequence != current_sequence:
        raise StaleCheckpointError(
            "stale checkpoint expected sequence: "
            f"expected {checkpoint.expected_sequence}, current {current_sequence}"
        )
    try:
        connection.execute(
            "INSERT INTO agent_checkpoints "
            "(checkpoint_id, run_id, attempt_id, actor_id, turn_id, phase, sequence, "
            "expected_sequence, state_json, idempotency_key, "
            "fresh_turn_resume_authorized, schema_version, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        return True
    except sqlite3.IntegrityError:
        replay = connection.execute(
            f"SELECT {_CHECKPOINT_COLUMNS} FROM agent_checkpoints WHERE idempotency_key=?",
            (checkpoint.idempotency_key,),
        ).fetchone()
        if replay is not None:
            if _checkpoint_semantics(tuple(replay)) == _checkpoint_semantics(values):
                return False
            raise ValueError(
                f"checkpoint idempotency_key collision: {checkpoint.idempotency_key}"
            ) from None
        current_sequence = current_actor_sequence(connection, str(checkpoint.actor_id))
        if current_sequence != checkpoint.expected_sequence:
            raise StaleCheckpointError(
                "stale checkpoint expected sequence: "
                f"expected {checkpoint.expected_sequence}, current {current_sequence}"
            ) from None
        if connection.execute(
            "SELECT 1 FROM agent_checkpoints WHERE checkpoint_id=?",
            (checkpoint.checkpoint_id,),
        ).fetchone() is not None:
            raise ValueError(f"checkpoint_id collision: {checkpoint.checkpoint_id}") from None
        raise ValueError("checkpoint run or actor sequence collision") from None


def latest_checkpoint(connection: sqlite3.Connection, run_id: str) -> AgentCheckpoint | None:
    """Load the newest checkpoint for one runtime run, including legacy v1 rows."""
    ensure_schema(connection)
    row = connection.execute(
        f"SELECT {_CHECKPOINT_COLUMNS} FROM agent_checkpoints "
        "WHERE run_id=? ORDER BY sequence DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    return None if row is None else _checkpoint_from_row(row)


def latest_actor_checkpoint(
    connection: sqlite3.Connection,
    actor_id: str,
) -> AgentCheckpoint | None:
    """Load the latest native v2 checkpoint for CAS planning, not resume."""
    ensure_schema(connection)
    row = connection.execute(
        f"SELECT {_CHECKPOINT_COLUMNS} FROM agent_checkpoints "
        "WHERE actor_id=? AND schema_version='2' ORDER BY sequence DESC LIMIT 1",
        (actor_id,),
    ).fetchone()
    return None if row is None else _checkpoint_from_row(row)


def current_actor_sequence(connection: sqlite3.Connection, actor_id: str) -> int:
    """Return the current native v2 actor sequence without reading resume state."""
    latest = latest_actor_checkpoint(connection, actor_id)
    return 0 if latest is None else latest.sequence


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
        if existing is not None:
            persisted = tuple(existing)
            # created_at is observation metadata, not part of the logical handoff identity.
            if persisted[1:7] == values[1:7] and persisted[8] == values[8]:
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


def append_recovery_result(
    connection: sqlite3.Connection,
    result: RecoveryExecutionResult,
) -> bool:
    """Append one immutable recovery lifecycle result with semantic replay."""
    ensure_schema(connection)
    values = (
        result.result_id,
        result.command_id,
        result.run_id,
        result.attempt_id,
        result.actor_id,
        result.turn_id,
        result.stage,
        result.outcome,
        result.terminal_status,
        _json_text(result.details),
        result.schema_version,
        result.occurred_at.isoformat(),
    )
    try:
        connection.execute(
            "INSERT INTO agent_recovery_results "
            "(result_id, command_id, run_id, attempt_id, actor_id, turn_id, stage, "
            "outcome, terminal_status, details_json, schema_version, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        return True
    except sqlite3.IntegrityError:
        existing = connection.execute(
            "SELECT result_id, command_id, run_id, attempt_id, actor_id, turn_id, "
            "stage, outcome, terminal_status, details_json, schema_version, occurred_at "
            "FROM agent_recovery_results WHERE command_id=? AND stage=?",
            (result.command_id, result.stage),
        ).fetchone()
        if existing is not None and tuple(existing)[1:11] == values[1:11]:
            return False
        raise ValueError(
            f"recovery result collision: {result.command_id}:{result.stage}"
        ) from None


def list_recovery_results(
    connection: sqlite3.Connection,
    *,
    command_id: str | None = None,
    attempt_id: str | None = None,
) -> list[RecoveryExecutionResult]:
    """Return recovery lifecycle results in causal stage order."""
    ensure_schema(connection)
    clauses: list[str] = []
    params: list[str] = []
    if command_id is not None:
        clauses.append("command_id=?")
        params.append(command_id)
    if attempt_id is not None:
        clauses.append("attempt_id=?")
        params.append(attempt_id)
    sql = (
        "SELECT result_id, command_id, run_id, attempt_id, actor_id, turn_id, stage, "
        "outcome, terminal_status, details_json, schema_version, occurred_at "
        "FROM agent_recovery_results"
    )
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += (
        " ORDER BY command_id, CASE stage WHEN 'started' THEN 1 WHEN 'executed' THEN 2 "
        "WHEN 'verified' THEN 3 ELSE 4 END, occurred_at, result_id"
    )
    return [
        RecoveryExecutionResult(
            result_id=str(row[0]),
            command_id=str(row[1]),
            run_id=str(row[2]),
            attempt_id=str(row[3]),
            actor_id=str(row[4]),
            turn_id=str(row[5]),
            stage=str(row[6]),  # type: ignore[arg-type]
            outcome=str(row[7]),
            terminal_status=(
                None if row[8] is None else str(row[8])
            ),  # type: ignore[arg-type]
            details=json.loads(str(row[9])),
            schema_version=str(row[10]),
            occurred_at=datetime.fromisoformat(str(row[11])),
        )
        for row in connection.execute(sql, tuple(params)).fetchall()
    ]


def enqueue_exception(
    connection: sqlite3.Connection,
    item: ApplicationException,
) -> bool:
    """Park one attempt; exact command replay never creates a second queue item."""
    ensure_schema(connection)
    values = (
        item.exception_id,
        item.command_id,
        item.run_id,
        item.attempt_id,
        item.actor_id,
        item.turn_id,
        item.queue_kind,
        item.failure_category,
        item.next_action,
        item.status,
        _json_text(item.context),
        item.created_at.isoformat(),
        None if item.resolved_at is None else item.resolved_at.isoformat(),
    )
    try:
        connection.execute(
            "INSERT INTO agent_exception_queue "
            "(exception_id, command_id, run_id, attempt_id, actor_id, turn_id, "
            "queue_kind, failure_category, next_action, status, context_json, "
            "created_at, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        return True
    except sqlite3.IntegrityError:
        existing = connection.execute(
            "SELECT exception_id, command_id, run_id, attempt_id, actor_id, turn_id, "
            "queue_kind, failure_category, next_action, status, context_json, "
            "created_at, resolved_at FROM agent_exception_queue WHERE command_id=?",
            (item.command_id,),
        ).fetchone()
        if existing is not None:
            persisted = tuple(existing)
            if persisted[1:11] == values[1:11] and persisted[12] == values[12]:
                return False
        raise ValueError(f"exception queue collision: {item.command_id}") from None


def list_exceptions(
    connection: sqlite3.Connection,
    *,
    status: str | None = "open",
    attempt_id: str | None = None,
    command_id: str | None = None,
) -> list[ApplicationException]:
    """List operator exceptions without changing job or application state."""
    ensure_schema(connection)
    clauses: list[str] = []
    params: list[str] = []
    for column, value in (
        ("status", status),
        ("attempt_id", attempt_id),
        ("command_id", command_id),
    ):
        if value is not None:
            clauses.append(f"{column}=?")
            params.append(value)
    sql = (
        "SELECT exception_id, command_id, run_id, attempt_id, actor_id, turn_id, "
        "queue_kind, failure_category, next_action, status, context_json, created_at, "
        "resolved_at FROM agent_exception_queue"
    )
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at, exception_id"
    return [
        ApplicationException(
            exception_id=str(row[0]),
            command_id=str(row[1]),
            run_id=str(row[2]),
            attempt_id=str(row[3]),
            actor_id=str(row[4]),
            turn_id=str(row[5]),
            queue_kind=str(row[6]),  # type: ignore[arg-type]
            failure_category=str(row[7]),
            next_action=str(row[8]),
            status=str(row[9]),
            context=json.loads(str(row[10])),
            created_at=datetime.fromisoformat(str(row[11])),
            resolved_at=None if row[12] is None else datetime.fromisoformat(str(row[12])),
        )
        for row in connection.execute(sql, tuple(params)).fetchall()
    ]


def resolve_exception(
    connection: sqlite3.Connection,
    exception_id: str,
    *,
    resolved_at: datetime | None = None,
) -> bool:
    """Resolve one open queue item without granting retry or Submit authority."""
    if not str(exception_id or "").strip():
        raise ValueError("exception_id is required")
    when = resolved_at or datetime.now(UTC)
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("resolved_at must be timezone-aware")
    ensure_schema(connection)
    cursor = connection.execute(
        "UPDATE agent_exception_queue SET status='resolved', resolved_at=? "
        "WHERE exception_id=? AND status='open'",
        (when.isoformat(), exception_id),
    )
    return cursor.rowcount == 1
