"""Extensible capability and MCP runtime descriptions for application agents.

The objects in this module describe what a tool can do; they deliberately do
not enforce application policy.  Orchestrators may use the phase, side-effect,
and concurrency hints when planning a run while retaining the freedom to add
new tools and agent runtimes without changing this module.
"""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from applypilot.apply.contracts import ToolSpec, contract_json

if TYPE_CHECKING:
    from collections.abc import MutableMapping


CAPABILITY_SCHEMA_VERSION = "1"
DEFAULT_PLAYWRIGHT_MCP_PACKAGE = "@playwright/mcp@0.0.79"


# Keep one canonical tool contract across discovery, runtime configuration, and
# orchestration.  The alias preserves the descriptive P0 import without
# creating a second model that can drift from ``ToolSpec``.
ToolCapability = ToolSpec


class CapabilityRegistry:
    """Small mutable registry that supports application and plugin extensions."""

    def __init__(self, capabilities: Iterable[ToolSpec] = ()) -> None:
        self._items: dict[str, ToolSpec] = {}
        for capability in capabilities:
            self.register(capability)

    def register(self, capability: ToolSpec, *, replace: bool = False) -> None:
        if capability.name in self._items and not replace:
            raise ValueError(f"capability already registered: {capability.name}")
        self._items[capability.name] = capability

    def get(self, name: str) -> ToolSpec | None:
        return self._items.get(name)

    def names(self) -> list[str]:
        return list(self._items)

    def describe(self) -> list[dict[str, Any]]:
        return [contract_json(item) for item in self._items.values()]

    def values(self) -> tuple[ToolSpec, ...]:
        """Return capabilities in registration order."""
        return tuple(self._items.values())


def default_browser_capabilities() -> CapabilityRegistry:
    """Return the current portable Playwright surface with advisory metadata."""
    read_tools = {"browser_snapshot", "browser_take_screenshot", "browser_wait_for"}
    ordered_names = (
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_fill_form",
        "browser_file_upload",
        "browser_tabs",
        "browser_take_screenshot",
        "browser_select_option",
        "browser_type",
        "browser_wait_for",
    )
    capabilities = []
    for name in ordered_names:
        read_only = name in read_tools
        phases = (
            ("prepare",)
            if name == "browser_navigate"
            else ("prepare", "submit")
        )
        tags = ["route:browser"]
        if name in {
            "browser_fill_form",
            "browser_select_option",
            "browser_type",
        }:
            tags.append("phase_requires_state:submit:repair")
        if name == "browser_file_upload":
            tags.append("phase_requires_state:submit:repair_resume_upload")
        capabilities.append(
            ToolSpec(
                name=name,
                description=(
                    "Browser observation tool"
                    if read_only
                    else "Browser action; the page writer should coordinate calls"
                ),
                phases=phases,
                side_effect="read" if read_only else "write",
                concurrency_mode="parallel_safe" if read_only else "serial_per_page",
                tags=tuple(tags),
                metadata={
                    "schema_version": CAPABILITY_SCHEMA_VERSION,
                    "server": "playwright",
                },
            )
        )
    return CapabilityRegistry(capabilities)


def default_auxiliary_capabilities() -> CapabilityRegistry:
    """Return provider-neutral ApplyPilot tools outside the browser server."""
    definitions = (
        ("detect_ats", ("prepare",), "applypilot_ats", ("requires_state:ats_unknown",)),
        ("get_application_context", ("prepare",), "applypilot_ats", ()),
        ("build_fill_plan", ("prepare",), "applypilot_ats", ()),
        ("resolve_answer", ("prepare", "submit"), "applypilot_ats", ()),
        (
            "evaluate_workday_progress",
            ("prepare",),
            "applypilot_ats",
            ("requires_state:ats_workday",),
        ),
        (
            "report_agent_turn",
            ("prepare", "submit", "receipt"),
            "applypilot_control",
            (),
        ),
    )
    return CapabilityRegistry(
        ToolSpec(
            name=name,
            description="Read/proposal-only application helper",
            phases=phases,
            side_effect="read" if name != "report_agent_turn" else "report",
            concurrency_mode="parallel_safe",
            tags=tags,
            metadata={"schema_version": CAPABILITY_SCHEMA_VERSION, "server": server},
        )
        for name, phases, server, tags in definitions
    )


def compose_runtime_capabilities(
    browser_registry: CapabilityRegistry | None = None,
) -> CapabilityRegistry:
    """Compose browser and ApplyPilot capabilities without binding a runtime."""
    combined = CapabilityRegistry((browser_registry or default_browser_capabilities()).values())
    for capability in default_auxiliary_capabilities().values():
        combined.register(capability, replace=combined.get(capability.name) is not None)
    return combined


