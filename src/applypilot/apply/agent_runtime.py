"""Isolated browser-agent command and process-runtime helpers.

The launcher owns orchestration and injects mutable runtime dependencies.  This
module owns deterministic command assembly, executable resolution, and timeout
watchdog construction.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from applypilot import config


def make_mcp_config(cdp_port: int) -> dict:
    """Build MCP config dict for a specific CDP port."""
    return {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": [
                    "@playwright/mcp@latest",
                    f"--cdp-endpoint=http://localhost:{cdp_port}",
                    f"--viewport-size={config.DEFAULTS['viewport']}",
                ],
            },
            "gmail": {
                "command": "npx",
                "args": ["-y", "@gongrzhe/server-gmail-autoauth-mcp"],
            },
        }
    }


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


def _toml_skill_config(paths: list[Path]) -> str:
    """Encode Codex's array-of-tables skill override without stringifying it."""
    entries = ",".join(
        f"{{path={_toml_value(str(path))},enabled=false}}" for path in paths
    )
    return f"[{entries}]"


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
) -> tuple[list[str], Path | None]:
    """Build an isolated browser-agent command for Claude or Codex."""
    if backend == "claude":
        return (
            resolve_claude()
            + [
                "--model", model,
                "-p",
                "--mcp-config", str(mcp_config_path),
                "--permission-mode", "bypassPermissions",
                "--no-session-persistence",
                "--disallowedTools",
                (
                    "mcp__gmail__draft_email,mcp__gmail__modify_email,"
                    "mcp__gmail__delete_email,mcp__gmail__download_attachment,"
                    "mcp__gmail__batch_modify_emails,mcp__gmail__batch_delete_emails,"
                    "mcp__gmail__create_label,mcp__gmail__update_label,"
                    "mcp__gmail__delete_label,mcp__gmail__get_or_create_label,"
                    "mcp__gmail__list_email_labels,mcp__gmail__create_filter,"
                    "mcp__gmail__list_filters,mcp__gmail__get_filter,"
                    "mcp__gmail__delete_filter"
                ),
                "--output-format", "stream-json",
                "--verbose", "-",
            ],
            None,
        )

    if backend != "codex":
        raise ValueError("agent backend must be 'codex' or 'claude'.")

    final_message_path = worker_dir / "codex-final-message.txt"
    enabled_tools = [
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
    ]
    if platform.system() == "Windows":
        playwright_command = os.environ.get("COMSPEC", "cmd.exe")
        playwright_args = ["/d", "/s", "/c", "npx"]
    else:
        playwright_command = shutil.which("npx")
        if not playwright_command:
            raise FileNotFoundError("npx was not found on PATH.")
        playwright_args = []
    playwright_args.extend([
        "-y",
        "@playwright/mcp@latest",
        f"--cdp-endpoint=http://localhost:{port}",
        f"--viewport-size={config.DEFAULTS['viewport']}",
    ])
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
        "-c", 'model_reasoning_effort="high"',
        "-c", "features.shell_tool=false",
        "-c", "features.skill_mcp_dependency_install=false",
        "-c", 'web_search="disabled"',
        "-c", f"skills.config={_toml_skill_config(disabled_skills)}",
        "-c", f"mcp_servers.playwright.command={_toml_value(playwright_command)}",
        "-c", f"mcp_servers.playwright.args={_toml_value(playwright_args)}",
        "-c", "mcp_servers.playwright.required=true",
        "-c", "mcp_servers.playwright.startup_timeout_sec=60",
        "-c", "mcp_servers.playwright.tool_timeout_sec=90",
        "-c", f"mcp_servers.playwright.enabled_tools={_toml_value(enabled_tools)}",
        "-c", 'mcp_servers.playwright.default_tools_approval_mode="approve"',
    ]
    if credential_relay_authorized:
        command.extend([
            "-c", f"mcp_servers.credential_relay.command={_toml_value(python_executable or sys.executable)}",
            "-c", f"mcp_servers.credential_relay.args={_toml_value(['-m', 'applypilot.apply.credential_relay_mcp'])}",
            "-c", "mcp_servers.credential_relay.required=true",
            "-c", "mcp_servers.credential_relay.startup_timeout_sec=20",
            "-c", "mcp_servers.credential_relay.tool_timeout_sec=30",
            "-c", 'mcp_servers.credential_relay.default_tools_approval_mode="approve"',
        ])
    command.extend([
        "--json",
        "--output-last-message", str(final_message_path),
        "-C", str(worker_dir),
        "-",
    ])
    return command, final_message_path


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------
