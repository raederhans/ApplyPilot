"""Feature-gated Agent runtime host selection.

The Runtime Cell owns only the choice of host for one Agent turn.  Browser
leases, page-write ownership, submission admission, and receipt observation
remain launcher/domain concerns and are deliberately absent from this module.

The Codex App Server seam is intentionally transport-neutral.  A concrete
adapter may use stdio, a local socket, or another supported transport, but it
must prove the minimum lifecycle surface before the cell can select it.  Until
such an adapter is installed, the established CLI subprocess remains active.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

RuntimeBackend = Literal["codex-app-server", "codex-cli", "claude-cli"]
RuntimeHealthStatus = Literal["ready", "degraded", "unavailable"]

CODEX_APP_SERVER_REQUIRED_CAPABILITIES = frozenset(
    {
        "initialize",
        "thread/start",
        "thread/resume",
        "turn/start",
        "turn/interrupt",
    }
)


def _validate_reason_code(value: str) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 80
        or value != value.upper()
        or not value.replace("_", "").isalnum()
    ):
        raise ValueError("runtime health reason_code must be a stable symbolic code")


@dataclass(frozen=True, slots=True)
class RuntimeCellRequest:
    """Framework-neutral input for one future runtime adapter turn.

    The request carries identity, prompt, and immutable context references.  It
    intentionally carries no browser handle, SubmissionGate claim, ledger
    connection, receipt writer, or other host-side authority.
    """

    run_id: str
    actor_id: str
    attempt_id: str
    phase: str
    prompt: str
    cwd: Path
    model: str
    context_refs: Mapping[str, str]
    parent_provider_session_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("run_id", "actor_id", "attempt_id", "phase", "model"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        if self.parent_provider_session_id is not None and not (
            isinstance(self.parent_provider_session_id, str) and self.parent_provider_session_id.strip()
        ):
            raise ValueError("parent_provider_session_id must be non-empty when provided")
        if any(
            not isinstance(key, str) or not key.strip() or not isinstance(value, str) or not value.strip()
            for key, value in self.context_refs.items()
        ):
            raise ValueError("context_refs must contain non-empty string pairs")


@dataclass(frozen=True, slots=True)
class RuntimeCellTurn:
    """Provider identity returned by a concrete runtime adapter."""

    backend: RuntimeBackend
    provider_session_id: str
    provider_turn_id: str
    events: Iterator[Mapping[str, object]]


@dataclass(frozen=True, slots=True)
class RuntimeAdapterHealth:
    """Bounded, secret-free health report from a runtime adapter."""

    backend: RuntimeBackend
    status: Literal["ready", "unavailable"]
    reason_code: str
    capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        _validate_reason_code(self.reason_code)
        if any(not isinstance(value, str) or not value.strip() for value in self.capabilities):
            raise ValueError("runtime adapter capabilities must be non-empty strings")


@runtime_checkable
class RuntimeCellAdapter(Protocol):
    """Replaceable host adapter; concrete protocols stay outside domain code."""

    backend: RuntimeBackend

    def health(self) -> RuntimeAdapterHealth: ...

    def start(self, request: RuntimeCellRequest) -> RuntimeCellTurn: ...

    def resume(self, request: RuntimeCellRequest) -> RuntimeCellTurn: ...

    def cancel(self, provider_turn_id: str) -> None: ...

    def close_application(self, provider_session_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class RuntimeCellHealth:
    """Resolved runtime and explicit degradation/fallback evidence."""

    status: RuntimeHealthStatus
    requested_backend: RuntimeBackend
    active_backend: RuntimeBackend
    reason_code: str
    feature_enabled: bool
    fallback_used: bool
    missing_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_reason_code(self.reason_code)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "1",
            "status": self.status,
            "requested_backend": self.requested_backend,
            "active_backend": self.active_backend,
            "reason_code": self.reason_code,
            "feature_enabled": self.feature_enabled,
            "fallback_used": self.fallback_used,
            "missing_capabilities": list(self.missing_capabilities),
        }


@dataclass(frozen=True, slots=True)
class RuntimeCellSelection:
    """Selected host plus the evidence used to select it."""

    health: RuntimeCellHealth
    adapter: RuntimeCellAdapter | None = None

    @property
    def active_backend(self) -> RuntimeBackend:
        return self.health.active_backend


def select_runtime_cell(
    agent_backend: str,
    *,
    codex_app_server_enabled: bool,
    codex_app_server_adapter: RuntimeCellAdapter | None = None,
) -> RuntimeCellSelection:
    """Select one runtime host without changing application authority.

    The App Server path is opt-in and capability-gated.  Missing, unhealthy, or
    incomplete adapters degrade to the existing CLI subprocess.  Probe
    exceptions are intentionally reduced to a stable reason code so health
    metadata cannot leak endpoint details or credentials.
    """

    normalized_backend = agent_backend.strip().casefold()
    if normalized_backend not in {"codex", "claude"}:
        raise ValueError("agent backend must be 'codex' or 'claude'")
    cli_backend: RuntimeBackend = "codex-cli" if normalized_backend == "codex" else "claude-cli"
    if normalized_backend != "codex":
        return RuntimeCellSelection(
            RuntimeCellHealth(
                status="ready",
                requested_backend=cli_backend,
                active_backend=cli_backend,
                reason_code="CODEX_APP_SERVER_NOT_APPLICABLE",
                feature_enabled=codex_app_server_enabled,
                fallback_used=False,
            )
        )
    if not codex_app_server_enabled:
        return RuntimeCellSelection(
            RuntimeCellHealth(
                status="ready",
                requested_backend=cli_backend,
                active_backend=cli_backend,
                reason_code="CODEX_APP_SERVER_FEATURE_DISABLED",
                feature_enabled=False,
                fallback_used=False,
            )
        )

    requested: RuntimeBackend = "codex-app-server"
    if codex_app_server_adapter is None:
        return RuntimeCellSelection(
            RuntimeCellHealth(
                status="degraded",
                requested_backend=requested,
                active_backend=cli_backend,
                reason_code="CODEX_APP_SERVER_ADAPTER_UNAVAILABLE",
                feature_enabled=True,
                fallback_used=True,
                missing_capabilities=tuple(sorted(CODEX_APP_SERVER_REQUIRED_CAPABILITIES)),
            )
        )
    if codex_app_server_adapter.backend != requested:
        raise ValueError("Codex App Server adapter declares the wrong backend")
    try:
        adapter_health = codex_app_server_adapter.health()
    except (OSError, RuntimeError, TimeoutError, ValueError):
        return RuntimeCellSelection(
            RuntimeCellHealth(
                status="degraded",
                requested_backend=requested,
                active_backend=cli_backend,
                reason_code="CODEX_APP_SERVER_HEALTH_PROBE_FAILED",
                feature_enabled=True,
                fallback_used=True,
                missing_capabilities=tuple(sorted(CODEX_APP_SERVER_REQUIRED_CAPABILITIES)),
            )
        )
    if adapter_health.backend != requested:
        raise ValueError("Codex App Server health declares the wrong backend")
    missing = tuple(sorted(CODEX_APP_SERVER_REQUIRED_CAPABILITIES - adapter_health.capabilities))
    if adapter_health.status != "ready" or missing:
        return RuntimeCellSelection(
            RuntimeCellHealth(
                status="degraded",
                requested_backend=requested,
                active_backend=cli_backend,
                reason_code=("CODEX_APP_SERVER_CAPABILITIES_INCOMPLETE" if missing else adapter_health.reason_code),
                feature_enabled=True,
                fallback_used=True,
                missing_capabilities=missing,
            )
        )
    return RuntimeCellSelection(
        RuntimeCellHealth(
            status="ready",
            requested_backend=requested,
            active_backend=requested,
            reason_code=adapter_health.reason_code,
            feature_enabled=True,
            fallback_used=False,
        ),
        adapter=codex_app_server_adapter,
    )


__all__ = [
    "CODEX_APP_SERVER_REQUIRED_CAPABILITIES",
    "RuntimeAdapterHealth",
    "RuntimeCellAdapter",
    "RuntimeCellHealth",
    "RuntimeCellRequest",
    "RuntimeCellSelection",
    "RuntimeCellTurn",
    "select_runtime_cell",
]
