from __future__ import annotations

from pathlib import Path

import pytest

from applypilot.apply import agent_runtime, launcher
from applypilot.apply.capabilities import (
    CapabilityRegistry,
    McpPackageSpec,
    ToolCapability,
    audit_verification_capabilities,
    compose_runtime_capabilities,
    default_browser_capabilities,
    resolve_capability_registry,
    resolve_playwright_mcp_spec,
    scope_capability_registry,
)
from applypilot.apply.email_routing import MailboxMcpSpec


def test_registry_is_extensible_and_metadata_is_advisory() -> None:
    registry = default_browser_capabilities()
    registry.register(
        ToolCapability(
            name="inspect_custom_form",
            description="Inspect a custom form",
            phases=("discover", "prepare"),
            side_effect="proposal-only",
            concurrency_mode="parallel-by-page",
            metadata={"schema_version": "plugin-2"},
        )
    )

    capability = registry.get("inspect_custom_form")
    assert capability is not None
    assert capability.phases == ("discover", "prepare")
    assert capability.concurrency_mode == "parallel-by-page"
    assert registry.names()[-1] == "inspect_custom_form"


def test_post_submit_diagnostics_are_read_only_browser_capabilities() -> None:
    registry = default_browser_capabilities()

    for name in ("browser_console_messages", "browser_network_requests"):
        capability = registry.get(name)
        assert capability is not None
        assert capability.side_effect == "read"
        assert capability.concurrency_mode == "parallel_safe"
        assert "submit" in capability.phases


def test_mcp_spec_resolution_allows_environment_configuration_without_locking_version() -> None:
    spec = resolve_playwright_mcp_spec(
        environ={
            "APPLYPILOT_PLAYWRIGHT_MCP_PACKAGE": "@playwright/mcp@next",
            "APPLYPILOT_PLAYWRIGHT_MCP_EXTRA_ARGS": '["--isolated", "--foo=bar"]',
            "APPLYPILOT_PLAYWRIGHT_MCP_SCHEMA_VERSION": "2026-08",
        }
    )

    assert spec.package == "@playwright/mcp@next"
    assert spec.extra_args == ("--isolated", "--foo=bar")
    assert spec.schema_version == "2026-08"
    assert spec.source == "environment"


def test_explicit_spec_takes_precedence_and_metadata_omits_environment_values() -> None:
    spec = resolve_playwright_mcp_spec(
        {
            "package": "custom-playwright-server@3",
            "command": "custom-runner",
            "env": {"PRIVATE_TOKEN": "secret-value"},
            "startup_timeout_seconds": 17,
        },
        environ={"APPLYPILOT_PLAYWRIGHT_MCP_PACKAGE": "ignored@1"},
    )

    metadata = spec.metadata()
    assert spec.package == "custom-playwright-server@3"
    assert spec.command == "custom-runner"
    assert metadata["environment_keys"] == ["PRIVATE_TOKEN"]
    assert "secret-value" not in repr(metadata)
    assert metadata["source"] == "explicit"


def test_make_mcp_config_preserves_default_and_records_resolved_surface() -> None:
    metadata: dict = {}
    config = agent_runtime.make_mcp_config(9432, runtime_metadata=metadata)

    playwright = config["mcpServers"]["playwright"]
    assert playwright["args"][:2] == ["-y", "@playwright/mcp@0.0.79"]
    assert all("@latest" not in str(value) for value in playwright["args"])
    assert "applypilot_control" in config["mcpServers"]
    assert config["mcpServers"]["applypilot_ats"]["args"] == [
        "-m",
        "applypilot.apply.ats_tools_mcp",
    ]
    assert "browser_snapshot" in {tool["name"] for tool in metadata["tools"]}
    assert metadata["playwright_mcp"]["source"] == "default"
    assert metadata["capability_schema_version"] == "1"
    assert metadata["application_tools"]["tools"] == [
        "detect_ats",
        "get_application_context",
        "build_fill_plan",
        "build_answer_mapping",
        "resolve_answer",
        "evaluate_workday_progress",
    ]


