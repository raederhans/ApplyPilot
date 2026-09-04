from __future__ import annotations

import io
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from applypilot import config, database
from applypilot.apply import agent_report_mcp, agent_runtime, launcher
from applypilot.apply.application_plan import ApplicationPlan
from applypilot.apply.contracts import (
    AgentCheckpoint,
    RecoveryCommand,
    application_actor_id,
)
from applypilot.apply.durable_agent_runtime import (
    DurableAgentRuntime,
    DurableLaunchIntent,
    RuntimeRecoveryAdmission,
)
from applypilot.apply.durable_browser_broker import DurableBrowserBroker
from applypilot.apply.operator_commands import OperatorCommand
from applypilot.apply.runtime_cell import (
    CODEX_APP_SERVER_REQUIRED_CAPABILITIES,
    RuntimeAdapterHealth,
    RuntimeCellExecutionState,
    RuntimeCellRequest,
    RuntimeCellTurn,
)
from applypilot.storage import agent_control, runtime_control


def _ready_answer_mapping_observations() -> dict[str, object]:
    """Minimal browser-ready strict-v2 provenance envelope for durable replays."""
    return {
        "answer_mappings": {
            "schema_version": "2",
            "adapter": "replay",
            "adapter_version": "1",
            "opaque_binding": "b" * 64,
            "snapshot_digest": "a" * 64,
            "mappings": [
                {
                    "field_key_hash": "c" * 64,
                    "semantic": "work_authorization",
                    "risk": "high",
                    "selected_option_digest": "d" * 64,
                    "fact_ref": "profile:work_authorization",
                }
            ],
        }
    }


class _FakeStdin:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def write(self, value: str) -> int:
        assert value.startswith("PROMPT")
        self._events.append("prompt")
        return len(value)

    def close(self) -> None:
        return None


@dataclass
class _FakeProcess:
    pid: int
    events: list[str]
    env: dict[str, str]
    returncode: int | None = 0
    stdin: _FakeStdin = field(init=False)
    stdout: io.StringIO = field(init=False)

    def __post_init__(self) -> None:
        self.stdin = _FakeStdin(self.events)
        report = {
            "schema_version": "future-compatible",
            "run_id": self.env[agent_report_mcp.RUN_ID_ENV],
            "status": "ready_to_submit",
            "summary": "Synthetic durable launcher result",
            "observations": _ready_answer_mapping_observations(),
        }
        Path(self.env[agent_report_mcp.REPORT_PATH_ENV]).write_text(
            json.dumps(report), encoding="utf-8"
        )
        self.stdout = io.StringIO(
            "\n".join(
                json.dumps(message)
                for message in (
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": "RESULT:READY_TO_SUBMIT",
                        },
                    },
                    {"type": "turn.completed", "usage": {}},
                )
            )
        )

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return 0 if self.returncode is None else self.returncode


class _Timer:
    def cancel(self) -> None:
        return None


def _job(attempt_id: str, **extra: object) -> dict[str, object]:
    return {
        "url": "https://example.test/jobs/durable-launcher",
        "application_url": "https://example.test/apply/durable-launcher",
        "title": "Data Intern",
        "company_name": "Example",
        "site": "example",
        "fit_score": 9,
        "_attempt_id": attempt_id,
        "_browser_backend": "edge",
        **extra,
    }


