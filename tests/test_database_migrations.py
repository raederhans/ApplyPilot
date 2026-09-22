from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from applypilot import database
from applypilot.storage.database_migrations import (
    DATABASE_SCHEMA_COMPONENT,
    DATABASE_SCHEMA_TABLE,
    read_database_schema_version,
    run_database_migrations,
)


def test_init_db_records_current_version_and_is_idempotent(tmp_path) -> None:
    db_path = tmp_path / "fresh.db"

    first = database.init_db(db_path)
    assert read_database_schema_version(first) == database.DATABASE_SCHEMA_VERSION
    assert first.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    first.close()
    database.close_connection(db_path)

    second = database.init_db(db_path)
    assert read_database_schema_version(second) == database.DATABASE_SCHEMA_VERSION
    assert second.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_init_db_upgrades_versionless_database_without_losing_rows(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(db_path)
    legacy.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY, title TEXT)")
    legacy.execute(
        "INSERT INTO jobs (url, title) VALUES (?, ?)",
        ("https://example.test/job/1", "Legacy role"),
    )
    legacy.commit()
    legacy.close()

    upgraded = database.init_db(db_path)

    assert read_database_schema_version(upgraded) == database.DATABASE_SCHEMA_VERSION
    row = upgraded.execute(
        "SELECT title FROM jobs WHERE url=?",
        ("https://example.test/job/1",),
    ).fetchone()
    assert row[0] == "Legacy role"


def test_newer_database_schema_fails_before_any_schema_write(tmp_path) -> None:
    db_path = tmp_path / "newer.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        f"CREATE TABLE {DATABASE_SCHEMA_TABLE} "
        "(component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
    )
    connection.execute(
        f"INSERT INTO {DATABASE_SCHEMA_TABLE} VALUES (?, ?)",
        (DATABASE_SCHEMA_COMPONENT, database.DATABASE_SCHEMA_VERSION + 1),
    )
    connection.execute("CREATE TABLE sentinel (value TEXT)")
    connection.execute("INSERT INTO sentinel VALUES ('unchanged')")
    connection.commit()
    journal_mode_before = connection.execute("PRAGMA journal_mode").fetchone()[0]
    before = connection.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    connection.close()

    with pytest.raises(RuntimeError, match="newer than supported"):
        database.init_db(db_path)

    verify = sqlite3.connect(db_path)
    after = verify.execute(
        "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    assert after == before
    assert verify.execute("SELECT value FROM sentinel").fetchone()[0] == "unchanged"
    assert verify.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
    ).fetchone() is None
    assert verify.execute("PRAGMA journal_mode").fetchone()[0] == journal_mode_before
    assert str(db_path) not in getattr(database._local, "connections", {})


def test_concurrent_newer_version_wins_before_legacy_bootstrap(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "concurrent-newer.db"
    legacy = sqlite3.connect(db_path)
    legacy.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY, site TEXT, source_site TEXT)")
    legacy.execute(
        "INSERT INTO jobs VALUES (?, ?, NULL)",
        ("https://example.test/job/race", "sentinel-board"),
    )
    legacy.commit()
    legacy.close()
    original_probe = database.require_supported_database_path

    def upgrade_after_probe(path, *, latest_version: int) -> int:
        version = original_probe(path, latest_version=latest_version)
        newer = sqlite3.connect(path)
        newer.execute(
            f"CREATE TABLE {DATABASE_SCHEMA_TABLE} "
            "(component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
        )
        newer.execute(
            f"INSERT INTO {DATABASE_SCHEMA_TABLE} VALUES (?, ?)",
            (DATABASE_SCHEMA_COMPONENT, latest_version + 1),
        )
        newer.commit()
        newer.close()
        return version

    monkeypatch.setattr(database, "require_supported_database_path", upgrade_after_probe)

    with pytest.raises(RuntimeError, match="newer than supported"):
        database.init_db(db_path)

    verify = sqlite3.connect(db_path)
    assert verify.execute(
        "SELECT source_site FROM jobs WHERE url=?",
        ("https://example.test/job/race",),
    ).fetchone()[0] is None
    assert str(db_path) not in getattr(database._local, "connections", {})


def test_init_db_bootstrap_failure_rolls_back_all_schema(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "failed-bootstrap.db"
    original = database._DATABASE_MIGRATIONS[0]

    def fail_after_bootstrap(connection: sqlite3.Connection) -> None:
        original(connection)
        raise RuntimeError("injected bootstrap failure")

    monkeypatch.setattr(database, "_DATABASE_MIGRATIONS", (fail_after_bootstrap,))

    with pytest.raises(RuntimeError, match="injected bootstrap failure"):
        database.init_db(db_path)

    verify = sqlite3.connect(db_path)
    assert verify.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall() == []
    assert str(db_path) not in getattr(database._local, "connections", {})


def test_ordered_migration_failure_rolls_back_version_and_schema() -> None:
    connection = sqlite3.connect(":memory:")

    def migration_one(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE stable (value TEXT)")

    run_database_migrations(connection, (migration_one,))

    def migration_two(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE transient (value TEXT)")
        raise RuntimeError("injected migration failure")

    with pytest.raises(RuntimeError, match="injected migration failure"):
        run_database_migrations(connection, (migration_one, migration_two))

    assert read_database_schema_version(connection) == 1
    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transient'"
    ).fetchone() is None
    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stable'"
    ).fetchone() is not None


def test_concurrent_initializers_converge_on_one_current_version(tmp_path) -> None:
    db_path = tmp_path / "concurrent.db"

    def initialize() -> int:
        connection = database.init_db(db_path)
        try:
            return read_database_schema_version(connection)
        finally:
            database.close_connection(db_path)

    with ThreadPoolExecutor(max_workers=3) as pool:
        versions = list(pool.map(lambda _index: initialize(), range(3)))

    assert versions == [database.DATABASE_SCHEMA_VERSION] * 3
    verify = sqlite3.connect(db_path)
    assert verify.execute(
        f"SELECT COUNT(*) FROM {DATABASE_SCHEMA_TABLE} WHERE component=?",
        (DATABASE_SCHEMA_COMPONENT,),
    ).fetchone()[0] == 1
