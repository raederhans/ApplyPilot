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
        "observations": _ready_provenance_observations(),
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


def _ready_provenance_observations() -> dict[str, object]:
    return {
        "answer_provenance": {"snapshot_digest": "a" * 64},
        "answer_mappings": {
            "schema_version": "2",
            "adapter": "smartrecruiters",
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
        },
    }


@pytest.mark.parametrize(
    "observations",
    [
        {},
        {
            "answer_provenance": {"snapshot_digest": "a" * 64},
            "answer_mapping": _ready_provenance_observations()["answer_mappings"],
        },
        {
            "answer_provenance": {"snapshot_digest": "a" * 64},
            "answer_mappings": [],
        },
        {
            "answer_provenance": {"snapshot_digest": "a" * 64},
            "answer_mappings": {
                **_ready_provenance_observations()["answer_mappings"],
                "legacy_key": True,
            },
        },
    ],
    ids=["omitted-browser-ready", "wrong-key", "legacy-list", "extra-envelope-key"],
)
def test_ready_report_with_provenance_rejects_invalid_v2_answer_mappings_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    observations: dict[str, object],
) -> None:
    path = tmp_path / "invalid-ready-turn.json"
    monkeypatch.setenv(agent_report_mcp.RUN_ID_ENV, "run-invalid-ready")
    monkeypatch.setenv(agent_report_mcp.REPORT_PATH_ENV, str(path))

    response = _tool_call(
        {
            "status": "ready_to_submit",
            "summary": "Form prepared",
            "observations": observations,
        }
    )
    report = agent_output.load_agent_turn_report(
        path,
        expected_run_id="run-invalid-ready",
    )

    assert response["result"]["isError"] is True
    assert report.status == "failed:answer_provenance_report_invalid"
    assert agent_output.reconcile_agent_turn_outputs(
        "RESULT:READY_TO_SUBMIT",
        report,
        dry_run=False,
        submission_phase="prepare",
    ) == ("failed:answer_provenance_report_invalid", None, "structured")


def test_ready_report_accepts_strict_v2_answer_mappings_when_provenance_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "valid-ready-turn.json"
    monkeypatch.setenv(agent_report_mcp.RUN_ID_ENV, "run-valid-ready")
    monkeypatch.setenv(agent_report_mcp.REPORT_PATH_ENV, str(path))

    response = _tool_call(
        {
            "status": "ready_to_submit",
            "summary": "Form prepared",
            "observations": _ready_provenance_observations(),
        }
    )
    report = agent_output.load_agent_turn_report(path, expected_run_id="run-valid-ready")

    assert response["result"]["isError"] is False
    assert report.status == "ready_to_submit"


def test_invalid_ready_report_allows_one_strict_v2_correction_for_same_turn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "corrected-ready-turn.json"
    monkeypatch.setenv(agent_report_mcp.RUN_ID_ENV, "run-corrected-ready")
    monkeypatch.setenv(agent_report_mcp.REPORT_PATH_ENV, str(path))

    invalid = _tool_call(
        {
            "status": "ready_to_submit",
            "summary": "Form prepared without mappings",
            "observations": {},
        }
    )
    denial = agent_output.load_agent_turn_report(
        path,
        expected_run_id="run-corrected-ready",
    )
    corrected = _tool_call(
        {
            "status": "ready_to_submit",
            "summary": "Form prepared with verified mappings",
            "observations": _ready_provenance_observations(),
        }
    )
    repeated = _tool_call(
        {
            "status": "ready_to_submit",
            "summary": "Form prepared with verified mappings",
            "observations": _ready_provenance_observations(),
        }
    )
    conflicting = _tool_call({"status": "failed", "summary": "Different"})
    report = agent_output.load_agent_turn_report(
        path,
        expected_run_id="run-corrected-ready",
    )

    assert invalid["result"]["isError"] is True
    assert denial.status == "failed:answer_provenance_report_invalid"
    assert corrected["result"]["isError"] is False
    assert corrected["result"]["structuredContent"]["recorded"] is True
    assert repeated["result"]["isError"] is False
    assert repeated["result"]["structuredContent"]["recorded"] is False
    assert conflicting["result"]["isError"] is True
    assert report.status == "ready_to_submit"


