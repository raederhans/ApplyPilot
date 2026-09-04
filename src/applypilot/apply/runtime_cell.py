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
from types import MappingProxyType
from typing import Literal, Protocol, runtime_checkable

RuntimeBackend = Literal["codex-app-server", "codex-cli", "claude-cli"]
RuntimeHealthStatus = Literal["ready", "degraded", "unavailable"]
RuntimeCellDisposition = Literal[
    "execute",
    "fallback",
    "continue",
    "park",
    "receipt_only",
]

CODEX_APP_SERVER_REQUIRED_CAPABILITIES = frozenset(
    {
        "initialize",
        "thread/start",
        "thread/resume",
        "turn/start",
        "turn/interrupt",
    }
)
RUNTIME_CELL_CONTEXT_REF_KEYS = frozenset(
    {
        "actor_checkpoint",
        "application_context",
        "ats_context",
        "material_manifest",
        "page_observation",
        "prompt_contract",
        "runtime_namespace",
        "tool_surface",
    }
)
RUNTIME_CELL_CONTEXT_REF_SCHEMES = frozenset({"sha256"})


def _validate_reason_code(value: str) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 80
        or value != value.upper()
        or not value.replace("_", "").isalnum()
    ):
        raise ValueError("runtime health reason_code must be a stable symbolic code")


def _validate_context_ref(value: str) -> None:
    if type(value) is not str:
        raise TypeError("context_refs values must be content-addressed references")
    scheme, separator, opaque_id = value.partition(":")
    if (
        separator != ":"
        or scheme not in RUNTIME_CELL_CONTEXT_REF_SCHEMES
        or len(opaque_id) != 64
        or any(character not in "0123456789abcdef" for character in opaque_id)
    ):
        raise ValueError("context_refs values must be content-addressed references")


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
        frozen_refs: dict[str, str] = {}
        for key, value in self.context_refs.items():
            if type(key) is not str or key not in RUNTIME_CELL_CONTEXT_REF_KEYS:
                raise ValueError(f"context_refs key is not allowed: {key!r}")
            _validate_context_ref(value)
            frozen_refs[key] = value
        object.__setattr__(self, "context_refs", MappingProxyType(frozen_refs))


