from __future__ import annotations

import pytest

from applypilot.apply.capabilities import CapabilityRegistry
from applypilot.apply.contracts import ToolSpec
from applypilot.apply.tool_broker import ToolBroker


def _registry() -> CapabilityRegistry:
    return CapabilityRegistry(
        [
            ToolSpec(
                name="inspect_fields",
                description="Read fields",
                phases=("prepare",),
                effect_class="read",
                idempotency="safe",
                authority="observation",
                sensitivity="normal",
                providers=("codex",),
                postcondition={"kind": "observation"},
            ),
            ToolSpec(
                name="browser_click",
                description="Click page",
                phases=("prepare",),
                effect_class="write",
                authority="browser_write",
            ),
            ToolSpec(
                name="direct_email_send",
                description="Send mail",
                phases=("prepare",),
                effect_class="write",
                authority="mailbox_send",
            ),
        ]
    )


def test_tool_spec_effect_class_preserves_side_effect_compatibility() -> None:
    tool = ToolSpec(
        name="report",
        description="Report result",
        effect_class="report",
        authority="report",
    )

    assert tool.effect_class == "report"
    assert tool.side_effect == "report"


def test_shadow_classifies_without_changing_existing_runtime_admission() -> None:
    broker = ToolBroker(_registry())
    surface = broker.compile_surface(phase="prepare")

    assert surface.names() == ["inspect_fields", "browser_click", "direct_email_send"]
    assert broker.classify_call("browser_click").admitted is False
    assert broker.classify_call("browser_click").enforced is False
    assert broker.admit_call("browser_click") is True
    assert broker.admit_call("undeclared") is True


def test_active_is_fail_closed_and_cannot_open_effect_authority() -> None:
    broker = ToolBroker(_registry(), mode="active")
    surface = broker.compile_surface(phase="prepare")

    assert surface.names() == ["inspect_fields"]
    assert broker.admit_call("inspect_fields") is True
    assert broker.admit_call("browser_click") is False
    assert broker.admit_call("direct_email_send") is False
    unknown = broker.classify_call("future_unknown")
    assert unknown.enforced is True
    assert unknown.admitted is False
    assert unknown.reason == "undeclared_tool"


def test_active_admission_is_bound_to_compiled_phase_and_provider() -> None:
    registry = CapabilityRegistry(
        [
            ToolSpec(
                name="codex_prepare_read",
                description="Read in prepare",
                phases=("prepare",),
                providers=("codex",),
                idempotency="safe",
                authority="observation",
                sensitivity="normal",
            ),
            ToolSpec(
                name="claude_submit_read",
                description="Read in submit",
                phases=("submit",),
                providers=("claude",),
                idempotency="safe",
                authority="observation",
                sensitivity="normal",
            ),
        ]
    )
    broker = ToolBroker(registry, mode="active")
    before_compile = broker.classify_call("codex_prepare_read")
    surface = broker.compile_surface(phase="prepare", provider="codex")

    assert before_compile.reason == "surface_not_compiled"
    assert surface.names() == ["codex_prepare_read"]
    assert broker.admit_call("codex_prepare_read") is True
    assert broker.admit_call("claude_submit_read") is False
    assert broker.classify_call("claude_submit_read").reason == "not_in_compiled_surface"


def test_deferred_namespace_loader_is_static_allowlisted_and_hashes_surface() -> None:
    deferred = ToolSpec(
        name="semantic_report",
        description="Return a proposal",
        phases=("prepare",),
        effect_class="proposal",
        authority="advisory",
        namespace="semantic",
        defer_loading=True,
    )
    broker = ToolBroker(
        CapabilityRegistry(),
        namespace_loaders={"semantic": lambda: (deferred,)},
    )

    empty = broker.compile_surface(phase="prepare")
    loaded = broker.compile_surface(
        phase="prepare", deferred_namespaces=("semantic",)
    )
    repeated = broker.compile_surface(
        phase="prepare", deferred_namespaces=("semantic",)
    )

    assert empty.names() == []
    assert loaded.names() == ["semantic_report"]
    assert loaded.loaded_namespaces == ("semantic",)
    assert loaded.surface_hash == repeated.surface_hash
    assert loaded.surface_hash != empty.surface_hash


def test_surface_hash_is_independent_of_registration_order() -> None:
    tools = _registry().values()
    forward = ToolBroker(CapabilityRegistry(tools)).compile_surface(phase="prepare")
    reverse = ToolBroker(CapabilityRegistry(reversed(tools))).compile_surface(
        phase="prepare"
    )

    assert forward.surface_hash == reverse.surface_hash


def test_unknown_deferred_namespace_fails_before_surface_compilation() -> None:
    broker = ToolBroker(CapabilityRegistry())

    with pytest.raises(ValueError, match="not allowlisted"):
        broker.compile_surface(phase="prepare", deferred_namespaces=("external",))


def test_active_rejects_legacy_tools_without_explicit_safe_idempotency() -> None:
    legacy = ToolSpec(name="legacy_lookup", description="Unclassified legacy tool")

    broker = ToolBroker(CapabilityRegistry((legacy,)), mode="active")
    surface = broker.compile_surface(phase="prepare")

    assert surface.names() == []
    assert broker.classify_call(legacy.name).reason == "not_in_compiled_surface"
    assert broker.admit_call(legacy.name) is False


def test_restricted_surface_hashes_the_actual_narrower_registry() -> None:
    surface = ToolBroker(_registry()).compile_surface(phase="prepare")
    narrowed_registry = CapabilityRegistry((surface.registry.get("inspect_fields"),))

    narrowed = surface.restrict_to(narrowed_registry)

    assert narrowed.names() == ["inspect_fields"]
    assert narrowed.surface_hash != surface.surface_hash
    with pytest.raises(ValueError, match="cannot add capabilities"):
        surface.restrict_to(
            CapabilityRegistry(
                (
                    ToolSpec(
                        name="new_tool",
                        description="Not previously compiled",
                    ),
                )
            )
        )