def test_invalid_ready_denial_cannot_be_replaced_by_direct_email_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "browser-denial-turn.json"
    monkeypatch.setenv(agent_report_mcp.RUN_ID_ENV, "run-browser-denial")
    monkeypatch.setenv(agent_report_mcp.REPORT_PATH_ENV, str(path))

    _tool_call(
        {
            "status": "ready_to_submit",
            "summary": "Browser form prepared without mappings",
            "observations": {},
        }
    )
    response = _tool_call(
        {
            "status": "ready_to_submit",
            "summary": "Different route",
            "observations": {
                "email_application": _direct_email_prepare_plan(),
                "answer_mappings": {},
            },
        }
    )
    report = agent_output.load_agent_turn_report(
        path,
        expected_run_id="run-browser-denial",
    )

    assert response["result"]["isError"] is True
    assert report.status == "failed:answer_provenance_report_invalid"


def test_second_invalid_ready_report_exhausts_the_only_correction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "exhausted-ready-turn.json"
    monkeypatch.setenv(agent_report_mcp.RUN_ID_ENV, "run-exhausted-ready")
    monkeypatch.setenv(agent_report_mcp.REPORT_PATH_ENV, str(path))

    first = _tool_call(
        {"status": "ready_to_submit", "summary": "Missing", "observations": {}}
    )
    second = _tool_call(
        {"status": "ready_to_submit", "summary": "Still invalid", "observations": {}}
    )
    third = _tool_call(
        {
            "status": "ready_to_submit",
            "summary": "Too late",
            "observations": _ready_provenance_observations(),
        }
    )
    report = agent_output.load_agent_turn_report(
        path,
        expected_run_id="run-exhausted-ready",
    )

    assert first["result"]["isError"] is True
    assert second["result"]["isError"] is True
    assert third["result"]["isError"] is True
    assert report.status == "failed:answer_provenance_report_invalid"
    assert report.observations == {
        "report_contract_error": "answer_mappings_v2_correction_exhausted"
    }


def test_submit_phase_never_recovers_contract_denial_as_ready() -> None:
    report = AgentTurnResult(
        run_id="run-submit-denial",
        status="failed:answer_provenance_report_invalid",
        summary="Strict report denied",
        observations={"report_contract_error": "answer_mappings_v2_required"},
    )

    assert agent_output.reconcile_agent_turn_outputs(
        "RESULT:READY_TO_SUBMIT",
        report,
        dry_run=False,
        submission_phase="submit",
    ) == ("submission_uncertain", None, "structured+legacy")


@pytest.mark.parametrize(
    "legacy_output",
    [
        "RESULT:CAPTCHA",
        "RESULT:APPLIED",
        "RESULT:SUBMISSION_UNCERTAIN",
        "RESULT:FAILED:stuck",
    ],
)
def test_prepare_contract_denial_only_dominates_stale_legacy_ready(
    legacy_output: str,
) -> None:
    report = AgentTurnResult(
        run_id="run-prepare-denial-conflict",
        status="failed:answer_provenance_report_invalid",
        summary="Strict report denied",
        observations={"report_contract_error": "answer_mappings_v2_required"},
    )

    assert agent_output.reconcile_agent_turn_outputs_with_diagnostics(
        legacy_output,
        report,
        dry_run=False,
        submission_phase="prepare",
    ) == (
        "failed:conflicting_agent_results",
        None,
        "conflict",
        "status_mismatch",
    )


def test_loader_fail_closes_existing_invalid_provenance_ready_report(
    tmp_path: Path,
) -> None:
    path = tmp_path / "externally-written-invalid-ready.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "run_id": "run-invalid-loader",
                "status": "ready_to_submit",
                "summary": "Form prepared",
                "observations": {"answer_provenance": {}},
            }
        ),
        encoding="utf-8",
    )

    report = agent_output.load_agent_turn_report(
        path,
        expected_run_id="run-invalid-loader",
    )

    assert report.status == "failed:answer_provenance_report_invalid"
    assert agent_output.reconcile_agent_turn_outputs(
        "RESULT:READY_TO_SUBMIT",
        report,
        dry_run=False,
        submission_phase="prepare",
    ) == ("failed:answer_provenance_report_invalid", None, "structured")