def test_runtime_capabilities_are_phase_scoped_from_one_registry() -> None:
    registry = compose_runtime_capabilities()

    prepare = scope_capability_registry(
        registry, phase="prepare", state=("ats_unknown",)
    )
    workday = scope_capability_registry(
        registry, phase="prepare", state=("ats_workday",)
    )
    submit = scope_capability_registry(registry, phase="submit")
    preview = scope_capability_registry(
        registry,
        phase="submit",
        route="browser",
        state=("preview", "ats_smartrecruiters"),
    )
    ordinary_repair = scope_capability_registry(
        registry, phase="submit", route="browser", state=("repair",)
    )
    resume_repair = scope_capability_registry(
        registry,
        phase="submit",
        route="browser",
        state=("repair", "repair_resume_upload"),
    )

    assert "detect_ats" in prepare.names()
    assert "build_fill_plan" in prepare.names()
    assert "build_answer_mapping" in prepare.names()
    assert "resolve_answer" in prepare.names()
    assert "evaluate_workday_progress" not in prepare.names()
    assert "detect_ats" not in workday.names()
    assert "evaluate_workday_progress" in workday.names()
    assert "detect_ats" not in submit.names()
    assert "build_fill_plan" not in submit.names()
    assert "build_answer_mapping" not in submit.names()
    assert "resolve_answer" in submit.names()
    assert "report_agent_turn" in submit.names()
    assert "browser_fill_form" not in submit.names()
    assert "browser_select_option" not in submit.names()
    assert "browser_type" not in submit.names()
    assert "browser_press_key" not in submit.names()
    assert "browser_file_upload" not in submit.names()
    assert "browser_fill_form" in preview.names()
    assert "browser_select_option" in preview.names()
    assert "browser_type" in preview.names()
    assert "browser_press_key" in preview.names()
    assert "browser_file_upload" in preview.names()
    assert "report_agent_turn" in preview.names()
    assert "browser_file_upload" not in ordinary_repair.names()
    assert "browser_file_upload" in resume_repair.names()


def test_scope_supports_open_route_and_state_extensions() -> None:
    registry = CapabilityRegistry(
        [
            ToolCapability(
                name="future_helper",
                description="Future helper",
                phases=("prepare",),
                tags=("route:direct_email", "requires_state:reserved"),
            )
        ]
    )

    assert scope_capability_registry(
        registry, phase="prepare", route="direct_email", state=("reserved",)
    ).names() == ["future_helper"]
    assert scope_capability_registry(
        registry, phase="prepare", route="browser", state=("reserved",)
    ).names() == []


def test_claude_surface_includes_resolver_from_canonical_registry(tmp_path: Path) -> None:
    registry = scope_capability_registry(
        compose_runtime_capabilities(), phase="submit"
    )
    command, _ = agent_runtime.build_agent_command(
        "claude",
        "model",
        9432,
        tmp_path,
        tmp_path / "mcp.json",
        resolve_claude=lambda: ["claude"],
        capability_registry=registry,
    )
    allowed = command[command.index("--allowedTools") + 1]

    assert "mcp__applypilot_ats__resolve_answer" in allowed
    assert "mcp__applypilot_ats__detect_ats" not in allowed


def test_prepare_runtime_surfaces_expose_answer_mapping_only_before_submit(
    tmp_path: Path,
) -> None:
    prepare = scope_capability_registry(
        compose_runtime_capabilities(), phase="prepare"
    )
    submit = scope_capability_registry(compose_runtime_capabilities(), phase="submit")

    claude_prepare, _ = agent_runtime.build_agent_command(
        "claude",
        "model",
        9432,
        tmp_path,
        tmp_path / "claude-prepare-mcp.json",
        resolve_claude=lambda: ["claude"],
        capability_registry=prepare,
    )
    claude_submit, _ = agent_runtime.build_agent_command(
        "claude",
        "model",
        9432,
        tmp_path,
        tmp_path / "claude-submit-mcp.json",
        resolve_claude=lambda: ["claude"],
        capability_registry=submit,
    )
    codex_prepare, _ = agent_runtime.build_agent_command(
        "codex",
        "model",
        9432,
        tmp_path,
        tmp_path / "unused-codex-prepare.json",
        resolve_codex=lambda: ["codex"],
        capability_registry=prepare,
    )
    codex_submit, _ = agent_runtime.build_agent_command(
        "codex",
        "model",
        9432,
        tmp_path,
        tmp_path / "unused-codex-submit.json",
        resolve_codex=lambda: ["codex"],
        capability_registry=submit,
    )

    claude_prepare_allowed = claude_prepare[
        claude_prepare.index("--allowedTools") + 1
    ]
    claude_submit_allowed = claude_submit[claude_submit.index("--allowedTools") + 1]
    codex_prepare_rendered = " ".join(codex_prepare)
    codex_submit_rendered = " ".join(codex_submit)
    mapping_tool = "build_answer_mapping"

    assert f"mcp__applypilot_ats__{mapping_tool}" in claude_prepare_allowed
    assert f"mcp__applypilot_ats__{mapping_tool}" not in claude_submit_allowed
    assert mapping_tool in codex_prepare_rendered
    assert mapping_tool not in codex_submit_rendered


