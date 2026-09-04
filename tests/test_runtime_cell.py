from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path

import pytest

from applypilot.apply.runtime_cell import (
    CODEX_APP_SERVER_REQUIRED_CAPABILITIES,
    RUNTIME_CELL_CONTEXT_REF_KEYS,
    RuntimeAdapterHealth,
    RuntimeCellExecutionState,
    RuntimeCellHealth,
    RuntimeCellRequest,
    RuntimeCellTurn,
    select_runtime_cell,
)

NO_EFFECTS = RuntimeCellExecutionState(
    request_accepted=False,
    tool_or_effect_started=False,
    submit_started=False,
    bound_backend=None,
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
        execution_state=NO_EFFECTS,
    )

    assert selection.active_backend == "codex-cli"
    assert selection.adapter is None
    assert selection.health.as_dict() == {
        "schema_version": "2",
        "status": "ready",
        "disposition": "execute",
        "requested_backend": "codex-cli",
        "active_backend": "codex-cli",
        "reason_code": "CODEX_APP_SERVER_FEATURE_DISABLED",
        "feature_enabled": False,
        "fallback_used": False,
        "execution_state": {
            "request_accepted": False,
            "tool_or_effect_started": False,
            "submit_started": False,
            "bound_backend": None,
        },
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
        context_refs={"page_observation": "sha256:" + "a" * 64},
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


def test_runtime_cell_request_copies_refs_into_an_immutable_scalar_mapping() -> None:
    before = "sha256:" + "a" * 64
    after = "sha256:" + "b" * 64
    source = {"page_observation": before}
    request = RuntimeCellRequest(
        run_id="turn-immutable",
        actor_id="actor-immutable",
        attempt_id="attempt-immutable",
        phase="prepare",
        prompt="bounded prompt",
        cwd=Path("runtime"),
        model="model",
        context_refs=source,
    )

    source["page_observation"] = after

    assert request.context_refs == {"page_observation": before}
    with pytest.raises(TypeError):
        request.context_refs["page_observation"] = after  # type: ignore[index]


@pytest.mark.parametrize(
    "unsafe_key",
    [
        "submission_gate",
        "submit_authority",
        "ledger_connection",
        "receipt_writer",
        "browser_handle",
        "cookies",
        "path",
        "secrets",
        "PAGE_OBSERVATION",
        "page-observation",
    ],
)
def test_runtime_cell_request_rejects_authority_sensitive_and_alias_keys(
    unsafe_key: str,
) -> None:
    assert unsafe_key not in RUNTIME_CELL_CONTEXT_REF_KEYS

    with pytest.raises(ValueError, match="key is not allowed"):
        RuntimeCellRequest(
            run_id="turn-unsafe",
            actor_id="actor-unsafe",
            attempt_id="attempt-unsafe",
            phase="prepare",
            prompt="bounded prompt",
            cwd=Path("runtime"),
            model="model",
            context_refs={unsafe_key: "sha256:" + "a" * 64},
        )


def test_runtime_cell_request_rejects_nested_refs_and_raw_paths() -> None:
    with pytest.raises(TypeError, match="content-addressed references"):
        RuntimeCellRequest(
            run_id="turn-nested",
            actor_id="actor-nested",
            attempt_id="attempt-nested",
            phase="prepare",
            prompt="bounded prompt",
            cwd=Path("runtime"),
            model="model",
            context_refs={
                "page_observation": {"submission_gate": "claim"}  # type: ignore[dict-item]
            },
        )

    with pytest.raises(ValueError, match="content-addressed references"):
        RuntimeCellRequest(
            run_id="turn-path",
            actor_id="actor-path",
            attempt_id="attempt-path",
            phase="prepare",
            prompt="bounded prompt",
            cwd=Path("runtime"),
            model="model",
            context_refs={"material_manifest": "C:\\private\\resume.pdf"},
        )


def test_runtime_cell_request_rejects_string_subclass_aliases() -> None:
    class Alias(str):
        pass

    with pytest.raises(ValueError, match="key is not allowed"):
        RuntimeCellRequest(
            run_id="turn-alias",
            actor_id="actor-alias",
            attempt_id="attempt-alias",
            phase="prepare",
            prompt="bounded prompt",
            cwd=Path("runtime"),
            model="model",
            context_refs={Alias("page_observation"): "sha256:" + "a" * 64},
        )


def test_enabled_app_server_without_adapter_degrades_to_subprocess() -> None:
    selection = select_runtime_cell(
        "codex",
        codex_app_server_enabled=True,
        execution_state=NO_EFFECTS,
    )

    assert selection.active_backend == "codex-cli"
    assert selection.adapter is None
    assert selection.health.status == "degraded"
    assert selection.health.disposition == "fallback"
    assert selection.health.fallback_used is True
    assert selection.health.reason_code == "CODEX_APP_SERVER_ADAPTER_UNAVAILABLE"
    assert set(selection.health.missing_capabilities) == (CODEX_APP_SERVER_REQUIRED_CAPABILITIES)


@pytest.mark.parametrize(
    ("execution_state", "disposition", "reason_code"),
    [
        (
            RuntimeCellExecutionState(
                request_accepted=True,
                tool_or_effect_started=False,
                submit_started=False,
                bound_backend="codex-app-server",
            ),
            "park",
            "CODEX_APP_SERVER_REQUEST_ACCEPTED_PARKED",
        ),
        (
            RuntimeCellExecutionState(
                request_accepted=False,
                tool_or_effect_started=True,
                submit_started=False,
                bound_backend="codex-app-server",
            ),
            "park",
            "CODEX_APP_SERVER_EFFECT_STARTED_PARKED",
        ),
        (
            RuntimeCellExecutionState(
                request_accepted=False,
                tool_or_effect_started=False,
                submit_started=True,
                bound_backend="codex-app-server",
            ),
            "receipt_only",
            "CODEX_APP_SERVER_SUBMIT_STARTED_RECEIPT_ONLY",
        ),
    ],
)
def test_app_server_unavailable_after_acceptance_or_effect_never_falls_back(
    execution_state: RuntimeCellExecutionState,
    disposition: str,
    reason_code: str,
) -> None:
    selection = select_runtime_cell(
        "codex",
        codex_app_server_enabled=True,
        execution_state=execution_state,
    )

    assert selection.active_backend == "codex-app-server"
    assert selection.adapter is None
    assert selection.can_start is False
    assert selection.health.status == "unavailable"
    assert selection.health.disposition == disposition
    assert selection.health.reason_code == reason_code
    assert selection.health.fallback_used is False


def test_healthy_app_server_after_acceptance_continues_without_new_start() -> None:
    adapter = _AppServerAdapter(
        RuntimeAdapterHealth(
            backend="codex-app-server",
            status="ready",
            reason_code="CODEX_APP_SERVER_READY",
            capabilities=CODEX_APP_SERVER_REQUIRED_CAPABILITIES,
        )
    )
    accepted = RuntimeCellExecutionState(
        request_accepted=True,
        tool_or_effect_started=False,
        submit_started=False,
        bound_backend="codex-app-server",
    )

    selection = select_runtime_cell(
        "codex",
        codex_app_server_enabled=True,
        execution_state=accepted,
        codex_app_server_adapter=adapter,
    )

    assert selection.active_backend == "codex-app-server"
    assert selection.adapter is adapter
    assert selection.health.disposition == "continue"
    assert selection.can_start is False


def test_disabling_feature_after_app_server_acceptance_cannot_switch_to_cli() -> None:
    accepted = RuntimeCellExecutionState(
        request_accepted=True,
        tool_or_effect_started=False,
        submit_started=False,
        bound_backend="codex-app-server",
    )

    selection = select_runtime_cell(
        "codex",
        codex_app_server_enabled=False,
        execution_state=accepted,
    )

    assert selection.active_backend == "codex-app-server"
    assert selection.health.disposition == "park"
    assert selection.health.feature_enabled is False
    assert selection.health.fallback_used is False
    assert selection.can_start is False


def test_enabling_app_server_after_cli_acceptance_cannot_switch_runtime() -> None:
    accepted = RuntimeCellExecutionState(
        request_accepted=True,
        tool_or_effect_started=False,
        submit_started=False,
        bound_backend="codex-cli",
    )
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
        execution_state=accepted,
        codex_app_server_adapter=adapter,
    )

    assert selection.active_backend == "codex-cli"
    assert selection.adapter is None
    assert selection.health.disposition == "continue"
    assert selection.health.reason_code == "RUNTIME_BACKEND_ALREADY_BOUND"
    assert selection.can_start is False