@dataclass(frozen=True, slots=True)
class RuntimeCellExecutionState:
    """Host-observed effect boundary used to decide whether fallback is safe."""

    request_accepted: bool
    tool_or_effect_started: bool
    submit_started: bool
    bound_backend: RuntimeBackend | None

    def __post_init__(self) -> None:
        for name in ("request_accepted", "tool_or_effect_started", "submit_started"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be bool")
        if self.bound_backend not in {None, "codex-app-server", "codex-cli", "claude-cli"}:
            raise ValueError("bound_backend is not a supported runtime backend")
        if self.fallback_safe and self.bound_backend is not None:
            raise ValueError("a pristine request cannot have a bound backend")
        if not self.fallback_safe and self.bound_backend is None:
            raise ValueError("accepted requests and started effects require a bound backend")

    @property
    def fallback_safe(self) -> bool:
        return not (self.request_accepted or self.tool_or_effect_started or self.submit_started)

    def as_dict(self) -> dict[str, object]:
        return {
            "request_accepted": self.request_accepted,
            "tool_or_effect_started": self.tool_or_effect_started,
            "submit_started": self.submit_started,
            "bound_backend": self.bound_backend,
        }


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
    disposition: RuntimeCellDisposition
    requested_backend: RuntimeBackend
    active_backend: RuntimeBackend
    reason_code: str
    feature_enabled: bool
    fallback_used: bool
    execution_state: RuntimeCellExecutionState
    missing_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_reason_code(self.reason_code)
        if tuple(sorted(set(self.missing_capabilities))) != self.missing_capabilities or not set(
            self.missing_capabilities
        ).issubset(CODEX_APP_SERVER_REQUIRED_CAPABILITIES):
            raise ValueError("missing_capabilities must be a canonical required subset")
        if self.disposition == "fallback":
            if (
                not self.execution_state.fallback_safe
                or not self.fallback_used
                or self.status != "degraded"
                or self.requested_backend != "codex-app-server"
                or self.active_backend != "codex-cli"
            ):
                raise ValueError("fallback requires a pristine App Server request state")
        elif self.fallback_used:
            raise ValueError("fallback_used requires fallback disposition")
        if self.disposition != "fallback" and self.requested_backend != self.active_backend:
            raise ValueError("non-fallback runtime selection cannot switch backends")
        if self.disposition == "execute" and not self.execution_state.fallback_safe:
            raise ValueError("execute requires a pristine runtime request state")
        if self.disposition == "continue" and self.execution_state.fallback_safe:
            raise ValueError("continue requires an accepted request or started effect")
        if self.disposition == "park" and (self.execution_state.fallback_safe or self.execution_state.submit_started):
            raise ValueError("park requires a non-submit accepted request or effect")
        if self.disposition == "receipt_only" and not self.execution_state.submit_started:
            raise ValueError("receipt_only requires submit_started")
        if self.disposition in {"park", "receipt_only"} and (
            self.requested_backend != "codex-app-server" or self.active_backend != "codex-app-server"
        ):
            raise ValueError("parked App Server work must keep its original backend")
        expected_status = {
            "execute": "ready",
            "continue": "ready",
            "fallback": "degraded",
            "park": "unavailable",
            "receipt_only": "unavailable",
        }[self.disposition]
        if self.status != expected_status:
            raise ValueError("runtime health status does not match disposition")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "2",
            "status": self.status,
            "disposition": self.disposition,
            "requested_backend": self.requested_backend,
            "active_backend": self.active_backend,
            "reason_code": self.reason_code,
            "feature_enabled": self.feature_enabled,
            "fallback_used": self.fallback_used,
            "execution_state": self.execution_state.as_dict(),
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

    @property
    def can_start(self) -> bool:
        """Return whether the caller may start exactly one new runtime turn."""
        return self.health.disposition in {"execute", "fallback"}


def _unavailable_selection(
    *,
    execution_state: RuntimeCellExecutionState,
    fallback_reason_code: str,
    feature_enabled: bool,
    missing_capabilities: tuple[str, ...],
) -> RuntimeCellSelection:
    """Fallback only before acceptance/effects; otherwise fail closed."""

    if execution_state.submit_started:
        return RuntimeCellSelection(
            RuntimeCellHealth(
                status="unavailable",
                disposition="receipt_only",
                requested_backend="codex-app-server",
                active_backend="codex-app-server",
                reason_code="CODEX_APP_SERVER_SUBMIT_STARTED_RECEIPT_ONLY",
                feature_enabled=feature_enabled,
                fallback_used=False,
                execution_state=execution_state,
                missing_capabilities=missing_capabilities,
            )
        )
    if execution_state.tool_or_effect_started:
        return RuntimeCellSelection(
            RuntimeCellHealth(
                status="unavailable",
                disposition="park",
                requested_backend="codex-app-server",
                active_backend="codex-app-server",
                reason_code="CODEX_APP_SERVER_EFFECT_STARTED_PARKED",
                feature_enabled=feature_enabled,
                fallback_used=False,
                execution_state=execution_state,
                missing_capabilities=missing_capabilities,
            )
        )
    if execution_state.request_accepted:
        return RuntimeCellSelection(
            RuntimeCellHealth(
                status="unavailable",
                disposition="park",
                requested_backend="codex-app-server",
                active_backend="codex-app-server",
                reason_code="CODEX_APP_SERVER_REQUEST_ACCEPTED_PARKED",
                feature_enabled=feature_enabled,
                fallback_used=False,
                execution_state=execution_state,
                missing_capabilities=missing_capabilities,
            )
        )
    return RuntimeCellSelection(
        RuntimeCellHealth(
            status="degraded",
            disposition="fallback",
            requested_backend="codex-app-server",
            active_backend="codex-cli",
            reason_code=fallback_reason_code,
            feature_enabled=feature_enabled,
            fallback_used=True,
            execution_state=execution_state,
            missing_capabilities=missing_capabilities,
        )
    )


def select_runtime_cell(
    agent_backend: str,
    *,
    codex_app_server_enabled: bool,
    execution_state: RuntimeCellExecutionState,
    codex_app_server_adapter: RuntimeCellAdapter | None = None,
) -> RuntimeCellSelection:
    """Select one runtime host without changing application authority.

    The App Server path is opt-in and capability-gated.  Missing, unhealthy, or
    incomplete adapters degrade to the existing CLI subprocess only before the
    App Server accepted the request and before any tool, effect, or Submit
    started.  Later ambiguity is parked, or made receipt-only after Submit, so
    the selector can never authorize a second runtime execution.  Probe errors
    are reduced to stable codes so metadata cannot leak endpoint details.
    """

    if not isinstance(codex_app_server_enabled, bool):
        raise TypeError("codex_app_server_enabled must be bool")
    normalized_backend = agent_backend.strip().casefold()
    if normalized_backend not in {"codex", "claude"}:
        raise ValueError("agent backend must be 'codex' or 'claude'")
    cli_backend: RuntimeBackend = "codex-cli" if normalized_backend == "codex" else "claude-cli"
    if execution_state.bound_backend == cli_backend:
        return RuntimeCellSelection(
            RuntimeCellHealth(
                status="ready",
                disposition="continue",
                requested_backend=cli_backend,
                active_backend=cli_backend,
                reason_code="RUNTIME_BACKEND_ALREADY_BOUND",
                feature_enabled=codex_app_server_enabled,
                fallback_used=False,
                execution_state=execution_state,
            )
        )
    app_server_already_bound = execution_state.bound_backend == "codex-app-server"
    if execution_state.bound_backend is not None and not app_server_already_bound:
        raise ValueError("bound runtime backend does not match the selected agent backend")
    if app_server_already_bound and normalized_backend != "codex":
        raise ValueError("Codex App Server cannot continue under a non-Codex backend")
    if normalized_backend != "codex":
        return RuntimeCellSelection(
            RuntimeCellHealth(
                status="ready",
                disposition=("execute" if execution_state.fallback_safe else "continue"),
                requested_backend=cli_backend,
                active_backend=cli_backend,
                reason_code="CODEX_APP_SERVER_NOT_APPLICABLE",
                feature_enabled=codex_app_server_enabled,
                fallback_used=False,
                execution_state=execution_state,
            )
        )
    if not codex_app_server_enabled and not app_server_already_bound:
        return RuntimeCellSelection(
            RuntimeCellHealth(
                status="ready",
                disposition=("execute" if execution_state.fallback_safe else "continue"),
                requested_backend=cli_backend,
                active_backend=cli_backend,
                reason_code="CODEX_APP_SERVER_FEATURE_DISABLED",
                feature_enabled=False,
                fallback_used=False,
                execution_state=execution_state,
            )
        )

    requested: RuntimeBackend = "codex-app-server"
    if codex_app_server_adapter is None:
        return _unavailable_selection(
            execution_state=execution_state,
            fallback_reason_code="CODEX_APP_SERVER_ADAPTER_UNAVAILABLE",
            feature_enabled=codex_app_server_enabled,
            missing_capabilities=tuple(sorted(CODEX_APP_SERVER_REQUIRED_CAPABILITIES)),
        )
    if codex_app_server_adapter.backend != requested:
        raise ValueError("Codex App Server adapter declares the wrong backend")
    try:
        adapter_health = codex_app_server_adapter.health()
    except (OSError, RuntimeError, TimeoutError, ValueError):
        return _unavailable_selection(
            execution_state=execution_state,
            fallback_reason_code="CODEX_APP_SERVER_HEALTH_PROBE_FAILED",
            feature_enabled=codex_app_server_enabled,
            missing_capabilities=tuple(sorted(CODEX_APP_SERVER_REQUIRED_CAPABILITIES)),
        )
    if adapter_health.backend != requested:
        raise ValueError("Codex App Server health declares the wrong backend")
    missing = tuple(sorted(CODEX_APP_SERVER_REQUIRED_CAPABILITIES - adapter_health.capabilities))
    if adapter_health.status != "ready" or missing:
        return _unavailable_selection(
            execution_state=execution_state,
            fallback_reason_code=(
                "CODEX_APP_SERVER_CAPABILITIES_INCOMPLETE" if missing else adapter_health.reason_code
            ),
            feature_enabled=codex_app_server_enabled,
            missing_capabilities=missing,
        )
    return RuntimeCellSelection(
        RuntimeCellHealth(
            status="ready",
            disposition=("execute" if execution_state.fallback_safe else "continue"),
            requested_backend=requested,
            active_backend=requested,
            reason_code=adapter_health.reason_code,
            feature_enabled=codex_app_server_enabled,
            fallback_used=False,
            execution_state=execution_state,
        ),
        adapter=codex_app_server_adapter,
    )


__all__ = [
    "CODEX_APP_SERVER_REQUIRED_CAPABILITIES",
    "RUNTIME_CELL_CONTEXT_REF_KEYS",
    "RUNTIME_CELL_CONTEXT_REF_SCHEMES",
    "RuntimeAdapterHealth",
    "RuntimeCellAdapter",
    "RuntimeCellExecutionState",
    "RuntimeCellHealth",
    "RuntimeCellRequest",
    "RuntimeCellSelection",
    "RuntimeCellTurn",
    "select_runtime_cell",
]
