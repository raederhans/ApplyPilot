from __future__ import annotations

from pathlib import Path

from applypilot.apply import agent_runtime
from applypilot.apply.capabilities import (
    CapabilityRegistry,
    McpPackageSpec,
    ToolCapability,
    compose_runtime_capabilities,
    default_browser_capabilities,
    resolve_capability_registry,
    resolve_playwright_mcp_spec,
    scope_capability_registry,
)


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
    assert playwright["args"][:2] == ["-y", "@playwright/mcp@latest"]
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
    assert "resolve_answer" in prepare.names()
    assert "evaluate_workday_progress" not in prepare.names()
    assert "detect_ats" not in workday.names()
    assert "evaluate_workday_progress" in workday.names()
    assert "detect_ats" not in submit.names()
    assert "build_fill_plan" not in submit.names()
    assert "resolve_answer" in submit.names()
    assert "report_agent_turn" in submit.names()
    assert "browser_file_upload" not in submit.names()
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
