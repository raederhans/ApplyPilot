from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from applypilot.apply.contracts import (
    AgentCheckpoint,
    AgentProposal,
    AgentRunRequest,
    AgentTurnResult,
    ApplicationEvent,
    HumanRequest,
    ToolSpec,
    contract_json,
)
from applypilot.database import append_agent_event, init_db
from applypilot.storage import agent_control


def connection() -> sqlite3.Connection:
    return sqlite3.connect(":memory:")


def test_contracts_are_provider_and_workflow_extensible() -> None:
    tool = ToolSpec(
        name="inspect_dynamic_form",
        description="Observe a form without writing it",
        phases=("tenant-specific-review-v2",),
        side_effect="proposal-only",
        concurrency_mode="parallel-by-page",
        metadata={"provider": "future-runtime", "capability": "semantic-map"},
    )
    request = AgentRunRequest(
        run_id="run-1",
        attempt_id="attempt-1",
        agent_role="form-mapper",
        phase="tenant-specific-review-v2",
        objective="Produce a fill proposal",
        context={"page_ref": "evidence://page/1"},
        available_tools=(tool.name,),
        concurrency_mode="parallel-by-page",
    )
    result = AgentTurnResult(
        run_id=request.run_id,
        status="proposed",
        summary="Two independent fields can be evaluated concurrently",
        proposals=(
            AgentProposal(kind="field-map", summary="Map location", concurrency_mode="parallel"),
            AgentProposal(kind="field-map", summary="Map portfolio", concurrency_mode="parallel"),
        ),
    )

    assert contract_json(tool)["side_effect"] == "proposal-only"
    assert contract_json(request)["created_at"].endswith("+00:00")
    assert len(result.proposals) == 2


@pytest.mark.parametrize(
    "unsafe",
    [
        {"browser_handle": object()},
        {"browser_handle": "live-page-1"},
        {"password": "plain text"},
        {"message_body": "private application text"},
        {"verification_code": "123456"},
        {"nested": {"cookies": ["session=secret"]}},
    ],
)
def test_durable_contracts_reject_handles_and_raw_secrets(unsafe: dict) -> None:
    with pytest.raises((TypeError, ValueError)):
        ApplicationEvent(
            event_id="event-unsafe",
            attempt_id="attempt-1",
            run_id="run-1",
            phase="prepare",
            actor="mapper",
            event_type="observed",
            payload=unsafe,
        )


def test_events_are_append_only_idempotent_and_collision_safe() -> None:
    conn = connection()
    event = ApplicationEvent(
        event_id="event-1",
        attempt_id="attempt-1",
        run_id="run-1",
        phase="custom-phase",
        actor="runtime-a",
        event_type="proposal.created",
        payload={"proposal_ids": ["p1", "p2"], "strategy": "parallel"},
        evidence_refs=("evidence://snapshot/1",),
    )

    assert agent_control.append_event(conn, event) is True
    assert agent_control.append_event(conn, event) is False
    assert agent_control.list_events(
        conn, attempt_id="attempt-1", event_type="proposal.created"
    ) == [event]
    with pytest.raises(ValueError, match="event_id collision"):
        agent_control.append_event(
            conn,
            ApplicationEvent(
                event_id="event-1",
                attempt_id="attempt-1",
                run_id="run-1",
                phase="custom-phase",
                actor="runtime-b",
                event_type="proposal.created",
            ),
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE agent_events SET actor='changed' WHERE event_id='event-1'")


def test_idempotency_key_replay_is_a_noop_and_conflicts_are_rejected() -> None:
    conn = connection()
    now = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)
    original = ApplicationEvent(
        event_id="event-original",
        attempt_id="attempt-1",
        run_id="run-1",
        phase="prepare",
        actor="runtime",
        event_type="agent.turn.started",
        payload={"surface": "dynamic"},
        idempotency_key="run-1:started",
        occurred_at=now,
    )
    replay = ApplicationEvent(
        event_id="event-retry",
        attempt_id="attempt-1",
        run_id="run-1",
        phase="prepare",
        actor="runtime",
        event_type="agent.turn.started",
        payload={"surface": "dynamic"},
        idempotency_key="run-1:started",
        occurred_at=now + timedelta(seconds=1),
    )

    assert agent_control.append_event(conn, original) is True
    assert agent_control.append_event(conn, replay) is False
    with pytest.raises(ValueError, match="idempotency_key collision"):
        agent_control.append_event(
            conn,
            ApplicationEvent(
                event_id="event-conflict",
                attempt_id="attempt-1",
                run_id="run-1",
                phase="submit",
                actor="runtime",
                event_type="agent.turn.completed",
                idempotency_key="run-1:started",
            ),
        )


def test_latest_checkpoint_is_sequence_based_and_does_not_change_jobs() -> None:
    conn = connection()
    conn.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY, apply_status TEXT)")
    conn.execute("INSERT INTO jobs VALUES ('https://example.test/job', 'in_progress')")
    now = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)
    first = AgentCheckpoint(
        checkpoint_id="cp-1",
        run_id="run-1",
        attempt_id="attempt-1",
        phase="prepare",
        sequence=1,
        state={"completed_proposals": []},
        created_at=now,
    )
    second = AgentCheckpoint(
        checkpoint_id="cp-2",
        run_id="run-1",
        attempt_id="attempt-1",
        phase="review",
        sequence=2,
        state={"completed_proposals": ["p1"], "next": ["p2"]},
        created_at=now,
    )

    assert agent_control.append_checkpoint(conn, first)
    assert agent_control.append_checkpoint(conn, second)
    assert agent_control.append_checkpoint(conn, second) is False
    latest = agent_control.latest_checkpoint(conn, "run-1")

    assert latest == second
    assert conn.execute("SELECT apply_status FROM jobs").fetchone()[0] == "in_progress"


def test_human_requests_persist_and_resolve_without_application_state_side_effects() -> None:
    conn = connection()
    request = HumanRequest(
        request_id="human-1",
        run_id="run-1",
        attempt_id="attempt-1",
        request_type="clarification",
        prompt="Confirm the work authorization answer",
        context={"question_ref": "evidence://question/1"},
    )

    assert agent_control.create_human_request(conn, request) is True
    assert agent_control.create_human_request(conn, request) is False
    assert agent_control.list_open_human_requests(conn, attempt_id="attempt-1") == [request]
    assert agent_control.resolve_human_request(
        conn,
        "human-1",
        resolved_at=datetime(2026, 8, 28, 11, 0, tzinfo=UTC),
    )
    assert agent_control.list_open_human_requests(conn) == []
    assert not agent_control.resolve_human_request(conn, "human-1")


def test_init_db_wires_control_schema_without_changing_jobs(tmp_path) -> None:
    conn = init_db(tmp_path / "agent-control.db")
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'agent_%'"
        )
    }

    assert tables == {
        "agent_events",
        "agent_checkpoints",
        "agent_human_requests",
        "agent_human_responses",
        "agent_tasks",
    }
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_database_facade_respects_outer_transaction_ownership(tmp_path) -> None:
    conn = init_db(tmp_path / "agent-rollback.db")
    event = ApplicationEvent(
        event_id="event-rollback",
        attempt_id="attempt-rollback",
        run_id="run-rollback",
        phase="future-phase",
        actor="future-runtime",
        event_type="proposal.created",
    )
    conn.execute("BEGIN")

    assert append_agent_event(event, conn) is True
    conn.rollback()

    assert agent_control.list_events(conn, run_id="run-rollback") == []