def scope_capability_registry(
    registry: CapabilityRegistry,
    *,
    phase: str,
    route: str | None = None,
    state: Iterable[str] = (),
) -> CapabilityRegistry:
    """Select tools for a turn from advisory phase/route/state metadata.

    Empty phase metadata remains open for plugin compatibility. Optional
    ``route:*`` and ``requires_state:*`` tags allow future extensions without
    hard-coding an ATS, model, or application provider here.
    """
    active_state = {str(item).casefold() for item in state}
    active_route = (route or "").casefold()
    selected: list[ToolSpec] = []
    for capability in registry.values():
        if capability.phases and phase not in capability.phases:
            continue
        route_tags = {
            tag.split(":", 1)[1].casefold()
            for tag in capability.tags
            if tag.startswith("route:")
        }
        if route_tags and active_route not in route_tags:
            continue
        required_state = {
            tag.split(":", 1)[1].casefold()
            for tag in capability.tags
            if tag.startswith("requires_state:")
        }
        if not required_state.issubset(active_state):
            continue
        phase_state_requirements = [
            tag.split(":", 2)
            for tag in capability.tags
            if tag.startswith("phase_requires_state:")
        ]
        if any(
            len(parts) == 3
            and parts[1].casefold() == phase.casefold()
            and parts[2].casefold() not in active_state
            for parts in phase_state_requirements
        ):
            continue
        selected.append(capability)
    return CapabilityRegistry(selected)


def capability_names_for_server(
    registry: CapabilityRegistry,
    server: str,
) -> list[str]:
    """Resolve one MCP server's tools from the canonical registry."""
    return [
        capability.name
        for capability in registry.values()
        if capability.metadata.get("server", "playwright") == server
    ]


def resolve_capability_registry(
    configured: object = None,
    *,
    inherit_defaults: bool = True,
) -> CapabilityRegistry:
    """Resolve a registry from open configuration without fixing roles or ATSes.

    ``configured`` may be a list of tool mappings, a mapping keyed by tool
    name, or existing ``ToolSpec`` instances.  Callers can extend the default
    browser surface or replace it completely; phases and concurrency modes are
    deliberately free-form advisory strings.
    """
    registry = default_browser_capabilities() if inherit_defaults else CapabilityRegistry()
    if configured is None:
        return registry

    raw_items: Iterable[object]
    if isinstance(configured, Mapping):
        raw_items = (
            ({"name": name, **dict(value)} if isinstance(value, Mapping) else value)
            for name, value in configured.items()
        )
    elif isinstance(configured, (list, tuple)):
        raw_items = configured
    else:
        raise TypeError("tool capability configuration must be a mapping or sequence")

    for raw in raw_items:
        if isinstance(raw, ToolSpec):
            tool = raw
        elif isinstance(raw, Mapping):
            tool = ToolSpec(
                name=str(raw.get("name") or ""),
                description=str(raw.get("description") or "Configured runtime tool"),
                input_schema=dict(raw.get("input_schema") or {}),
                output_schema=dict(raw.get("output_schema") or {}),
                phases=tuple(str(value) for value in (raw.get("phases") or ())),
                side_effect=str(raw.get("side_effect") or "read"),
                concurrency_mode=str(raw.get("concurrency_mode") or "adaptive"),
                tags=tuple(str(value) for value in (raw.get("tags") or ())),
                metadata=dict(raw.get("metadata") or {}),
            )
        else:
            raise TypeError("each tool capability must be a ToolSpec or mapping")
        registry.register(tool, replace=registry.get(tool.name) is not None)
    return registry


