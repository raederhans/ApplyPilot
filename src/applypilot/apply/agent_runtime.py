"""Isolated browser-agent command and process-runtime helpers.

The launcher owns orchestration and injects mutable runtime dependencies.  This
module owns deterministic command assembly, executable resolution, and timeout
watchdog construction.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

from applypilot import config
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
_REAL_POPEN_TYPE = subprocess.Popen


def _current_working_set_bytes(counters: object) -> int:
    """Return current Windows working-set bytes, never the historical peak."""

    return max(0, int(getattr(counters, "WorkingSetSize", 0)))


def process_rss_bytes(pid: int) -> int:
    """Return best-effort resident bytes for one child process.

    Telemetry must never affect runtime authority, so unsupported platforms,
    exited processes, and access-denied handles return zero.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return 0
    try:
        if platform.system() == "Windows":
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            process_vm_read = 0x0010

            class ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(ProcessMemoryCounters),
                wintypes.DWORD,
            ]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(
                process_query_limited_information | process_vm_read,
                False,
                pid,
            )
            if not handle:
                return 0
            try:
                counters = ProcessMemoryCounters()
                counters.cb = ctypes.sizeof(counters)
                if not psapi.GetProcessMemoryInfo(
                    handle,
                    ctypes.byref(counters),
                    counters.cb,
                ):
                    return 0
                return _current_working_set_bytes(counters)
            finally:
                kernel32.CloseHandle(handle)

        status = Path(f"/proc/{pid}/status")
        if status.is_file():
            for line in status.read_text(encoding="ascii", errors="ignore").splitlines():
                if line.startswith("VmRSS:"):
                    fields = line.split()
                    return max(0, int(fields[1]) * 1024)
    except (OSError, TypeError, ValueError):
        return 0
    return 0

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


def resolve_reasoning_effort(
    workload_class: str | None = None,
    *,
    configured: dict[str, str] | None = None,
    environ: dict[str, str] | None = None,
) -> str:
    """Resolve a configurable workload effort while preserving the old default."""
    environment = os.environ if environ is None else environ
    mapping: dict[str, str] = {"default": "high"}
    raw = environment.get("APPLYPILOT_REASONING_EFFORTS")
    if raw:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("APPLYPILOT_REASONING_EFFORTS must be a JSON object")
        mapping.update({str(key): str(value) for key, value in parsed.items()})
    mapping.update(configured or {})
    return mapping.get(workload_class or "default", mapping["default"])


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
    """Resolve Codex to a native executable suitable for ``Popen``."""
    if platform.system() == "Windows":
        cmd_shim = shutil.which("codex.cmd")
        if cmd_shim:
            npm_root = Path(cmd_shim).parent / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai"
            native_candidates = sorted(npm_root.glob("codex-win32-*/vendor/*/bin/codex.exe"))
            if native_candidates:
                return [str(native_candidates[0])]
            return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", cmd_shim]
        native = shutil.which("codex.exe")
        if native:
            return [native]
    else:
        native = shutil.which("codex")
        if native:
            return [native]
    raise FileNotFoundError("Codex CLI was not found on PATH.")


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


def start_timeout_watchdog(
    proc: subprocess.Popen,
    timeout_seconds: float,
    *,
    kill_process_tree: Callable[[int], None],
) -> tuple[threading.Event, threading.Timer]:
    """Kill an agent that does not reach EOF before its wall-clock deadline."""
    timed_out = threading.Event()

    def terminate_if_running() -> None:
        if proc.poll() is None:
            timed_out.set()
            kill_process_tree(proc.pid)

    timer = threading.Timer(timeout_seconds, terminate_if_running)
    timer.daemon = True
    timer.start()
    return timed_out, timer


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
    reasoning_effort = resolve_reasoning_effort(
        workload_class,
        configured=reasoning_efforts,
    )
    record_runtime_surface(runtime_metadata, spec, registry)
    if runtime_metadata is not None:
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
                f"{_toml_value(list(CREDENTIAL_RELAY_ENV_VARS))}"
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
            f"{_toml_value(list(APPLICATION_TOOL_ENV_VARS))}"
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
            f"{_toml_value(list(CONTROL_REPORT_ENV_VARS))}"
        ),
        "-c", "mcp_servers.applypilot_control.startup_timeout_sec=20",
        "-c", "mcp_servers.applypilot_control.tool_timeout_sec=20",
        "-c", f"mcp_servers.applypilot_control.enabled_tools={_toml_value(control_tool_names)}",
        "-c", 'mcp_servers.applypilot_control.default_tools_approval_mode="approve"',
    ])
    command.extend([
        "--json",
        "--output-last-message", str(final_message_path),
        "-C", str(worker_dir),
        "-",
    ])
    return command, final_message_path


