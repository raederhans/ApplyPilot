from __future__ import annotations

import pytest

from applypilot.runtime_settings import load_runtime_settings


def test_runtime_settings_preserve_established_defaults() -> None:
    settings = load_runtime_settings({})

    assert settings.resolve_apply_backend() == "codex"
    assert settings.resolve_browser_backend() == "edge"
    assert settings.resolve_interaction_mode() == "auto"
    assert settings.resolve_model("codex") == "gpt-5.6-sol"
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


def test_runtime_settings_keep_backend_and_browser_validation_contracts() -> None:
    settings = load_runtime_settings({
        "APPLYPILOT_APPLY_BACKEND": "unsupported",
        "APPLYPILOT_BROWSER_BACKEND": "unsupported",
    })

    with pytest.raises(ValueError, match="must be codex or claude"):
        settings.resolve_apply_backend()
    assert settings.resolve_apply_backend(fallback_invalid=True) == "codex"
    with pytest.raises(ValueError, match="browser backend must be edge, cloak, or auto"):
        settings.resolve_browser_backend()