def _configure_launcher(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    events: list[str],
    *,
    checkpoint_write_fails: bool = False,
) -> Path:
    """Replace every external edge of ``run_job`` with a local deterministic one."""
    app_dir = tmp_path / "app"
    worker_dir = app_dir / "workers" / "worker-0"
    log_dir = app_dir / "logs"
    worker_dir.mkdir(parents=True)
    log_dir.mkdir(parents=True)
    db_path = tmp_path / "control.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    database.init_db(db_path)
    database.close_connection(db_path)

    monkeypatch.setattr(config, "APP_DIR", app_dir)
    monkeypatch.setattr(config, "APPLY_WORKER_DIR", app_dir / "workers")
    monkeypatch.setattr(config, "LOG_DIR", log_dir)
    monkeypatch.setattr(
        config,
        "load_profile",
        lambda: {"authentication": {}, "agent_runtime": {"playwright_mcp": {"env": {}}}},
    )
    monkeypatch.setattr(launcher, "reset_worker_dir", lambda _worker_id: worker_dir)
    monkeypatch.setattr(launcher.prompt_mod, "build_prompt", lambda **_kwargs: "PROMPT")
    monkeypatch.setattr(launcher, "_resolve_codex_command", lambda: ["codex"])
    monkeypatch.setattr(launcher, "update_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(launcher, "add_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(launcher, "get_state", lambda *_args: None)
    monkeypatch.setattr(launcher, "_archive_worker_evidence", lambda *_args: [])
    monkeypatch.setattr(
        launcher,
        "_start_timeout_watchdog",
        lambda *_args: (threading.Event(), _Timer()),
    )

    def identity(pid: int) -> tuple[int, int]:
        return pid, 1_000_000 + pid

    monkeypatch.setattr(launcher, "_process_identity_tuple", identity)
    monkeypatch.setattr(
        launcher,
        "_browser_broker",
        DurableBrowserBroker(
            launcher._open_durable_control_connection,
            process_identity_provider=lambda: identity(9999),
            close_connections=True,
        ),
    )
    runtime = agent_runtime.SubprocessAgentRuntime(
        kill_process_tree=lambda _pid: None
    )
    monkeypatch.setattr(launcher, "_agent_subprocess_runtime", runtime)
    monkeypatch.setattr(
        launcher,
        "_durable_agent_runtime",
        DurableAgentRuntime(
            runtime,
            launcher._open_durable_control_connection,
            process_identity=identity,
            resume_authorizer=launcher._consume_runtime_recovery_authorization,
            close_connections=True,
        ),
    )

    original_reserve = runtime_control.start_runtime_turn
    original_attach = runtime_control.attach_runtime_turn_process

    def reserve(*args, **kwargs):
        events.append("reserve")
        return original_reserve(*args, **kwargs)

    def attach(*args, **kwargs):
        events.append("attach")
        return original_attach(*args, **kwargs)

    monkeypatch.setattr(runtime_control, "start_runtime_turn", reserve)
    monkeypatch.setattr(runtime_control, "attach_runtime_turn_process", attach)
    original_started = launcher._persist_agent_turn_started

    def started(*args, **kwargs):
        events.append("advisory_started")
        return original_started(*args, **kwargs)

    monkeypatch.setattr(launcher, "_persist_agent_turn_started", started)

    pid = 4000

    def popen(_command, **kwargs):
        nonlocal pid
        pid += 1
        events.append("popen")
        return _FakeProcess(pid, events, kwargs["env"])

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    if checkpoint_write_fails:
        def fail_control(*_args, **_kwargs):
            raise sqlite3.OperationalError("checkpoint write unavailable")

        monkeypatch.setattr(database, "record_agent_turn_control", fail_control)
    return db_path


def _durable_turn(db_path: Path, attempt_id: str):
    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM agent_runtime_turns WHERE attempt_id=? ORDER BY started_at DESC",
            (attempt_id,),
        ).fetchone()
        assert row is not None
        return row
    finally:
        connection.close()


def _durable_child_turn(db_path: Path, attempt_id: str, parent_turn_id: str):
    connection = sqlite3.connect(db_path)
    try:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM agent_runtime_turns "
            "WHERE attempt_id=? AND parent_turn_id=?",
            (attempt_id, parent_turn_id),
        ).fetchone()
        assert row is not None
        return row
    finally:
        connection.close()


def _recovery_command(*, attempt_id: str, parent_turn_id: str) -> RecoveryCommand:
    return RecoveryCommand(
        command_id=f"recovery:{parent_turn_id}",
        run_id=parent_turn_id,
        attempt_id=attempt_id,
        actor_id=application_actor_id(attempt_id),
        turn_id=parent_turn_id,
        command="retry_same_application",
        effect_scope="same_application",
        recovery_action="retry_same_application",
        failure_category="transient_browser_failure",
        next_action="retry_current_application",
        retry_budget_remaining=1,
    )


def _operator_command(*, attempt_id: str, parent_turn_id: str) -> OperatorCommand:
    return OperatorCommand(
        command_id=f"operator:{parent_turn_id}",
        exception_id=f"exception:{parent_turn_id}",
        action="resume",
        run_id=parent_turn_id,
        attempt_id=attempt_id,
        actor_id=application_actor_id(attempt_id),
        turn_id=parent_turn_id,
        input_ref="human-response:" + "a" * 32,
        input_sha256="b" * 64,
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
    )


