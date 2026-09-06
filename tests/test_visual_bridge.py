from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from applypilot.apply import visual_bridge, visual_bridge_mcp


@pytest.mark.parametrize("authorized", [False, None, 1, "true"])
def test_submit_host_requires_explicit_boolean_authorization(tmp_path, authorized):
    with pytest.raises(visual_bridge.VisualBridgeError, match="explicit host authorization"):
        visual_bridge.write_host_metadata(tmp_path, session_id="s", token_epoch="e", surface="browser",
            phase="submit", submission_authorized=authorized,
            target={"runtime": "iab", "tab_id": "real-tab", "application_url": "https://jobs.example.test/apply"})


def test_submit_binding_and_upload_reference_validation(tmp_path):
    visual_bridge.write_host_metadata(tmp_path, session_id="s", token_epoch="e", surface="browser",
        phase="submit", submission_authorized=True,
        target={"runtime": "iab", "tab_id": "real-tab", "application_url": "https://jobs.example.test/apply"})
    assert visual_bridge.read_active_host(tmp_path).submission_authorized is True
    visual_bridge._validate_operation("upload_artifact", "fresh", {"artifact_id": "resume", "node_id": "4"})
    for args in ({"path": "private-file", "node_id": "4"}, {"artifact_id": "resume"}):
        with pytest.raises(visual_bridge.VisualBridgeError):
            visual_bridge.request_visual_operation(tmp_path, operation="upload_artifact", observation_id="fresh", arguments=args)
    assert not list((tmp_path / "pending").glob("*.json"))


def _host(root: Path, *, heartbeat_at: float | None = None) -> dict[str, object]:
    return visual_bridge.write_host_metadata(
        root,
        session_id="session-1",
        token_epoch="epoch-1",
        surface="browser",
        target={
            "cdp_port": 9222,
            "application_url": "https://jobs.example.test/application/123",
        },
        heartbeat_at=heartbeat_at,
    )


def test_visual_request_is_claimed_and_returns_text_and_image(tmp_path: Path) -> None:
    _host(tmp_path)

    def host() -> None:
        pending_dir = tmp_path / "pending"
        deadline = time.time() + 2
        pending: Path | None = None
        while time.time() < deadline and pending is None:
            pending = next(pending_dir.glob("*.json"), None)
            time.sleep(0.005)
        assert pending is not None
        request = json.loads(pending.read_text(encoding="utf-8"))
        claimed = tmp_path / "claimed" / pending.name
        os.replace(pending, claimed)
        visual_bridge.write_bridge_response(
            tmp_path,
            {
                "schema_version": 1,
                "request_id": request["request_id"],
                "session_id": request["session_id"],
                "token_epoch": request["token_epoch"],
                "ok": True,
                "outcome": "completed",
                "observation_id": "observation-1",
                "content": [
                    {"type": "text", "text": "Application form visible"},
                    {"type": "image", "data": "cG5n", "mimeType": "image/png"},
                ],
            },
        )

    worker = threading.Thread(target=host)
    worker.start()
    response = visual_bridge.request_visual_operation(
        tmp_path,
        operation="observe",
        arguments={"mode": "screenshot"},
        timeout_seconds=2,
    )
    worker.join(timeout=2)

    assert response["ok"] is True
    assert response["observation_id"] == "observation-1"
    assert [block["type"] for block in response["content"]] == ["text", "image"]
    assert not list((tmp_path / "claimed").glob("*.json"))


def test_unclaimed_timeout_is_atomically_cancelled_and_cannot_be_late_claimed(tmp_path: Path) -> None:
    _host(tmp_path)

    with pytest.raises(visual_bridge.VisualBridgeError) as raised:
        visual_bridge.request_visual_operation(
            tmp_path,
            operation="observe",
            arguments={},
            timeout_seconds=0.03,
            poll_interval_seconds=0.005,
        )

    assert raised.value.code == "cancelled"
    assert not list((tmp_path / "pending").glob("*.json"))
    cancelled = list((tmp_path / "cancelled").glob("*.json"))
    assert len(cancelled) == 1
    with pytest.raises(FileNotFoundError):
        os.replace(tmp_path / "pending" / cancelled[0].name, tmp_path / "claimed" / cancelled[0].name)


def test_claimed_timeout_reports_unknown_instead_of_replaying(tmp_path: Path) -> None:
    _host(tmp_path)

    def claim_only() -> None:
        while not (pending := next((tmp_path / "pending").glob("*.json"), None)):
            time.sleep(0.002)
        os.replace(pending, tmp_path / "claimed" / pending.name)

    worker = threading.Thread(target=claim_only)
    worker.start()
    with pytest.raises(visual_bridge.VisualBridgeError) as raised:
        visual_bridge.request_visual_operation(
            tmp_path,
            operation="observe",
            arguments={},
            timeout_seconds=0.05,
            poll_interval_seconds=0.005,
        )
    worker.join(timeout=1)

    assert raised.value.code == "outcome_unknown"
    assert len(list((tmp_path / "claimed").glob("*.json"))) == 1


