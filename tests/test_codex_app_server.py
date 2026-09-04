from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from applypilot.apply.codex_app_server import (
    CodexAppServerAdapter,
    CodexAppServerError,
    CodexAppServerExecutionError,
    CodexAppServerProtocolError,
    CodexAppServerStdioTransport,
    CodexAppServerTimeout,
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
allowed_thread_sandboxes = {"read-only", "workspace-write", "danger-full-access"}


def emit(message):
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def record(message):
    with open(log_path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(message, separators=(",", ":")) + "\n")


def validate_thread_sandbox(params, request_id):
    sandbox = params.get("sandbox")
    if sandbox in allowed_thread_sandboxes:
        return True
    emit({"id": request_id, "error": {
        "code": -32602,
        "message": f"unknown sandbox enum: {sandbox}",
    }})
    return False


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
        if not validate_thread_sandbox(params, request_id):
            continue
        thread_counter += 1
        thread_id = f"thread-{thread_counter}"
        emit({"id": request_id, "result": {"thread": {"id": thread_id}}})
        emit({"method": "thread/started", "params": {"thread": {"id": thread_id}}})
    elif method == "thread/resume":
        if not validate_thread_sandbox(params, request_id):
            continue
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
        if mode in {"effect_hold", "read_hold"}:
            emit({"method": "item/started", "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "startedAtMs": 1,
                "item": {
                    "id": "tool-1",
                    "type": "mcpToolCall",
                    "server": "playwright",
                    "tool": "browser_click" if mode == "effect_hold" else "browser_snapshot",
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
        if mode != "lost_terminal":
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


def _fake_transport(
    tmp_path: Path,
    *,
    mode: str,
    request_timeout: float = 5,
    shutdown_timeout: float = 5,
) -> tuple[CodexAppServerStdioTransport, Path]:
    script = tmp_path / "fake_app_server.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    log_path = tmp_path / "wire.jsonl"
    transport = CodexAppServerStdioTransport(
        [sys.executable, "-u", str(script), str(log_path), mode],
        startup_timeout=5,
        request_timeout=request_timeout,
        shutdown_timeout=shutdown_timeout,
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


def _block_event_consumer(
    adapter: CodexAppServerAdapter,
    provider_turn_id: str,
    events: Iterator[Mapping[str, object]],
) -> tuple[threading.Thread, list[Exception]]:
    errors: list[Exception] = []

    def consume() -> None:
        try:
            next(events)
        except (CodexAppServerError, StopIteration) as exc:
            errors.append(exc)

    consumer = threading.Thread(target=consume, daemon=True)
    consumer.start()
    state = adapter._active_turns[provider_turn_id]
    deadline = time.monotonic() + 1
    while not state.consumer_lock.locked() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert state.consumer_lock.locked(), "event consumer did not enter the blocking receive"
    return consumer, errors


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


def test_transport_routes_notifications_to_matching_thread_and_turn_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = CodexAppServerStdioTransport(["unused"], subscription_queue_size=2)
    monkeypatch.setattr(transport, "start", lambda: None)
    first = transport.subscribe(thread_id="thread-1", turn_id="turn-1")
    second = transport.subscribe(thread_id="thread-2", turn_id="turn-2")

    transport._dispatch_message(
        {
            "method": "item/completed",
            "params": {"threadId": "thread-1", "turnId": "turn-1", "item": {}},
        }
    )

    assert transport.receive(first, timeout=0.01)["params"]["threadId"] == "thread-1"
    with pytest.raises(CodexAppServerTimeout):
        transport.receive(second, timeout=0.01)


def test_transport_queue_overflow_isolates_only_the_slow_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = CodexAppServerStdioTransport(["unused"], subscription_queue_size=1)
    monkeypatch.setattr(transport, "start", lambda: None)
    slow = transport.subscribe(thread_id="thread-1", turn_id="turn-1")
    healthy = transport.subscribe(thread_id="thread-2", turn_id="turn-2")
    first = {
        "method": "item/completed",
        "params": {"threadId": "thread-1", "turnId": "turn-1", "item": {}},
    }

    transport._dispatch_message(first)
    transport._dispatch_message(first)
    transport._dispatch_message(
        {
            "method": "turn/completed",
            "params": {"threadId": "thread-2", "turn": {"id": "turn-2"}},
        }
    )

    with pytest.raises(CodexAppServerError, match="event queue overflow"):
        transport.receive(slow, timeout=0.01)
    assert transport.receive(healthy, timeout=0.01)["params"]["threadId"] == "thread-2"
    assert slow.token not in transport._subscriptions
    assert healthy.token in transport._subscriptions


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("thread/start", {"sandbox": "readOnly"}),
        ("thread/resume", {"threadId": "thread-1", "sandbox": "readOnly"}),
    ],
)
def test_fake_server_rejects_unknown_thread_sandbox_enum(
    tmp_path: Path,
    method: str,
    params: dict[str, object],
) -> None:
    transport, _ = _fake_transport(tmp_path, mode="hold")
    transport.start()

    with pytest.raises(CodexAppServerProtocolError) as captured:
        transport.request(method, params)

    assert captured.value.code == -32602
    transport.shutdown()


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
    thread_requests = [
        message for message in _wire_messages(log_path) if message["method"] in {"thread/start", "thread/resume"}
    ]
    assert [message["params"]["sandbox"] for message in thread_requests] == [
        "read-only",
        "read-only",
    ]
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


def test_read_only_observation_does_not_mark_effect_started(tmp_path: Path) -> None:
    transport, _ = _fake_transport(tmp_path, mode="read_hold")
    adapter = CodexAppServerAdapter(transport)
    turn = adapter.start(_request(tmp_path))
    events = iter(turn.events)

    assert next(events)["type"] == "turn.started"
    assert next(events)["item"]["tool"] == "browser_snapshot"
    assert adapter.execution_state(turn.provider_turn_id).tool_or_effect_started is False

    adapter.interrupt(turn.provider_turn_id)
    adapter.drain(turn.provider_turn_id, timeout=5)
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


def test_drain_timeout_includes_waiting_for_event_consumer_lock(tmp_path: Path) -> None:
    transport, _ = _fake_transport(tmp_path, mode="hold", shutdown_timeout=0.2)
    adapter = CodexAppServerAdapter(transport, drain_timeout=0.1)
    turn = adapter.start(_request(tmp_path))
    events = iter(turn.events)
    assert next(events)["type"] == "turn.started"
    consumer, _ = _block_event_consumer(adapter, turn.provider_turn_id, events)
    outcome: dict[str, object] = {}

    def drain() -> None:
        started = time.monotonic()
        try:
            adapter.drain(turn.provider_turn_id, timeout=0.1)
        except CodexAppServerError as exc:
            outcome["error"] = exc
        finally:
            outcome["elapsed"] = time.monotonic() - started

    drainer = threading.Thread(target=drain, daemon=True)
    drainer.start()
    drainer.join(timeout=0.75)
    try:
        assert not drainer.is_alive(), "drain exceeded its caller-supplied timeout"
        assert isinstance(outcome.get("error"), CodexAppServerTimeout)
        assert float(outcome["elapsed"]) < 0.5
    finally:
        transport.shutdown()
        consumer.join(timeout=1)
        drainer.join(timeout=1)


def test_drain_timeout_is_preserved_when_server_stays_silent(tmp_path: Path) -> None:
    transport, _ = _fake_transport(tmp_path, mode="hold", shutdown_timeout=0.2)
    adapter = CodexAppServerAdapter(transport, drain_timeout=0.1)
    turn = adapter.start(_request(tmp_path))
    started = time.monotonic()

    try:
        with pytest.raises(CodexAppServerTimeout):
            adapter.drain(turn.provider_turn_id, timeout=0.1)
        assert time.monotonic() - started < 0.5
    finally:
        transport.shutdown()


def test_shutdown_contains_process_when_terminal_event_is_lost(tmp_path: Path) -> None:
    transport, _ = _fake_transport(
        tmp_path,
        mode="lost_terminal",
        request_timeout=0.2,
        shutdown_timeout=0.2,
    )
    adapter = CodexAppServerAdapter(transport, drain_timeout=0.1)
    turn = adapter.start(_request(tmp_path))
    events = iter(turn.events)
    assert next(events)["type"] == "turn.started"
    consumer, _ = _block_event_consumer(adapter, turn.provider_turn_id, events)
    process = transport.process
    outcome: dict[str, object] = {}

    def shut_down() -> None:
        started = time.monotonic()
        try:
            adapter.shutdown()
        except CodexAppServerError as exc:
            outcome["error"] = exc
        finally:
            outcome["elapsed"] = time.monotonic() - started

    supervisor = threading.Thread(target=shut_down, daemon=True)
    supervisor.start()
    supervisor.join(timeout=1)
    try:
        assert not supervisor.is_alive(), "shutdown never reached transport containment"
        assert "error" not in outcome
        assert float(outcome["elapsed"]) < 0.75
        assert process is not None and process.poll() is not None
    finally:
        transport.shutdown()
        consumer.join(timeout=1)
        supervisor.join(timeout=1)


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