def _prepared_parent(
    attempt_id: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[dict[str, object], Path, list[str]]:
    events: list[str] = []
    db_path = _configure_launcher(monkeypatch, tmp_path, events)
    job = _job(attempt_id)
    assert launcher.run_job(
        job,
        port=9432,
        worker_id=0,
        model="model",
        agent_backend="codex",
        submission_phase="prepare",
    )[0] == "ready_to_submit"
    assert job.get("_parent_agent_run_id")
    assert job.get("_parent_agent_checkpoint_id")
    events.clear()
    return job, db_path, events


def test_run_job_durable_launch_attaches_before_prompt_and_advisory_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    db_path = _configure_launcher(monkeypatch, tmp_path, events)
    job = _job("attempt-order")

    status, _ = launcher.run_job(
        job, port=9432, worker_id=0, model="model", agent_backend="codex", submission_phase="prepare"
    )

    assert status == "ready_to_submit"
    assert events[:5] == ["reserve", "popen", "attach", "prompt", "advisory_started"]
    turn = _durable_turn(db_path, "attempt-order")
    assert turn["status"] == "completed"
    assert turn["process_id"] == 4001
    assert job["_parent_agent_checkpoint_id"] == (
        f"agent-turn:v2:{application_actor_id('attempt-order')}:{turn['turn_id']}:completed:checkpoint"
    )


def test_run_job_feature_flag_records_app_server_degradation_and_uses_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    db_path = _configure_launcher(monkeypatch, tmp_path, events)
    monkeypatch.setenv("APPLYPILOT_CODEX_APP_SERVER_ENABLED", "1")

    class UnavailableAdapter:
        backend = "codex-app-server"

        def health(self) -> RuntimeAdapterHealth:
            return RuntimeAdapterHealth(
                backend="codex-app-server",
                status="unavailable",
                reason_code="CODEX_APP_SERVER_UNAVAILABLE",
            )

    class UnavailablePool:
        def adapter_for_worker(self, _worker_id: int) -> UnavailableAdapter:
            return UnavailableAdapter()

    monkeypatch.setattr(launcher, "_app_server_runtime_pool", UnavailablePool())
    job = _job("attempt-runtime-cell-fallback")

    status, _ = launcher.run_job(
        job,
        port=9432,
        worker_id=0,
        model="model",
        agent_backend="codex",
        submission_phase="prepare",
    )

    assert status == "ready_to_submit"
    assert job["_runtime_cell"] == {
        "schema_version": "2",
        "status": "degraded",
        "disposition": "fallback",
        "requested_backend": "codex-app-server",
        "active_backend": "codex-cli",
        "reason_code": "CODEX_APP_SERVER_CAPABILITIES_INCOMPLETE",
        "feature_enabled": True,
        "fallback_used": True,
        "execution_state": {
            "request_accepted": False,
            "tool_or_effect_started": False,
            "submit_started": False,
            "bound_backend": None,
        },
        "missing_capabilities": [
            "initialize",
            "thread/resume",
            "thread/start",
            "turn/interrupt",
            "turn/start",
        ],
    }
    turn = _durable_turn(db_path, "attempt-runtime-cell-fallback")
    assert turn["runtime_backend"] == "codex-cli"
    assert events[:5] == ["reserve", "popen", "attach", "prompt", "advisory_started"]


def test_run_job_feature_flag_uses_warm_app_server_and_resumes_same_thread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    _configure_launcher(monkeypatch, tmp_path, events)
    monkeypatch.setenv("APPLYPILOT_CODEX_APP_SERVER_ENABLED", "1")
    monkeypatch.setenv("APPLYPILOT_APPLICATION_PLAN_SHADOW_ENABLED", "1")

    class FakeAdapter:
        backend = "codex-app-server"

        def __init__(self) -> None:
            self.transport = SimpleNamespace(process=SimpleNamespace(pid=4002))
            self.requests: list[tuple[str, RuntimeCellRequest]] = []
            self.thread_config: dict[str, object] = {}
            self.terminal_status = "completed"

        def health(self) -> RuntimeAdapterHealth:
            return RuntimeAdapterHealth(
                backend="codex-app-server",
                status="ready",
                reason_code="CODEX_APP_SERVER_READY",
                capabilities=CODEX_APP_SERVER_REQUIRED_CAPABILITIES,
            )

        def configure_thread(self, config: dict[str, object]) -> None:
            self.thread_config = config

        def _turn(self, kind: str, request: RuntimeCellRequest) -> RuntimeCellTurn:
            self.requests.append((kind, request))
            number = len(self.requests)
            report_path = request.cwd / "agent-turn-report.json"
            report_path.write_text(
                json.dumps(
                    {
                        "schema_version": "future-compatible",
                        "run_id": request.run_id,
                        "status": "ready_to_submit",
                        "summary": "Synthetic App Server result",
                        "observations": _ready_answer_mapping_observations(),
                    }
                ),
                encoding="utf-8",
            )
            return RuntimeCellTurn(
                backend="codex-app-server",
                provider_session_id="provider-thread-1",
                provider_turn_id=f"provider-turn-{number}",
                events=iter(
                    (
                        {
                            "type": "item.completed",
                            "item": {
                                "id": f"tool-{number}",
                                "type": "mcp_tool_call",
                                "server": "playwright",
                                "tool": "browser_snapshot",
                                "result": {"isError": False},
                            },
                        },
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": "RESULT:READY_TO_SUBMIT",
                            },
                        },
                        {
                            "type": "turn.completed",
                            "status": self.terminal_status,
                            "usage": {},
                        },
                    )
                ),
            )

        def start(self, request: RuntimeCellRequest) -> RuntimeCellTurn:
            return self._turn("start", request)

        def resume(self, request: RuntimeCellRequest) -> RuntimeCellTurn:
            return self._turn("resume", request)

        def mark_submit_started(self, _provider_turn_id: str) -> RuntimeCellExecutionState:
            raise AssertionError("prepare turns cannot mark Submit started")

        def cancel(self, _provider_turn_id: str) -> None:
            return None

        def drain(
            self,
            _provider_turn_id: str | None = None,
            *,
            timeout: float | None = None,
        ) -> tuple[dict[str, object], ...]:
            del timeout
            return ()

        def shutdown(self) -> None:
            return None

    adapter = FakeAdapter()

    class FakePool:
        def adapter_for_worker(self, worker_id: int) -> FakeAdapter:
            assert worker_id == 0
            return adapter

    monkeypatch.setattr(launcher, "_app_server_runtime_pool", FakePool())
    job = _job(
        "attempt-runtime-cell-app-server",
        _application_plan=ApplicationPlan(
            plan_id="plan-1",
            attempt_id="attempt-runtime-cell-app-server",
            revision=1,
            route="browser_form",
            provider="generic",
            target_semantic_code="application_form",
            target_binding_ref="sha256:" + "a" * 64,
        ),
    )

    first_status, _ = launcher.run_job(
        job,
        port=9432,
        worker_id=0,
        model="model",
        agent_backend="codex",
        submission_phase="prepare",
    )
    second_status, _ = launcher.run_job(
        job,
        port=9432,
        worker_id=0,
        model="model",
        agent_backend="codex",
        submission_phase="prepare",
        resume_existing_page=True,
    )
    adapter.terminal_status = "failed"
    failed_status, _ = launcher.run_job(
        job,
        port=9432,
        worker_id=0,
        model="model",
        agent_backend="codex",
        submission_phase="prepare",
        resume_existing_page=True,
    )

    assert (first_status, second_status) == ("ready_to_submit", "ready_to_submit")
    assert failed_status == "failed:app_server_turn_failed"
    assert [kind for kind, _request in adapter.requests] == ["start", "resume", "resume"]
    assert adapter.requests[1][1].parent_provider_session_id == "provider-thread-1"
    assert all(job["url"] not in request.prompt for _kind, request in adapter.requests)
    assert all(str(tmp_path) not in request.prompt for _kind, request in adapter.requests)
    assert all("APPLICATION_PLAN_DELTA_V1" in request.prompt for _kind, request in adapter.requests)
    assert set(adapter.thread_config["mcp_servers"]) == {"playwright"}
    serialized_config = json.dumps(adapter.thread_config)
    assert str(tmp_path) not in serialized_config
    assert "mailbox" not in serialized_config
    assert "credential_relay" not in serialized_config
    assert "applypilot_control" not in serialized_config
    assert "applypilot_ats" not in serialized_config
    assert job["_runtime_cell"]["active_backend"] == "codex-app-server"


