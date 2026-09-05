"""Per-command ApplyPilot runtime settings resolved from an environment snapshot."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

_PROVIDER_RECIPE_SHADOW_PROVIDERS = frozenset(
    {"greenhouse", "smartrecruiters", "workday"}
)


@dataclass(frozen=True, slots=True)
class ResolvedRuntimeModel:
    """Final runtime model plus its precedence layer."""

    backend: str
    value: str
    source: str

    def as_dict(self) -> dict[str, str]:
        return {
            "backend": self.backend,
            "model": self.value,
            "model_source": self.source,
        }


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

    def resolve_model_configuration(
        self,
        backend: str,
        override: str | None = None,
    ) -> ResolvedRuntimeModel:
        """Resolve one model and retain the non-secret configuration source."""

        normalized_backend = backend.strip().casefold()
        if normalized_backend not in {"codex", "claude"}:
            raise ValueError("agent backend must be 'codex' or 'claude'")
        if override:
            value = override.strip()
            source = "override"
        else:
            environment_key = (
                "APPLYPILOT_CODEX_MODEL"
                if normalized_backend == "codex"
                else "APPLYPILOT_CLAUDE_MODEL"
            )
            if environment_key in self.environ:
                value = self.environ[environment_key].strip()
                source = "environment"
            else:
                value = "gpt-5.6-sol" if normalized_backend == "codex" else "opus"
                source = "default"
        if not value:
            raise ValueError("resolved agent model must be non-empty")
        return ResolvedRuntimeModel(
            backend=normalized_backend,
            value=value,
            source=source,
        )

    def resolve_model(self, backend: str, override: str | None = None) -> str:
        return self.resolve_model_configuration(backend, override).value

    @property
    def codex_app_server_mode(self) -> str:
        """Return the App Server rollout mode with legacy boolean compatibility."""

        raw_mode = self.environ.get("APPLYPILOT_CODEX_APP_SERVER_MODE")
        if raw_mode is not None:
            mode = raw_mode.strip().casefold()
            if mode not in {"off", "shadow", "canary"}:
                raise ValueError(
                    "APPLYPILOT_CODEX_APP_SERVER_MODE must be off, shadow, or canary"
                )
            return mode

        raw = self.environ.get(
            "APPLYPILOT_CODEX_APP_SERVER_ENABLED", "0"
        ).strip().casefold()
        if raw in {"1", "true", "yes", "on"}:
            return "shadow"
        if raw in {"0", "false", "no", "off", ""}:
            return "off"
        raise ValueError(
            "APPLYPILOT_CODEX_APP_SERVER_ENABLED must be a boolean flag"
        )

    @property
    def codex_app_server_enabled(self) -> bool:
        """Return whether either additive App Server rollout lane is enabled."""

        return self.codex_app_server_mode != "off"

    @property
    def runtime_cell_mode(self) -> str:
        """Return the gated Runtime Cell rollout mode, defaulting fully off."""

        mode = self.environ.get("APPLYPILOT_RUNTIME_CELL_MODE", "off").strip().casefold()
        if mode not in {"off", "shadow", "canary"}:
            raise ValueError(
                "APPLYPILOT_RUNTIME_CELL_MODE must be off, shadow, or canary"
            )
        return mode

    @property
    def runtime_cell_admission_manifest(self) -> Path | None:
        """Return an explicit manifest path; absence can never enable two Cells."""

        raw = self.environ.get("APPLYPILOT_RUNTIME_CELL_ADMISSION_MANIFEST", "").strip()
        return Path(raw).expanduser().resolve() if raw else None

    @property
    def semantic_batch_mode(self) -> str:
        """Return the routine-field batch rollout mode, defaulting fully off."""

        mode = self.environ.get("APPLYPILOT_SEMANTIC_BATCH_MODE", "off").strip().casefold()
        if mode not in {"off", "shadow", "canary"}:
            raise ValueError(
                "APPLYPILOT_SEMANTIC_BATCH_MODE must be off, shadow, or canary"
            )
        return mode

    @property
    def provider_recipe_shadow_providers(self) -> tuple[str, ...]:
        """Return independently admitted providers for read-only recipe shadowing."""

        raw = self.environ.get("APPLYPILOT_PROVIDER_RECIPE_SHADOW_PROVIDERS", "")
        if not raw.strip():
            return ()
        providers = tuple(item.strip().casefold() for item in raw.split(","))
        if any(not item for item in providers):
            raise ValueError(
                "APPLYPILOT_PROVIDER_RECIPE_SHADOW_PROVIDERS must be a comma-separated provider list"
            )
        if len(providers) != len(set(providers)):
            raise ValueError(
                "APPLYPILOT_PROVIDER_RECIPE_SHADOW_PROVIDERS must not contain duplicates"
            )
        unknown = set(providers) - _PROVIDER_RECIPE_SHADOW_PROVIDERS
        if unknown:
            raise ValueError(
                "APPLYPILOT_PROVIDER_RECIPE_SHADOW_PROVIDERS supports only "
                "greenhouse, smartrecruiters, and workday"
            )
        return providers

    @property
    def application_plan_shadow_enabled(self) -> bool:
        """Return the ref-only ApplicationPlan canary flag, defaulting off."""
        raw = self.environ.get(
            "APPLYPILOT_APPLICATION_PLAN_SHADOW_ENABLED", "0"
        ).strip().casefold()
        if raw in {"1", "true", "yes", "on"}:
            return True
        if raw in {"0", "false", "no", "off", ""}:
            return False
        raise ValueError(
            "APPLYPILOT_APPLICATION_PLAN_SHADOW_ENABLED must be a boolean flag"
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