def test_previewed_and_failure_reports_do_not_require_final_answer_mappings() -> None:
    preview_audit = {
        "submission_attempted": False,
        "resume_uploaded": True,
        "filled_fields": [],
        "manual_review_fields": [],
        "final_control_label": "Submit",
    }
    cases = (
        ("previewed", {"answer_provenance": {}, "preview_audit": preview_audit}),
        ("failed:stuck", {"answer_provenance": {}}),
    )

    for status, observations in cases:
        result = AgentTurnResult(
            run_id=f"run-{status}",
            status=status,
            summary="Bounded result",
            observations=observations,
        )
        interpreted, _ = agent_output.interpret_agent_turn_result(
            result,
            dry_run=True,
            submission_phase="prepare",
        )
        assert interpreted == status


def test_non_dry_prepare_previewed_alias_requires_final_answer_mappings() -> None:
    result = AgentTurnResult(
        run_id="run-preview-alias-without-mappings",
        status="previewed",
        summary="Browser form completed",
    )

    assert agent_output.reconcile_agent_turn_outputs(
        "RESULT:READY_TO_SUBMIT",
        result,
        dry_run=False,
        submission_phase="prepare",
    ) == ("failed:conflicting_agent_results", None, "conflict")


def _direct_email_prepare_plan() -> dict[str, object]:
    return {
        "route": "direct_email",
        "recipient": "jobs@example.test",
        "recipient_domain": "example.test",
        "recipient_source": "official_listing",
        "listing_evidence": "Official listing: apply to jobs@example.test",
        "subject": "Application for Data Intern",
        "body_sha256": "e" * 64,
        "attachment_names": ["Candidate_Resume.pdf"],
        "attachments_verified": True,
        "duplicate_check": {
            "folder": "sent",
            "completed": True,
            "duplicate_found": False,
            "provider_query_id": "query-digest-1",
        },
    }


def test_complete_direct_email_prepare_plan_does_not_require_browser_mappings() -> None:
    result = AgentTurnResult(
        run_id="run-direct-email-ready",
        status="ready_to_submit",
        summary="Email plan prepared without sending",
        observations={"email_application": _direct_email_prepare_plan()},
    )

    assert agent_output.reconcile_agent_turn_outputs(
        "RESULT:READY_TO_SUBMIT",
        result,
        dry_run=False,
        submission_phase="prepare",
    ) == ("ready_to_submit", None, "structured+legacy")


def test_partial_direct_email_key_cannot_bypass_browser_mapping_contract() -> None:
    result = AgentTurnResult(
        run_id="run-forged-direct-email-ready",
        status="ready_to_submit",
        summary="Incomplete email plan",
        observations={"email_application": {"route": "direct_email"}},
    )

    assert agent_output.reconcile_agent_turn_outputs(
        "RESULT:READY_TO_SUBMIT",
        result,
        dry_run=False,
        submission_phase="prepare",
    ) == ("failed:conflicting_agent_results", None, "conflict")


def test_legacy_only_browser_ready_fails_closed_without_structured_mappings() -> None:
    assert agent_output.reconcile_agent_turn_outputs_with_diagnostics(
        "RESULT:READY_TO_SUBMIT",
        None,
        dry_run=False,
        submission_phase="prepare",
    ) == (
        "failed:answer_provenance_report_missing",
        None,
        "legacy",
        "structured_ready_report_missing",
    )