def test_run_job_keeps_direct_email_on_cli_mailbox_route_when_app_server_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    _configure_launcher(monkeypatch, tmp_path, events)
    monkeypatch.setenv("APPLYPILOT_CODEX_APP_SERVER_ENABLED", "1")

    class RejectingPool:
        def adapter_for_worker(self, _worker_id: int) -> object:
            raise AssertionError("direct email must not enter App Server")

    monkeypatch.setattr(launcher, "_app_server_runtime_pool", RejectingPool())
    job = _job(
        "attempt-runtime-cell-direct-email",
        _email_application={"route": "direct_email"},
    )

    launcher.run_job(
        job,
        port=9432,
        worker_id=0,
        model="model",
        agent_backend="codex",
        submission_phase="prepare",
    )

    assert job["_runtime_cell"]["active_backend"] == "codex-cli"
    assert job["_runtime_cell"]["reason_code"] == "CODEX_APP_SERVER_FEATURE_DISABLED"


def test_checkpoint_write_failure_keeps_durable_turn_unknown_and_no_parent_continuation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    db_path = _configure_launcher(
        monkeypatch, tmp_path, events, checkpoint_write_fails=True
    )
    job = _job("attempt-checkpoint-failure")

    status, _ = launcher.run_job(
        job, port=9432, worker_id=0, model="model", agent_backend="codex", submission_phase="prepare"
    )

    assert status == "ready_to_submit"
    turn = _durable_turn(db_path, "attempt-checkpoint-failure")
    assert (turn["status"], turn["failure_code"]) == (
        "unknown",
        "CONTROL_CHECKPOINT_UNCONFIRMED",
    )
    assert "_parent_agent_run_id" not in job
    assert "_parent_agent_checkpoint_id" not in job