class SubprocessRuntimeError(RuntimeError):
    """Base failure for the concrete subprocess runtime adapter."""


class RuntimeContinuityError(SubprocessRuntimeError):
    """A resumed turn violated actor, runtime, or profile continuity."""


@dataclass(frozen=True, slots=True)
class SubprocessParentIdentity:
    """Durable parent identity used when the old launcher is no longer in memory."""

    run_id: str
    actor_id: str
    attempt_id: str
    runtime_id: str
    profile_id: str
    submit_started: bool


@dataclass(frozen=True, slots=True)
class SubprocessLaunchSpec:
    """Provider-neutral subprocess turn description.

    The spec controls process lifecycle only.  It carries no browser-write,
    submit, ledger, manifest, or receipt authority.
    """

    run_id: str
    attempt_id: str
    actor_id: str
    turn_id: str
    command: tuple[str, ...]
    prompt: str
    cwd: Path
    env: MutableMapping[str, str]
    runtime_id: str
    profile_id: str
    parent_run_id: str | None = None
    submit_started: bool = False

    def __post_init__(self) -> None:
        for name in (
            "run_id",
            "attempt_id",
            "actor_id",
            "turn_id",
            "runtime_id",
            "profile_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        if self.run_id != self.turn_id:
            raise ValueError("run_id must remain the compatibility alias for turn_id")
        if not self.command or any(not str(item).strip() for item in self.command):
            raise ValueError("command must contain executable arguments")
        if self.parent_run_id is not None and not self.parent_run_id.strip():
            raise ValueError("parent_run_id must be non-empty when provided")

    def redacted_for_history(self) -> SubprocessLaunchSpec:
        """Retain continuity identity without keeping prompt or environment data."""
        return replace(
            self,
            command=(self.command[0],),
            prompt="",
            env={},
        )


@dataclass(frozen=True, slots=True)
class SubprocessRuntimeHealth:
    run_id: str
    status: str
    pid: int | None
    returncode: int | None
    started_at: float
    updated_at: float


@dataclass(slots=True)
class _ManagedSubprocess:
    spec: SubprocessLaunchSpec
    process: subprocess.Popen[str]
    status: str
    started_at: float
    updated_at: float


@runtime_checkable
class SubprocessRuntimeAdapter(Protocol):
    """Lifecycle port implemented by concrete local/provider CLI adapters."""

    def start(
        self,
        spec: SubprocessLaunchSpec,
        *,
        popen_factory: Callable[..., subprocess.Popen[str]] | None = None,
        on_spawned: Callable[[subprocess.Popen[str]], None] | None = None,
    ) -> subprocess.Popen[str]: ...

    def resume(
        self,
        parent_run_id: str,
        spec: SubprocessLaunchSpec,
        *,
        popen_factory: Callable[..., subprocess.Popen[str]] | None = None,
        on_spawned: Callable[[subprocess.Popen[str]], None] | None = None,
        persisted_parent: SubprocessParentIdentity | None = None,
    ) -> subprocess.Popen[str]: ...

    def cancel(self, run_id: str) -> None: ...

    def health(self, run_id: str) -> SubprocessRuntimeHealth: ...

    def close(self, run_id: str | None = None) -> None: ...


class SubprocessAgentRuntime:
    """Real subprocess lifecycle adapter with fresh-turn resume continuity."""

    def __init__(
        self,
        *,
        kill_process_tree: Callable[[int], None],
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._kill_process_tree = kill_process_tree
        self._popen_factory = popen_factory
        self._clock = clock
        self._lock = threading.RLock()
        self._runs: dict[str, _ManagedSubprocess] = {}
        self._closed = False

    @staticmethod
    def _fallback_clock() -> float:
        """Return a cleanup timestamp without relying on an injected clock."""
        try:
            return time.monotonic()
        except BaseException as error:
            logger.debug("fallback monotonic clock failed", exc_info=error)
            return 0.0

    @staticmethod
    def _is_terminated(process: subprocess.Popen[str]) -> bool:
        """Treat an unreadable process state as live so cleanup fails closed."""
        try:
            return process.poll() is not None
        except BaseException as error:
            logger.debug("subprocess poll failed during cleanup", exc_info=error)
            return False

    def _quarantine_spawn_failure(
        self,
        spec: SubprocessLaunchSpec,
        process: subprocess.Popen[str],
        managed: _ManagedSubprocess | None,
    ) -> bool:
        """Stop an exact spawned child, retaining ownership until death is proven."""
        try:
            if not self._is_terminated(process):
                self._kill_process_tree(process.pid)
        except BaseException as error:
            logger.debug("subprocess tree kill failed during quarantine", exc_info=error)
        try:
            process.wait(timeout=5)
        except BaseException as error:
            logger.debug("subprocess wait failed during quarantine", exc_info=error)
        terminated = self._is_terminated(process)
        now = self._fallback_clock()
        with self._lock:
            current = managed or self._runs.get(spec.run_id)
            if current is None:
                current = _ManagedSubprocess(
                    spec=spec,
                    process=process,
                    status="failed" if terminated else "quarantined",
                    started_at=now,
                    updated_at=now,
                )
                self._runs[spec.run_id] = current
            else:
                current.status = "failed" if terminated else "quarantined"
                current.updated_at = now
        return terminated

    def _spawn(
        self,
        spec: SubprocessLaunchSpec,
        *,
        popen_factory: Callable[..., subprocess.Popen[str]] | None = None,
        on_spawned: Callable[[subprocess.Popen[str]], None] | None = None,
    ) -> subprocess.Popen[str]:
        with self._lock:
            if self._closed:
                raise SubprocessRuntimeError("subprocess runtime is closed")
            if spec.run_id in self._runs:
                raise SubprocessRuntimeError(f"run_id already exists: {spec.run_id}")
            factory = popen_factory or self._popen_factory
            process = factory(
                list(spec.command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=dict(spec.env),
                cwd=str(spec.cwd),
            )
            fallback_now = self._fallback_clock()
            managed = _ManagedSubprocess(
                spec=spec,
                process=process,
                status="starting",
                started_at=fallback_now,
                updated_at=fallback_now,
            )
            self._runs[spec.run_id] = managed
        try:
            # Register the exact returned handle before any callback, injected
            # clock, or pipe operation can fail.  A failed cleanup keeps this
            # entry quarantined so close() can retry it.
            if on_spawned is not None:
                on_spawned(process)
            now = self._clock()
            with self._lock:
                managed.status = "running"
                managed.started_at = now
                managed.updated_at = now
            if process.stdin is None:
                raise SubprocessRuntimeError("subprocess stdin pipe was not created")
            process.stdin.write(spec.prompt)
            process.stdin.close()
            # The runtime owns stdin exclusively: the prompt is the complete
            # request, so closing it is how the child receives EOF.  CPython's
            # POSIX ``Popen.communicate()`` may still try to flush a closed
            # public ``stdin`` handle, however.  Detach the consumed pipe from
            # real Popen instances so callers can safely use ``communicate()``
            # to collect output after start().  Keep injected test doubles
            # intact because they model the pipe contract directly.
            if isinstance(process, _REAL_POPEN_TYPE):
                process.stdin = None
        except BaseException as error:
            terminated = self._quarantine_spawn_failure(spec, process, managed)
            if isinstance(error, BrokenPipeError) and terminated:
                try:
                    startup_output = process.stdout.read() if process.stdout else ""
                except BaseException as output_error:
                    logger.debug(
                        "startup output read failed after broken pipe",
                        exc_info=output_error,
                    )
                    startup_output = ""
                raise SubprocessRuntimeError(
                    "Agent exited before accepting the prompt: "
                    + startup_output.strip()[:500]
                ) from error
            raise
        return process

    def start(
        self,
        spec: SubprocessLaunchSpec,
        *,
        popen_factory: Callable[..., subprocess.Popen[str]] | None = None,
        on_spawned: Callable[[subprocess.Popen[str]], None] | None = None,
    ) -> subprocess.Popen[str]:
        """Start a new root turn."""
        if spec.parent_run_id is not None:
            raise RuntimeContinuityError("root start cannot declare parent_run_id")
        return self._spawn(
            spec,
            popen_factory=popen_factory,
            on_spawned=on_spawned,
        )

    def resume(
        self,
        parent_run_id: str,
        spec: SubprocessLaunchSpec,
        *,
        popen_factory: Callable[..., subprocess.Popen[str]] | None = None,
        on_spawned: Callable[[subprocess.Popen[str]], None] | None = None,
        persisted_parent: SubprocessParentIdentity | None = None,
    ) -> subprocess.Popen[str]:
        """Start a fresh child process bound to its completed parent turn."""
        with self._lock:
            parent = self._runs.get(parent_run_id)
            if parent is None and persisted_parent is None:
                raise RuntimeContinuityError("resume parent run is unknown")
            if spec.parent_run_id != parent_run_id:
                raise RuntimeContinuityError("resume parent binding does not match")
            if parent is not None:
                if parent.process.poll() is None:
                    raise RuntimeContinuityError("cannot resume while parent is still running")
                parent_identity = SubprocessParentIdentity(
                    run_id=parent.spec.run_id,
                    actor_id=parent.spec.actor_id,
                    attempt_id=parent.spec.attempt_id,
                    runtime_id=parent.spec.runtime_id,
                    profile_id=parent.spec.profile_id,
                    submit_started=parent.spec.submit_started,
                )
            else:
                assert persisted_parent is not None
                parent_identity = persisted_parent
                if persisted_parent.run_id != parent_run_id:
                    raise RuntimeContinuityError("persisted parent binding does not match")
            if (
                parent_identity.actor_id != spec.actor_id
                or parent_identity.attempt_id != spec.attempt_id
            ):
                raise RuntimeContinuityError(
                    "resume must keep the same actor and application attempt"
                )
            switched = (
                parent_identity.runtime_id != spec.runtime_id
                or parent_identity.profile_id != spec.profile_id
            )
            if switched and (parent_identity.submit_started or spec.submit_started):
                raise RuntimeContinuityError(
                    "runtime/profile switch is forbidden after submit_started"
                )
        return self._spawn(
            spec,
            popen_factory=popen_factory,
            on_spawned=on_spawned,
        )

    def health(self, run_id: str) -> SubprocessRuntimeHealth:
        """Return current process health without interpreting Agent output."""
        with self._lock:
            try:
                managed = self._runs[run_id]
            except KeyError as exc:
                raise KeyError(f"unknown subprocess run: {run_id}") from exc
            returncode = managed.process.poll()
            if managed.status == "running" and returncode is not None:
                managed.status = "completed" if returncode == 0 else "failed"
                managed.updated_at = self._clock()
            return SubprocessRuntimeHealth(
                run_id=run_id,
                status=managed.status,
                pid=managed.process.pid,
                returncode=returncode,
                started_at=managed.started_at,
                updated_at=managed.updated_at,
            )

    def cancel(self, run_id: str) -> None:
        """Cancel exactly one owned subprocess tree."""
        with self._lock:
            try:
                managed = self._runs[run_id]
            except KeyError as exc:
                raise KeyError(f"unknown subprocess run: {run_id}") from exc
            if not self._is_terminated(managed.process):
                try:
                    self._kill_process_tree(managed.process.pid)
                    managed.process.wait(timeout=5)
                except BaseException as error:
                    logger.debug("subprocess cancel cleanup failed", exc_info=error)
            if not self._is_terminated(managed.process):
                managed.status = "quarantined"
                managed.updated_at = self._fallback_clock()
                raise SubprocessRuntimeError(
                    f"cancel could not prove subprocess termination: {run_id}"
                )
            managed.status = "cancelled"
            managed.updated_at = self._fallback_clock()

    def close(self, run_id: str | None = None) -> None:
        """Close one run, or close the adapter and all live children."""
        with self._lock:
            targets = (
                [self._runs[run_id]]
                if run_id is not None and run_id in self._runs
                else list(self._runs.values()) if run_id is None else []
            )
            if run_id is not None and not targets:
                raise KeyError(f"unknown subprocess run: {run_id}")
            quarantined: list[str] = []
            for managed in targets:
                if not self._is_terminated(managed.process):
                    try:
                        self._kill_process_tree(managed.process.pid)
                        managed.process.wait(timeout=5)
                    except BaseException as error:
                        logger.debug("subprocess close cleanup failed", exc_info=error)
                if not self._is_terminated(managed.process):
                    managed.status = "quarantined"
                    managed.updated_at = self._fallback_clock()
                    quarantined.append(managed.spec.run_id)
                    continue
                managed.status = "closed"
                managed.updated_at = self._fallback_clock()
                managed.spec = managed.spec.redacted_for_history()
            if run_id is None and not quarantined:
                self._closed = True
            if quarantined:
                raise SubprocessRuntimeError(
                    "close could not prove subprocess termination: "
                    + ", ".join(quarantined)
                )


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------
