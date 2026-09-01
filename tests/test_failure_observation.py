from __future__ import annotations

import json

import pytest

from applypilot.apply import agent_report_mcp
from applypilot.apply.agent_output import (
    interpret_agent_turn_result,
    load_agent_turn_report,
    reconcile_agent_turn_outputs,
    reconcile_agent_turn_outputs_with_diagnostics,
)
from applypilot.apply.application_actor import decision_for_turn
from applypilot.apply.contracts import (
    AgentRunRequest,
    AgentTurnResult,
    FailureObservation,
    agent_turn_result_from_mapping,
    application_actor_id,
    contract_json,
)


def _failure(**overrides: object) -> FailureObservation:
    values: dict[str, object] = {
        "code": "resume_upload_failed",
        "source": "provider_adapter",
        "provider": "workday",
        "phase": "prepare",
        "submit_started": False,
        "field_semantic": "resume",
        "page_epoch": 4,
        "evidence_refs": ("observation:resume-card-4",),
        "detail_ref": "diagnostic:failure-4",
        "missing_capability": "browser_file_upload_or_site_adapter",
    }
    values.update(overrides)
    return FailureObservation(**values)  # type: ignore[arg-type]


def _request(*, phase: str = "prepare") -> AgentRunRequest:
    return AgentRunRequest(
        run_id="run-failure-1",
        attempt_id="attempt-failure-1",
        actor_id=application_actor_id("attempt-failure-1"),
        turn_id="run-failure-1",
        agent_role="browser-application-agent",
        phase=phase,
        objective="Exercise the typed failure path",
        context={
            "actor_same_application_retries_remaining": 2,
            "actor_new_session_retries_remaining": 2,
        },
    )


def test_failure_observation_strict_mapping_round_trip() -> None:
    original = AgentTurnResult(
        run_id="run-failure-1",
        status="failed:resume_upload_failed",
        summary="Resume attachment was not accepted",
        failure=_failure(),
    )

    restored = agent_turn_result_from_mapping(contract_json(original))

    assert restored.failure == original.failure
    assert restored.status == original.status
    assert "recoverability" not in contract_json(restored.failure)
    assert "next_action" not in contract_json(restored.failure)


@pytest.mark.parametrize(
    ("extra_key", "value"),
    [
        ("message", "raw provider text"),
        ("dom", "<html>raw</html>"),
        ("recoverability", "retry_same_application"),
        ("next_action", "retry"),
    ],
)
def test_failure_mapping_rejects_raw_or_policy_fields(
    extra_key: str, value: object
) -> None:
    payload = contract_json(_failure())
    payload[extra_key] = value  # type: ignore[assignment]

    with pytest.raises(ValueError, match="unsupported FailureObservation fields"):
        agent_turn_result_from_mapping(
            {
                "run_id": "run-failure-1",
                "status": "failed",
                "summary": "Failure",
                "failure": payload,
            }
        )


def test_failure_mapping_rejects_unknown_code_and_status_conflict() -> None:
    payload = contract_json(_failure())
    payload["code"] = "provider_message_said_no"
    with pytest.raises(ValueError, match="unsupported FailureObservation code"):
        agent_turn_result_from_mapping(
            {
                "run_id": "run-failure-1",
                "status": "failed",
                "summary": "Failure",
                "failure": payload,
            }
        )

    with pytest.raises(ValueError, match="status conflicts"):
        AgentTurnResult(
            run_id="run-failure-1",
            status="ready_to_submit",
            summary="Contradictory result",
            failure=_failure(),
        )


@pytest.mark.parametrize(
    ("field", "invalid_value", "error"),
    [
        ("schema_version", 0, "schema_version must be 1"),
        ("schema_version", False, "schema_version must be 1"),
        ("schema_version", None, "schema_version must be 1"),
        ("evidence_refs", False, "evidence_refs must be an array"),
        ("evidence_refs", None, "evidence_refs must be an array"),
        ("evidence_refs", {}, "evidence_refs must be an array"),
    ],
)
def test_explicit_falsey_failure_mapping_values_never_become_defaults(
    field: str, invalid_value: object, error: str
) -> None:
    payload = contract_json(_failure())
    payload[field] = invalid_value  # type: ignore[assignment]

    with pytest.raises((TypeError, ValueError), match=error):
        agent_turn_result_from_mapping(
            {
                "run_id": "run-failure-1",
                "status": "failed",
                "summary": "Invalid falsey mapping",
                "failure": payload,
            }
        )