def test_job_parent_fields_without_active_command_start_a_root_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    db_path = _configure_launcher(monkeypatch, tmp_path, events)
    job = _job(
        "attempt-no-capability",
        _parent_agent_run_id="forged-parent",
        _parent_agent_checkpoint_id="forged-checkpoint",
    )

    status, _ = launcher.run_job(
        job, port=9432, worker_id=0, model="model", agent_backend="codex", submission_phase="prepare"
    )

    assert status == "ready_to_submit"
    turn = _durable_turn(db_path, "attempt-no-capability")
    assert turn["parent_turn_id"] is None
    assert turn["resume_mode"] == "root"


def test_active_recovery_command_binds_exact_parent_and_consumes_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    db_path = _configure_launcher(monkeypatch, tmp_path, events)
    attempt_id = "attempt-recovery-capability"
    parent_job = _job(attempt_id)
    assert launcher.run_job(
        parent_job,
        port=9432,
        worker_id=0,
        model="model",
        agent_backend="codex",
        submission_phase="prepare",
    )[0] == "ready_to_submit"
    parent_turn_id = str(parent_job["_parent_agent_run_id"])
    checkpoint_id = str(parent_job["_parent_agent_checkpoint_id"])
    command = _recovery_command(
        attempt_id=attempt_id, parent_turn_id=parent_turn_id
    )
    monkeypatch.setattr(
        launcher._durable_agent_runtime,
        "reconcile_actor",
        lambda actor_id, current_attempt_id: RuntimeRecoveryAdmission(
            disposition="recovery_required",
            actor_id=actor_id,
            attempt_id=current_attempt_id,
            parent_turn_id=parent_turn_id,
            reason_code="PROCESS_DISAPPEARED",
            requires_fresh_observation=True,
        ),
    )
    child_job = _job(
        attempt_id,
        _parent_agent_run_id=parent_turn_id,
        _parent_agent_checkpoint_id=checkpoint_id,
    )

    with launcher._runtime_recovery_scope(command):
        assert launcher._active_runtime_recovery(
            actor_id=application_actor_id(attempt_id),
            attempt_id=attempt_id,
            parent_turn_id="forged-parent",
        ) is None
        assert launcher.run_job(
            child_job,
            port=9432,
            worker_id=0,
            model="model",
            agent_backend="codex",
            submission_phase="prepare",
        )[0] == "ready_to_submit"
        assert launcher._scoped_runtime_recovery() is None

    turn = _durable_child_turn(db_path, attempt_id, parent_turn_id)
    assert turn["parent_turn_id"] == parent_turn_id
    assert turn["checkpoint_id"] == checkpoint_id
    assert turn["resume_mode"] == "resume"


def test_operator_resume_scope_binds_one_prepare_only_child_without_submit_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    attempt_id = "attempt-operator-resume"
    parent_job, db_path, events = _prepared_parent(attempt_id, monkeypatch, tmp_path)
    parent_turn_id = str(parent_job["_parent_agent_run_id"])
    checkpoint_id = str(parent_job["_parent_agent_checkpoint_id"])
    command = _operator_command(
        attempt_id=attempt_id,
        parent_turn_id=parent_turn_id,
    )
    monkeypatch.setattr(
        launcher._durable_agent_runtime,
        "reconcile_actor",
        lambda actor_id, current_attempt_id: RuntimeRecoveryAdmission(
            disposition="recovery_required",
            actor_id=actor_id,
            attempt_id=current_attempt_id,
            parent_turn_id=parent_turn_id,
            reason_code="OPERATOR_HUMAN_RESPONSE",
            requires_fresh_observation=True,
        ),
    )
    child_job = _job(
        attempt_id,
        _parent_agent_run_id=parent_turn_id,
        _parent_agent_checkpoint_id=checkpoint_id,
    )

    with launcher._runtime_operator_resume_scope(
        command,
        checkpoint_id=checkpoint_id,
        resume_context={
            "resume_mode": "fresh_agent_turn",
            "parent_run_id": parent_turn_id,
            "checkpoint_ref": checkpoint_id,
            "human_response": {
                "request_id": f"{parent_turn_id}:human:1",
                "response_type": "human_boundary",
            },
        },
    ):
        assert launcher.run_job(
            child_job,
            port=9432,
            worker_id=0,
            model="model",
            agent_backend="codex",
            resume_existing_page=True,
            submission_phase="prepare",
        )[0] == "ready_to_submit"
        assert launcher._scoped_operator_resume() is None

    turn = _durable_child_turn(db_path, attempt_id, parent_turn_id)
    assert (turn["parent_turn_id"], turn["checkpoint_id"], turn["resume_mode"]) == (
        parent_turn_id,
        checkpoint_id,
        "resume",
    )
    assert turn["submit_started"] == 0
    assert events[:4] == ["reserve", "popen", "attach", "prompt"]