def test_report_tool_documents_and_accepts_legacy_open_failure_status(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tool = agent_report_mcp._report_tool()
    properties = tool["inputSchema"]["properties"]
    assert "legacy/open status label such as failed:stuck" in properties["status"]["description"]
    assert "omit this top-level typed failure" in properties["failure"]["description"]
    failure_description = properties["failure"]["description"]
    assert "submit_started=true requires status submission_uncertain" in failure_description
    assert "Otherwise use failed or failed:<failure.code>" in failure_description
    assert "captcha_required may use captcha" in failure_description
    assert "expired may use expired" in failure_description

    path = tmp_path / "legacy-failure-turn.json"
    monkeypatch.setenv(agent_report_mcp.RUN_ID_ENV, "run-legacy-failure")
    monkeypatch.setenv(agent_report_mcp.REPORT_PATH_ENV, str(path))
    arguments = {
        "status": "failed:stuck",
        "summary": "Autocomplete did not converge after one correction",
        "observations": {
            "failure_context": {
                "category": "autocomplete_stuck",
                "visible_state": "Invalid institution remained visible",
                "attempts": 1,
            }
        },
    }

    response = _tool_call(arguments)
    payload = json.loads(path.read_text(encoding="utf-8"))
    report = agent_output.load_agent_turn_report(
        path,
        expected_run_id="run-legacy-failure",
    )

    assert response["result"]["isError"] is False
    assert payload["status"] == "failed:stuck"
    assert "failure" not in payload
    assert payload["observations"]["failure_context"]["attempts"] == 1
    assert agent_output.reconcile_agent_turn_outputs(
        "RESULT:FAILED:stuck",
        report,
        dry_run=False,
        submission_phase="prepare",
    ) == ("failed:stuck", None, "structured+legacy")


@pytest.mark.parametrize(
    ("status", "code", "submit_started", "phase", "legacy_marker"),
    [
        (
            "submission_uncertain",
            "provider_submission_error",
            True,
            "submit",
            "RESULT:SUBMISSION_UNCERTAIN",
        ),
        ("captcha", "captcha_required", False, "prepare", "RESULT:CAPTCHA"),
        ("expired", "expired", False, "prepare", "RESULT:EXPIRED"),
    ],
)
def test_report_tool_persists_and_reconciles_typed_failure_status_matrix(
    monkeypatch,
    tmp_path: Path,
    status: str,
    code: str,
    submit_started: bool,
    phase: str,
    legacy_marker: str,
) -> None:
    run_id = f"run-{code}"
    path = tmp_path / f"{code}.json"
    monkeypatch.setenv(agent_report_mcp.RUN_ID_ENV, run_id)
    monkeypatch.setenv(agent_report_mcp.REPORT_PATH_ENV, str(path))
    arguments = {
        "status": status,
        "summary": f"Observed {code}",
        "failure": {
            "schema_version": "1",
            "code": code,
            "source": "agent",
            "provider": "generic",
            "phase": phase,
            "submit_started": submit_started,
        },
    }

    response = _tool_call(arguments)
    report = agent_output.load_agent_turn_report(path, expected_run_id=run_id)

    assert response["result"]["isError"] is False
    assert report.failure is not None
    assert report.failure.code == code
    assert agent_output.reconcile_agent_turn_outputs(
        legacy_marker,
        report,
        dry_run=False,
        submission_phase=phase,
    ) == (status, None, "structured+legacy")


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
                    "observations": _ready_provenance_observations(),
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
        observations=_ready_provenance_observations(),
    )

    assert agent_output.reconcile_agent_turn_outputs(
        "",
        result,
        dry_run=False,
        submission_phase="prepare",
    ) == ("ready_to_submit", None, "structured")


def test_prepared_for_audit_is_non_authorizing_and_prepare_only() -> None:
    result = AgentTurnResult(
        run_id="run-prepared-audit",
        status="prepared_for_audit",
        summary="Form filled for host audit",
    )

    assert agent_output.reconcile_agent_turn_outputs(
        "RESULT:PREPARED_FOR_AUDIT",
        result,
        dry_run=False,
        submission_phase="prepare",
    ) == ("prepared_for_audit", None, "structured+legacy")
    assert agent_output.reconcile_agent_turn_outputs(
        "RESULT:PREPARED_FOR_AUDIT",
        result,
        dry_run=True,
        submission_phase="prepare",
    )[0] != "prepared_for_audit"
    assert agent_output.reconcile_agent_turn_outputs(
        "RESULT:PREPARED_FOR_AUDIT",
        result,
        dry_run=False,
        submission_phase="submit",
    )[0] == "submission_uncertain"


def test_prepared_for_audit_rejects_answer_mappings_and_legacy_only_authority() -> None:
    mapped = AgentTurnResult(
        run_id="run-prepared-mapped",
        status="prepared_for_audit",
        summary="Invalid staged result",
        observations=_ready_provenance_observations(),
    )

    assert agent_output.reconcile_agent_turn_outputs(
        "RESULT:PREPARED_FOR_AUDIT",
        mapped,
        dry_run=False,
        submission_phase="prepare",
    )[0] == "failed:prepared_for_audit_contract_invalid"
    assert agent_output.reconcile_agent_turn_outputs(
        "RESULT:PREPARED_FOR_AUDIT",
        None,
        dry_run=False,
        submission_phase="prepare",
    )[0] == "failed:prepared_for_audit_report_missing"