def test_stale_host_and_unbounded_arguments_are_rejected_before_queueing(tmp_path: Path) -> None:
    _host(tmp_path, heartbeat_at=time.time() - 121)
    with pytest.raises(visual_bridge.VisualBridgeError, match="heartbeat is stale"):
        visual_bridge.request_visual_operation(
            tmp_path,
            operation="observe",
            arguments={},
            timeout_seconds=0.01,
        )

    _host(tmp_path)
    with pytest.raises(visual_bridge.VisualBridgeError) as raised:
        visual_bridge.request_visual_operation(
            tmp_path,
            operation="click",
            observation_id="obs-1",
            arguments={"url": "https://other.example.test"},
            timeout_seconds=0.01,
        )
    assert raised.value.code == "invalid_request"
    assert not list((tmp_path / "pending").glob("*.json"))


def test_mcp_surfaces_structured_not_available_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(visual_bridge_mcp.BRIDGE_DIR_ENV, raising=False)
    response = visual_bridge_mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "visual_operation",
                "arguments": {"operation": "observe", "arguments": {}},
            },
        }
    )

    assert response is not None
    assert response["result"]["isError"] is True
    assert response["result"]["structuredContent"] == {
        "code": "not_available",
        "outcome": "not_available",
    }
    tool = visual_bridge_mcp._handle({"jsonrpc": "2.0", "id": 8, "method": "tools/list"})
    assert tool is not None
    schema = tool["result"]["tools"][0]["inputSchema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["arguments"]["additionalProperties"] is False


def test_loopback_http_is_allowed_and_missing_type_text_is_structured_error(tmp_path: Path) -> None:
    visual_bridge.write_host_metadata(
        tmp_path,
        session_id="session-local",
        token_epoch="epoch-local",
        surface="browser",
        target={"cdp_port": 8766, "application_url": "http://127.0.0.1:8766/job/1"},
    )
    assert visual_bridge.read_active_host(tmp_path).target["cdp_port"] == 8766

    with pytest.raises(visual_bridge.VisualBridgeError) as raised:
        visual_bridge.request_visual_operation(
            tmp_path,
            operation="type_text",
            observation_id="obs-1",
            arguments={},
            timeout_seconds=0.01,
        )
    assert raised.value.code == "invalid_request"


def test_existing_claimed_request_blocks_a_second_operation(tmp_path: Path) -> None:
    _host(tmp_path)
    (tmp_path / "claimed" / "old.json").write_text("{}", encoding="utf-8")

    with pytest.raises(visual_bridge.VisualBridgeError) as raised:
        visual_bridge.request_visual_operation(
            tmp_path,
            operation="observe",
            arguments={},
            timeout_seconds=0.01,
        )

    assert raised.value.code == "busy"
    assert not list((tmp_path / "pending").glob("*.json"))


def test_iab_discovery_uses_tab_identity_without_cdp_alias(tmp_path: Path) -> None:
    target = {"runtime": "iab", "tab_id": "tab-7", "application_url": "https://www.linkedin.com/jobs/"}
    visual_bridge.write_host_metadata(tmp_path, session_id="s", token_epoch="e",
                                      surface="browser", target=target, phase="discovery")
    assert visual_bridge.read_active_host(tmp_path).phase == "discovery"
    with pytest.raises(visual_bridge.VisualBridgeError, match="impersonate"):
        visual_bridge.write_host_metadata(tmp_path, session_id="s", token_epoch="e",
                                          surface="browser", target={**target, "cdp_port": 9222})


def test_action_can_request_its_result_observation_format() -> None:
    visual_bridge._validate_operation("navigate", "fresh", {"url": "https://example.test/job", "mode": "dom"})
    visual_bridge._validate_operation("click", "fresh", {"node_id": "2", "mode": "screenshot"})
    visual_bridge._validate_operation("type_text", "fresh", {"node_id": "2", "text": "Migration Test", "mode": "dom"})
    with pytest.raises(visual_bridge.VisualBridgeError):
        visual_bridge._validate_operation("navigate", "fresh", {"url": "https://example.test/job", "mode": "eval"})


def test_host_pause_cancels_waiting_input_promptly(tmp_path: Path) -> None:
    metadata = _host(tmp_path)

    def pause() -> None:
        while not list((tmp_path / "pending").glob("*.json")):
            time.sleep(0.005)
        visual_bridge.write_host_metadata(tmp_path, session_id="session-1", token_epoch="epoch-1",
                                          surface="browser", target=metadata["target"], status="paused")

    worker = threading.Thread(target=pause, daemon=True)
    worker.start()
    started = time.monotonic()
    with pytest.raises(visual_bridge.VisualBridgeError, match="paused"):
        visual_bridge.request_visual_operation(tmp_path, operation="click", observation_id="old",
                                                arguments={"node_id": "3"}, timeout_seconds=10)
    worker.join(timeout=1)
    assert time.monotonic() - started < 3
    assert not list((tmp_path / "pending").glob("*.json"))
    assert len(list((tmp_path / "cancelled").glob("*.json"))) == 1