def test_audit_verification_scope_binds_one_read_only_child_surface(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    attempt_id = "attempt-audit-verification"
    parent_job, db_path, _events = _prepared_parent(
        attempt_id, monkeypatch, tmp_path
    )
    parent_turn_id = str(parent_job["_parent_agent_run_id"])
    checkpoint_id = str(parent_job["_parent_agent_checkpoint_id"])
    monkeypatch.setattr(
        launcher._durable_agent_runtime,
        "reconcile_actor",
        lambda actor_id, current_attempt_id: RuntimeRecoveryAdmission(
            disposition="recovery_required",
            actor_id=actor_id,
            attempt_id=current_attempt_id,
            parent_turn_id=parent_turn_id,
            reason_code="HOST_PROVENANCE_AUDIT",
            requires_fresh_observation=True,
        ),
    )
    child_job = _job(
        attempt_id,
        _parent_agent_run_id=parent_turn_id,
        _parent_agent_checkpoint_id=checkpoint_id,
        _answer_provenance_verification_child=True,
        _browser_observation={
            "ats_adapter_context": {"fields": []},
            "answer_provenance": {"snapshot_digest": "a" * 64},
        },
    )

    with launcher._runtime_audit_verification_scope(child_job):
        assert launcher.run_job(
            child_job,
            port=9432,
            worker_id=0,
            model="model",
            agent_backend="codex",
            resume_existing_page=True,
            submission_phase="prepare",
        )[0] == "ready_to_submit"
        assert launcher._scoped_audit_verification() is None

    assert child_job["_available_tools"] == [
        "browser_snapshot",
        "browser_take_screenshot",
        "browser_wait_for",
        "get_application_context",
        "build_answer_mapping",
        "report_agent_turn",
    ]
    turn = _durable_child_turn(db_path, attempt_id, parent_turn_id)
    assert (turn["parent_turn_id"], turn["checkpoint_id"], turn["resume_mode"]) == (
        parent_turn_id,
        checkpoint_id,
        "resume",
    )
    assert turn["submit_started"] == 0


def test_operator_resume_scope_rejects_submit_phase_before_popen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    attempt_id = "attempt-operator-no-submit"
    parent_job, _db_path, events = _prepared_parent(attempt_id, monkeypatch, tmp_path)
    parent_turn_id = str(parent_job["_parent_agent_run_id"])
    checkpoint_id = str(parent_job["_parent_agent_checkpoint_id"])
    child_job = _job(
        attempt_id,
        _parent_agent_run_id=parent_turn_id,
        _parent_agent_checkpoint_id=checkpoint_id,
    )

    with (
        launcher._runtime_operator_resume_scope(
            _operator_command(attempt_id=attempt_id, parent_turn_id=parent_turn_id),
            checkpoint_id=checkpoint_id,
            resume_context={
                "resume_mode": "fresh_agent_turn",
                "parent_run_id": parent_turn_id,
                "checkpoint_ref": checkpoint_id,
                "human_response": {
                    "request_id": f"{parent_turn_id}:human:1",
                    "response_type": "human_boundary",
                },
            },
        ),
        pytest.raises(RuntimeError, match="operator resume parent/checkpoint"),
    ):
        launcher.run_job(
            child_job,
            port=9432,
            worker_id=0,
            model="model",
            agent_backend="codex",
            submission_phase="submit",
        )

    assert events == []


def test_submit_scope_creates_parent_linked_submit_child_and_consumes_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    attempt_id = "attempt-submit-continuation"
    parent_job, db_path, events = _prepared_parent(attempt_id, monkeypatch, tmp_path)
    parent_turn_id = str(parent_job["_parent_agent_run_id"])
    checkpoint_id = str(parent_job["_parent_agent_checkpoint_id"])
    submit_job = _job(
        attempt_id,
        _parent_agent_run_id=parent_turn_id,
        _parent_agent_checkpoint_id=checkpoint_id,
        _submission_gate_binding={
            "gate_id": "gate:exact",
            "attempt_id": attempt_id,
            "job_url": parent_job["url"],
        },
        _submission_gate=True,
    )

    class RejectingPool:
        def adapter_for_worker(self, _worker_id: int) -> object:
            raise AssertionError("submit phase must remain on the CLI runtime")

    monkeypatch.setenv("APPLYPILOT_CODEX_APP_SERVER_ENABLED", "1")
    monkeypatch.setattr(launcher, "_app_server_runtime_pool", RejectingPool())

    with launcher._runtime_submit_scope(submit_job):
        assert launcher.run_job(
            submit_job,
            port=9432,
            worker_id=0,
            model="model",
            agent_backend="codex",
            resume_existing_page=True,
            submission_phase="submit",
        )[0] == "submission_uncertain"
        assert launcher._scoped_submit_continuation() is None

    assert submit_job["_runtime_recovery_admission"]["disposition"] == "none"
    assert events[:1] == ["reserve"]
    turn = _durable_child_turn(db_path, attempt_id, parent_turn_id)
    assert (turn["parent_turn_id"], turn["checkpoint_id"], turn["resume_mode"]) == (
        parent_turn_id,
        checkpoint_id,
        "resume",
    )
    assert turn["submit_started"] == 1
    assert events[:4] == ["reserve", "popen", "attach", "prompt"]
    # Runtime host selection precedes request acceptance/tool effects.  The
    # submit phase alone must not be mistaken for an already-clicked Submit,
    # while the durable turn records submit_started for recovery admission.
    assert submit_job["_runtime_cell"]["execution_state"] == {
        "request_accepted": False,
        "tool_or_effect_started": False,
        "submit_started": False,
        "bound_backend": None,
    }
    assert submit_job["_runtime_cell"]["disposition"] == "execute"
    assert submit_job["_runtime_cell"]["active_backend"] == "codex-cli"


@pytest.mark.parametrize(
    "mutation",
    ("binding_attempt", "binding_job", "runtime_parent"),
)
def test_submit_scope_rejects_stale_gate_binding_before_popen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutation: str
) -> None:
    attempt_id = f"attempt-submit-stale-{mutation}"
    parent_job, _db_path, events = _prepared_parent(attempt_id, monkeypatch, tmp_path)
    submit_job = _job(
        attempt_id,
        _parent_agent_run_id=parent_job["_parent_agent_run_id"],
        _parent_agent_checkpoint_id=parent_job["_parent_agent_checkpoint_id"],
        _submission_gate_binding={
            "gate_id": "gate:exact",
            "attempt_id": attempt_id,
            "job_url": parent_job["url"],
        },
    )
    if mutation == "binding_attempt":
        submit_job["_submission_gate_binding"]["attempt_id"] = "wrong-attempt"  # type: ignore[index]
        with (
            pytest.raises(RuntimeError, match="binding.*stale"),
            launcher._runtime_submit_scope(submit_job),
        ):
            pass
    elif mutation == "binding_job":
        submit_job["_submission_gate_binding"]["job_url"] = "https://wrong.test/job"  # type: ignore[index]
        with (
            pytest.raises(RuntimeError, match="binding.*stale"),
            launcher._runtime_submit_scope(submit_job),
        ):
            pass
    else:
        with launcher._runtime_submit_scope(submit_job):
            submit_job["_parent_agent_run_id"] = "wrong-parent"
            with pytest.raises(RuntimeError, match="parent/checkpoint binding"):
                launcher.run_job(
                    submit_job,
                    port=9432,
                    worker_id=0,
                    model="model",
                    agent_backend="codex",
                    submission_phase="submit",
                )

    assert events == []


