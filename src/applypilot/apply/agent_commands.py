"""Provider command assembly and MCP wiring; no agent process lifecycle.

Keep runtime-specific command details here. The application orchestrator only
supplies resolved configuration and the permitted capability surface.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from applypilot import config
from applypilot.apply.agent_configuration import (
    AgentRuntimeConfiguration,
    resolve_agent_runtime_configuration,
)
from applypilot.apply.capabilities import (
    CapabilityRegistry,
    McpPackageSpec,
    capability_names_for_server,
    compose_runtime_capabilities,
    default_browser_capabilities,
    record_runtime_surface,
    resolve_playwright_mcp_spec,
)
from applypilot.apply.email_routing import MailboxMcpSpec, resolve_mailbox_mcp_spec

logger = logging.getLogger(__name__)


CREDENTIAL_RELAY_ENV_VARS = (
    "APPLYPILOT_ATS_CREDENTIAL_FILE",
    "APPLYPILOT_IDENTITY_CREDENTIAL_FILE",
    "APPLYPILOT_CDP_PORT",
    "APPLYPILOT_CREDENTIAL_ALLOWED_HOSTS",
    "APPLYPILOT_CREDENTIAL_PASSWORD_ALLOWED_HOSTS",
    "APPLYPILOT_CREDENTIAL_ALLOW_KNOWN_ATS_REDIRECT",
    "APPLYPILOT_CREDENTIAL_ROOT_TARGET_IDS",
    "APPLYPILOT_ATS_CONTEXT_PATH",
    "APPLYPILOT_CREDENTIAL_APPLICATION_CONTEXT_SHA256",
    "APPLYPILOT_CREDENTIAL_ATTEMPT_ID",
    "APPLYPILOT_CREDENTIAL_APPLICATION_ID",
    "APPLYPILOT_CREDENTIAL_RELAY_AUTHORIZED",
    "APPLYPILOT_IDENTITY_RELAY_AUTHORIZED",
    "APPLYPILOT_TOOL_BROKER_MODE",
)
APPLICATION_TOOL_ENV_VARS = (
    "APPLYPILOT_ATS_CONTEXT_PATH",
    "APPLYPILOT_TOOL_BROKER_MODE",
)
CONTROL_REPORT_ENV_VARS = (
    "APPLYPILOT_AGENT_REPORT_PATH",
    "APPLYPILOT_AGENT_RUN_ID",
    "APPLYPILOT_TOOL_BROKER_MODE",
)


def bound_visual_bridge_dir(*, phase: str, cdp_port: int, application_url: str) -> str | None:
    """Opt in only to a live supervisor explicitly bound to this worker page.

    A desktop/browser plugin session is not interchangeable with a worker CDP
    session. The attending supervisor must verify that binding before attaching.
    """
    directory = os.environ.get("APPLYPILOT_VISUAL_BRIDGE_DIR")
    if phase != "prepare" or not directory:
        return None
    from applypilot.apply.visual_bridge import VisualBridgeError, read_active_host

    try:
        host = read_active_host(Path(directory))
        target = host.target
        if (
            target.get("cdp_port") == cdp_port
            and target.get("application_url") == application_url
            and target.get("worker_session_verified") is True
        ):
            return str(Path(directory).resolve())
    except (OSError, ValueError, TypeError, VisualBridgeError):
        pass
    return None


def _project_python_mcp_env_vars(env_vars: tuple[str, ...]) -> list[str]:
    """Keep project-owned Python MCPs on the caller-selected source tree."""

    if os.environ.get("PYTHONPATH"):
        return [*env_vars, "PYTHONPATH"]
    return list(env_vars)


DEFAULT_MAILBOX_BLOCKED_TOOLS = (
    "draft_email",
    "modify_email",
    "delete_email",
    "download_attachment",
    "batch_modify_emails",
    "batch_delete_emails",
    "create_label",
    "update_label",
    "delete_label",
    "get_or_create_label",
    "list_email_labels",
    "create_filter",
    "list_filters",
    "get_filter",
    "delete_filter",
)


def _runtime_tool_names(
    registry: CapabilityRegistry,
    server: str,
    fallback: list[str],
) -> list[str]:
    names = capability_names_for_server(registry, server)
    return names or fallback


def make_mcp_config(
    cdp_port: int,
    *,
    playwright_mcp: McpPackageSpec | dict[str, object] | str | None = None,
    capability_registry: CapabilityRegistry | None = None,
    runtime_metadata: dict[str, Any] | None = None,
    python_executable: str | None = None,
    control_reporting: bool = True,
    application_tools: bool = True,
    mailbox_mcp: MailboxMcpSpec | dict[str, object] | None = None,
    direct_email_send_authorized: bool = False,
    credential_relay_authorized: bool = False,
    identity_relay_authorized: bool = False,
    visual_bridge_dir: str | None = None,
) -> dict:
    """Build MCP config dict for a specific CDP port."""
    spec = resolve_playwright_mcp_spec(playwright_mcp)
    using_default_registry = capability_registry is None
    registry = capability_registry or compose_runtime_capabilities()
    record_runtime_surface(runtime_metadata, spec, registry)
    playwright_tools = _runtime_tool_names(
        registry, "playwright", default_browser_capabilities().names() if using_default_registry else []
    )
    application_tool_names = _runtime_tool_names(
        registry,
        "applypilot_ats",
        (
            capability_names_for_server(compose_runtime_capabilities(), "applypilot_ats")
            if using_default_registry
            else []
        ),
    )
    mailbox_spec = resolve_mailbox_mcp_spec(mailbox_mcp)
    mailbox_tools = mailbox_spec.enabled_tools(
        direct_email_send_authorized=direct_email_send_authorized
    )
    if not using_default_registry:
        mailbox_tools = [name for name in mailbox_tools if registry.get(name) is not None]
    credential_relay_authorized = bool(
        credential_relay_authorized
        and (using_default_registry or registry.get("fill_ats_credentials") is not None)
    )
    identity_relay_authorized = bool(
        identity_relay_authorized
        and (using_default_registry or registry.get("fill_protected_identifier") is not None)
    )
    effective_direct_email_send = mailbox_spec.send_tool in mailbox_tools
    servers: dict[str, dict[str, object]] = {
        "playwright": {
            "command": spec.command,
            "args": [
                *spec.process_args(),
                f"--cdp-endpoint=http://localhost:{cdp_port}",
                f"--viewport-size={config.DEFAULTS['viewport']}",
            ],
        },
    }
    if visual_bridge_dir:
        servers["applypilot_visual"] = {
            "command": python_executable or sys.executable,
            "args": ["-m", "applypilot.apply.visual_bridge_mcp", "--bridge-dir", visual_bridge_dir],
        }
    if mailbox_spec.enabled and mailbox_tools:
        servers[mailbox_spec.server_name] = {
            "command": mailbox_spec.command,
            "args": mailbox_spec.process_args(),
        }
    if credential_relay_authorized or identity_relay_authorized:
        servers["credential_relay"] = {
            "command": python_executable or sys.executable,
            "args": ["-m", "applypilot.apply.credential_relay_mcp"],
        }
    if runtime_metadata is not None:
        runtime_metadata["mailbox_mcp"] = mailbox_spec.metadata(
            direct_email_send_authorized=effective_direct_email_send
        )
    if control_reporting:
        servers["applypilot_control"] = {
            "command": python_executable or sys.executable,
            "args": ["-m", "applypilot.apply.agent_report_mcp"],
        }
        if runtime_metadata is not None:
            runtime_metadata["control_plane"] = {
                "schema_version": "1",
                "tools": ["report_agent_turn"],
            }
    if application_tools:
        servers["applypilot_ats"] = {
            "command": python_executable or sys.executable,
            "args": ["-m", "applypilot.apply.ats_tools_mcp"],
        }
        if runtime_metadata is not None:
            runtime_metadata["application_tools"] = {
                "schema_version": "1",
                "tools": application_tool_names,
            }
    if runtime_metadata is not None:
        runtime_metadata["playwright_tools"] = playwright_tools
    return {"mcpServers": servers}


def resolve_claude_command() -> list[str]:
    """Resolve the Claude CLI to a directly executable command.

    npm exposes extensionless and .cmd shims on Windows. ``CreateProcess``
    cannot execute the extensionless POSIX shim, even though ``shutil.which``
    reports it as available. Current Claude Code npm installs include a native
    executable beside the shim; prefer it and fall back to ``cmd /c`` only for
    older layouts.
    """
    candidate = shutil.which("claude")
    if platform.system() != "Windows":
        if not candidate:
            raise FileNotFoundError("Claude Code CLI was not found on PATH.")
        return [candidate]

    cmd_shim = shutil.which("claude.cmd")
    shim = Path(cmd_shim or candidate or "")
    if shim:
        native = shim.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if native.is_file():
            return [str(native)]

    if cmd_shim:
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", cmd_shim]
    raise FileNotFoundError("Claude Code CLI was not found on PATH.")


def resolve_codex_command() -> list[str]:
    """Prefer the newest verified Windows CLI across App and npm installs."""
    override = os.environ.get("APPLYPILOT_CODEX_EXECUTABLE", "").strip()
    if override:
        executable = Path(override).expanduser().resolve()
        if not executable.is_file():
            raise FileNotFoundError(f"Configured Codex executable does not exist: {executable}")
        return [str(executable)]
    if platform.system() == "Windows":
        cmd_shim = shutil.which("codex.cmd")
        npm_native = None
        if cmd_shim:
            npm_root = Path(cmd_shim).parent / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai"
            native_candidates = sorted(npm_root.glob("codex-win32-*/vendor/*/bin/codex.exe"))
            if native_candidates:
                npm_native = native_candidates[0]
        local_app_data = os.environ.get("LOCALAPPDATA")
        bundled = (
            list((Path(local_app_data) / "OpenAI" / "Codex" / "bin").glob("*/codex.exe"))
            if local_app_data else []
        )
        if bundled:
            candidates = [*bundled, *([npm_native] if npm_native else [])]
            path_native = shutil.which("codex.exe")
            if path_native:
                candidates.append(Path(path_native))
            verified = [(version, path) for path in dict.fromkeys(candidates)
                        if (version := _codex_executable_version(path)) is not None]
            if verified:
                version, executable = max(verified, key=lambda item: item[0])
                logger.info("Selected Codex %s at %s", ".".join(map(str, version)), executable)
                return [str(executable)]
        if npm_native:
            return [str(npm_native)]
        if cmd_shim:
            return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", cmd_shim]
        native = shutil.which("codex.exe")
        if native:
            return [native]
    else:
        native = shutil.which("codex")
        if native:
            return [native]
    raise FileNotFoundError("Codex CLI was not found on PATH.")


def _codex_executable_version(path: Path) -> tuple[int, int, int] | None:
    try:
        result = subprocess.run(
            [str(path), "--version"], capture_output=True, text=True,
            timeout=3, check=False,
        )
        match = re.fullmatch(r"codex-cli (\d+)\.(\d+)\.(\d+)\s*", result.stdout)
        if result.returncode == 0 and match:
            return tuple(map(int, match.groups()))
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _toml_value(value: object) -> str:
    """Encode JSON-compatible values accepted by Codex's TOML overrides."""
    return json.dumps(value, ensure_ascii=False)