def test_audit_verification_child_surface_is_physical_read_only() -> None:
    scoped = audit_verification_capabilities(compose_runtime_capabilities())

    assert scoped.names() == [
        "browser_snapshot",
        "browser_take_screenshot",
        "browser_wait_for",
        "get_application_context",
        "build_answer_mapping",
        "report_agent_turn",
    ]
    assert all(
        capability.side_effect == "read"
        for capability in scoped.values()
        if capability.name.startswith("browser_")
    )
    assert not {
        "browser_navigate",
        "browser_click",
        "browser_fill_form",
        "browser_file_upload",
        "browser_select_option",
        "browser_type",
        "browser_tabs",
        "resolve_answer",
    }.intersection(scoped.names())


def test_reasoning_effort_is_configurable_by_workload_without_model_binding(
    tmp_path: Path,
) -> None:
    command, _ = agent_runtime.build_agent_command(
        "codex",
        "model",
        9432,
        tmp_path,
        tmp_path / "mcp.json",
        resolve_codex=lambda: ["codex"],
        workload_class="submit-review",
        reasoning_efforts={"submit-review": "medium"},
    )

    assert 'model_reasoning_effort="medium"' in command


def test_runtime_configuration_is_shared_with_command_and_comparison_metadata(
    tmp_path: Path,
) -> None:
    resolved = agent_runtime.resolve_agent_runtime_configuration(
        "codex",
        "gpt-5.6-sol",
        workload_class="submit_repair",
        reasoning_efforts={"submit_repair": "medium"},
        environ={"APPLYPILOT_REASONING_EFFORTS": '{"submit_repair":"low"}'},
        model_source="environment",
    )
    metadata: dict = {}

    command, _ = agent_runtime.build_agent_command(
        "codex",
        "gpt-5.6-sol",
        9432,
        tmp_path,
        tmp_path / "mcp.json",
        resolve_codex=lambda: ["codex"],
        runtime_metadata=metadata,
        resolved_configuration=resolved,
    )

    assert 'model_reasoning_effort="medium"' in command
    assert metadata["runtime_configuration"] == {
        "schema_version": "1",
        "backend": "codex",
        "model": "gpt-5.6-sol",
        "model_source": "environment",
        "reasoning_effort": "medium",
        "reasoning_effort_source": "profile",
        "workload_class": "submit_repair",
        "reasoning_effort_source_key": "submit_repair",
        "reasoning_effort_applied": True,
    }


def test_reasoning_resolution_records_default_fallback_and_rejects_unknown_values() -> None:
    resolved = agent_runtime.resolve_reasoning_effort_configuration(
        "prepare_repair",
        configured={"default": " HIGH "},
        environ={"APPLYPILOT_REASONING_EFFORTS": '{"default":"low"}'},
    )

    assert resolved.value == "high"
    assert resolved.source == "profile"
    assert resolved.source_key == "default"
    assert resolved.workload_class == "prepare_repair"
    with pytest.raises(ValueError, match="unsupported reasoning effort"):
        agent_runtime.resolve_reasoning_effort(
            "prepare",
            configured={"prepare": "turbo"},
            environ={},
        )


