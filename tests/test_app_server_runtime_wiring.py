from __future__ import annotations

import json
import sqlite3
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from applypilot.apply.app_server_runtime_wiring import (
    AppServerRuntimePool,
    AppServerTurnProcess,
    DurableAppServerStateStore,
    build_ref_only_request,
    build_thread_config,
    open_app_server_turn,
)
from applypilot.apply.application_plan import ApplicationPlan
from applypilot.apply.codex_app_server import (
    CodexAppServerAdapter,
    CodexAppServerStdioTransport,
    CodexAppServerTimeout,
)
from applypilot.apply.runtime_cell import RuntimeCellTurn, select_runtime_cell

FAKE_SERVER = r"""
import json
import sys

thread_counter = 0
turn_counter = 0

def emit(message):
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()

for raw_line in sys.stdin:
    message = json.loads(raw_line)
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params", {})
    if method == "initialize":
        emit({"id": request_id, "result": {"userAgent": "fake"}})
    elif method == "initialized":
        continue
    elif method == "thread/start":
        thread_counter += 1
        thread_id = f"thread-{thread_counter}"
        emit({"id": request_id, "result": {"thread": {"id": thread_id}}})
    elif method == "thread/resume":
        thread_id = params["threadId"]
        emit({"id": request_id, "result": {"thread": {"id": thread_id}}})
    elif method == "turn/start":
        turn_counter += 1
        turn_id = f"turn-{turn_counter}"
        thread_id = params["threadId"]
        emit({"id": request_id, "result": {"turn": {"id": turn_id}}})
        emit({"method": "item/completed", "params": {
            "threadId": thread_id,
            "turnId": turn_id,
            "item": {
                "id": f"tool-{turn_counter}",
                "type": "mcpToolCall",
                "server": "applypilot_control",
                "tool": "report_agent_turn",
                "status": "completed",
                "result": {"isError": False},
            },
        }})
        emit({"method": "item/completed", "params": {
            "threadId": thread_id,
            "turnId": turn_id,
            "item": {
                "id": f"message-{turn_counter}",
                "type": "agentMessage",
                "text": "RESULT:READY_TO_SUBMIT",
                "phase": "final_answer",
            },
        }})
        emit({"method": "turn/completed", "params": {
            "threadId": thread_id,
            "turn": {"id": turn_id, "status": "completed", "items": [], "error": None},
        }})
    elif method == "turn/interrupt":
        emit({"id": request_id, "result": {}})
    elif method == "thread/unsubscribe":
        emit({"id": request_id, "result": {}})
"""


def _store(path: Path) -> DurableAppServerStateStore:
    def connect() -> sqlite3.Connection:
        return sqlite3.connect(path)

    return DurableAppServerStateStore(connect, close_connections=True)


def _plan() -> ApplicationPlan:
    return ApplicationPlan(
        plan_id="plan-sensitive-id",
        attempt_id="attempt-1",
        revision=1,
        route="browser_form",
        provider="greenhouse",
        target_semantic_code="application_form",
        target_binding_ref="sha256:" + "a" * 64,
    )


def test_feature_off_peek_does_not_create_durable_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "control.db"
    store = _store(database_path)

    assert store.peek("application:attempt-1", "attempt-1") is None

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='app_server_runtime_bindings'"
        ).fetchone() is None


def test_ref_only_request_adds_optional_plan_delta_without_raw_inputs(tmp_path: Path) -> None:
    request = build_ref_only_request(
        run_id="run-1",
        actor_id="application:attempt-1",
        attempt_id="attempt-1",
        phase="prepare",
        cwd=tmp_path,
        model="gpt-5.6-sol",
        prompt_contract={"source_url": "https://secret.example/job", "resume": "raw resume"},
        ats_context={"path": "C:/secret/context.json", "cookie": "secret-cookie"},
        plan=_plan(),
        plan_shadow_enabled=True,
    )

    assert "APPLICATION_PLAN_DELTA_V1" in request.prompt
    assert "https://secret.example/job" not in request.prompt
    assert "raw resume" not in request.prompt
    assert "C:/secret/context.json" not in request.prompt
    assert "secret-cookie" not in request.prompt
    assert set(request.context_refs) == {"prompt_contract", "ats_context"}
    assert all(value.startswith("sha256:") for value in request.context_refs.values())


def test_thread_config_scopes_per_turn_env_and_tools() -> None:
    config = build_thread_config(
        {
            "mcpServers": {
                "playwright": {"command": "pw", "args": ["--old"]},
                "applypilot_control": {"command": "python", "args": ["-m", "control"]},
            }
        },
        process_env={"REPORT_PATH": "C:/one/report.json", "UNRELATED": "no"},
        server_env_names={"applypilot_control": ("REPORT_PATH",)},
        enabled_tools={
            "playwright": ("browser_snapshot",),
            "applypilot_control": ("report_agent_turn",),
        },
        playwright_url="http://127.0.0.1:3210/mcp",
    )

    servers = config["mcp_servers"]
    assert servers["playwright"]["url"] == "http://127.0.0.1:3210/mcp"
    assert "command" not in servers["playwright"]
    assert servers["applypilot_control"]["env"] == {
        "REPORT_PATH": "C:/one/report.json"
    }
    assert "UNRELATED" not in json.dumps(config)


