"""Canonical, fail-closed admission for runtime tool surfaces.

The broker is deliberately policy-narrow.  Shadow and advisory modes describe
the existing surface without changing it.  Active mode starts with only
read/report/proposal tools and cannot confer browser-write, submit, credential,
ledger, or mailbox-send authority.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from applypilot.apply.capabilities import CapabilityRegistry, scope_capability_registry
from applypilot.apply.contracts import ToolSpec, contract_json

BROKER_MODES = frozenset({"shadow", "advisory", "active"})
ACTIVE_EFFECT_CLASSES = frozenset({"read", "report", "proposal", "proposal-only"})
ACTIVE_AUTHORITIES = frozenset({"none", "read", "observation", "advisory", "report"})
FORBIDDEN_ACTIVE_NAME_MARKERS = (
    "submit",
    "credential",
    "protected_identifier",
    "ledger",
    "mailbox_send",
    "direct_email_send",
    "email_send",
    "send_email",
    "mail_send",
    "send_mail",
    "browser_click",
    "browser_fill",
    "browser_type",
    "browser_press",
    "browser_select",
    "browser_upload",
    "browser_navigate",
)

NamespaceLoader = Callable[[], Iterable[ToolSpec]]


@dataclass(frozen=True, slots=True)
class ToolAdmission:
    name: str
    mode: str
    admitted: bool
    enforced: bool
    effect_class: str
    reason: str


@dataclass(frozen=True, slots=True)
class ToolSurface:
    registry: CapabilityRegistry
    mode: str
    phase: str
    route: str | None
    provider: str | None
    state: tuple[str, ...]
    surface_hash: str
    loaded_namespaces: tuple[str, ...]

    def names(self) -> list[str]:
        return self.registry.names()

    def restrict_to(self, registry: CapabilityRegistry) -> ToolSurface:
        """Return a narrower surface while preserving the compiled context."""

        current = set(self.registry.names())
        requested = set(registry.names())
        unexpected = sorted(requested.difference(current))
        if unexpected:
            raise ValueError(
                "restricted tool surface cannot add capabilities: "
                + ", ".join(unexpected)
            )
        tools = registry.values()
        return ToolSurface(
            registry=CapabilityRegistry(tools),
            mode=self.mode,
            phase=self.phase,
            route=self.route,
            provider=self.provider,
            state=self.state,
            surface_hash=_surface_digest(
                tools,
                mode=self.mode,
                phase=self.phase,
                route=self.route,
                provider=self.provider,
                state=self.state,
            ),
            loaded_namespaces=self.loaded_namespaces,
        )


def _active_safe(tool: ToolSpec) -> tuple[bool, str]:
    name = tool.name.casefold().replace("-", "_").replace(".", "_").replace(":", "_")
    if any(marker in name for marker in FORBIDDEN_ACTIVE_NAME_MARKERS):
        return False, "sensitive_name_blocked"
    if str(tool.effect_class).casefold() not in ACTIVE_EFFECT_CLASSES:
        return False, "effect_class_not_active"
    if tool.authority.casefold() not in ACTIVE_AUTHORITIES:
        return False, "authority_not_active"
    if tool.sensitivity.casefold() not in {"normal", "low", "public"}:
        return False, "sensitivity_not_active"
    if tool.idempotency.casefold() not in {"safe", "idempotent"}:
        return False, "idempotency_not_active"
    return True, "active_safe"


def _surface_digest(
    tools: Iterable[ToolSpec],
    *,
    mode: str,
    phase: str,
    route: str | None,
    provider: str | None,
    state: tuple[str, ...],
) -> str:
    payload = {
        "schema_version": "tool-broker-v1",
        "mode": mode,
        "phase": phase,
        "route": route,
        "provider": provider,
        "state": list(state),
        "tools": [
            contract_json(tool)
            for tool in sorted(tools, key=lambda item: item.name.casefold())
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ToolBroker:
    """Compile one provider-neutral tool surface and classify runtime calls."""

    def __init__(
        self,
        registry: CapabilityRegistry,
        *,
        mode: str = "shadow",
        namespace_loaders: Mapping[str, NamespaceLoader] | None = None,
    ) -> None:
        normalized_mode = mode.strip().casefold()
        if normalized_mode not in BROKER_MODES:
            raise ValueError(f"unsupported tool broker mode: {mode}")
        self._registry = CapabilityRegistry(registry.values())
        self.mode = normalized_mode
        # Loaders are explicit callables registered by the host.  Names from
        # profile/job data can select these entries but can never import code.
        self._namespace_loaders = dict(namespace_loaders or {})
        self._loaded_namespaces: set[str] = set()
        self._compiled_names: set[str] | None = None

    def resolve_namespaces(self, namespaces: Iterable[str]) -> None:
        for raw_name in namespaces:
            name = str(raw_name).strip()
            if not name or name in self._loaded_namespaces:
                continue
            loader = self._namespace_loaders.get(name)
            if loader is None:
                raise ValueError(f"tool namespace is not allowlisted: {name}")
            for tool in loader():
                if not isinstance(tool, ToolSpec):
                    raise TypeError(f"tool namespace {name} returned a non-ToolSpec item")
                if tool.namespace != name:
                    raise ValueError(
                        f"tool namespace loader {name} returned mismatched namespace {tool.namespace}"
                    )
                self._registry.register(tool, replace=False)
            self._loaded_namespaces.add(name)

    def compile_surface(
        self,
        *,
        phase: str,
        route: str | None = None,
        provider: str | None = None,
        state: Iterable[str] = (),
        deferred_namespaces: Iterable[str] = (),
    ) -> ToolSurface:
        self.resolve_namespaces(deferred_namespaces)
        normalized_state = tuple(sorted({str(item).casefold() for item in state}))
        scoped = scope_capability_registry(
            self._registry,
            phase=phase,
            route=route,
            state=normalized_state,
        )
        scoped = CapabilityRegistry(
            tool
            for tool in scoped.values()
            if not tool.defer_loading or tool.namespace in self._loaded_namespaces
        )
        normalized_provider = provider.strip().casefold() if provider else None
        if normalized_provider:
            scoped = CapabilityRegistry(
                tool
                for tool in scoped.values()
                if not tool.providers
                or normalized_provider in {item.casefold() for item in tool.providers}
            )
        if self.mode == "active":
            scoped = CapabilityRegistry(
                tool for tool in scoped.values() if _active_safe(tool)[0]
            )
        tools = scoped.values()
        self._compiled_names = set(scoped.names())
        return ToolSurface(
            registry=scoped,
            mode=self.mode,
            phase=phase,
            route=route,
            provider=normalized_provider,
            state=normalized_state,
            surface_hash=_surface_digest(
                tools,
                mode=self.mode,
                phase=phase,
                route=route,
                provider=normalized_provider,
                state=normalized_state,
            ),
            loaded_namespaces=tuple(sorted(self._loaded_namespaces)),
        )

    def classify_call(self, name: str) -> ToolAdmission:
        tool = self._registry.get(name)
        if tool is None:
            return ToolAdmission(
                name=name,
                mode=self.mode,
                admitted=False,
                enforced=self.mode == "active",
                effect_class="unknown",
                reason="undeclared_tool",
            )
        if self._compiled_names is None or name not in self._compiled_names:
            return ToolAdmission(
                name=name,
                mode=self.mode,
                admitted=False,
                enforced=self.mode == "active",
                effect_class=str(tool.effect_class),
                reason=(
                    "surface_not_compiled"
                    if self._compiled_names is None
                    else "not_in_compiled_surface"
                ),
            )
        active_safe, reason = _active_safe(tool)
        return ToolAdmission(
            name=name,
            mode=self.mode,
            admitted=active_safe,
            enforced=self.mode == "active",
            effect_class=str(tool.effect_class),
            reason=reason,
        )

    def admit_call(self, name: str) -> bool:
        decision = self.classify_call(name)
        return decision.admitted if decision.enforced else True
