from __future__ import annotations

import sqlite3

import pytest

from applypilot.storage import runtime_cells, task_journal
from applypilot.storage.transactions import execute_transactional_script, write_transaction


class _Cancelled(BaseException):
    pass


def test_nested_helper_failure_rolls_back_only_its_savepoint() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE values_log (value TEXT)")

    with write_transaction(connection):
        connection.execute("INSERT INTO values_log VALUES ('outer-before')")
        with pytest.raises(RuntimeError, match="injected"), runtime_cells._write(
            connection,
            "runtime_cell_test",
        ):
            connection.execute("INSERT INTO values_log VALUES ('inner')")
            raise RuntimeError("injected")
        connection.execute("INSERT INTO values_log VALUES ('outer-after')")

    assert connection.execute("SELECT value FROM values_log").fetchall() == [
        ("outer-before",),
        ("outer-after",),
    ]


def test_cancellation_rolls_back_owned_write() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE values_log (value TEXT)")
    connection.commit()

    with pytest.raises(_Cancelled), task_journal._write_transaction(connection):
        connection.execute("INSERT INTO values_log VALUES ('cancelled')")
        raise _Cancelled()

    assert connection.execute("SELECT value FROM values_log").fetchall() == []


def test_write_transaction_rejects_unsafe_savepoint_prefix() -> None:
    connection = sqlite3.connect(":memory:")
    with pytest.raises(ValueError, match="safe SQL identifier"), write_transaction(
        connection,
        prefix="unsafe; rollback",
    ):
        pass


def test_transactional_script_does_not_commit_callers_transaction() -> None:
    connection = sqlite3.connect(":memory:")

    with pytest.raises(RuntimeError, match="rollback script"), write_transaction(
        connection
    ):
        execute_transactional_script(
            connection,
            "CREATE TABLE scripted (value TEXT); INSERT INTO scripted VALUES ('x');",
        )
        raise RuntimeError("rollback script")

    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scripted'"
    ).fetchone() is None
