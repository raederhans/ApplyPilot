from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from applypilot.apply.codex_app_server import (
    CodexAppServerAdapter,
    CodexAppServerExecutionError,
    CodexAppServerStdioTransport,
)
from applypilot.apply.runtime_cell import (
    RuntimeCellExecutionState,
    RuntimeCellRequest,
    select_runtime_cell,
)

FAKE_SERVER = r"""
import json
import sys

log_path = sys.argv[1]
mode = sys.argv[2]
thread_counter = 0
turn_counter = 0


def emit(message):
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def record(message):
    with open(log_path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(message, separators=(",", ":")) + "\n")


for raw_line in sys.stdin:
    message = json.loads(raw_line)
    record(message)
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params", {})

    if method == "initialize":
        emit({"id": request_id, "result": {
            "userAgent": "fake-codex-app-server",
            "platformFamily": "windows",
            "platformOs": "windows",
        }})
    elif method == "initialized":
        continue
    elif method == "thread/start":
        thread_counter += 1
        thread_id = f"thread-{thread_counter}"
        emit({"id": request_id, "result": {"thread": {"id": thread_id}}})
        emit({"method": "thread/started", "params": {"thread": {"id": thread_id}}})
    elif method == "thread/resume":
        thread_id = params["threadId"]
        emit({"id": request_id, "result": {"thread": {"id": thread_id}}})
        emit({"method": "thread/started", "params": {"thread": {"id": thread_id}}})
    elif method == "turn/start":
        turn_counter += 1
        turn_id = f"turn-{turn_counter}"
        thread_id = params["threadId"]
        # Exercise notification-before-response ordering. A real app-server may
        # interleave stream notifications with the matching request response.
        emit({"method": "turn/started", "params": {
            "threadId": thread_id,
            "turn": {"id": turn_id, "status": "inProgress", "items": []},
        }})
        emit({"id": request_id, "result": {
            "turn": {"id": turn_id, "status": "inProgress", "items": []}
        }})
        if mode == "effect_hold":
            emit({"method": "item/started", "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "startedAtMs": 1,
                "item": {
                    "id": "tool-1",
                    "type": "mcpToolCall",
                    "server": "playwright",
                    "tool": "browser_snapshot",
                    "status": "inProgress",
                    "arguments": {},
                },
            }})
        elif mode == "complete":
            emit({"method": "item/agentMessage/delta", "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "itemId": "message-1",
                "delta": "RESULT:",
            }})
            emit({"method": "item/completed", "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "completedAtMs": 1,
                "item": {
                    "id": "tool-1",
                    "type": "mcpToolCall",
                    "server": "playwright",
                    "tool": "browser_snapshot",
                    "status": "completed",
                    "arguments": {},
                    "result": {"isError": False},
                },
            }})
            emit({"method": "item/completed", "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "completedAtMs": 2,
                "item": {
                    "id": "message-1",
                    "type": "agentMessage",
                    "text": "RESULT:READY_TO_SUBMIT",
                    "phase": "final_answer",
                },
            }})
            emit({"method": "turn/completed", "params": {
                "threadId": thread_id,
                "turn": {
                    "id": turn_id,
                    "status": "completed",
                    "items": [],
                    "error": None,
                },
            }})
    elif method == "turn/interrupt":
        thread_id = params["threadId"]
        turn_id = params["turnId"]
        emit({"id": request_id, "result": {}})
        emit({"method": "turn/completed", "params": {
            "threadId": thread_id,
            "turn": {
                "id": turn_id,
                "status": "interrupted",
                "items": [],
                "error": None,
            },
        }})
    elif method == "thread/unsubscribe":
        emit({"id": request_id, "result": {}})
    else:
        emit({"id": request_id, "error": {
            "code": -32601,
            "message": f"unsupported method: {method}",
        }})
"""


def _fake_transport(tmp_path: Path, *, mode: str) -> tuple[CodexAppServerStdioTransport, Path]:
    script = tmp_path / "fake_app_server.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    log_path = tmp_path / "wire.jsonl"
    transport = CodexAppServerStdioTransport(
        [sys.executable, "-u", str(script), str(log_path), mode],
        startup_timeout=5,
        request_timeout=5,
        shutdown_timeout=5,
    )
    return transport, log_path


def _request(
    tmp_path: Path,
    *,
    actor_id: str = "application-1",
    parent_provider_session_id: str | None = None,
) -> RuntimeCellRequest:
    return RuntimeCellRequest(
        run_id="run-1",
        actor_id=actor_id,
        attempt_id="attempt-1",
        phase="prepare",
        prompt="Inspect the already-bound application without submitting it.",
        cwd=tmp_path.resolve(),
        model="gpt-5.6-sol",
        context_refs={"prompt_contract": "sha256:" + "a" * 64},
        parent_provider_session_id=parent_provider_session_id,
    )


def _wire_messages(log_path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]


