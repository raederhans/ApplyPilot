"""Provider-neutral model and reasoning configuration, without process I/O."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass

from applypilot.apply.runtime_cell import REASONING_EFFORT_VALUES


@dataclass(frozen=True, slots=True)
class ReasoningEffortResolution:
    """Final effort and the exact precedence key that selected it."""

    value: str
    source: str
    workload_class: str
    source_key: str

    def as_dict(self) -> dict[str, str]:
        return {
            "reasoning_effort": self.value,
            "reasoning_effort_source": self.source,
            "workload_class": self.workload_class,
            "reasoning_effort_source_key": self.source_key,
        }


@dataclass(frozen=True, slots=True)
class AgentRuntimeConfiguration:
    """One resolved model/effort pair shared by CLI and App Server."""

    backend: str
    model: str
    model_source: str
    reasoning: ReasoningEffortResolution

    def __post_init__(self) -> None:
        if self.backend not in {"codex", "claude"}:
            raise ValueError("agent backend must be 'codex' or 'claude'")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("resolved agent model must be non-empty")
        if not isinstance(self.model_source, str) or not self.model_source.strip():
            raise ValueError("model_source must be non-empty")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "1",
            "backend": self.backend,
            "model": self.model,
            "model_source": self.model_source,
            **self.reasoning.as_dict(),
            "reasoning_effort_applied": self.backend == "codex",
        }


def _validated_reasoning_mapping(
    value: Mapping[object, object] | None,
    *,
    source: str,
) -> dict[str, str]:
    validated: dict[str, str] = {}
    for raw_key, raw_effort in (value or {}).items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise ValueError(f"{source} reasoning effort keys must be non-empty strings")
        if not isinstance(raw_effort, str):
            raise ValueError(f"{source} reasoning effort values must be strings")  # noqa: TRY004 - configuration errors preserve the ValueError contract
        effort = raw_effort.strip().casefold()
        if effort not in REASONING_EFFORT_VALUES:
            allowed = ", ".join(sorted(REASONING_EFFORT_VALUES))
            raise ValueError(f"unsupported reasoning effort {raw_effort!r}; expected one of {allowed}")
        validated[raw_key.strip()] = effort
    return validated


def resolve_reasoning_effort_configuration(
    workload_class: str | None = None,
    *,
    configured: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> ReasoningEffortResolution:
    """Resolve effort with profile > environment > established-high precedence."""

    environment = os.environ if environ is None else environ
    workload = (workload_class or "default").strip()
    if not workload:
        raise ValueError("workload_class must be non-empty when provided")
    raw_environment_mapping = environment.get("APPLYPILOT_REASONING_EFFORTS")
    parsed_environment: object = {}
    if raw_environment_mapping:
        parsed_environment = json.loads(raw_environment_mapping)
        if not isinstance(parsed_environment, dict):
            raise ValueError("APPLYPILOT_REASONING_EFFORTS must be a JSON object")
    environment_mapping = _validated_reasoning_mapping(
        parsed_environment,
        source="environment",
    )
    configured_mapping = _validated_reasoning_mapping(
        configured,
        source="profile",
    )
    for source, mapping in (
        ("profile", configured_mapping),
        ("environment", environment_mapping),
    ):
        if workload in mapping:
            return ReasoningEffortResolution(mapping[workload], source, workload, workload)
    for source, mapping in (
        ("profile", configured_mapping),
        ("environment", environment_mapping),
    ):
        if "default" in mapping:
            return ReasoningEffortResolution(mapping["default"], source, workload, "default")
    return ReasoningEffortResolution("high", "default", workload, "default")


def resolve_reasoning_effort(
    workload_class: str | None = None,
    *,
    configured: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve a configurable workload effort while preserving the old default."""
    return resolve_reasoning_effort_configuration(
        workload_class,
        configured=configured,
        environ=environ,
    ).value


def resolve_agent_runtime_configuration(
    backend: str,
    model: str,
    *,
    workload_class: str | None = None,
    reasoning_efforts: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
    model_source: str = "launcher_argument",
) -> AgentRuntimeConfiguration:
    """Resolve the immutable configuration consumed by every runtime host."""

    normalized_backend = backend.strip().casefold()
    return AgentRuntimeConfiguration(
        backend=normalized_backend,
        model=model.strip() if isinstance(model, str) else model,
        model_source=model_source,
        reasoning=resolve_reasoning_effort_configuration(
            workload_class,
            configured=reasoning_efforts,
            environ=environ,
        ),
    )

