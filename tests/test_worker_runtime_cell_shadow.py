from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from applypilot.apply.worker_runtime_cell_shadow import open_runtime_cell_shadow_session


@dataclass
class _MockSettings:
    runtime_cell_mode: str
    runtime_cell_admission_manifest: str | None = None


@dataclass
class _MockPorts:
    connection_factory: Any
    coordinator_factory: Any
    host_factory: Any
    source_root: Path
    process_identity: Any
    production_enabled: bool = False


def test_open_runtime_cell_shadow_session_returns_none_when_off() -> None:
    settings = _MockSettings(runtime_cell_mode="off")
    assert (
        open_runtime_cell_shadow_session(
            ports=None,
            settings=settings,
            requested_workers=1,
        )
        is None
    )


def test_open_runtime_cell_shadow_session_raises_on_production_enabled(tmp_path: Path) -> None:
    def connection_factory() -> sqlite3.Connection:
        connection = sqlite3.connect(tmp_path / "test.sqlite3")
        connection.row_factory = sqlite3.Row
        return connection

    class MockDecision:
        effective_cells = 1

    class MockCoordinator:
        def __init__(self, *args, **kwargs):
            self.decision = MockDecision()

    settings = _MockSettings(runtime_cell_mode="shadow")
    ports = _MockPorts(
        connection_factory=connection_factory,
        coordinator_factory=MockCoordinator,
        host_factory=lambda **kwargs: None,
        source_root=tmp_path,
        process_identity=lambda: (1, 1),
        production_enabled=True,
    )
    with pytest.raises(RuntimeError, match="production Runtime Cell shadow must remain hard-gated to one Cell"):
        open_runtime_cell_shadow_session(
            ports=ports,
            settings=settings,
            requested_workers=1,
        )


def test_open_runtime_cell_shadow_session_raises_on_multi_cell(tmp_path: Path) -> None:
    def connection_factory() -> sqlite3.Connection:
        connection = sqlite3.connect(tmp_path / "test.sqlite3")
        connection.row_factory = sqlite3.Row
        return connection

    # We create a mock coordinator factory that yields effective_cells = 2
    class MockDecision:
        effective_cells = 2

    class MockCoordinator:
        def __init__(self, *args, **kwargs):
            self.decision = MockDecision()

    settings = _MockSettings(runtime_cell_mode="shadow")
    ports = _MockPorts(
        connection_factory=connection_factory,
        coordinator_factory=MockCoordinator,
        host_factory=lambda **kwargs: None,
        source_root=tmp_path,
        process_identity=lambda: (1, 1),
        production_enabled=False,
    )
    with pytest.raises(RuntimeError, match="production Runtime Cell shadow must remain hard-gated to one Cell"):
        open_runtime_cell_shadow_session(
            ports=ports,
            settings=settings,
            requested_workers=1,
        )


def test_open_runtime_cell_shadow_session_success(tmp_path: Path) -> None:
    def connection_factory() -> sqlite3.Connection:
        connection = sqlite3.connect(tmp_path / "test.sqlite3")
        connection.row_factory = sqlite3.Row
        return connection

    class MockDecision:
        effective_cells = 1

    class MockCoordinator:
        def __init__(self, *args, **kwargs):
            self.decision = MockDecision()

        def register_next(self, *args, **kwargs):
            return "binding"

    settings = _MockSettings(runtime_cell_mode="shadow")
    ports = _MockPorts(
        connection_factory=connection_factory,
        coordinator_factory=MockCoordinator,
        host_factory=lambda **kwargs: "host",
        source_root=tmp_path,
        process_identity=lambda: (1, 1),
        production_enabled=False,
    )

    session = open_runtime_cell_shadow_session(
        ports=ports,
        settings=settings,
        requested_workers=1,
    )
    assert session is not None
    assert session.host == "host"
    assert session.binding == "binding"
