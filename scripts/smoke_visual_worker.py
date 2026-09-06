"""Run one isolated Codex worker against an already attached local fixture host.

The attending Codex task services visual-bridge-host.mjs peek/execute calls.
This does not launch a browser or run applications against real employers.
"""
import argparse
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

from applypilot.apply.agent_runtime import resolve_codex_command
from applypilot.apply.visual_bridge import read_active_host


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge-dir", type=Path, required=True)
    parser.add_argument("--codex-executable", type=Path)
    parser.add_argument("--visual", action="store_true", help="Choose the time filter from a screenshot")
    args = parser.parse_args()
    binding = read_active_host(args.bridge_dir)
    if binding.target["application_url"] != "http://127.0.0.1:8766/visual-worker-smoke.html":
        parser.error("This smoke worker is restricted to the local fixture")
    settings = tomllib.loads((Path.home() / ".codex/config.toml").read_text(encoding="utf-8"))
    codex = [str(args.codex_executable)] if args.codex_executable else resolve_codex_command()
    command = codex + [
        "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
        "--skip-git-repo-check", "--sandbox", "read-only", "--model", settings["model"],
        "-c", "features.shell_tool=false", "-c", 'web_search="disabled"',
        "-c", f"mcp_servers.applypilot_visual.command={json.dumps(sys.executable)}",
        "-c", f"mcp_servers.applypilot_visual.args={json.dumps(['-m', 'applypilot.apply.visual_bridge_mcp', '--bridge-dir', str(args.bridge_dir.resolve())])}",
        "-c", 'mcp_servers.applypilot_visual.env_vars=["PYTHONPATH","APPLYPILOT_VISUAL_BRIDGE_TIMEOUT_SECONDS"]',
        "-c", "mcp_servers.applypilot_visual.required=true",
        "-c", 'mcp_servers.applypilot_visual.enabled_tools=["visual_operation"]',
        "-c", 'mcp_servers.applypilot_visual.default_tools_approval_mode="approve"',
        "-c", "mcp_servers.applypilot_visual.tool_timeout_sec=130",
        "--json", "-",
    ]
    env = dict(os.environ)
    env["APPLYPILOT_VISUAL_BRIDGE_TIMEOUT_SECONDS"] = "120"
    prompt = """Exercise the attached applypilot_visual MCP visual_operation tool on the local test fixture.
Use tool discovery or the functions orchestration surface to find and call that
MCP tool if it is deferred; this setup is allowed. Do not conclude it is absent
without searching available deferred tools. All page actions must use visual_operation.
The parent task supervises and services your calls. First observe dom state.
Choose Last 8 hours. Open Product intern from the visible list. Verify details
identify B-202, then click Apply as guest and verify Guest application entry: B-202.
Use returned observation_id for each action. No external navigation, credentials,
uploads or submission. Finish with the observed result.
If a tool reports error, stop and report it. Keep this smoke to <=6 calls."""
    if args.visual:
        prompt = prompt.replace(
            "First observe dom state.\nChoose Last 8 hours.",
            "First observe with mode screenshot. Inspect that image and click Last 24 hours "
            "using x/y coordinates grounded in it. Do not request DOM before that first click.",
        )
    completed = subprocess.run(command, input=prompt, text=True, encoding="utf-8", env=env, check=False)
    raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