def test_runtime_execution_state_rejects_truthy_non_booleans() -> None:
    with pytest.raises(TypeError, match="request_accepted must be bool"):
        RuntimeCellExecutionState(
            request_accepted=1,  # type: ignore[arg-type]
            tool_or_effect_started=False,
            submit_started=False,
            bound_backend=None,
        )

    with pytest.raises(ValueError, match="require a bound backend"):
        RuntimeCellExecutionState(
            request_accepted=True,
            tool_or_effect_started=False,
            submit_started=False,
            bound_backend=None,
        )


def test_runtime_health_rejects_fallback_after_request_acceptance() -> None:
    accepted = RuntimeCellExecutionState(
        request_accepted=True,
        tool_or_effect_started=False,
        submit_started=False,
        bound_backend="codex-app-server",
    )

    with pytest.raises(ValueError, match="fallback requires a pristine"):
        RuntimeCellHealth(
            status="degraded",
            disposition="fallback",
            requested_backend="codex-app-server",
            active_backend="codex-cli",
            reason_code="CODEX_APP_SERVER_ADAPTER_UNAVAILABLE",
            feature_enabled=True,
            fallback_used=True,
            execution_state=accepted,
        )


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
        execution_state=NO_EFFECTS,
        codex_app_server_adapter=adapter,
    )

    assert selection.active_backend == "codex-app-server"
    assert selection.adapter is adapter
    assert selection.health.status == "ready"
    assert selection.health.disposition == "execute"
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
        execution_state=NO_EFFECTS,
        codex_app_server_adapter=adapter,
    )

    assert selection.active_backend == "codex-cli"
    assert selection.adapter is None
    assert selection.health.status == "degraded"
    assert selection.health.disposition == "fallback"
    assert selection.health.reason_code == "CODEX_APP_SERVER_CAPABILITIES_INCOMPLETE"
    assert selection.health.missing_capabilities == (
        "thread/resume",
        "turn/interrupt",
    )


def test_claude_runtime_is_unchanged_when_codex_flag_is_enabled() -> None:
    selection = select_runtime_cell(
        "claude",
        codex_app_server_enabled=True,
        execution_state=NO_EFFECTS,
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
        execution_state=NO_EFFECTS,
        codex_app_server_adapter=adapter,
    )

    encoded = selection.health.as_dict()
    assert selection.active_backend == "codex-cli"
    assert encoded["reason_code"] == "CODEX_APP_SERVER_HEALTH_PROBE_FAILED"
    assert "secret-token" not in str(encoded)