def test_build_agent_command_accepts_custom_process_and_tool_surface(tmp_path: Path) -> None:
    registry = CapabilityRegistry(
        [
            ToolCapability(
                name="observe_only",
                description="Observe only",
                phases=("prepare",),
                side_effect="read",
            )
        ]
    )
    metadata: dict = {}
    spec = McpPackageSpec(
        package="custom-mcp@2",
        command="custom-runner",
        launcher_args=(),
        extra_args=("--mode=local",),
        env={"MCP_MODE": "test"},
        startup_timeout_seconds=12,
        tool_timeout_seconds=34,
        schema_version="tool-schema-2",
        source="test",
    )

    command, _ = agent_runtime.build_agent_command(
        "codex",
        "model",
        9432,
        tmp_path,
        tmp_path / "unused.json",
        resolve_codex=lambda: ["codex"],
        playwright_mcp=spec,
        capability_registry=registry,
        runtime_metadata=metadata,
    )
    rendered = " ".join(command)

    assert 'mcp_servers.playwright.command="custom-runner"' in rendered
    assert "custom-mcp@2" in rendered
    assert "--mode=local" in rendered
    assert "observe_only" in rendered
    assert "browser_click" not in rendered
    assert "startup_timeout_sec=12" in rendered
    assert "tool_timeout_sec=34" in rendered
    assert "mcp_servers.playwright.env" not in rendered
    assert "MCP_MODE" not in rendered
    inherited: dict[str, str] = {"HOST_SETTING": "preserved"}
    agent_runtime.apply_mcp_process_environment(inherited, spec)
    assert inherited == {"HOST_SETTING": "preserved", "MCP_MODE": "test"}
    assert "mcp_servers.applypilot_control.required" not in rendered
    assert metadata["playwright_mcp"]["schema_version"] == "tool-schema-2"


def test_configured_capabilities_can_extend_or_replace_defaults() -> None:
    extended = resolve_capability_registry(
        [
            {
                "name": "future_semantic_mapper",
                "description": "Map an unfamiliar form",
                "phases": ["future-phase"],
                "concurrency_mode": "parallel-by-form",
            }
        ]
    )
    replaced = resolve_capability_registry(
        {
            "future_submitter": {
                "description": "Plugin-owned submit tool",
                "side_effect": "plugin-defined",
                "concurrency_mode": "plugin-exclusive",
            }
        },
        inherit_defaults=False,
    )

    assert "browser_snapshot" in extended.names()
    assert extended.get("future_semantic_mapper").phases == ("future-phase",)
    assert replaced.names() == ["future_submitter"]
    assert replaced.get("future_submitter").side_effect == "plugin-defined"


def test_launcher_compiles_one_broker_surface_before_command_assembly() -> None:
    deferred = ToolCapability(
        name="semantic_report",
        description="Return a semantic proposal",
        phases=("prepare",),
        effect_class="proposal",
        idempotency="safe",
        authority="advisory",
        sensitivity="normal",
        namespace="semantic",
        defer_loading=True,
        metadata={"server": "applypilot_ats"},
    )
    profile = {
        "agent_runtime": {
            "tool_broker": {
                "mode": "active",
                "deferred_namespaces": ["semantic"],
            }
        }
    }
    job = {"_agent_tool_namespace_loaders": {"semantic": lambda: (deferred,)}}

    surface = launcher._compile_agent_tool_surface(
        profile,
        job,
        default_browser_capabilities(),
        phase="prepare",
        route="browser",
        state={"ats_unknown"},
    )

    assert surface.mode == "active"
    assert surface.names() == [
        "browser_snapshot",
        "browser_take_screenshot",
        "browser_console_messages",
        "browser_network_requests",
        "browser_wait_for",
        "detect_ats",
        "get_application_context",
        "build_fill_plan",
        "build_answer_mapping",
        "resolve_answer",
        "report_agent_turn",
        "semantic_report",
    ]
    metadata: dict = {
        "tool_broker": {"surface_hash": surface.surface_hash}
    }
    command, _ = agent_runtime.build_agent_command(
        "claude",
        "model",
        9432,
        Path("worker"),
        Path("mcp.json"),
        resolve_claude=lambda: ["claude"],
        capability_registry=surface.registry,
        runtime_metadata=metadata,
    )
    allowed = command[command.index("--allowedTools") + 1]

    assert "mcp__applypilot_ats__semantic_report" in allowed
    assert "mcp__playwright__browser_click" not in allowed
    assert metadata["tool_broker"]["surface_hash"] == surface.surface_hash


