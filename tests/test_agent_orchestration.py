from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from applypilot import config, database
from applypilot.apply import agent_output, agent_report_mcp, launcher
from applypilot.apply.contracts import (
    MAX_AGENT_PROPOSALS,
    MAX_AGENT_REPORT_BYTES,
    AgentProposal,
    AgentTurnResult,
    agent_turn_result_from_mapping,
)
from applypilot.apply.orchestration import execute_proposal_waves, plan_proposal_waves


def test_agent_event_clock_advances_when_wall_clock_collides(monkeypatch) -> None:
    fixed = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(launcher, "_agent_event_clock", lambda: fixed)

    started = launcher._ordered_agent_event_time()
    proposals = launcher._ordered_agent_event_time(started)
    completed = launcher._ordered_agent_event_time(proposals)

    assert started == fixed
    assert proposals == fixed + timedelta(microseconds=1)
    assert completed == fixed + timedelta(microseconds=2)


def proposal(
    proposal_id: str,
    *,
    mode: str = "parallel",
    key: str | None = None,
    depends_on: tuple[str, ...] = (),
) -> AgentProposal:
    return AgentProposal(
        proposal_id=proposal_id,
        kind="specialist",
        summary=f"Run {proposal_id}",
        concurrency_mode=mode,
        concurrency_key=key,
        depends_on=depends_on,
    )


def test_independent_specialists_can_execute_in_parallel() -> None:
    barrier = threading.Barrier(2, timeout=2)

    def runner(item: AgentProposal) -> str:
        barrier.wait()
        return item.proposal_id

    results = execute_proposal_waves(
        [proposal("research"), proposal("form-map")],
        runner,
        max_workers=2,
    )

    assert results == {"research": "research", "form-map": "form-map"}


def test_proposal_execution_is_serial_until_a_parallel_budget_is_explicit() -> None:
    active = 0
    peak = 0
    lock = threading.Lock()

    def runner(item: AgentProposal) -> str:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        with lock:
            active -= 1
        return item.proposal_id

    results = execute_proposal_waves(
        [proposal("research"), proposal("form-map")],
        runner,
    )

    assert results == {"research": "research", "form-map": "form-map"}
    assert peak == 1


def test_same_page_or_serial_proposals_are_split_into_ordered_waves() -> None:
    waves = plan_proposal_waves(
        [
            proposal("read-a", key="page-1"),
            proposal("read-b", key="page-2"),
            proposal("write-a", key="page-1"),
            proposal("submit", mode="serial", key="page-1"),
        ]
    )

    assert tuple(item.proposal_id for item in waves[0]) == ("read-a", "read-b")
    assert tuple(item.proposal_id for item in waves[1]) == ("write-a",)
    assert tuple(item.proposal_id for item in waves[2]) == ("submit",)


def test_dependencies_are_respected_without_fixing_agent_roles() -> None:
    waves = plan_proposal_waves(
        [
            proposal("plugin-discovery", mode="custom-parallel"),
            proposal("future-agent", depends_on=("plugin-discovery",)),
        ],
        parallel_modes={"custom-parallel", "parallel"},
    )

    assert [[item.proposal_id for item in wave] for wave in waves] == [
        ["plugin-discovery"],
        ["future-agent"],
    ]


def test_failed_dependency_blocks_downstream_specialist() -> None:
    called: list[str] = []

    def runner(item: AgentProposal) -> dict[str, str]:
        called.append(item.proposal_id)
        return {"status": "failed" if item.proposal_id == "first" else "completed"}

    outcomes = execute_proposal_waves(
        [proposal("first"), proposal("downstream", depends_on=("first",))],
        runner,
        dependency_succeeded=lambda outcome: outcome["status"] == "completed",
        blocked_result=lambda _item, dependencies: {
            "status": "blocked",
            "blocked_by": ",".join(dependencies),
        },
    )

    assert called == ["first"]
    assert outcomes["downstream"] == {"status": "blocked", "blocked_by": "first"}


