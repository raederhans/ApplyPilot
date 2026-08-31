from __future__ import annotations

import io
import json
import sqlite3
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from applypilot import config, database
from applypilot.apply import agent_report_mcp, launcher
from applypilot.apply.contracts import (
    AgentCheckpoint,
    AgentRunRequest,
    AgentTurnResult,
    ApplicationEvent,
    HumanRequest,
    application_actor_id,
)
from applypilot.database import init_db
from applypilot.storage import agent_control


def request(*, actor_id: str, turn_id: str) -> AgentRunRequest:
    return AgentRunRequest(
        run_id=turn_id,
        actor_id=actor_id,
        turn_id=turn_id,
        attempt_id="attempt-durable-1",
        agent_role="browser-application-agent",
        phase="prepare",
        objective="Prepare one synthetic application turn",
    )


def result(turn_id: str, *, completed_at: datetime) -> AgentTurnResult:
    return AgentTurnResult(
        run_id=turn_id,
        status="completed",
        summary="Synthetic durable completion",
        completed_at=completed_at,
    )


def persist_completion(
    request_value: AgentRunRequest,
    result_value: AgentTurnResult,
    *,
    occurred_after: datetime,
    expected_checkpoint_sequence: int | None = None,
    application_status: str = "completed",
) -> datetime:
    if expected_checkpoint_sequence is None:
        return launcher._persist_agent_turn_completed(
            request_value,
            result_value,
            application_status=application_status,
            duration_ms=10,
            source="synthetic-replay-test",
            occurred_after=occurred_after,
        )
    return launcher._persist_agent_turn_completed(
        request_value,
        result_value,
        application_status=application_status,
        duration_ms=10,
        source="synthetic-replay-test",
        occurred_after=occurred_after,
        expected_checkpoint_sequence=expected_checkpoint_sequence,
    )


