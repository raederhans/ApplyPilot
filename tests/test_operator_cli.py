"""P4 CLI contracts for the local operator control plane.

These tests deliberately use an explicit file-backed database and prevent the
ordinary CLI bootstrap/default database from being used.  They exercise only
the local command plane; no test may open a browser, spawn a process, or
submit an application.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from applypilot import cli, database
from applypilot.apply import launcher
from applypilot.apply.contracts import (
    AgentCheckpoint,
    ApplicationException,
    HumanRequest,
    application_actor_id,
)
from applypilot.apply.human_handoff import HumanResponseRef, append_human_response
from applypilot.storage import agent_control, runtime_control

NOW = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
RESPONSE_DIGEST = hashlib.sha256(b"operator-answer-reference").hexdigest()


@pytest.fixture
def operator_db(tmp_path: Path) -> Iterator[tuple[Path, sqlite3.Connection]]:
    """Create an isolated, file-backed control plane, never the user DB."""
    db_path = tmp_path / "operator-cli.db"
    connection = database.init_db(db_path)
    try:
        yield db_path, connection
    finally:
        database.close_connection(db_path)


def _exception(
    connection: sqlite3.Connection,
    *,
    exception_id: str,
    attempt_id: str = "attempt-cli",
    run_id: str = "run-cli",
    queue_kind: str = "parked",
    context: dict[str, object] | None = None,
) -> ApplicationException:
    item = ApplicationException(
        exception_id=exception_id,
        command_id=f"park:{exception_id}",
        run_id=run_id,
        attempt_id=attempt_id,
        actor_id=application_actor_id(attempt_id),
        turn_id=run_id,
        queue_kind=queue_kind,  # type: ignore[arg-type]
        failure_category="bounded_failure",
        next_action="operator_review",
        context=context or {"application_ref": f"application:{exception_id}"},
        created_at=NOW,
    )
    assert agent_control.enqueue_exception(connection, item)
    connection.commit()
    return item


def _json_output(result: object) -> dict[str, object]:
    output = result.output
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:  # pragma: no cover - assertion helper
        raise AssertionError(f"operator command must emit only JSON, got: {output!r}") from exc
    assert isinstance(value, dict)
    return value


def _invoke_isolated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    arguments: list[str],
) -> object:
    """Invoke a CLI command while proving it cannot use the default workspace."""
    forbidden_default = tmp_path / "default-db-must-not-be-opened.db"
    original_get_connection = database.get_connection
    monkeypatch.setattr(database, "DB_PATH", forbidden_default)

    def guarded_get_connection(path: Path | str | None = None) -> sqlite3.Connection:
        if path is None or Path(path) == forbidden_default:
            raise AssertionError("operator CLI touched database.DB_PATH instead of --db-path")
        return original_get_connection(path)

    monkeypatch.setattr(database, "get_connection", guarded_get_connection)
    monkeypatch.setattr(
        cli,
        "_bootstrap",
        lambda: (_ for _ in ()).throw(AssertionError("operator CLI must not call _bootstrap")),
    )
    return CliRunner().invoke(cli.app, arguments)


def _table_snapshot(connection: sqlite3.Connection, table: str) -> list[tuple[object, ...]]:
    columns = connection.execute(f"PRAGMA table_info({table})").fetchall()
    if not columns:
        return []
    order = str(columns[0][1])
    return [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY {order}")]


def _job_state_snapshot(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
    """Capture execution-relevant job state without coupling to schema migrations."""
    return [
        tuple(row)
        for row in connection.execute(
            "SELECT url,apply_status,apply_attempts,apply_retry_blocked,apply_retry_reason,"
            "verification_confidence,application_evidence,application_recorded_at "
            "FROM jobs ORDER BY url"
        )
    ]


def test_exception_read_commands_are_json_read_only_and_explicit_db(
    operator_db: tuple[Path, sqlite3.Connection],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path, connection = operator_db
    open_item = _exception(connection, exception_id="exception-open")
    _exception(
        connection,
        exception_id="exception-sensitive",
        context={"human_specific": True, "application_ref": "application:sensitive"},
    )
    connection.execute("UPDATE agent_exception_queue SET status='resolved' WHERE exception_id='exception-sensitive'")
    connection.commit()
    before = _table_snapshot(connection, "agent_exception_queue")

    listed = _invoke_isolated(
        monkeypatch,
        tmp_path,
        ["exceptions", "list", "--db-path", str(db_path), "--status", "open"],
    )
    assert listed.exit_code == 0, listed.output
    listed_json = _json_output(listed)
    assert "exception-open" in json.dumps(listed_json)
    assert "exception-sensitive" not in json.dumps(listed_json)

    shown = _invoke_isolated(
        monkeypatch,
        tmp_path,
        ["exceptions", "show", open_item.exception_id, "--db-path", str(db_path)],
    )
    assert shown.exit_code == 0, shown.output
    assert _json_output(shown)["exception"]["exception_id"] == open_item.exception_id

    grouped = _invoke_isolated(
        monkeypatch,
        tmp_path,
        ["exceptions", "group", "--db-path", str(db_path)],
    )
    assert grouped.exit_code == 0, grouped.output
    assert "application:exception-open" in json.dumps(_json_output(grouped))
    assert _table_snapshot(connection, "agent_exception_queue") == before

    # A semantic group is display-only, never a broad execution target.
    broad = _invoke_isolated(
        monkeypatch,
        tmp_path,
        [
            "exceptions",
            "resolve",
            "application:exception-open",
            "--db-path",
            str(db_path),
            "--command-id",
            "cmd-broad",
        ],
    )
    assert broad.exit_code != 0
    _json_output(broad)
    assert _table_snapshot(connection, "agent_exception_queue") == before


def test_exception_resolve_only_dismisses_exact_queue_item(
    operator_db: tuple[Path, sqlite3.Connection],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path, connection = operator_db
    item = _exception(connection, exception_id="exception-resolve")
    job_url = "https://jobs.example.test/operator-resolve"
    connection.execute(
        "INSERT INTO jobs (url, apply_status, apply_retry_blocked) VALUES (?, 'submission_uncertain', 1)",
        (job_url,),
    )
    connection.commit()
    unchanged = {
        "jobs": _job_state_snapshot(connection),
        **{
            table: _table_snapshot(connection, table)
            for table in (
                "application_attempts",
                "agent_runtime_turns",
                "application_submission_gates",
                "application_receipts",
            )
        },
    }

    result = _invoke_isolated(
        monkeypatch,
        tmp_path,
        [
            "exceptions",
            "resolve",
            item.exception_id,
            "--db-path",
            str(db_path),
            "--command-id",
            "cmd-resolve",
        ],
    )
    assert result.exit_code == 0, result.output
    body = _json_output(result)
    assert body["result"]["command_id"] == "cmd-resolve"
    assert body["result"]["resolved"] is True
    replay = _invoke_isolated(
        monkeypatch,
        tmp_path,
        [
            "exceptions",
            "resolve",
            item.exception_id,
            "--db-path",
            str(db_path),
            "--command-id",
            "cmd-resolve",
        ],
    )
    assert replay.exit_code == 0, replay.output
    replay_body = _json_output(replay)
    assert replay_body["result"]["replayed"] is True
    assert replay_body["result"]["resolved"] is True
    assert agent_control.get_exception(connection, item.exception_id).status == "resolved"  # type: ignore[union-attr]
    after = {
        "jobs": _job_state_snapshot(connection),
        **{table: _table_snapshot(connection, table) for table in unchanged if table != "jobs"},
    }
    assert after == unchanged


def test_resume_without_same_process_owner_persists_request_without_side_effects(
    operator_db: tuple[Path, sqlite3.Connection],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path, connection = operator_db
    attempt_id = "attempt-resume"
    actor_id = application_actor_id(attempt_id)
    run_id = "run-resume"
    job_url = "https://jobs.example.test/operator-resume"
    profile_id = "profile:operator-cli"
    runtime_id = "runtime:operator-cli"
    connection.execute("INSERT INTO jobs (url, apply_status) VALUES (?, 'in_progress')", (job_url,))
    started_attempt = database.start_application_attempt(job_url, "worker-cli", conn=connection)
    # The operator target must use the attempt actually issued by the ledger.
    attempt_id = started_attempt
    actor_id = application_actor_id(attempt_id)
    parent = runtime_control.start_runtime_turn(
        connection,
        turn_id=run_id,
        actor_id=actor_id,
        attempt_id=attempt_id,
        runtime_id=runtime_id,
        profile_id=profile_id,
        runtime_backend="test-runtime",
        resume_mode="root",
        submit_started=False,
        tool_surface_hash="tools",
        prompt_contract_hash="prompt",
    )
    runtime_control.mark_runtime_turn_terminal(
        connection,
        token=runtime_control.token_from_turn(parent),
        status="blocked",
        failure_code="HUMAN_INPUT_REQUIRED",
        exit_code=0,
    )
    checkpoint = AgentCheckpoint(
        checkpoint_id="checkpoint-resume",
        run_id=run_id,
        attempt_id=attempt_id,
        actor_id=actor_id,
        turn_id=run_id,
        phase="prepare",
        sequence=1,
        expected_sequence=0,
        state={"next": ["human_response"]},
        idempotency_key="checkpoint:resume",
        schema_version="2",
    )
    assert agent_control.append_checkpoint(connection, checkpoint)
    request = HumanRequest(
        request_id="request-cli",
        run_id=run_id,
        attempt_id=attempt_id,
        request_type="screening_answer",
        prompt="Confirm the referenced answer",
        context={"actor_id": actor_id, "turn_id": run_id},
        created_at=datetime.now(UTC),
    )
    assert agent_control.create_human_request(connection, request)
    lease = runtime_control.acquire_browser_resource_lease(
        connection,
        lease_id="lease-resume",
        resource_kind="browser",
        scope_id="scope-resume",
        profile_id=profile_id,
        page_target_id="page-resume",
        owner_id="owner-resume",
        actor_id=actor_id,
        attempt_id=attempt_id,
        runtime_id=runtime_id,
        lease_seconds=600,
    )
    item = _exception(
        connection,
        exception_id="exception-resume",
        attempt_id=attempt_id,
        run_id=run_id,
        queue_kind="human_only",
        context={
            "request_id": "request-cli",
            "checkpoint_id": checkpoint.checkpoint_id,
            "job_url": job_url,
            "profile_id": profile_id,
            "browser_lease_id": lease.lease_id,
            "browser_lease_epoch": lease.lease_epoch,
            "page_target_id": str(lease.page_target_id),
            "page_epoch": lease.page_epoch,
        },
    )
    response = HumanResponseRef(
        request_id="request-cli",
        response_ref="response-store://operator-cli",
        response_digest=RESPONSE_DIGEST,
        response_type="screening_answer",
        resolved_by="human:user",
        resolved_at=datetime.now(UTC),
    )
    assert append_human_response(connection, response)
    connection.commit()
    spawned: list[object] = []
    launched: list[object] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append((a, k)))
    monkeypatch.setattr(launcher, "run_job", lambda *a, **k: launched.append((a, k)))

    result = _invoke_isolated(
        monkeypatch,
        tmp_path,
        [
            "exceptions",
            "resume",
            item.exception_id,
            "--db-path",
            str(db_path),
            "--command-id",
            "cmd-resume",
            "--request-id",
            response.request_id,
        ],
    )
    assert result.exit_code == 0, result.output
    body = _json_output(result)
    assert body["ok"] is True
    assert body["result"]["status"] == "requested"
    assert body["result"]["resolved"] is False
    replay = _invoke_isolated(
        monkeypatch,
        tmp_path,
        [
            "exceptions",
            "resume",
            item.exception_id,
            "--db-path",
            str(db_path),
            "--command-id",
            "cmd-resume",
            "--request-id",
            response.request_id,
        ],
    )
    assert replay.exit_code == 0, replay.output
    assert _json_output(replay)["result"]["replayed"] is True
    assert "operator-answer-reference" not in result.output
    assert "--answer" not in result.output
    assert spawned == []
    assert launched == []
    assert agent_control.get_exception(connection, item.exception_id).status == "open"  # type: ignore[union-attr]
    assert connection.execute(
        "SELECT COUNT(*) FROM operator_command_envelopes WHERE command_id='cmd-resume'"
    ).fetchone()[0] == 1
    assert connection.execute(
        "SELECT stage FROM operator_command_results WHERE command_id='cmd-resume'"
    ).fetchone()[0] == "requested"
    assert connection.execute(
        "SELECT status FROM agent_human_requests WHERE request_id='request-cli'"
    ).fetchone()[0] == "open"


def _receipt_exception_with_exact_gate(
    connection: sqlite3.Connection,
) -> tuple[ApplicationException, dict[str, object]]:
    job_url = "https://jobs.example.test/operator-reconcile"
    connection.execute(
        "INSERT INTO jobs (url, title, company_name, apply_status) VALUES (?, ?, ?, 'in_progress')",
        (job_url, "Operator Analyst", "Example Operator"),
    )
    attempt_id = database.start_application_attempt(job_url, "worker-cli", conn=connection)
    assert database.update_application_attempt(attempt_id, phase="reservation", submit_started=False, conn=connection)
    claim = database.claim_submission_gate(
        "batch-operator-cli",
        job_url,
        1,
        attempt_id,
        success_target=1,
        hourly_maximum=10,
        minimum_gap_seconds=0,
        conn=connection,
        now=datetime.now(UTC),
    )
    assert claim["claimed"] is True
    assert database.update_application_attempt(attempt_id, phase="submit", submit_started=True, conn=connection)
    connection.execute("UPDATE jobs SET apply_status='submission_uncertain' WHERE url=?", (job_url,))
    assert database.update_submission_gate_state(attempt_id, "submission_uncertain", conn=connection)
    database.update_batch_submission_status("batch-operator-cli", job_url, "submission_uncertain", conn=connection)
    item = _exception(
        connection,
        exception_id="exception-reconcile",
        attempt_id=attempt_id,
        queue_kind="receipt_reconciliation",
    )
    evidence = {
        "source": "browser_receipt",
        "receipt_id": "receipt-operator-cli",
        "job_url": job_url,
        "company_name": "Example Operator",
        "job_title": "Operator Analyst",
        "confirmation_text": "Application successfully submitted",
        "gate_id": claim["gate_id"],
        "batch_id": "batch-operator-cli",
        "attempt_id": attempt_id,
    }
    return item, evidence


def test_runs_reconcile_only_closes_exact_receipt_exception_after_verified_receipt(
    operator_db: tuple[Path, sqlite3.Connection],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path, connection = operator_db
    item, evidence = _receipt_exception_with_exact_gate(connection)
    evidence_path = tmp_path / "exact-receipt.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    spawned: list[object] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append((a, k)))

    result = _invoke_isolated(
        monkeypatch,
        tmp_path,
        [
            "runs",
            "reconcile",
            item.attempt_id,
            "--db-path",
            str(db_path),
            "--command-id",
            "cmd-reconcile",
            "--evidence-file",
            str(evidence_path),
        ],
    )
    assert result.exit_code == 0, result.output
    body = _json_output(result)
    assert body["result"]["command_id"] == "cmd-reconcile"
    assert body["result"]["resolved"] is True
    assert spawned == []
    assert agent_control.get_exception(connection, item.exception_id).status == "resolved"  # type: ignore[union-attr]
    assert (
        connection.execute("SELECT apply_status FROM jobs WHERE url=?", (evidence["job_url"],)).fetchone()[0]
        == "applied"
    )


def test_runs_reconcile_rejects_substituted_or_incomplete_evidence_and_keeps_queue_open(
    operator_db: tuple[Path, sqlite3.Connection],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path, connection = operator_db
    item, evidence = _receipt_exception_with_exact_gate(connection)
    evidence["job_title"] = "Different role"
    evidence.pop("gate_id")
    evidence_path = tmp_path / "rejected-receipt.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    rejected = _invoke_isolated(
        monkeypatch,
        tmp_path,
        [
            "runs",
            "reconcile",
            item.attempt_id,
            "--db-path",
            str(db_path),
            "--command-id",
            "cmd-reconcile-rejected",
            "--evidence-file",
            str(evidence_path),
        ],
    )
    assert rejected.exit_code != 0
    assert _json_output(rejected)["ok"] is False
    assert agent_control.get_exception(connection, item.exception_id).status == "open"  # type: ignore[union-attr]

    # The immutable command is byte-bound. Substitution after the failed run must
    # not turn the same command id into a successful reconciliation.
    evidence["job_title"] = "Operator Analyst"
    evidence["gate_id"] = "substituted-gate-id"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    replay = _invoke_isolated(
        monkeypatch,
        tmp_path,
        [
            "runs",
            "reconcile",
            item.attempt_id,
            "--db-path",
            str(db_path),
            "--command-id",
            "cmd-reconcile-rejected",
            "--evidence-file",
            str(evidence_path),
        ],
    )
    assert replay.exit_code != 0
    assert _json_output(replay)["ok"] is False
    assert agent_control.get_exception(connection, item.exception_id).status == "open"  # type: ignore[union-attr]