def test_dynamic_tools_follow_the_compiled_ats_state() -> None:
    profile = {"agent_runtime": {"tool_broker": {"mode": "shadow"}}}
    generic = launcher._compile_agent_tool_surface(
        profile,
        {},
        default_browser_capabilities(),
        phase="prepare",
        route="browser",
        state={"ats_unknown"},
        provider="codex",
    )
    workday = launcher._compile_agent_tool_surface(
        profile,
        {},
        default_browser_capabilities(),
        phase="prepare",
        route="browser",
        state={"ats_workday"},
        provider="codex",
    )

    assert [tool.name for tool in launcher._compile_app_server_dynamic_tools({}, generic.registry)] == [
        "detect_ats"
    ]
    assert launcher._compile_app_server_dynamic_tools({}, workday.registry) == ()


def test_active_surface_excludes_mailbox_send_and_credential_relay() -> None:
    mailbox = MailboxMcpSpec(
        server_name="mailbox",
        package=None,
        command="mailbox-test",
        search_tool="search_emails",
        read_tool="read_email",
        send_tool="send_email",
    )
    external = launcher._with_external_tool_capabilities(
        default_browser_capabilities(),
        mailbox_mcp=mailbox,
        mailbox_read_authorized=True,
        direct_email_send_authorized=True,
        credential_relay_authorized=True,
        identity_relay_authorized=True,
    )
    surface = launcher._compile_agent_tool_surface(
        {"agent_runtime": {"tool_broker": {"mode": "active"}}},
        {},
        external,
        phase="submit",
        route="direct_email",
        state={"submit", "reserved"},
        provider="codex",
    )

    assert "send_email" not in surface.names()
    assert "fill_ats_credentials" not in surface.names()
    assert "fill_protected_identifier" not in surface.names()
    command, _ = agent_runtime.build_agent_command(
        "codex",
        "model",
        9432,
        Path("worker"),
        Path("unused.json"),
        resolve_codex=lambda: ["codex"],
        capability_registry=surface.registry,
        mailbox_mcp=mailbox,
        direct_email_send_authorized=True,
        credential_relay_authorized=True,
        identity_relay_authorized=True,
    )
    rendered = " ".join(command)

    assert "mcp_servers.mailbox.command" not in rendered
    assert "mcp_servers.credential_relay.command" not in rendered


def test_claude_and_codex_share_the_same_resolved_mcp_surface(tmp_path: Path) -> None:
    metadata: dict = {}
    spec = McpPackageSpec(
        package="custom-mcp@future",
        command="custom-runner",
        launcher_args=("--launch",),
        source="test",
    )
    config = agent_runtime.make_mcp_config(
        9432,
        playwright_mcp=spec,
        runtime_metadata=metadata,
        python_executable="python",
    )
    command, final_path = agent_runtime.build_agent_command(
        "claude",
        "model",
        9432,
        tmp_path,
        tmp_path / "mcp.json",
        resolve_claude=lambda: ["claude"],
        playwright_mcp=spec,
        runtime_metadata=metadata,
    )

    assert config["mcpServers"]["playwright"]["args"][:2] == [
        "--launch",
        "custom-mcp@future",
    ]
    assert command[0] == "claude"
    assert "--strict-mcp-config" in command
    assert command[command.index("--tools") + 1] == ""
    assert "--disable-slash-commands" in command
    assert final_path is None
    assert metadata["playwright_mcp"]["package"] == "custom-mcp@future"


def test_mcp_environment_values_are_never_written_to_config() -> None:
    spec = McpPackageSpec(
        package=None,
        command="portable-mcp-server",
        env={"PRIVATE_TOKEN": "secret-value"},
        source="test",
    )

    config = agent_runtime.make_mcp_config(9432, playwright_mcp=spec)

    assert "env" not in config["mcpServers"]["playwright"]
    assert "secret-value" not in repr(config)


def test_direct_mcp_process_does_not_require_a_package_or_version() -> None:
    spec = resolve_playwright_mcp_spec(
        {
            "package": None,
            "command": "portable-mcp-server",
            "extra_args": ["--stdio"],
        }
    )

    assert spec.package is None
    assert spec.process_args() == ["--stdio"]