def test_launcher_completion_replay_after_commit_is_one_event_and_one_checkpoint(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = init_db(tmp_path / "completion-replay.db")
    monkeypatch.setattr(database, "get_connection", lambda: conn)
    request_value = request(
        actor_id=application_actor_id("attempt-durable-1"), turn_id="turn-1"
    )
    completed_at = datetime(2026, 8, 30, 9, 0, tzinfo=UTC)

    first_occurred_at = persist_completion(
        request_value,
        result(request_value.turn_id, completed_at=completed_at),
        occurred_after=completed_at,
        expected_checkpoint_sequence=0,
    )
    replay_occurred_at = persist_completion(
        request_value,
        result(request_value.turn_id, completed_at=completed_at + timedelta(seconds=5)),
        occurred_after=first_occurred_at + timedelta(seconds=5),
        expected_checkpoint_sequence=0,
    )

    events = agent_control.list_events(
        conn,
        attempt_id=request_value.attempt_id,
        event_type="agent.turn.completed",
    )
    checkpoints = conn.execute(
        "SELECT actor_id, turn_id, sequence, idempotency_key "
        "FROM agent_checkpoints WHERE actor_id=?",
        (request_value.actor_id,),
    ).fetchall()

    assert replay_occurred_at != first_occurred_at
    assert len(events) == 1
    assert events[0].schema_version == "2"
    assert events[0].actor_id == request_value.actor_id
    assert events[0].turn_id == request_value.turn_id
    assert len(checkpoints) == 1
    assert tuple(checkpoints[0]) == (
        request_value.actor_id,
        request_value.turn_id,
        1,
        events[0].idempotency_key,
    )
    assert agent_control.current_actor_sequence(conn, request_value.actor_id) == 1


def test_two_turns_share_actor_and_advance_actor_checkpoint_sequence(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = init_db(tmp_path / "two-turns.db")
    monkeypatch.setattr(database, "get_connection", lambda: conn)
    actor_id = application_actor_id("attempt-durable-1")
    now = datetime(2026, 8, 30, 10, 0, tzinfo=UTC)
    first_request = request(actor_id=actor_id, turn_id="turn-1")
    second_request = request(actor_id=actor_id, turn_id="turn-2")

    persist_completion(
        first_request,
        result(first_request.turn_id, completed_at=now),
        occurred_after=now,
    )
    persist_completion(
        second_request,
        result(second_request.turn_id, completed_at=now + timedelta(seconds=1)),
        occurred_after=now + timedelta(seconds=1),
    )

    checkpoints = conn.execute(
        "SELECT actor_id, turn_id, sequence FROM agent_checkpoints "
        "WHERE actor_id=? ORDER BY sequence",
        (actor_id,),
    ).fetchall()
    events = agent_control.list_events(
        conn,
        attempt_id=first_request.attempt_id,
        event_type="agent.turn.completed",
    )

    assert [tuple(row) for row in checkpoints] == [
        (actor_id, "turn-1", 1),
        (actor_id, "turn-2", 2),
    ]
    assert [(event.actor_id, event.turn_id) for event in events] == [
        (actor_id, "turn-1"),
        (actor_id, "turn-2"),
    ]
    latest = agent_control.latest_actor_checkpoint(conn, actor_id)
    assert latest is not None
    assert latest.turn_id == "turn-2"


def test_human_only_completion_replay_is_atomic_across_all_control_records(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = init_db(tmp_path / "human-only-replay.db")
    monkeypatch.setattr(database, "get_connection", lambda: conn)
    recorded_outcomes: list[tuple[bool, bool, bool | None]] = []
    real_record_agent_turn_control = database.record_agent_turn_control

    def record_agent_turn_control(*args, **kwargs):
        outcome = real_record_agent_turn_control(*args, **kwargs)
        recorded_outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(database, "record_agent_turn_control", record_agent_turn_control)
    request_value = request(
        actor_id=application_actor_id("attempt-durable-1"), turn_id="turn-captcha"
    )
    completed_at = datetime(2026, 8, 30, 10, 30, tzinfo=UTC)
    first_result = AgentTurnResult(
        run_id=request_value.turn_id,
        status="captcha",
        summary="Synthetic CAPTCHA boundary",
        requested_human_input="Untrusted free text",
        completed_at=completed_at,
    )

    first_occurred_at = persist_completion(
        request_value,
        first_result,
        occurred_after=completed_at,
        expected_checkpoint_sequence=0,
        application_status="captcha",
    )
    persist_completion(
        request_value,
        replace(first_result, completed_at=completed_at + timedelta(seconds=5)),
        occurred_after=first_occurred_at + timedelta(seconds=5),
        expected_checkpoint_sequence=0,
        application_status="captcha",
    )

    completed_events = agent_control.list_events(
        conn,
        attempt_id=request_value.attempt_id,
        event_type="agent.turn.completed",
    )
    checkpoint_count = conn.execute(
        "SELECT COUNT(*) FROM agent_checkpoints WHERE actor_id=?",
        (request_value.actor_id,),
    ).fetchone()[0]
    open_requests = agent_control.list_open_human_requests(
        conn, attempt_id=request_value.attempt_id
    )

    assert len(completed_events) == 1
    assert checkpoint_count == 1
    assert len(open_requests) == 1
    assert recorded_outcomes == [(True, True, True), (False, False, False)]
    assert open_requests[0].request_type == "captcha"
    assert open_requests[0].context["actor_id"] == request_value.actor_id
    assert open_requests[0].context["turn_id"] == request_value.turn_id


def test_run_job_persists_one_v2_actor_identity_across_the_real_control_chain(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_dir = tmp_path / "app"
    worker_dir = app_dir / "workers" / "worker-0"
    log_dir = app_dir / "logs"
    worker_dir.mkdir(parents=True)
    log_dir.mkdir(parents=True)
    db_path = tmp_path / "run-job-control.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    database.init_db(db_path)
    monkeypatch.setattr(config, "APP_DIR", app_dir)
    monkeypatch.setattr(config, "APPLY_WORKER_DIR", app_dir / "workers")
    monkeypatch.setattr(config, "LOG_DIR", log_dir)
    monkeypatch.setattr(
        config,
        "load_profile",
        lambda: {
            "authentication": {},
            "agent_runtime": {"playwright_mcp": {"env": {}}},
        },
    )
    monkeypatch.setattr(launcher, "reset_worker_dir", lambda _worker_id: worker_dir)
    monkeypatch.setattr(launcher.prompt_mod, "build_prompt", lambda **_kwargs: "PROMPT")
    monkeypatch.setattr(launcher, "_resolve_codex_command", lambda: ["codex"])
    monkeypatch.setattr(launcher, "update_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "add_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "get_state", lambda *_args: None)
    monkeypatch.setattr(launcher, "_archive_worker_evidence", lambda *_args: [])

    class Timer:
        def cancel(self) -> None:
            return None

    monkeypatch.setattr(
        launcher,
        "_start_timeout_watchdog",
        lambda *_args: (threading.Event(), Timer()),
    )

    class Process:
        pid = 4321
        returncode = 0

        def __init__(self, *, env: dict[str, str]) -> None:
            self.stdin = io.StringIO()
            report = {
                "schema_version": "future-compatible",
                "run_id": env[agent_report_mcp.RUN_ID_ENV],
                "status": "ready_to_submit",
                "summary": "Synthetic structured result",
                "observations": {},
                "proposals": [
                    {
                        "kind": "specialist-review",
                        "summary": "Review one unfamiliar field",
                        "concurrency_mode": "adaptive",
                    }
                ],
            }
            Path(env[agent_report_mcp.REPORT_PATH_ENV]).write_text(
                json.dumps(report), encoding="utf-8"
            )
            messages = [
                {
                    "type": "item.completed",
                    "item": {
                        "type": "mcp_tool_call",
                        "server": "playwright",
                        "tool": "browser_snapshot",
                        "status": "completed",
                    },
                },
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "RESULT:READY_TO_SUBMIT"},
                },
                {"type": "turn.completed", "usage": {}},
            ]
            self.stdout = io.StringIO(
                "\n".join(json.dumps(message) for message in messages)
            )

        def wait(self, timeout: int | None = None) -> int:
            return self.returncode

        def poll(self) -> int:
            return self.returncode

    monkeypatch.setattr(
        launcher.subprocess,
        "Popen",
        lambda _command, **kwargs: Process(env=kwargs["env"]),
    )
    attempt_id = "attempt-run-job-v2"
    job = {
        "url": "https://example.test/jobs/durable-v2",
        "application_url": "https://example.test/apply/durable-v2",
        "title": "Data Intern",
        "company_name": "Example",
        "site": "example",
        "fit_score": 9,
        "_attempt_id": attempt_id,
        "_browser_backend": "edge",
        "_agent_proposal_runner": lambda item: f"handled:{item.kind}",
    }

    status, _ = launcher.run_job(
        job,
        port=9432,
        worker_id=0,
        model="model",
        dry_run=False,
        agent_backend="codex",
        submission_phase="prepare",
    )
    conn = database.get_connection(db_path)
    events = conn.execute(
        "SELECT event_type, run_id, actor_id, turn_id, schema_version, idempotency_key "
        "FROM agent_events WHERE attempt_id=? ORDER BY occurred_at, event_id",
        (attempt_id,),
    ).fetchall()
    checkpoint = conn.execute(
        "SELECT run_id, actor_id, turn_id, expected_sequence, sequence, "
        "fresh_turn_resume_authorized, schema_version, idempotency_key "
        "FROM agent_checkpoints WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()

    assert status == "ready_to_submit"
    assert [row[0] for row in events] == [
        "agent.turn.started",
        "agent.proposals.executed",
        "agent.turn.completed",
    ]
    turn_id = events[0][1]
    actor_id = application_actor_id(attempt_id)
    assert all(tuple(row[1:5]) == (turn_id, actor_id, turn_id, "2") for row in events)
    assert all(row[5] for row in events)
    assert len({row[5] for row in events}) == 3
    assert checkpoint is not None
    assert tuple(checkpoint[:7]) == (turn_id, actor_id, turn_id, 0, 1, 0, "2")
    assert checkpoint[7] == events[-1][5]
    database.close_connection(db_path)


def test_checkpoint_compare_and_swap_rejects_stale_expected_sequence(tmp_path) -> None:
    conn = init_db(tmp_path / "stale-sequence.db")
    now = datetime(2026, 8, 30, 11, 0, tzinfo=UTC)
    first = AgentCheckpoint(
        checkpoint_id="application:attempt-1:turn-1:checkpoint:1",
        run_id="turn-1",
        actor_id=application_actor_id("attempt-1"),
        turn_id="turn-1",
        attempt_id="attempt-1",
        phase="prepare",
        sequence=1,
        expected_sequence=0,
        idempotency_key="application:attempt-1:turn-1:completed",
        schema_version="2",
        state={"application_status": "completed"},
        created_at=now,
    )
    stale = AgentCheckpoint(
        checkpoint_id="application:attempt-1:turn-2:checkpoint:1",
        run_id="turn-2",
        actor_id=application_actor_id("attempt-1"),
        turn_id="turn-2",
        attempt_id="attempt-1",
        phase="prepare",
        sequence=1,
        expected_sequence=0,
        idempotency_key="application:attempt-1:turn-2:completed",
        schema_version="2",
        state={"application_status": "completed"},
        created_at=now + timedelta(seconds=1),
    )

    assert agent_control.append_checkpoint(conn, first) is True
    with pytest.raises(ValueError, match="stale expected sequence|expected sequence"):
        agent_control.append_checkpoint(conn, stale)

    actor_id = application_actor_id("attempt-1")
    assert agent_control.current_actor_sequence(conn, actor_id) == 1
    assert agent_control.latest_actor_checkpoint(conn, actor_id) == first


def test_turn_control_savepoint_preserves_caller_transaction_on_stale_checkpoint(
    tmp_path,
) -> None:
    conn = init_db(tmp_path / "caller-owned-transaction.db")
    actor_id = application_actor_id("attempt-1")
    now = datetime(2026, 8, 30, 11, 30, tzinfo=UTC)
    first = AgentCheckpoint(
        checkpoint_id="application:attempt-1:turn-1:checkpoint:1",
        run_id="turn-1",
        actor_id=actor_id,
        turn_id="turn-1",
        attempt_id="attempt-1",
        phase="prepare",
        sequence=1,
        expected_sequence=0,
        idempotency_key="application:attempt-1:turn-1:completed",
        schema_version="2",
        state={"application_status": "completed"},
        created_at=now,
    )
    assert agent_control.append_checkpoint(conn, first) is True
    conn.commit()
    conn.execute("CREATE TABLE caller_notes (note TEXT NOT NULL)")
    conn.commit()

    event = ApplicationEvent(
        event_id="application:attempt-1:turn-2:completed",
        attempt_id="attempt-1",
        run_id="turn-2",
        actor_id=actor_id,
        turn_id="turn-2",
        phase="prepare",
        actor="browser-application-agent",
        event_type="agent.turn.completed",
        idempotency_key="application:attempt-1:turn-2:completed",
        schema_version="2",
        occurred_at=now + timedelta(seconds=1),
    )
    stale_checkpoint = AgentCheckpoint(
        checkpoint_id="application:attempt-1:turn-2:checkpoint:1",
        run_id="turn-2",
        actor_id=actor_id,
        turn_id="turn-2",
        attempt_id="attempt-1",
        phase="prepare",
        sequence=1,
        expected_sequence=0,
        idempotency_key=event.idempotency_key,
        schema_version="2",
        state={"application_status": "completed"},
        created_at=now + timedelta(seconds=1),
    )
    human_request = HumanRequest(
        request_id="application:attempt-1:turn-2:human:1",
        run_id="turn-2",
        attempt_id="attempt-1",
        request_type="captcha",
        prompt="Inspect the synthetic interruption.",
        context={"actor_id": actor_id, "turn_id": "turn-2"},
        created_at=now + timedelta(seconds=1),
    )

    conn.execute("BEGIN")
    conn.execute("INSERT INTO caller_notes VALUES ('caller-owned')")
    with pytest.raises(ValueError, match="stale expected sequence|expected sequence"):
        database.record_agent_turn_control(
            event,
            stale_checkpoint,
            human_request,
            conn=conn,
        )
    conn.commit()

    assert [
        row[0] for row in conn.execute("SELECT note FROM caller_notes").fetchall()
    ] == ["caller-owned"]
    assert conn.execute(
        "SELECT COUNT(*) FROM agent_events WHERE event_id=?", (event.event_id,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM agent_checkpoints WHERE turn_id='turn-2'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM agent_human_requests WHERE request_id=?",
        (human_request.request_id,),
    ).fetchone()[0] == 0


def test_existing_v1_sqlite_rows_migrate_readably_without_write_or_resume_authority(
    tmp_path,
) -> None:
    db_path = tmp_path / "legacy-v1.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE agent_events (
            event_id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            phase TEXT NOT NULL,
            actor TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            idempotency_key TEXT,
            occurred_at TEXT NOT NULL
        );
        CREATE TABLE agent_checkpoints (
            checkpoint_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            attempt_id TEXT NOT NULL,
            phase TEXT NOT NULL,
            sequence INTEGER NOT NULL CHECK(sequence >= 0),
            state_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, sequence)
        );
        """
    )
    legacy.execute(
        "INSERT INTO agent_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy-event-1",
            "attempt-legacy",
            "legacy-run-1",
            "prepare",
            "legacy-runtime",
            "agent.turn.completed",
            "{}",
            "[]",
            "legacy-run-1:completed",
            "2026-08-29T09:00:00+00:00",
        ),
    )
    legacy.execute(
        "INSERT INTO agent_checkpoints VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "legacy-checkpoint-1",
            "legacy-run-1",
            "attempt-legacy",
            "prepare",
            1,
            '{"application_status":"completed"}',
            "2026-08-29T09:00:00+00:00",
        ),
    )
    legacy.commit()
    legacy.close()

    conn = init_db(db_path)
    legacy_event = agent_control.list_events(conn, run_id="legacy-run-1")[0]
    legacy_checkpoint = agent_control.latest_checkpoint(conn, "legacy-run-1")

    assert legacy_event.schema_version == "1"
    assert legacy_checkpoint is not None
    assert legacy_checkpoint.schema_version == "1"
    assert legacy_checkpoint.fresh_turn_resume_authorized is False
    with pytest.raises(ValueError, match="schema v1|schema_version|legacy"):
        agent_control.append_checkpoint(
            conn,
            replace(
                legacy_checkpoint,
                checkpoint_id="legacy-checkpoint-rewrite",
                idempotency_key="legacy-run-1:rewrite",
            ),
        )
