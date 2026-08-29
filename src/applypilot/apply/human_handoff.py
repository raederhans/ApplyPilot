"""Typed human handoff references and fresh-run resume context."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from applypilot.apply.contracts import ensure_persistable


@dataclass(frozen=True, slots=True)
class HumanResponseRef:
    request_id: str
    response_ref: str
    response_digest: str
    response_type: str
    resolved_by: str
    resolved_at: datetime

    def __post_init__(self) -> None:
        values = (
            self.request_id,
            self.response_ref,
            self.response_digest,
            self.response_type,
            self.resolved_by,
        )
        if not all(value.strip() for value in values):
            raise ValueError("human response references must be non-empty")
        if not re.fullmatch(r"[a-f0-9]{64}", self.response_digest):
            raise ValueError("response_digest must be a lowercase SHA-256 digest")
        if self.resolved_at.tzinfo is None or self.resolved_at.utcoffset() is None:
            raise ValueError("resolved_at must be timezone-aware")


def ensure_schema(connection: sqlite3.Connection) -> None:
    was_in_transaction = connection.in_transaction
    connection.execute("""
        CREATE TABLE IF NOT EXISTS agent_human_responses (
            request_id       TEXT PRIMARY KEY,
            response_ref     TEXT NOT NULL,
            response_digest  TEXT NOT NULL,
            response_type    TEXT NOT NULL,
            resolved_by      TEXT NOT NULL,
            resolved_at      TEXT NOT NULL
        )
    """)
    connection.execute("""
        CREATE TRIGGER IF NOT EXISTS agent_human_responses_no_update
        BEFORE UPDATE ON agent_human_responses BEGIN
            SELECT RAISE(ABORT, 'agent_human_responses is append-only');
        END
    """)
    connection.execute("""
        CREATE TRIGGER IF NOT EXISTS agent_human_responses_no_delete
        BEFORE DELETE ON agent_human_responses BEGIN
            SELECT RAISE(ABORT, 'agent_human_responses is append-only');
        END
    """)
    if not was_in_transaction:
        connection.commit()


def load_human_response(
    connection: sqlite3.Connection,
    request_id: str,
) -> HumanResponseRef | None:
    ensure_schema(connection)
    row = connection.execute(
        "SELECT request_id,response_ref,response_digest,response_type,resolved_by,resolved_at "
        "FROM agent_human_responses WHERE request_id=?",
        (request_id,),
    ).fetchone()
    if row is None:
        return None
    return HumanResponseRef(
        request_id=str(row[0]),
        response_ref=str(row[1]),
        response_digest=str(row[2]),
        response_type=str(row[3]),
        resolved_by=str(row[4]),
        resolved_at=datetime.fromisoformat(str(row[5])),
    )


def append_human_response(
    connection: sqlite3.Connection,
    response: HumanResponseRef,
) -> bool:
    """Append a reference-only response; exact replay is a no-op."""
    ensure_schema(connection)
    owns_transaction = not connection.in_transaction
    try:
        connection.execute(
            "INSERT INTO agent_human_responses(request_id,response_ref,response_digest,"
            "response_type,resolved_by,resolved_at) VALUES(?,?,?,?,?,?)",
            (
                response.request_id,
                response.response_ref,
                response.response_digest,
                response.response_type,
                response.resolved_by,
                response.resolved_at.astimezone(UTC).isoformat(),
            ),
        )
        if owns_transaction:
            connection.commit()
        return True
    except sqlite3.IntegrityError:
        if owns_transaction and connection.in_transaction:
            connection.rollback()
        existing = load_human_response(connection, response.request_id)
        if existing == response:
            return False
        raise ValueError("human response request_id was reused with different metadata") from None


def fresh_resume_context(
    connection: sqlite3.Connection,
    *,
    parent_run_id: str,
    checkpoint_ref: str,
    request_id: str,
) -> dict[str, object]:
    """Return context for a new Agent turn, never an SDK-session token."""
    response = load_human_response(connection, request_id)
    if response is None:
        raise LookupError(f"durable human response was not found: {request_id}")
    context = {
        "resume_mode": "fresh_agent_turn",
        "parent_run_id": parent_run_id,
        "checkpoint_ref": checkpoint_ref,
        "human_response": {
            "request_id": response.request_id,
            "response_ref": response.response_ref,
            "response_digest": response.response_digest,
            "response_type": response.response_type,
            "resolved_by": response.resolved_by,
            "resolved_at": response.resolved_at.astimezone(UTC).isoformat(),
        },
    }
    return ensure_persistable(context)  # type: ignore[return-value]