def test_launcher_dispatches_only_trusted_non_submit_proposals() -> None:
    assert launcher._proposal_dispatch_allowed(
        result_source="structured",
        phase="prepare",
        dry_run=False,
    )
    assert launcher._proposal_dispatch_allowed(
        result_source="structured+legacy",
        phase="prepare",
        dry_run=False,
    )
    assert not launcher._proposal_dispatch_allowed(
        result_source="conflict",
        phase="prepare",
        dry_run=False,
    )
    assert not launcher._proposal_dispatch_allowed(
        result_source="structured",
        phase="submit",
        dry_run=False,
    )
    assert launcher._proposal_dispatch_allowed(
        result_source="structured",
        phase="submit",
        dry_run=True,
    )


def _tool_call(arguments: dict[str, object]) -> dict[str, object]:
    response = agent_report_mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "report_agent_turn", "arguments": arguments},
        }
    )
    assert response is not None
    return response


def test_report_tool_writes_one_idempotent_provider_neutral_result(
    monkeypatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "turn.json"
    monkeypatch.setenv(agent_report_mcp.RUN_ID_ENV, "run-1")
    monkeypatch.setenv(agent_report_mcp.REPORT_PATH_ENV, str(path))
    arguments = {
        "status": "ready_to_submit",
        "summary": "Form prepared",
        "proposals": [
            {
                "proposal_id": "review-1",
                "kind": "review",
                "summary": "Review an unfamiliar field",
                "concurrency_mode": "plugin-mode",
            }
        ],
    }

    first = _tool_call(arguments)
    second = _tool_call(arguments)
    conflicting = _tool_call({"status": "failed", "summary": "Different"})
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert first["result"]["isError"] is False
    assert second["result"]["isError"] is False
    assert conflicting["result"]["isError"] is True
    assert payload["run_id"] == "run-1"
    assert payload["proposals"][0]["concurrency_mode"] == "plugin-mode"


def test_report_stdio_accepts_utf8_when_windows_text_encoding_is_legacy(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unicode-turn.json"
    env = {
        **os.environ,
        agent_report_mcp.RUN_ID_ENV: "run-unicode",
        agent_report_mcp.REPORT_PATH_ENV: str(path),
        "PYTHONIOENCODING": "cp1252:strict",
    }
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "report_agent_turn",
                "arguments": {
                    "status": "ready_to_submit",
                    "summary": "Verified six‑month internship plan",
                    "observations": {"listing_evidence": "Apply by email — official listing"},
                },
            },
        },
    ]
    stdin_bytes = b"".join(
        (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        for message in messages
    )

    completed = subprocess.run(
        [sys.executable, "-m", "applypilot.apply.agent_report_mcp"],
        input=stdin_bytes,
        capture_output=True,
        env=env,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert responses[-1]["result"]["isError"] is False
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["summary"] == "Verified six‑month internship plan"


def test_report_tool_rejects_raw_secret_or_browser_handle(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(agent_report_mcp.RUN_ID_ENV, "run-secret")
    monkeypatch.setenv(agent_report_mcp.REPORT_PATH_ENV, str(tmp_path / "turn.json"))

    response = _tool_call(
        {
            "status": "failed:manual_review",
            "summary": "Needs review",
            "observations": {"browser_handle": "live-page"},
        }
    )

    assert response["result"]["isError"] is True
    assert not (tmp_path / "turn.json").exists()


def test_provider_neutral_report_loader_enforces_resource_bounds(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.json"
    oversized.write_text("x" * (MAX_AGENT_REPORT_BYTES + 1), encoding="utf-8")

    with pytest.raises(ValueError, match="too large"):
        agent_output.load_agent_turn_report(oversized, expected_run_id="run-large")

    proposals = [
        {"kind": "review", "summary": f"Review {index}"}
        for index in range(MAX_AGENT_PROPOSALS + 1)
    ]
    with pytest.raises(ValueError, match="at most"):
        agent_turn_result_from_mapping(
            {
                "run_id": "run-many",
                "status": "ready_to_submit",
                "summary": "Too many proposals",
                "proposals": proposals,
            }
        )


def test_structured_and_legacy_results_reconcile_without_weakening_receipt_gate() -> None:
    evidence = {
        "receipt_visible": True,
        "applied_badge_visible": False,
        "confirmation_text": "Application received",
        "confirmation_url": "https://example.test/confirmation",
    }
    result = AgentTurnResult(
        run_id="run-1",
        status="applied",
        summary="Visible receipt",
        observations={"submission_evidence": evidence},
    )
    output = "RESULT:APPLIED\nSUBMISSION_EVIDENCE: " + json.dumps(evidence)

    status, admitted_evidence, source = agent_output.reconcile_agent_turn_outputs(
        output,
        result,
        dry_run=False,
        submission_phase="submit",
    )
    conflict, conflict_evidence, conflict_source = agent_output.reconcile_agent_turn_outputs(
        "RESULT:SUBMISSION_UNCERTAIN",
        result,
        dry_run=False,
        submission_phase="submit",
    )

    assert (status, admitted_evidence, source) == (
        "applied",
        evidence,
        "structured+legacy",
    )
    assert (conflict, conflict_evidence, conflict_source) == (
        "submission_uncertain",
        None,
        "conflict",
    )


def test_structured_applied_status_can_use_valid_legacy_receipt_evidence() -> None:
    evidence = {
        "receipt_visible": True,
        "applied_badge_visible": False,
        "confirmation_text": "Application was successfully submitted",
        "confirmation_url": "https://example.test/application",
    }
    result = AgentTurnResult(
        run_id="run-receipt-split",
        status="applied",
        summary="Submitted successfully",
    )
    output = "RESULT:APPLIED\nSUBMISSION_EVIDENCE: " + json.dumps(evidence)

    assert agent_output.reconcile_agent_turn_outputs(
        output,
        result,
        dry_run=False,
        submission_phase="submit",
    ) == ("applied", evidence, "structured+legacy")


def test_structured_only_result_keeps_legacy_application_status_contract() -> None:
    result = AgentTurnResult(
        run_id="run-prepare",
        status="ready_to_submit",
        summary="Prepared by another runtime",
    )

    assert agent_output.reconcile_agent_turn_outputs(
        "",
        result,
        dry_run=False,
        submission_phase="prepare",
    ) == ("ready_to_submit", None, "structured")


def test_prepare_reconciles_previewed_report_with_ready_to_submit_marker() -> None:
    result = AgentTurnResult(
        run_id="run-prepare-alias",
        status="previewed",
        summary="Form completed without submitting",
    )

    assert agent_output.reconcile_agent_turn_outputs(
        "RESULT:READY_TO_SUBMIT",
        result,
        dry_run=False,
        submission_phase="prepare",
    ) == ("ready_to_submit", None, "structured+legacy")


def test_prepare_reconciles_previewed_report_with_previewed_marker() -> None:
    result = AgentTurnResult(
        run_id="run-prepare-preview-alias",
        status="previewed",
        summary="Form completed without submitting",
    )

    assert agent_output.reconcile_agent_turn_outputs(
        "RESULT:PREVIEWED",
        result,
        dry_run=False,
        submission_phase="prepare",
    ) == ("ready_to_submit", None, "structured+legacy")


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("COVER_NOT_REQUIRED", "cover_not_required"),
        ("COVER_LETTER_REQUIRED", "cover_letter_required"),
    ],
)
def test_prepare_prefers_specific_cover_result_over_generic_preview_report(
    marker: str,
    expected: str,
) -> None:
    result = AgentTurnResult(
        run_id="run-cover-discovery",
        status="previewed",
        summary="Inspected the non-submitting form",
    )

    assert agent_output.reconcile_agent_turn_outputs(
        f"RESULT:{marker}",
        result,
        dry_run=False,
        submission_phase="prepare",
    ) == (expected, None, "structured+legacy")


def test_run_job_records_structured_turn_events_without_changing_status_contract(
    monkeypatch,
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "app"
    worker_dir = app_dir / "workers" / "worker-0"
    log_dir = app_dir / "logs"
    worker_dir.mkdir(parents=True)
    log_dir.mkdir(parents=True)
    db_path = tmp_path / "control.db"
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
            "agent_runtime": {
                "playwright_mcp": {
                    "env": {"PRIVATE_TOKEN": "secret-value"},
                }
            },
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

        def __init__(self, *, env: dict[str, str]) -> None:
            self.returncode = 0
            self.stdin = io.StringIO()
            report = {
                "schema_version": "future-compatible",
                "run_id": env[agent_report_mcp.RUN_ID_ENV],
                "status": "ready_to_submit",
                "summary": "Form prepared through structured reporting",
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
                json.dumps(report),
                encoding="utf-8",
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
            self.stdout = io.StringIO("\n".join(json.dumps(item) for item in messages))

        def wait(self, timeout: int | None = None) -> int:
            return self.returncode

        def poll(self) -> int:
            return self.returncode

    captured_command: list[str] = []
    captured_env: dict[str, str] = {}
    captured_ats_context: dict[str, object] = {}

    def popen(command: list[str], **kwargs) -> Process:
        captured_command.extend(command)
        captured_env.update(kwargs["env"])
        captured_ats_context.update(
            json.loads(
                Path(kwargs["env"]["APPLYPILOT_ATS_CONTEXT_PATH"]).read_text(
                    encoding="utf-8"
                )
            )
        )
        return Process(env=kwargs["env"])

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    job = {
        "url": "https://example.test/jobs/1",
        "application_url": "https://example.test/apply/1",
        "title": "Data Intern",
        "company_name": "Example",
        "site": "example",
        "fit_score": 9,
        "_attempt_id": "attempt-1",
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
        "SELECT event_type, payload_json FROM agent_events "
        "WHERE attempt_id=? ORDER BY occurred_at, event_id",
        ("attempt-1",),
    ).fetchall()

    assert status == "ready_to_submit"
    assert job["_agent_turn_source"] == "structured+legacy"
    assert [row[0] for row in events] == [
        "agent.turn.started",
        "agent.proposals.executed",
        "agent.turn.completed",
    ]
    completed_payload = json.loads(events[-1][1])
    assert completed_payload["metrics"]["browser_tool_call_count"] == 1
    assert completed_payload["metrics"]["browser_tool_success_count"] == 1
    assert completed_payload["actor_decision"] == {
        "run_id": completed_payload["actor_decision"]["run_id"],
        "attempt_id": "attempt-1",
        "phase": "verify",
        "disposition": "checkpoint",
        "next_phase": "checkpoint",
        "recovery_action": None,
        "human_interruption": None,
        "shadow_only": True,
        "schema_version": "1",
    }
    assert conn.execute("SELECT COUNT(*) FROM agent_checkpoints").fetchone()[0] == 1
    assert "mcp_servers.applypilot_control.command" in " ".join(captured_command)
    assert "mcp_servers.applypilot_ats.command" in " ".join(captured_command)
    assert captured_ats_context["adapter"] == "generic"
    assert captured_ats_context["side_effect"] == "proposal-only"
    assert captured_env["PRIVATE_TOKEN"] == "secret-value"
    assert "secret-value" not in " ".join(captured_command)
    assert "secret-value" not in (app_dir / ".mcp-apply-0.json").read_text(encoding="utf-8")
    assert job["_agent_proposal_results"]
    assert {
        outcome["value"] for outcome in job["_agent_proposal_results"].values()
    } == {"handled:specialist-review"}
    assert job["_agent_specialist_context"][0]["summary"] == (
        "handled:specialist-review"
    )
    checkpoint_json = conn.execute(
        "SELECT state_json FROM agent_checkpoints LIMIT 1"
    ).fetchone()[0]
    checkpoint = json.loads(checkpoint_json)
    assert checkpoint["actor_decision"] == completed_payload["actor_decision"]
    assert "Form prepared through structured reporting" not in checkpoint_json
    assert "Review one unfamiliar field" not in checkpoint_json
    assert not (worker_dir / "agent-turn-report.json").exists()
    assert not (worker_dir / "ats-application-context.json").exists()
    database.close_connection(db_path)