def test_report_tool_rejects_prepared_for_audit_with_mappings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "premature-mappings.json"
    monkeypatch.setenv(agent_report_mcp.RUN_ID_ENV, "run-premature-mappings")
    monkeypatch.setenv(agent_report_mcp.REPORT_PATH_ENV, str(path))

    response = _tool_call(
        {
            "status": "prepared_for_audit",
            "summary": "Premature mappings",
            "observations": _ready_provenance_observations(),
        }
    )

    assert response["result"]["isError"] is True
    assert not path.exists()


def test_prepare_reconciles_previewed_report_with_ready_to_submit_marker() -> None:
    result = AgentTurnResult(
        run_id="run-prepare-alias",
        status="previewed",
        summary="Form completed without submitting",
        observations=_ready_provenance_observations(),
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
        observations=_ready_provenance_observations(),
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


def test_codex_final_message_is_the_only_legacy_output_for_preview_contract(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Progress prose must not be cross-checked as the final RESULT contract."""
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
    monkeypatch.setattr(config, "load_profile", lambda: {"authentication": {}})
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
    preview_audit = {
        "submission_attempted": False,
        "channel": "ats",
        "resume_uploaded": True,
        "filled_fields": [],
        "manual_review_fields": [],
        "final_control_label": "Submit application",
    }
    final_text = "RESULT:PREVIEWED\nPREVIEW_AUDIT: " + json.dumps(preview_audit)

    class Process:
        pid = 4321

        def __init__(self, *, env: dict[str, str], final_message_path: Path) -> None:
            self.returncode = 0
            self.stdin = io.StringIO()
            Path(env[agent_report_mcp.REPORT_PATH_ENV]).write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "run_id": env[agent_report_mcp.RUN_ID_ENV],
                        "status": "previewed",
                        "summary": "Preview completed through structured reporting",
                        "observations": {"preview_audit": preview_audit},
                    }
                ),
                encoding="utf-8",
            )
            final_message_path.write_text(final_text, encoding="utf-8")
            messages = [
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "Progress: preparing RESULT:PREVIEWED evidence.",
                    },
                },
                {"type": "turn.completed", "usage": {}},
            ]
            self.stdout = io.StringIO("\n".join(json.dumps(item) for item in messages))

        def wait(self, timeout: int | None = None) -> int:
            return self.returncode

        def poll(self) -> int:
            return self.returncode

    def popen(command: list[str], **kwargs) -> Process:
        final_path = Path(command[command.index("--output-last-message") + 1])
        return Process(env=kwargs["env"], final_message_path=final_path)

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    monkeypatch.setattr(
        launcher.agent_runtime_mod,
        "process_rss_bytes",
        lambda _pid: 12_345,
    )
    monkeypatch.setattr(launcher, "_process_identity_tuple", lambda pid: (pid, 123_456))
    job = {
        "url": "https://example.test/jobs/preview",
        "application_url": "https://example.test/apply/preview",
        "title": "Data Intern",
        "company_name": "Example",
        "site": "example",
        "fit_score": 9,
        "_attempt_id": "attempt-final-message-preview",
        "_browser_backend": "edge",
    }

    status, _ = launcher.run_job(
        job,
        port=9432,
        worker_id=0,
        model="model",
        dry_run=True,
        agent_backend="codex",
        submission_phase="submit",
    )

    assert status == "previewed"
    assert job["_agent_turn_source"] == "structured+legacy"
    database.close_connection(db_path)


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
                "observations": _ready_provenance_observations(),
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
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 11, "output_tokens": 7},
                },
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
    monkeypatch.setattr(
        launcher.agent_runtime_mod,
        "process_rss_bytes",
        lambda _pid: 12_345,
    )
    monkeypatch.setattr(
        launcher,
        "_process_identity_tuple",
        lambda pid: (pid, 123_456),
    )
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
    assert completed_payload["metrics"]["mcp_ready_observed"] == 1
    assert completed_payload["metrics"]["mcp_ready_ms"] >= 0
    assert completed_payload["metrics"]["process_spawn_ms"] >= 0
    assert completed_payload["metrics"]["process_rss_peak_bytes"] == 12_345
    assert completed_payload["metrics"]["input_tokens"] == 11
    assert completed_payload["metrics"]["output_tokens"] == 7
    assert completed_payload["metrics"]["total_tokens"] == 18
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
    output_root = Path(job["_runtime_output_root"])
    assert output_root.is_relative_to(config.APPLY_WORKER_DIR)
    assert job["_runtime_namespace"]["run_id"] == job["_run_namespace_id"]
    assert "secret-value" not in (output_root / "mcp-config.json").read_text(
        encoding="utf-8"
    )
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


def _run_codex_event_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    messages: list[dict[str, object]],
    structured_status: str = "ready_to_submit",
    final_text: str = "RESULT:READY_TO_SUBMIT",
    dry_run: bool = False,
    attempt_id: str = "attempt-codex-event-fixture",
) -> tuple[str, dict[str, object]]:
    """Run one deterministic Codex stream and return its durable completion event."""
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
    monkeypatch.setattr(config, "load_profile", lambda: {"authentication": {}})
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

        def __init__(self, *, env: dict[str, str], final_message_path: Path) -> None:
            self.returncode = 0
            self.stdin = io.StringIO()
            Path(env[agent_report_mcp.REPORT_PATH_ENV]).write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "run_id": env[agent_report_mcp.RUN_ID_ENV],
                        "status": structured_status,
                        "summary": "bounded fixture report",
                        "observations": (
                            _ready_provenance_observations()
                            if structured_status == "ready_to_submit"
                            else {}
                        ),
                    }
                ),
                encoding="utf-8",
            )
            final_message_path.write_text(final_text, encoding="utf-8")
            self.stdout = io.StringIO(
                "\n".join(json.dumps(message) for message in messages)
            )

        def wait(self, timeout: int | None = None) -> int:
            return self.returncode

        def poll(self) -> int:
            return self.returncode

    def popen(command: list[str], **kwargs) -> Process:
        final_path = Path(command[command.index("--output-last-message") + 1])
        return Process(env=kwargs["env"], final_message_path=final_path)

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    monkeypatch.setattr(launcher, "_process_identity_tuple", lambda pid: (pid, 123_456))
    job = {
        "url": "https://example.test/jobs/codex-events",
        "application_url": "https://example.test/apply/codex-events",
        "title": "Data Intern",
        "company_name": "Example",
        "site": "example",
        "fit_score": 9,
        "_attempt_id": attempt_id,
        "_browser_backend": "edge",
    }

    status, _ = launcher.run_job(
        job,
        port=9432,
        worker_id=0,
        model="model",
        dry_run=dry_run,
        agent_backend="codex",
        submission_phase="prepare",
    )
    conn = database.get_connection(db_path)
    payload_json = conn.execute(
        "SELECT payload_json FROM agent_events "
        "WHERE attempt_id=? AND event_type='agent.turn.completed'",
        (attempt_id,),
    ).fetchone()[0]
    database.close_connection(db_path)
    return status, json.loads(payload_json)


@pytest.mark.parametrize(
    "result_field",
    [
        {"result": {"isError": True}},
        {"output": {"is_error": True}},
    ],
    ids=["result-isError", "output-is_error"],
)
def test_codex_completed_browser_error_payload_is_not_counted_as_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    result_field: dict[str, object],
) -> None:
    """A completed transport event can still carry an MCP-level failure."""
    status, payload = _run_codex_event_fixture(
        monkeypatch,
        tmp_path,
        messages=[
            {
                "type": "item.completed",
                "item": {
                    "id": "browser-error-1",
                    "type": "mcp_tool_call",
                    "server": "playwright",
                    "tool": "browser_file_upload",
                    "status": "completed",
                    **result_field,
                },
            },
            {"type": "turn.completed", "usage": {}},
        ],
    )

    assert status == "ready_to_submit"
    assert payload["metrics"]["browser_tool_call_count"] == 1
    assert payload["metrics"]["browser_tool_success_count"] == 0


def test_codex_user_camel_case_tool_error_is_not_counted_as_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Codex's camel-case error flag has the same failure semantics as snake case."""
    status, payload = _run_codex_event_fixture(
        monkeypatch,
        tmp_path,
        messages=[
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "browser-user-error-1",
                            "name": "mcp__playwright__browser_file_upload",
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "browser-user-error-1",
                            "isError": True,
                        }
                    ]
                },
            },
            {"type": "turn.completed", "usage": {}},
        ],
    )

    assert status == "ready_to_submit"
    assert payload["metrics"]["browser_tool_call_count"] == 1
    assert payload["metrics"]["browser_tool_success_count"] == 0