def apply_mcp_process_environment(
    target: MutableMapping[str, str],
    spec: McpPackageSpec | MailboxMcpSpec | None,
) -> None:
    """Pass server env through process inheritance, not config files or argv."""
    if spec is not None:
        target.update(spec.env)


def _toml_skill_config(paths: list[Path]) -> str:
    """Encode Codex's array-of-tables skill override without stringifying it."""
    entries = ",".join(
        f"{{path={_toml_value(str(path))},enabled=false}}" for path in paths
    )
    return f"[{entries}]"


def _persistent_playwright_url(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("persistent Playwright MCP URL has an invalid port") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or parsed.path != "/mcp"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "persistent Playwright MCP URL must be http://127.0.0.1:<port>/mcp"
        )
    return f"http://127.0.0.1:{port}/mcp"


def build_agent_command(
    backend: str,
    model: str,
    port: int,
    worker_dir: Path,
    mcp_config_path: Path,
    *,
    resolve_claude: Callable[[], list[str]] = resolve_claude_command,
    resolve_codex: Callable[[], list[str]] = resolve_codex_command,
    python_executable: str | None = None,
    credential_relay_authorized: bool = False,
    identity_relay_authorized: bool = False,
    playwright_mcp: McpPackageSpec | dict[str, object] | str | None = None,
    capability_registry: CapabilityRegistry | None = None,
    runtime_metadata: dict[str, Any] | None = None,
    mailbox_mcp: MailboxMcpSpec | dict[str, object] | None = None,
    direct_email_send_authorized: bool = False,
    workload_class: str | None = None,
    reasoning_efforts: dict[str, str] | None = None,
    playwright_mcp_url: str | None = None,
    resolved_configuration: AgentRuntimeConfiguration | None = None,
    visual_bridge_dir: str | None = None,
) -> tuple[list[str], Path | None]:
    """Build an isolated browser-agent command for Claude or Codex."""
    spec = resolve_playwright_mcp_spec(playwright_mcp)
    using_default_registry = capability_registry is None
    registry = capability_registry or compose_runtime_capabilities()
    mailbox_spec = resolve_mailbox_mcp_spec(mailbox_mcp)
    mailbox_tools = mailbox_spec.enabled_tools(
        direct_email_send_authorized=direct_email_send_authorized
    )
    if not using_default_registry:
        mailbox_tools = [name for name in mailbox_tools if registry.get(name) is not None]
    credential_relay_authorized = bool(
        credential_relay_authorized
        and (using_default_registry or registry.get("fill_ats_credentials") is not None)
    )
    identity_relay_authorized = bool(
        identity_relay_authorized
        and (using_default_registry or registry.get("fill_protected_identifier") is not None)
    )
    effective_direct_email_send = mailbox_spec.send_tool in mailbox_tools
    playwright_tools = _runtime_tool_names(
        registry, "playwright", default_browser_capabilities().names() if using_default_registry else []
    )
    application_tool_names = _runtime_tool_names(
        registry,
        "applypilot_ats",
        (
            capability_names_for_server(compose_runtime_capabilities(), "applypilot_ats")
            if using_default_registry
            else []
        ),
    )
    control_tool_names = _runtime_tool_names(
        registry, "applypilot_control", ["report_agent_turn"] if using_default_registry else []
    )
    runtime_configuration = resolved_configuration or resolve_agent_runtime_configuration(
        backend,
        model,
        workload_class=workload_class,
        reasoning_efforts=reasoning_efforts,
    )
    if runtime_configuration.backend != backend.strip().casefold():
        raise ValueError("resolved runtime backend does not match command backend")
    if runtime_configuration.model != model:
        raise ValueError("resolved runtime model does not match command model")
    reasoning_effort = runtime_configuration.reasoning.value
    record_runtime_surface(runtime_metadata, spec, registry)
    if runtime_metadata is not None:
        runtime_metadata["runtime_configuration"] = runtime_configuration.as_dict()
        runtime_metadata["control_plane"] = {
            "schema_version": "1",
            "tools": control_tool_names,
        }
        runtime_metadata["application_tools"] = {
            "schema_version": "1",
            "tools": application_tool_names,
        }
        runtime_metadata["mailbox_mcp"] = mailbox_spec.metadata(
            direct_email_send_authorized=effective_direct_email_send
        )
    if backend == "claude":
        allowed_mcp_tools = [
            *(f"mcp__playwright__{name}" for name in playwright_tools),
            *(f"mcp__applypilot_control__{name}" for name in control_tool_names),
            *(f"mcp__applypilot_ats__{name}" for name in application_tool_names),
        ]
        if visual_bridge_dir:
            allowed_mcp_tools.append("mcp__applypilot_visual__visual_operation")
        if credential_relay_authorized:
            allowed_mcp_tools.append("mcp__credential_relay__fill_ats_credentials")
        if identity_relay_authorized:
            allowed_mcp_tools.append(
                "mcp__credential_relay__fill_protected_identifier"
            )
        if mailbox_spec.enabled and mailbox_tools:
            allowed_mcp_tools.extend(
                f"mcp__{mailbox_spec.server_name}__{name}"
                for name in mailbox_tools
            )
        disallowed_mailbox_tools = []
        if mailbox_spec.enabled:
            disallowed_mailbox_tools.extend(
                f"mcp__{mailbox_spec.server_name}__{name}"
                for name in DEFAULT_MAILBOX_BLOCKED_TOOLS
            )
            if not effective_direct_email_send:
                disallowed_mailbox_tools.append(
                    f"mcp__{mailbox_spec.server_name}__{mailbox_spec.send_tool}"
                )
        return (
            resolve_claude()
            + [
                "--model", model,
                "-p",
                "--mcp-config", str(mcp_config_path),
                "--strict-mcp-config",
                "--tools", "",
                "--allowedTools", ",".join(allowed_mcp_tools),
                "--disable-slash-commands",
                "--permission-mode", "bypassPermissions",
                "--no-session-persistence",
                "--disallowedTools",
                ",".join(disallowed_mailbox_tools),
                "--output-format", "stream-json",
                "--verbose", "-",
            ],
            None,
        )

    if backend != "codex":
        raise ValueError("agent backend must be 'codex' or 'claude'.")

    final_message_path = worker_dir / "codex-final-message.txt"
    enabled_tools = playwright_tools
    persistent_url = _persistent_playwright_url(playwright_mcp_url)
    playwright_connection_overrides: list[str]
    if persistent_url is not None:
        playwright_connection_overrides = [
            f"mcp_servers.playwright.url={_toml_value(persistent_url)}"
        ]
    else:
        if spec.command == "npx" and platform.system() == "Windows":
            playwright_command = os.environ.get("COMSPEC", "cmd.exe")
            playwright_args = ["/d", "/s", "/c", "npx"]
        elif spec.command == "npx":
            playwright_command = shutil.which("npx")
            if not playwright_command:
                raise FileNotFoundError("npx was not found on PATH.")
            playwright_args = []
        else:
            playwright_command = spec.command
            playwright_args = []
        playwright_args.extend([
            *spec.process_args(),
            f"--cdp-endpoint=http://localhost:{port}",
            f"--viewport-size={config.DEFAULTS['viewport']}",
        ])
        playwright_connection_overrides = [
            f"mcp_servers.playwright.command={_toml_value(playwright_command)}",
            f"mcp_servers.playwright.args={_toml_value(playwright_args)}",
        ]
    disabled_skills = [
        Path.home() / ".codex" / "skills" / "gstack-browse",
        Path.home() / ".codex" / "skills" / "playwright",
    ]
    command = resolve_codex() + [
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox", "read-only",
        "--model", model,
        "-c", f"model_reasoning_effort={_toml_value(reasoning_effort)}",
        "-c", "features.shell_tool=false",
        "-c", "features.skill_mcp_dependency_install=false",
        "-c", 'web_search="disabled"',
        "-c", f"skills.config={_toml_skill_config(disabled_skills)}",
    ]
    for override in playwright_connection_overrides:
        command.extend(["-c", override])
    command.extend([
        "-c", "mcp_servers.playwright.required=true",
        "-c", f"mcp_servers.playwright.startup_timeout_sec={spec.startup_timeout_seconds}",
        "-c", f"mcp_servers.playwright.tool_timeout_sec={spec.tool_timeout_seconds}",
        "-c", f"mcp_servers.playwright.enabled_tools={_toml_value(enabled_tools)}",
        "-c", 'mcp_servers.playwright.default_tools_approval_mode="approve"',
    ])
    if mailbox_spec.enabled and mailbox_tools:
        if mailbox_spec.command == "npx" and platform.system() == "Windows":
            mailbox_command = os.environ.get("COMSPEC", "cmd.exe")
            mailbox_args = ["/d", "/s", "/c", "npx", *mailbox_spec.process_args()]
        elif mailbox_spec.command == "npx":
            mailbox_command = shutil.which("npx")
            if not mailbox_command:
                raise FileNotFoundError("npx was not found on PATH.")
            mailbox_args = mailbox_spec.process_args()
        else:
            mailbox_command = mailbox_spec.command
            mailbox_args = mailbox_spec.process_args()
        server_key = f"mcp_servers.{mailbox_spec.server_name}"
        command.extend([
            "-c", f"{server_key}.command={_toml_value(mailbox_command)}",
            "-c", f"{server_key}.args={_toml_value(mailbox_args)}",
            "-c", f"{server_key}.required=false",
            "-c", f"{server_key}.startup_timeout_sec={mailbox_spec.startup_timeout_seconds}",
            "-c", f"{server_key}.tool_timeout_sec={mailbox_spec.tool_timeout_seconds}",
            "-c", (
                f"{server_key}.enabled_tools="
                f"{_toml_value(mailbox_tools)}"
            ),
            "-c", f'{server_key}.default_tools_approval_mode="approve"',
        ])
        if mailbox_spec.env:
            command.extend([
                "-c",
                f"{server_key}.env_vars={_toml_value(sorted(mailbox_spec.env))}",
            ])
    relay_tools = []
    if credential_relay_authorized:
        relay_tools.append("fill_ats_credentials")
    if identity_relay_authorized:
        relay_tools.append("fill_protected_identifier")
    if relay_tools:
        command.extend([
            "-c", f"mcp_servers.credential_relay.command={_toml_value(python_executable or sys.executable)}",
            "-c", f"mcp_servers.credential_relay.args={_toml_value(['-m', 'applypilot.apply.credential_relay_mcp'])}",
            "-c", (
                "mcp_servers.credential_relay.env_vars="
                f"{_toml_value(_project_python_mcp_env_vars(CREDENTIAL_RELAY_ENV_VARS))}"
            ),
            "-c", "mcp_servers.credential_relay.required=true",
            "-c", "mcp_servers.credential_relay.startup_timeout_sec=20",
            "-c", "mcp_servers.credential_relay.tool_timeout_sec=30",
            "-c", f"mcp_servers.credential_relay.enabled_tools={_toml_value(relay_tools)}",
            "-c", 'mcp_servers.credential_relay.default_tools_approval_mode="approve"',
        ])
    command.extend([
        "-c", f"mcp_servers.applypilot_ats.command={_toml_value(python_executable or sys.executable)}",
        "-c", f"mcp_servers.applypilot_ats.args={_toml_value(['-m', 'applypilot.apply.ats_tools_mcp'])}",
        "-c", (
            "mcp_servers.applypilot_ats.env_vars="
            f"{_toml_value(_project_python_mcp_env_vars(APPLICATION_TOOL_ENV_VARS))}"
        ),
        "-c", "mcp_servers.applypilot_ats.startup_timeout_sec=20",
        "-c", "mcp_servers.applypilot_ats.tool_timeout_sec=20",
        "-c", f"mcp_servers.applypilot_ats.enabled_tools={_toml_value(application_tool_names)}",
        "-c", 'mcp_servers.applypilot_ats.default_tools_approval_mode="approve"',
    ])
    command.extend([
        "-c", f"mcp_servers.applypilot_control.command={_toml_value(python_executable or sys.executable)}",
        "-c", f"mcp_servers.applypilot_control.args={_toml_value(['-m', 'applypilot.apply.agent_report_mcp'])}",
        "-c", (
            "mcp_servers.applypilot_control.env_vars="
            f"{_toml_value(_project_python_mcp_env_vars(CONTROL_REPORT_ENV_VARS))}"
        ),
        "-c", "mcp_servers.applypilot_control.startup_timeout_sec=20",
        "-c", "mcp_servers.applypilot_control.tool_timeout_sec=20",
        "-c", f"mcp_servers.applypilot_control.enabled_tools={_toml_value(control_tool_names)}",
        "-c", 'mcp_servers.applypilot_control.default_tools_approval_mode="approve"',
    ])
    if visual_bridge_dir:
        command.extend([
            "-c", f"mcp_servers.applypilot_visual.command={_toml_value(python_executable or sys.executable)}",
            "-c", f"mcp_servers.applypilot_visual.args={_toml_value(['-m', 'applypilot.apply.visual_bridge_mcp', '--bridge-dir', visual_bridge_dir])}",
            "-c", "mcp_servers.applypilot_visual.required=true",
            "-c", "mcp_servers.applypilot_visual.tool_timeout_sec=130",
            "-c", f"mcp_servers.applypilot_visual.env_vars={_toml_value(_project_python_mcp_env_vars(('APPLYPILOT_VISUAL_BRIDGE_TIMEOUT_SECONDS',)))}",
            "-c", 'mcp_servers.applypilot_visual.enabled_tools=["visual_operation"]',
            "-c", 'mcp_servers.applypilot_visual.default_tools_approval_mode="approve"',
        ])
        if runtime_metadata is not None:
            runtime_metadata["visual_bridge"] = {"mode": "supervised", "tools": ["visual_operation"]}
    command.extend([
        "--json",
        "--output-last-message", str(final_message_path),
        "-C", str(worker_dir),
        "-",
    ])
    return command, final_message_path
