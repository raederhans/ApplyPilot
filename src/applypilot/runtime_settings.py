"""Per-command ApplyPilot runtime settings resolved from an environment snapshot."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ApplyRuntimeSettings:
    """Typed runtime knobs without import-time environment reads."""

    environ: Mapping[str, str] = field(repr=False)

    def raw_apply_backend(self, override: str | None = None) -> str:
        """Return the normalized backend token before policy validation."""
        return (override or self.environ.get("APPLYPILOT_APPLY_BACKEND", "codex")).strip().casefold()

    def resolve_apply_backend(
        self,
        override: str | None = None,
        *,
        fallback_invalid: bool = False,
    ) -> str:
        backend = self.raw_apply_backend(override)
        if backend in {"codex", "claude"}:
            return backend
        if fallback_invalid:
            return "codex"
        raise ValueError("APPLYPILOT_APPLY_BACKEND must be codex or claude")

    def resolve_browser_backend(
        self,
        override: str | None = None,
        *,
        allow_auto: bool = True,
    ) -> str:
        backend = (override or self.environ.get("APPLYPILOT_BROWSER_BACKEND", "edge")).strip().lower()
        allowed = {"edge", "cloak"} | ({"auto"} if allow_auto else set())
        if backend not in allowed:
            expected = "edge, cloak, or auto" if allow_auto else "edge or cloak"
            raise ValueError(f"browser backend must be {expected}")
        return backend

    def resolve_interaction_mode(self, override: str | None = None) -> str:
        mode = (override or self.environ.get("APPLYPILOT_INTERACTION_MODE", "auto")).strip().lower()
        if mode not in {"auto", "playwright"}:
            raise ValueError("interaction mode must be auto or playwright")
        return mode

    def resolve_model(self, backend: str, override: str | None = None) -> str:
        if override:
            return override
        if backend == "codex":
            return self.environ.get("APPLYPILOT_CODEX_MODEL", "gpt-5.6-sol")
        return self.environ.get("APPLYPILOT_CLAUDE_MODEL", "opus")

    @property
    def codex_app_server_enabled(self) -> bool:
        """Return the explicit App Server feature flag, defaulting off."""
        raw = self.environ.get(
            "APPLYPILOT_CODEX_APP_SERVER_ENABLED", "0"
        ).strip().casefold()
        if raw in {"1", "true", "yes", "on"}:
            return True
        if raw in {"0", "false", "no", "off", ""}:
            return False
        raise ValueError(
            "APPLYPILOT_CODEX_APP_SERVER_ENABLED must be a boolean flag"
        )

    @property
    def agent_timeout_seconds(self) -> int:
        return int(self.environ.get("APPLYPILOT_AGENT_TIMEOUT_SECONDS", "300"))

    @property
    def application_lease_minutes(self) -> int:
        configured = int(self.environ.get("APPLYPILOT_APPLICATION_LEASE_MINUTES", "45"))
        timeout_floor = max(7, (self.agent_timeout_seconds + 179) // 60)
        return max(timeout_floor, configured)


def load_runtime_settings(
    environ: Mapping[str, str] | None = None,
) -> ApplyRuntimeSettings:
    """Capture one stable environment snapshot for a command or worker action."""
    return ApplyRuntimeSettings(dict(os.environ if environ is None else environ))