def test_codex_duplicate_tool_id_across_event_shapes_is_counted_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Equivalent lifecycle events describe one browser action, not three."""
    status, payload = _run_codex_event_fixture(
        monkeypatch,
        tmp_path,
        messages=[
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "browser-duplicate-1",
                            "name": "mcp__playwright__browser_snapshot",
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "browser-duplicate-1",
                            "isError": False,
                        }
                    ]
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "browser-duplicate-1",
                    "type": "mcp_tool_call",
                    "server": "playwright",
                    "tool": "browser_snapshot",
                    "status": "completed",
                },
            },
            {"type": "turn.completed", "usage": {}},
        ],
    )

    assert status == "ready_to_submit"
    assert payload["metrics"]["browser_tool_call_count"] == 1
    assert payload["metrics"]["browser_tool_success_count"] == 1


def test_durable_completed_event_classifies_status_conflict_without_raw_contracts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The control plane keeps a bounded mismatch reason after transient files vanish."""
    status, payload = _run_codex_event_fixture(
        monkeypatch,
        tmp_path,
        messages=[{"type": "turn.completed", "usage": {}}],
        structured_status="failed:resume_upload",
        final_text="RESULT:READY_TO_SUBMIT",
        dry_run=True,
        attempt_id="attempt-status-conflict",
    )

    serialized = json.dumps(payload)
    assert status == "failed:conflicting_agent_results"
    assert payload["source"] == "conflict"
    assert payload["conflict_classification"] == "status_mismatch"
    assert "failed:resume_upload" not in serialized
    assert "RESULT:READY_TO_SUBMIT" not in serialized


