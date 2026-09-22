"""Small SQLite write units that respect the caller's transaction ownership."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def write_transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Commit an owned write, or isolate a nested write with a savepoint.

    A nested success never commits the caller's work. A failure, including
    cancellation, rolls back this unit only. Code inside the unit must not
    commit or roll back the connection itself.
    """
    nested = connection.in_transaction
    savepoint = f"applypilot_write_{uuid.uuid4().hex}" if nested else None
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
