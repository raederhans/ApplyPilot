from __future__ import annotations

import pytest

from applypilot.commands.apply import (
    _select_runnable_browser_backend,
    _worker_summary_lines,
)
from applypilot.runtime_settings import load_runtime_settings

pytestmark = pytest.mark.compatibility


def test_runtime_settings_preserve_established_defaults() -> None:
    settings = load_runtime_settings({})

    assert settings.resolve_apply_backend() == "codex"
    assert settings.resolve_browser_backend() == "edge"
    assert settings.resolve_interaction_mode() == "auto"
    assert settings.resolve_model("codex") == "gpt-5.6-sol"
    assert settings.codex_app_server_enabled is False
    assert settings.semantic_batch_mode == "off"
    assert settings.agent_timeout_seconds == 300
    assert settings.application_lease_minutes == 45


def test_runtime_settings_are_snapshotted_per_command() -> None:
    environ = {"APPLYPILOT_AGENT_TIMEOUT_SECONDS": "300"}
    first = load_runtime_settings(environ)
    environ["APPLYPILOT_AGENT_TIMEOUT_SECONDS"] = "3600"
    second = load_runtime_settings(environ)

    assert first.agent_timeout_seconds == 300
    assert second.agent_timeout_seconds == 3600
    assert second.application_lease_minutes == 62


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_runtime_settings_enable_codex_app_server_only_explicitly(value: str) -> None:
    settings = load_runtime_settings({"APPLYPILOT_CODEX_APP_SERVER_ENABLED": value})

    assert settings.codex_app_server_enabled is True


def test_runtime_settings_reject_invalid_codex_app_server_flag() -> None:
    settings = load_runtime_settings({"APPLYPILOT_CODEX_APP_SERVER_ENABLED": "sometimes"})

    with pytest.raises(ValueError, match="must be a boolean flag"):
        _ = settings.codex_app_server_enabled


@pytest.mark.parametrize("value", ["off", "SHADOW", "canary"])
def test_runtime_settings_accept_semantic_batch_rollout_modes(value: str) -> None:
    settings = load_runtime_settings({"APPLYPILOT_SEMANTIC_BATCH_MODE": value})

    assert settings.semantic_batch_mode == value.casefold()


def test_runtime_settings_reject_invalid_semantic_batch_mode() -> None:
    settings = load_runtime_settings({"APPLYPILOT_SEMANTIC_BATCH_MODE": "enabled"})

    with pytest.raises(ValueError, match="must be off, shadow, or canary"):
        _ = settings.semantic_batch_mode


def test_runtime_settings_keep_backend_and_browser_validation_contracts() -> None:
    settings = load_runtime_settings(
        {
            "APPLYPILOT_APPLY_BACKEND": "unsupported",
            "APPLYPILOT_BROWSER_BACKEND": "unsupported",
        }
    )

    with pytest.raises(ValueError, match="must be codex or claude"):
        settings.resolve_apply_backend()
    assert settings.resolve_apply_backend(fallback_invalid=True) == "codex"
    with pytest.raises(ValueError, match="browser backend must be edge, cloak, or auto"):
        settings.resolve_browser_backend()


def test_auto_browser_backend_uses_edge_when_optional_cloak_is_unavailable() -> None:
    def resolve(backend: str) -> str:
        if backend == "cloak":
            raise RuntimeError("cloak not installed")
        return "msedge.exe"

    selected, unavailable = _select_runnable_browser_backend("auto", resolve)

    assert selected == "edge"
    assert unavailable == {"cloak": "cloak not installed"}


def test_auto_browser_backend_uses_cloak_when_edge_is_unavailable() -> None:
    def resolve(backend: str) -> str:
        if backend == "edge":
            raise FileNotFoundError("edge not installed")
        return "cloakbrowser.exe"

    selected, unavailable = _select_runnable_browser_backend("auto", resolve)

    assert selected == "cloak"
    assert unavailable == {"edge": "edge not installed"}


def test_auto_browser_backend_preserves_fallback_when_both_are_available() -> None:
    selected, unavailable = _select_runnable_browser_backend("auto", lambda backend: f"{backend}.exe")

    assert selected == "auto"
    assert unavailable == {}


def test_dry_run_worker_summary_preserves_bounded_queue_preview_concurrency() -> None:
    lines, effective_workers = _worker_summary_lines(
        {
            "requested_workers": 4,
            "bound_candidates": 0,
            "executable_candidates": 0,
            "blocked_candidates": 0,
            "effective_workers": 2,
        },
        dry_run=True,
        preview_selection="queue",
        preview_candidates=2,
    )

    assert lines == [
        "  Preview selection:       queue",
        "  Preview candidates cap:  2",
        "  Workers passed to launcher: 2",
    ]
    assert effective_workers == 2
    assert all("Executable" not in line for line in lines)


def test_submission_worker_summary_preserves_manifest_admission_counts() -> None:
    lines, effective_workers = _worker_summary_lines(
        {
            "requested_workers": 4,
            "bound_candidates": 3,
            "executable_candidates": 2,
            "blocked_candidates": 1,
            "effective_workers": 2,
        },
        dry_run=False,
    )

    assert lines == [
        "  Manifest-bound:     3",
        "  Executable:         2",
        "  Blocked:            1",
        "  Workers effective:  2",
    ]
    assert effective_workers == 2