@pytest.mark.parametrize(
    ("disposition", "expected_status"),
    (
        ("live_owner", "failed:runtime_owner_active"),
        ("recovery_required", "failed:runtime_recovery_required"),
        ("receipt_only", "submission_uncertain"),
    ),
)
def test_restart_reconciliation_returns_before_browser_lease_or_popen(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    disposition: str,
    expected_status: str,
) -> None:
    events: list[str] = []
    _configure_launcher(monkeypatch, tmp_path, events)

    class Reconciler:
        def reconcile_actor(self, actor_id: str, attempt_id: str) -> RuntimeRecoveryAdmission:
            return RuntimeRecoveryAdmission(
                disposition=disposition,  # type: ignore[arg-type]
                actor_id=actor_id,
                attempt_id=attempt_id,
                parent_turn_id="interrupted-turn",
                reason_code="TEST_INTERRUPTION",
                requires_fresh_observation=True,
            )

    monkeypatch.setattr(launcher, "_durable_agent_runtime", Reconciler())
    monkeypatch.setattr(
        launcher,
        "_browser_lease_for_agent_turn",
        lambda *_args, **_kwargs: events.append("browser_lease"),
    )
    monkeypatch.setattr(
        launcher,
        "reset_worker_dir",
        lambda *_args, **_kwargs: events.append("reset_worker_dir"),
    )
    job = _job(f"attempt-restart-{disposition}")

    status, duration_ms = launcher.run_job(
        job,
        port=9432,
        worker_id=0,
        model="model",
        agent_backend="codex",
        submission_phase="prepare",
    )

    assert (status, duration_ms) == (expected_status, 0)
    assert events == []
    assert job["_runtime_recovery_admission"] == {
        "disposition": disposition,
        "parent_turn_id": "interrupted-turn",
        "reason_code": "TEST_INTERRUPTION",
        "requires_fresh_observation": True,
    }


