from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path

from applypilot.apply.runtime_cell import (
    CODEX_APP_SERVER_REQUIRED_CAPABILITIES,
    RuntimeAdapterHealth,
    RuntimeCellRequest,
    RuntimeCellTurn,
    select_runtime_cell,
)


@dataclass
class _AppServerAdapter:
    health_result: RuntimeAdapterHealth
    backend: str = "codex-app-server"

    def health(self) -> RuntimeAdapterHealth:
        return self.health_result

    def start(self, request: RuntimeCellRequest) -> RuntimeCellTurn:
        raise AssertionError(f"selection must not start {request.run_id}")

    def resume(self, request: RuntimeCellRequest) -> RuntimeCellTurn:
        raise AssertionError(f"selection must not resume {request.run_id}")

    def cancel(self, provider_turn_id: str) -> None:
        raise AssertionError(f"selection must not cancel {provider_turn_id}")

    def close_application(self, provider_session_id: str) -> None:
        raise AssertionError(f"selection must not close {provider_session_id}")


def test_runtime_cell_keeps_subprocess_when_feature_is_disabled() -> None:
    selection = select_runtime_cell(
        "codex",
        codex_app_server_enabled=False,
    )

    assert selection.active_backend == "codex-cli"
    assert selection.adapter is None
    assert selection.health.as_dict() == {
        "schema_version": "1",
        "status": "ready",
        "requested_backend": "codex-cli",
        "active_backend": "codex-cli",
        "reason_code": "CODEX_APP_SERVER_FEATURE_DISABLED",
        "feature_enabled": False,
        "fallback_used": False,
        "missing_capabilities": [],
    }


def test_runtime_cell_request_carries_no_host_submission_authority() -> None:
    request = RuntimeCellRequest(
        run_id="turn-1",
        actor_id="actor-1",
        attempt_id="attempt-1",
        phase="prepare",
        prompt="bounded prompt",
        cwd=Path("runtime"),
        model="model",
        context_refs={"page_observation": "sha256:abc"},
    )

    assert request.phase == "prepare"
    assert {field.name for field in fields(request)} == {
        "run_id",
        "actor_id",
        "attempt_id",
        "phase",
        "prompt",
        "cwd",
        "model",
        "context_refs",
        "parent_provider_session_id",
    }
    assert {
        "browser_handle",
        "submission_gate",
        "submit_authority",
        "ledger_connection",
        "receipt_writer",
    }.isdisjoint(field.name for field in fields(request))


def test_enabled_app_server_without_adapter_degrades_to_subprocess() -> None:
    selection = select_runtime_cell(
        "codex",
        codex_app_server_enabled=True,
    )

    assert selection.active_backend == "codex-cli"
    assert selection.adapter is None
    assert selection.health.status == "degraded"
    assert selection.health.fallback_used is True
    assert selection.health.reason_code == "CODEX_APP_SERVER_ADAPTER_UNAVAILABLE"
    assert set(selection.health.missing_capabilities) == (CODEX_APP_SERVER_REQUIRED_CAPABILITIES)


def test_capability_complete_app_server_adapter_is_selectable() -> None:
    adapter = _AppServerAdapter(
        RuntimeAdapterHealth(
            backend="codex-app-server",
            status="ready",
            reason_code="CODEX_APP_SERVER_READY",
            capabilities=CODEX_APP_SERVER_REQUIRED_CAPABILITIES,
        )
    )

    selection = select_runtime_cell(
        "codex",
        codex_app_server_enabled=True,
        codex_app_server_adapter=adapter,
    )

    assert selection.active_backend == "codex-app-server"
    assert selection.adapter is adapter
    assert selection.health.status == "ready"
    assert selection.health.fallback_used is False


def test_incomplete_app_server_capabilities_degrade_without_invoking_adapter() -> None:
    adapter = _AppServerAdapter(
        RuntimeAdapterHealth(
            backend="codex-app-server",
            status="ready",
            reason_code="CODEX_APP_SERVER_READY",
            capabilities=frozenset({"initialize", "thread/start", "turn/start"}),
        )
    )

    selection = select_runtime_cell(
        "codex",
        codex_app_server_enabled=True,
        codex_app_server_adapter=adapter,
    )

    assert selection.active_backend == "codex-cli"
    assert selection.adapter is None
    assert selection.health.status == "degraded"
    assert selection.health.reason_code == "CODEX_APP_SERVER_CAPABILITIES_INCOMPLETE"
    assert selection.health.missing_capabilities == (
        "thread/resume",
        "turn/interrupt",
    )


def test_claude_runtime_is_unchanged_when_codex_flag_is_enabled() -> None:
    selection = select_runtime_cell(
        "claude",
        codex_app_server_enabled=True,
    )

    assert selection.active_backend == "claude-cli"
    assert selection.adapter is None
    assert selection.health.status == "ready"
    assert selection.health.fallback_used is False


def test_app_server_probe_failure_degrades_without_leaking_exception_text() -> None:
    adapter = _AppServerAdapter(
        RuntimeAdapterHealth(
            backend="codex-app-server",
            status="ready",
            reason_code="CODEX_APP_SERVER_READY",
            capabilities=CODEX_APP_SERVER_REQUIRED_CAPABILITIES,
        )
    )

    def fail_health() -> RuntimeAdapterHealth:
        raise OSError("secret-token-at-private-endpoint")

    adapter.health = fail_health  # type: ignore[method-assign]
    selection = select_runtime_cell(
        "codex",
        codex_app_server_enabled=True,
        codex_app_server_adapter=adapter,
    )

    encoded = selection.health.as_dict()
    assert selection.active_backend == "codex-cli"
    assert encoded["reason_code"] == "CODEX_APP_SERVER_HEALTH_PROBE_FAILED"
    assert "secret-token" not in str(encoded)
