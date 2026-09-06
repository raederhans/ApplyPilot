import json
import time

from applypilot.apply.agent_runtime import bound_visual_bridge_dir, build_agent_command, make_mcp_config


def test_visual_bridge_requires_current_worker_session_and_prepare(tmp_path, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_VISUAL_BRIDGE_DIR", str(tmp_path))
    host = {
        "schema_version": 1, "surface": "browser",
        "status": "active", "phase": "prepare", "heartbeat_at": time.time(),
        "session_id": "test", "token_epoch": "1",
        "target": {"cdp_port": 9222, "application_url": "https://example.test/job", "worker_session_verified": True},
    }
    (tmp_path / "host.json").write_text(json.dumps(host))
    args = {"cdp_port": 9222, "application_url": "https://example.test/job"}
    assert bound_visual_bridge_dir(phase="prepare", **args) == str(tmp_path.resolve())
    assert bound_visual_bridge_dir(phase="submit", **args) is None
    assert bound_visual_bridge_dir(phase="prepare", **{**args, "cdp_port": 9223}) is None
    host["heartbeat_at"] = time.time() - 121
    (tmp_path / "host.json").write_text(json.dumps(host))
    assert bound_visual_bridge_dir(phase="prepare", **args) is None


def test_visual_tool_is_in_both_worker_configurations_only_when_attached(tmp_path):
    config = make_mcp_config(9222, visual_bridge_dir=str(tmp_path))
    assert config["mcpServers"]["applypilot_visual"]["args"][-1] == str(tmp_path)
    assert "applypilot_visual" not in make_mcp_config(9222)["mcpServers"]
    for backend in ("codex", "claude"):
        command, _ = build_agent_command(
            backend, "test-model", 9222, tmp_path, tmp_path / "mcp.json",
            resolve_codex=lambda: ["codex"], resolve_claude=lambda: ["claude"],
            visual_bridge_dir=str(tmp_path),
        )
        assert any("applypilot_visual" in item for item in command)
