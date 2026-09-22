"""Ordered database-level schema migrations for ApplyPilot.

The database predates a top-level version marker.  A missing marker therefore
means "legacy baseline" rather than a corrupt database.  Callers must inspect
the version before running legacy bootstrap SQL so a database created by newer
code is rejected without any writes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path

from applypilot.storage.transactions import write_transaction

DATABASE_SCHEMA_COMPONENT = "applypilot_database"
DATABASE_SCHEMA_TABLE = "applypilot_schema_version"
Migration = Callable[[sqlite3.Connection], None]


def read_database_schema_version(connection: sqlite3.Connection) -> int:
    """Read the current database version without mutating the connection."""
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (DATABASE_SCHEMA_TABLE,),
    ).fetchone()
    if exists is None:
        return 0
    row = connection.execute(
        f"SELECT version FROM {DATABASE_SCHEMA_TABLE} WHERE component=?",
        (DATABASE_SCHEMA_COMPONENT,),
    ).fetchone()
    if row is None:
        return 0
    version = row[0]
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise RuntimeError(f"invalid ApplyPilot database schema version: {version!r}")
    return version


def require_supported_database_schema(
    connection: sqlite3.Connection,
    *,
    latest_version: int,
) -> int:
    """Fail closed when the database was written by newer code."""
    current = read_database_schema_version(connection)
    if current > latest_version:
        raise RuntimeError(
            f"ApplyPilot database schema {current} is newer than supported "
            f"{latest_version}"
        )
    return current


def require_supported_database_path(
    path: Path | str,
    *,
    latest_version: int,
) -> int:
    """Probe an existing file read-only before opening a cached writer."""
    if str(path) == ":memory:":
        return 0
    database_path = Path(path)
    if not database_path.exists():
        return 0
    probe = sqlite3.connect(
        f"{database_path.resolve().as_uri()}?mode=ro",
        uri=True,
    )
    try:
        return require_supported_database_schema(
            probe,
            latest_version=latest_version,
        )
    finally:
        probe.close()


def run_database_migrations(
    connection: sqlite3.Connection,
    migrations: Sequence[Migration],
) -> int:
    """Apply each missing migration atomically and record its ordered version."""
    latest_version = len(migrations)
    current = require_supported_database_schema(
        connection,
        latest_version=latest_version,
    )
    if current == latest_version:
        return current
    with write_transaction(connection):
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {DATABASE_SCHEMA_TABLE} (
                component TEXT PRIMARY KEY,
                version   INTEGER NOT NULL CHECK(version >= 0)
            )
            """
        )
        # Re-read after BEGIN IMMEDIATE so concurrent initializers serialize on
        # the version observed while holding the write lock.
        current = require_supported_database_schema(
            connection,
            latest_version=latest_version,
        )
        for target_version in range(current + 1, latest_version + 1):
            migrations[target_version - 1](connection)
            connection.execute(
                f"""
                INSERT INTO {DATABASE_SCHEMA_TABLE} (component, version)
                VALUES (?, ?)
                ON CONFLICT(component) DO UPDATE SET version=excluded.version
                """,
                (DATABASE_SCHEMA_COMPONENT, target_version),
            )
    return latest_version