def test_fake_server_turn_persists_effect_and_repairs_on_same_thread(tmp_path: Path) -> None:
    script = tmp_path / "fake_app_server.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    transport = CodexAppServerStdioTransport(
        [sys.executable, "-u", str(script)],
        startup_timeout=5,
        request_timeout=5,
        shutdown_timeout=5,
    )
    adapter = CodexAppServerAdapter(transport)
    store = _store(tmp_path / "control.db")
    first_request = build_ref_only_request(
        run_id="run-1",
        actor_id="application:attempt-1",
        attempt_id="attempt-1",
        phase="prepare",
        cwd=tmp_path,
        model="gpt-5.6-sol",
        prompt_contract={"phase": "prepare"},
        ats_context={"schema_version": "1"},
    )
    try:
        first = open_app_server_turn(
            adapter=adapter,
            state_store=store,
            request=first_request,
            submit_started=False,
        )
        assert [json.loads(line)["type"] for line in first.stdout][-1] == "turn.completed"
        first_state = store.load("application:attempt-1", "attempt-1")
        assert first_state is not None
        assert first_state.provider_session_id == "thread-1"
        assert first_state.tool_or_effect_started is True
        assert first_state.status == "completed"
        parked = select_runtime_cell(
            "codex",
            codex_app_server_enabled=True,
            execution_state=first_state.execution_state,
            codex_app_server_adapter=None,
        )
        assert parked.health.disposition == "park"
        assert parked.health.fallback_used is False

        selection = select_runtime_cell(
            "codex",
            codex_app_server_enabled=True,
            execution_state=first_state.execution_state,
            codex_app_server_adapter=adapter,
        )
        assert selection.health.disposition == "continue"

        repair_request = build_ref_only_request(
            run_id="run-2",
            actor_id="application:attempt-1",
            attempt_id="attempt-1",
            phase="repair",
            cwd=tmp_path,
            model="gpt-5.6-sol",
            prompt_contract={"phase": "repair"},
            ats_context={"schema_version": "1"},
            parent_provider_session_id=first_state.provider_session_id,
        )
        repair = open_app_server_turn(
            adapter=adapter,
            state_store=store,
            request=repair_request,
            submit_started=True,
        )
        assert repair.turn.provider_session_id == "thread-1"
        list(repair.stdout)
        final_state = store.load("application:attempt-1", "attempt-1")
        assert final_state is not None
        assert final_state.provider_session_id == "thread-1"
        assert final_state.submit_started is True
        assert final_state.status == "receipt_only"
        receipt_only = select_runtime_cell(
            "codex",
            codex_app_server_enabled=True,
            execution_state=final_state.execution_state,
            codex_app_server_adapter=None,
        )
        assert receipt_only.health.disposition == "receipt_only"
        assert receipt_only.health.fallback_used is False
    finally:
        adapter.shutdown()
    assert transport.process is not None
    assert transport.process.poll() is not None


def test_pool_reuses_one_adapter_per_worker_and_shutdown_is_bounded() -> None:
    created: list[object] = []

    class FakeAdapter:
        def __init__(self) -> None:
            self.shutdown_calls = 0
            created.append(self)

        def shutdown(self) -> None:
            self.shutdown_calls += 1

    pool = AppServerRuntimePool(FakeAdapter)  # type: ignore[arg-type]
    first = pool.adapter_for_worker(0)
    assert pool.adapter_for_worker(0) is first
    second = pool.adapter_for_worker(1)
    assert second is not first

    pool.shutdown()

    assert len(created) == 2
    assert first.shutdown_calls == 1
    assert second.shutdown_calls == 1


def test_turn_facade_stops_waiting_when_cancel_cannot_reach_server(tmp_path: Path) -> None:
    released = threading.Event()

    def blocked_events():
        released.wait(timeout=2)
        return
        yield  # pragma: no cover - keeps this function an iterator

    class BlockingAdapter:
        drain_timeout = 0.05

        def __init__(self) -> None:
            self.transport = SimpleNamespace(process=SimpleNamespace(pid=4003))

        def cancel(self, _provider_turn_id: str) -> None:
            raise RuntimeError("transport unavailable")

    store = _store(tmp_path / "control.db")
    store.record_accepted(
        actor_id="application:attempt-1",
        attempt_id="attempt-1",
        provider_session_id="thread-1",
        provider_turn_id="turn-1",
        submit_started=False,
    )
    process = AppServerTurnProcess(
        adapter=BlockingAdapter(),  # type: ignore[arg-type]
        turn=RuntimeCellTurn(
            backend="codex-app-server",
            provider_session_id="thread-1",
            provider_turn_id="turn-1",
            events=blocked_events(),
        ),
        state_store=store,
        actor_id="application:attempt-1",
        attempt_id="attempt-1",
    )
    try:
        with pytest.raises(RuntimeError, match="transport unavailable"):
            process.cancel()
        with pytest.raises(CodexAppServerTimeout, match="did not terminate"):
            list(process.stdout)
    finally:
        released.set()
