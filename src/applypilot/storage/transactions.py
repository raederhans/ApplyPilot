"""Small SQLite write units that respect the caller's transaction ownership."""

from __future__ import annotations

import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

_SAVEPOINT_PREFIX = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def execute_transactional_script(
    connection: sqlite3.Connection,
    script: str,
) -> None:
    """Execute a SQL script without ``executescript``'s implicit commit.

    Python's SQLite ``executescript`` commits a pending transaction before it
    runs.  Schema bootstraps that participate in a larger migration therefore
    need to execute complete statements one at a time.
    """
    pending: list[str] = []
    for character in script:
        pending.append(character)
        statement = "".join(pending).strip()
        if character == ";" and statement and sqlite3.complete_statement(statement):
            connection.execute(statement)
            pending.clear()
    remainder = "".join(pending).strip()
    if remainder:
        raise ValueError("incomplete SQL statement in transactional script")


@contextmanager
def write_transaction(
    connection: sqlite3.Connection,
    *,
    prefix: str = "applypilot_write",
) -> Iterator[sqlite3.Connection]:
    """Commit an owned write, or isolate a nested write with a savepoint.

    A nested success never commits the caller's work. A failure, including
    cancellation, rolls back this unit only. Code inside the unit must not
    commit or roll back the connection itself.
    """
    if not _SAVEPOINT_PREFIX.fullmatch(prefix):
        raise ValueError("transaction savepoint prefix must be a safe SQL identifier")
    nested = connection.in_transaction
    savepoint = f"{prefix}_{uuid.uuid4().hex}" if nested else None
    if savepoint is not None:
        connection.execute(f"SAVEPOINT {savepoint}")
    else:
        connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
        if not connection.in_transaction:
            raise RuntimeError("write transaction was ended by its operation")
        if savepoint is not None:
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            connection.commit()
    except BaseException:
        # SQLite can itself abort a transaction on an I/O or interruption error.
        if connection.in_transaction:
            if savepoint is not None:
                connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            else:
                connection.rollback()
        raise