@dataclass(frozen=True, slots=True)
class McpPackageSpec:
    """Resolved, provider-neutral MCP process specification."""

    package: str | None = DEFAULT_PLAYWRIGHT_MCP_PACKAGE
    command: str = "npx"
    launcher_args: tuple[str, ...] | None = None
    extra_args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    startup_timeout_seconds: int = 60
    tool_timeout_seconds: int = 90
    schema_version: str = CAPABILITY_SCHEMA_VERSION
    source: str = "default"

    def __post_init__(self) -> None:
        if not self.command.strip():
            raise ValueError("MCP command must not be empty")
        if self.package is not None and not self.package.strip():
            raise ValueError("MCP package must be non-empty or None")
        if self.startup_timeout_seconds <= 0 or self.tool_timeout_seconds <= 0:
            raise ValueError("MCP timeouts must be positive")

    def resolved_launcher_args(self) -> tuple[str, ...]:
        if self.launcher_args is not None:
            return self.launcher_args
        return ("-y",) if self.command.casefold() in {"npx", "npx.cmd"} else ()

    def process_args(self, *, confirm_install: bool = True) -> list[str]:
        prefix = list(self.resolved_launcher_args()) if confirm_install else []
        package = [self.package] if self.package is not None else []
        return [*prefix, *package, *self.extra_args]

    def metadata(self) -> dict[str, Any]:
        """Return reproducibility metadata without copying environment values."""
        return {
            "package": self.package,
            "command": self.command,
            "launcher_args": list(self.resolved_launcher_args()),
            "extra_args": list(self.extra_args),
            "environment_keys": sorted(self.env),
            "startup_timeout_seconds": self.startup_timeout_seconds,
            "tool_timeout_seconds": self.tool_timeout_seconds,
            "schema_version": self.schema_version,
            "source": self.source,
        }


def _sequence(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return ()
        if stripped.startswith("["):
            parsed = json.loads(stripped)
            if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
                raise ValueError("MCP argument JSON must be an array of strings")
            return tuple(parsed)
        return tuple(shlex.split(stripped, posix=os.name != "nt"))
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return tuple(value)
    raise TypeError("MCP arguments must be a string or a sequence of strings")


def resolve_playwright_mcp_spec(
    explicit: McpPackageSpec | Mapping[str, object] | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    configured: Mapping[str, object] | None = None,
) -> McpPackageSpec:
    """Resolve an MCP spec with explicit > configured > environment > default precedence."""
    if isinstance(explicit, McpPackageSpec):
        return explicit
    environment = os.environ if environ is None else environ
    values: dict[str, object] = dict(configured or {})
    env_values: dict[str, object] = {
        "package": environment.get("APPLYPILOT_PLAYWRIGHT_MCP_PACKAGE"),
        "command": environment.get("APPLYPILOT_PLAYWRIGHT_MCP_COMMAND"),
        "launcher_args": environment.get("APPLYPILOT_PLAYWRIGHT_MCP_LAUNCHER_ARGS"),
        "extra_args": environment.get("APPLYPILOT_PLAYWRIGHT_MCP_EXTRA_ARGS"),
        "startup_timeout_seconds": environment.get("APPLYPILOT_PLAYWRIGHT_MCP_STARTUP_TIMEOUT"),
        "tool_timeout_seconds": environment.get("APPLYPILOT_PLAYWRIGHT_MCP_TOOL_TIMEOUT"),
        "schema_version": environment.get("APPLYPILOT_PLAYWRIGHT_MCP_SCHEMA_VERSION"),
    }
    for key, value in env_values.items():
        if value is not None and key not in values:
            values[key] = value
    if isinstance(explicit, str):
        values["package"] = explicit
    elif explicit is not None:
        values.update(explicit)

    source = "explicit" if explicit is not None else "configured" if configured else "environment" if any(
        value is not None for value in env_values.values()
    ) else "default"
    package_value = values.get("package", DEFAULT_PLAYWRIGHT_MCP_PACKAGE)
    package = None if package_value is None else str(package_value).strip() or None
    return McpPackageSpec(
        package=package,
        command=str(values.get("command") or "npx"),
        launcher_args=(
            _sequence(values["launcher_args"])
            if "launcher_args" in values
            else None
        ),
        extra_args=_sequence(values.get("extra_args")),
        env={str(key): str(value) for key, value in dict(values.get("env") or {}).items()},
        startup_timeout_seconds=int(values.get("startup_timeout_seconds") or 60),
        tool_timeout_seconds=int(values.get("tool_timeout_seconds") or 90),
        schema_version=str(values.get("schema_version") or CAPABILITY_SCHEMA_VERSION),
        source=source,
    )


def record_runtime_surface(
    target: MutableMapping[str, Any] | None,
    spec: McpPackageSpec,
    registry: CapabilityRegistry,
) -> None:
    """Optionally record a resolved, secret-free runtime surface."""
    if target is not None:
        target["capability_schema_version"] = CAPABILITY_SCHEMA_VERSION
        target["playwright_mcp"] = spec.metadata()
        target["tools"] = [
            {
                "name": item.name,
                "phases": list(item.phases),
                "side_effect": item.side_effect,
                "concurrency_mode": item.concurrency_mode,
                "tags": list(item.tags),
                "schema_version": str(
                    item.metadata.get("schema_version", CAPABILITY_SCHEMA_VERSION)
                ),
            }
            for item in (registry.get(name) for name in registry.names())
            if item is not None
        ]