def test_stdio_transport_handshake_turn_and_event_normalization(tmp_path: Path) -> None:
    transport, log_path = _fake_transport(tmp_path, mode="complete")
    adapter = CodexAppServerAdapter(transport)

    health = adapter.health()
    turn = adapter.start(_request(tmp_path))
    events = list(turn.events)

    assert health.status == "ready"
    assert health.reason_code == "CODEX_APP_SERVER_READY"
    assert turn.backend == "codex-app-server"
    assert turn.provider_session_id == "thread-1"
    assert turn.provider_turn_id == "turn-1"
    assert [event["type"] for event in events] == [
        "turn.started",
        "item.agent_message.delta",
        "item.completed",
        "item.completed",
        "turn.completed",
    ]
    assert events[2]["item"] == {
        "id": "tool-1",
        "type": "mcp_tool_call",
        "server": "playwright",
        "tool": "browser_snapshot",
        "status": "completed",
        "arguments": {},
        "result": {"isError": False},
    }
    assert events[3]["item"]["type"] == "agent_message"
    assert events[-1]["status"] == "completed"

    adapter.close_application(turn.provider_session_id)
    adapter.shutdown()
    wire = _wire_messages(log_path)
    assert [message["method"] for message in wire] == [
        "initialize",
        "initialized",
        "thread/start",
        "turn/start",
        "thread/unsubscribe",
    ]
    assert all("jsonrpc" not in message for message in wire)
    assert wire[0]["params"]["clientInfo"]["name"] == "applypilot"
    assert wire[2]["params"]["approvalPolicy"] == "never"
    assert wire[2]["params"]["sandbox"] == "read-only"


def test_repair_resumes_the_same_application_thread(tmp_path: Path) -> None:
    transport, log_path = _fake_transport(tmp_path, mode="complete")
    adapter = CodexAppServerAdapter(transport)

    first = adapter.start(_request(tmp_path))
    list(first.events)
    repair = adapter.resume(_request(tmp_path, parent_provider_session_id=first.provider_session_id))
    list(repair.events)

    assert repair.provider_session_id == first.provider_session_id == "thread-1"
    assert repair.provider_turn_id == "turn-2"
    methods = [message["method"] for message in _wire_messages(log_path)]
    assert methods.count("thread/start") == 1
    assert methods.count("thread/resume") == 1
    assert methods.count("turn/start") == 2
    adapter.shutdown()


def test_interrupt_and_drain_complete_without_starting_a_second_turn(tmp_path: Path) -> None:
    transport, log_path = _fake_transport(tmp_path, mode="hold")
    adapter = CodexAppServerAdapter(transport)
    turn = adapter.start(_request(tmp_path))

    adapter.interrupt(turn.provider_turn_id)
    drained = adapter.drain(turn.provider_turn_id, timeout=5)

    assert [event["type"] for event in drained] == [
        "turn.started",
        "turn.completed",
    ]
    assert drained[-1]["status"] == "interrupted"
    methods = [message["method"] for message in _wire_messages(log_path)]
    assert methods.count("turn/start") == 1
    assert methods.count("turn/interrupt") == 1
    adapter.shutdown()


def test_runtime_state_never_allows_cli_fallback_after_acceptance_or_effect(
    tmp_path: Path,
) -> None:
    transport, _ = _fake_transport(tmp_path, mode="effect_hold")
    adapter = CodexAppServerAdapter(transport)
    turn = adapter.start(_request(tmp_path))
    events = iter(turn.events)

    assert next(events)["type"] == "turn.started"
    assert next(events)["type"] == "item.started"
    effect_state = adapter.execution_state(turn.provider_turn_id)
    assert effect_state.request_accepted is True
    assert effect_state.tool_or_effect_started is True
    assert (
        select_runtime_cell(
            "codex",
            codex_app_server_enabled=True,
            execution_state=effect_state,
        ).health.disposition
        == "park"
    )

    submit_state = adapter.mark_submit_started(turn.provider_turn_id)
    selection = select_runtime_cell(
        "codex",
        codex_app_server_enabled=True,
        execution_state=submit_state,
    )
    assert selection.health.disposition == "receipt_only"
    assert selection.active_backend == "codex-app-server"
    assert selection.health.fallback_used is False

    adapter.interrupt(turn.provider_turn_id)
    drained = adapter.drain(turn.provider_turn_id, timeout=5)
    assert drained[-1]["status"] == "interrupted"
    with pytest.raises(KeyError):
        adapter.execution_state(turn.provider_turn_id)
    adapter.shutdown()


def test_one_application_cannot_start_two_live_turns(tmp_path: Path) -> None:
    transport, log_path = _fake_transport(tmp_path, mode="hold")
    adapter = CodexAppServerAdapter(transport)
    turn = adapter.start(_request(tmp_path))

    with pytest.raises(CodexAppServerExecutionError) as captured:
        adapter.start(_request(tmp_path))

    assert captured.value.execution_state.request_accepted is True
    methods = [message["method"] for message in _wire_messages(log_path)]
    assert methods.count("thread/start") == 1
    assert methods.count("turn/start") == 1
    adapter.cancel(turn.provider_turn_id)
    adapter.drain(turn.provider_turn_id, timeout=5)
    adapter.shutdown()


def test_pristine_health_failure_preserves_cli_fallback() -> None:
    adapter = CodexAppServerAdapter(command=[sys.executable, "-c", "raise SystemExit(3)"])
    selection = select_runtime_cell(
        "codex",
        codex_app_server_enabled=True,
        execution_state=RuntimeCellExecutionState(
            request_accepted=False,
            tool_or_effect_started=False,
            submit_started=False,
            bound_backend=None,
        ),
        codex_app_server_adapter=adapter,
    )

    assert selection.active_backend == "codex-cli"
    assert selection.health.disposition == "fallback"
    assert selection.health.reason_code == "CODEX_APP_SERVER_UNAVAILABLE"
    assert selection.health.fallback_used is True
    adapter.shutdown()