def test_codex_completed_tool_failures_are_counted_per_report_and_upload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Codex terminal failures can have null ``error`` and text-only results."""
    status, payload = _run_codex_event_fixture(
        monkeypatch,
        tmp_path,
        messages=[
            {
                "type": "item.completed",
                "item": {
                    "id": "report-success-1",
                    "type": "mcp_tool_call",
                    "server": "applypilot_control",
                    "tool": "report_agent_turn",
                    "status": "completed",
                    "error": None,
                    "result": {"content": [{"type": "text", "text": "recorded"}]},
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "report-failure-2",
                    "type": "mcp_tool_call",
                    "server": "applypilot_control",
                    "tool": "report_agent_turn",
                    "status": "failed",
                    "error": None,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": "a different report is already recorded",
                            }
                        ]
                    },
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "upload-success-1",
                    "type": "mcp_tool_call",
                    "server": "playwright",
                    "tool": "browser_file_upload",
                    "status": "completed",
                    "error": None,
                    "result": {"content": [{"type": "text", "text": "uploaded"}]},
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "upload-failure-2",
                    "type": "mcp_tool_call",
                    "server": "playwright",
                    "tool": "browser_file_upload",
                    "status": "failed",
                    "error": None,
                    "result": {
                        "content": [
                            {"type": "text", "text": "file chooser unavailable"}
                        ]
                    },
                },
            },
            {"type": "turn.completed", "usage": {}},
        ],
    )

    assert status == "ready_to_submit"
    assert payload["metrics"].get("report_tool_call_count") == 2
    assert payload["metrics"].get("report_tool_success_count") == 1
    assert payload["metrics"].get("report_tool_failure_count") == 1
    assert payload["metrics"].get("browser_file_upload_call_count") == 2
    assert payload["metrics"].get("browser_file_upload_success_count") == 1
    assert payload["metrics"].get("browser_file_upload_failure_count") == 1