def test_receipt_phase_returns_uncertain_without_agent_or_browser_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    _configure_launcher(monkeypatch, tmp_path, events)
    monkeypatch.setattr(
        launcher,
        "_browser_lease_for_agent_turn",
        lambda *_args, **_kwargs: events.append("browser_lease"),
    )
    job = _job("attempt-receipt-phase")

    assert launcher.run_job(
        job,
        port=9432,
        worker_id=0,
        model="model",
        agent_backend="codex",
        submission_phase="receipt",
    ) == ("submission_uncertain", 0)
    assert events == []
    assert job["_runtime_recovery_admission"] == {
        "disposition": "receipt_only",
        "reason_code": "DETERMINISTIC_RECEIPT_OBSERVER_REQUIRED",
        "requires_fresh_observation": True,
    }


def test_submit_started_parent_never_invokes_a_receipt_agent_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    _configure_launcher(monkeypatch, tmp_path, events)
    intent = agent_runtime.SubprocessLaunchSpec(
        run_id="child",
        attempt_id="attempt-submit",
        actor_id=application_actor_id("attempt-submit"),
        turn_id="child",
        command=("agent",),
        prompt="PROMPT",
        cwd=tmp_path,
        env={},
        runtime_id="codex:isolated:cdp:9432",
        profile_id="isolated:worker:0",
        parent_run_id="submitted-parent",
        submit_started=True,
    )

    with pytest.raises(ValueError, match="deterministic receipt observer"):
        launcher.DurableLaunchIntent(
            spec=intent,
            runtime_backend="codex-cli",
            resume_mode="receipt_only",
            checkpoint_id="checkpoint",
            recovery_authorization_id="receipt-command",
            tool_surface_hash="tools",
            prompt_contract_hash="prompt",
        )
    assert events == []


def test_submit_started_parent_blocks_launcher_child_before_popen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A retry command cannot turn a post-submit parent into a receipt subprocess."""
    events: list[str] = []
    db_path = _configure_launcher(monkeypatch, tmp_path, events)
    attempt_id = "attempt-post-submit"
    parent_turn_id = "submitted-parent"
    parent_spec = agent_runtime.SubprocessLaunchSpec(
        run_id=parent_turn_id,
        attempt_id=attempt_id,
        actor_id=application_actor_id(attempt_id),
        turn_id=parent_turn_id,
        command=("agent",),
        prompt="PROMPT",
        cwd=tmp_path,
        env={
            agent_report_mcp.RUN_ID_ENV: parent_turn_id,
            agent_report_mcp.REPORT_PATH_ENV: str(tmp_path / "parent-report.json"),
        },
        runtime_id="codex:isolated:cdp:9432",
        profile_id="isolated:worker:0",
        submit_started=True,
    )
    parent_intent = DurableLaunchIntent(
        spec=parent_spec,
        runtime_backend="codex-cli",
        resume_mode="root",
        tool_surface_hash="tools",
        prompt_contract_hash="prompt",
    )
    parent = launcher._durable_agent_runtime.start(
        parent_intent,
        popen_factory=lambda *_args, **_kwargs: _FakeProcess(7001, [], parent_spec.env),
    )
    launcher._durable_agent_runtime.terminal(
        parent,
        status="unknown",
        failure_code="SUBMISSION_RESULT_UNKNOWN",
        exit_code=0,
    )
    events.clear()
    checkpoint_id = (
        f"agent-turn:v2:{application_actor_id(attempt_id)}:{parent_turn_id}:completed:checkpoint"
    )
    connection = sqlite3.connect(db_path)
    try:
        assert agent_control.append_checkpoint(
            connection,
            AgentCheckpoint(
                checkpoint_id=checkpoint_id,
                run_id=parent_turn_id,
                attempt_id=attempt_id,
                actor_id=application_actor_id(attempt_id),
                turn_id=parent_turn_id,
                phase="submit",
                sequence=1,
                expected_sequence=0,
                state={"application_status": "submission_uncertain"},
                idempotency_key=f"checkpoint:{parent_turn_id}",
                schema_version="2",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    command = _recovery_command(attempt_id=attempt_id, parent_turn_id=parent_turn_id)
    job = _job(
        attempt_id,
        _parent_agent_run_id=parent_turn_id,
        _parent_agent_checkpoint_id=checkpoint_id,
    )
    with launcher._runtime_recovery_scope(command):
        status, _ = launcher.run_job(
            job,
            port=9432,
            worker_id=0,
            model="model",
            agent_backend="codex",
            submission_phase="prepare",
        )

    assert status == "submission_uncertain"
    assert job["_runtime_recovery_admission"]["disposition"] == "receipt_only"
    assert events == []