def test_agent_report_mcp_emits_failure_directly_into_production_consumers(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    report_path = tmp_path / "agent-report.json"
    monkeypatch.setenv(agent_report_mcp.RUN_ID_ENV, "run-failure-1")
    monkeypatch.setenv(agent_report_mcp.REPORT_PATH_ENV, str(report_path))
    failure = contract_json(_failure(code="captcha_required", source="browser_observer"))

    recorded = agent_report_mcp._write_report(
        {
            "status": "failed",
            "summary": "Human verification is visible",
            "failure": failure,
        }
    )
    loaded = load_agent_turn_report(report_path, expected_run_id="run-failure-1")
    status, evidence = interpret_agent_turn_result(
        loaded,
        dry_run=False,
        submission_phase="prepare",
    )
    decision = decision_for_turn(_request(), loaded, application_status=status)

    assert recorded["recorded"] is True
    assert (status, evidence) == ("captcha", None)
    assert decision.recovery_action is not None
    assert decision.recovery_action.action == "human_only"
    assert decision.human_interruption is not None
    assert decision.human_interruption.interruption_type == "captcha"


def test_submit_started_typed_failure_is_receipt_only_even_with_retry_budget() -> None:
    failure = _failure(
        code="provider_submission_error",
        source="submission_observer",
        phase="submit",
        submit_started=True,
        field_semantic=None,
        missing_capability="provider_submission_diagnostics_or_adapter",
    )
    result = AgentTurnResult(
        run_id="run-failure-1",
        status="submission_uncertain",
        summary="Submit started; provider acknowledgement is unavailable",
        failure=failure,
    )

    assert interpret_agent_turn_result(
        result, dry_run=False, submission_phase="submit"
    ) == ("submission_uncertain", None)
    decision = decision_for_turn(
        _request(phase="submit"), result, application_status="submission_uncertain"
    )

    assert decision.recovery_action is not None
    assert decision.recovery_action.action == "reconcile_receipt"
    assert decision.recovery_action.retry_budget_remaining == 0


@pytest.mark.parametrize(
    ("code", "expected_interruption"),
    [
        ("captcha_required", "captcha"),
        ("security_challenge_required", "security_challenge"),
        ("assessment_required", "assessment"),
        ("sensitive_identity_material_required", "sensitive_identity_or_financial_material"),
        ("unsupported_legal_declaration", "unsupported_legal_declaration"),
    ],
)
def test_typed_hard_stops_remain_human_only(
    code: str, expected_interruption: str
) -> None:
    result = AgentTurnResult(
        run_id="run-failure-1",
        status="failed",
        summary="Hard stop",
        failure=_failure(code=code, source="policy"),
    )

    status, _ = interpret_agent_turn_result(
        result, dry_run=False, submission_phase="prepare"
    )
    decision = decision_for_turn(_request(), result, application_status=status)

    assert decision.recovery_action is not None
    assert decision.recovery_action.action == "human_only"
    assert decision.human_interruption is not None
    assert decision.human_interruption.interruption_type == expected_interruption


def test_unknown_allowlisted_failure_parks_and_typed_legacy_conflict_fails_closed() -> None:
    unknown = AgentTurnResult(
        run_id="run-failure-1",
        status="failed:unknown",
        summary="No bounded classification matched",
        failure=_failure(code="unknown", source="agent"),
    )
    decision = decision_for_turn(_request(), unknown, application_status="failed:unknown")
    assert decision.recovery_action is not None
    assert decision.recovery_action.action == "park"

    captcha = AgentTurnResult(
        run_id="run-failure-1",
        status="failed",
        summary="CAPTCHA visible",
        failure=_failure(code="captcha_required", source="browser_observer"),
    )
    conflict = reconcile_agent_turn_outputs(
        "RESULT:FAILED:page_error",
        captcha,
        dry_run=False,
        submission_phase="prepare",
    )
    assert conflict == ("failed:conflicting_agent_results", None, "conflict")
    conflict_decision = decision_for_turn(
        _request(), captcha, application_status=conflict[0]
    )
    assert conflict_decision.recovery_action is not None
    assert conflict_decision.recovery_action.action == "park"


def test_failure_phase_mismatch_fails_closed() -> None:
    result = AgentTurnResult(
        run_id="run-failure-1",
        status="failed",
        summary="Stale phase",
        failure=_failure(phase="verify"),
    )

    assert interpret_agent_turn_result(
        result, dry_run=False, submission_phase="prepare"
    ) == ("failed:failure_phase_mismatch", None)


@pytest.mark.parametrize(
    "legacy_output",
    [
        "RESULT:FAILED:page_error\nRESULT:FAILED:explicit_do_not_apply",
        "RESULT:DO_NOT_APPLY",
    ],
)
def test_present_but_non_unique_or_malformed_result_marker_conflicts(
    legacy_output: str,
) -> None:
    typed = AgentTurnResult(
        run_id="run-failure-1",
        status="failed",
        summary="Typed retryable observation",
        failure=_failure(),
    )

    reconciled = reconcile_agent_turn_outputs(
        legacy_output,
        typed,
        dry_run=False,
        submission_phase="prepare",
    )
    decision = decision_for_turn(
        _request(), typed, application_status=reconciled[0]
    )

    assert reconciled == ("failed:conflicting_agent_results", None, "conflict")
    assert decision.recovery_action is not None
    assert decision.recovery_action.action == "park"


def test_diagnostics_classifies_invalid_legacy_result_without_contract_text() -> None:
    typed = AgentTurnResult(
        run_id="run-failure-1",
        status="failed",
        summary="Typed retryable observation",
        failure=_failure(),
    )

    assert reconcile_agent_turn_outputs_with_diagnostics(
        "RESULT:DO_NOT_APPLY",
        typed,
        dry_run=False,
        submission_phase="prepare",
    ) == (
        "failed:conflicting_agent_results",
        None,
        "conflict",
        "legacy_result_invalid",
    )


def test_diagnostics_classifies_divergent_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """A future typed evidence carrier must still fail closed when it disagrees."""
    typed = AgentTurnResult(
        run_id="run-failure-1",
        status="ready_to_submit",
        summary="Typed preparation complete",
    )
    monkeypatch.setattr(
        "applypilot.apply.agent_output.interpret_agent_turn_result",
        lambda *_args, **_kwargs: ("ready_to_submit", {"receipt": "typed"}),
    )
    monkeypatch.setattr(
        "applypilot.apply.agent_output.interpret_agent_output",
        lambda *_args, **_kwargs: ("ready_to_submit", {"receipt": "legacy"}),
    )

    assert reconcile_agent_turn_outputs_with_diagnostics(
        "RESULT:READY_TO_SUBMIT",
        typed,
        dry_run=False,
        submission_phase="prepare",
    ) == (
        "failed:conflicting_agent_results",
        None,
        "conflict",
        "evidence_mismatch",
    )


def test_absent_result_marker_keeps_typed_path_but_malformed_submit_is_uncertain() -> None:
    typed = AgentTurnResult(
        run_id="run-failure-1",
        status="failed",
        summary="Typed CAPTCHA",
        failure=_failure(code="captcha_required", source="browser_observer"),
    )
    assert reconcile_agent_turn_outputs(
        "ordinary agent prose without a legacy marker",
        typed,
        dry_run=False,
        submission_phase="prepare",
    ) == ("captcha", None, "structured")

    submit_failure = AgentTurnResult(
        run_id="run-failure-1",
        status="submission_uncertain",
        summary="Submit started",
        failure=_failure(
            code="provider_submission_error",
            source="submission_observer",
            phase="submit",
            submit_started=True,
            field_semantic=None,
            missing_capability="provider_submission_diagnostics_or_adapter",
        ),
    )
    assert reconcile_agent_turn_outputs(
        "RESULT:APPLIED trailing-junk",
        submit_failure,
        dry_run=False,
        submission_phase="submit",
    ) == ("submission_uncertain", None, "conflict")


def test_mcp_schema_exposes_facts_but_no_recovery_policy() -> None:
    schema = agent_report_mcp._report_tool()["inputSchema"]
    serialized = json.dumps(schema, sort_keys=True)

    assert '"failure"' in serialized
    assert "recoverability" not in serialized
    assert "next_action" not in serialized
