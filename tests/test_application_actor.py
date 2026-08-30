from __future__ import annotations

from dataclasses import replace

import pytest

from applypilot.apply import launcher
from applypilot.apply.application_actor import (
    ApplicationActorState,
    advance_actor,
    apply_recovery,
    decide_recovery,
    decision_for_status,
    decision_for_turn,
    human_request_for_decision,
)
from applypilot.apply.contracts import (
    AgentRunRequest,
    AgentTurnResult,
    ApplicationEvent,
    DecisionEnvelope,
    RecoveryAction,
    contract_json,
    decision_envelope_from_mapping,
)


def actor(**changes: object) -> ApplicationActorState:
    values = {
        "run_id": "run-1",
        "attempt_id": "attempt-1",
        "application_id": "application-1",
        "page_id": "page-1",
        "write_owner": "playwright",
        "same_application_retries_remaining": 1,
        "new_session_retries_remaining": 1,
    }
    values.update(changes)
    return ApplicationActorState(**values)  # type: ignore[arg-type]


def event(
    phase: str,
    *,
    actor_id: str = "observer",
    event_type: str = "phase.completed",
    payload: dict[str, object] | None = None,
) -> ApplicationEvent:
    return ApplicationEvent(
        event_id=f"event-{phase}-{event_type}",
        attempt_id="attempt-1",
        run_id="run-1",
        phase=phase,
        actor=actor_id,
        event_type=event_type,
        payload=payload or {},
    )


def to_policy(state: ApplicationActorState) -> ApplicationActorState:
    for phase in ("normalize", "classify", "plan", "policy"):
        state = advance_actor(state, event(phase))
    return state


def test_actor_accepts_only_the_declared_shadow_lifecycle() -> None:
    state = to_policy(actor())
    state = advance_actor(
        state,
        event("act", actor_id="playwright", payload={"page_id": "page-1"}),
    )
    state = advance_actor(state, event("verify"))
    state = advance_actor(state, event("complete", event_type="application.completed"))

    assert state.phase == "complete"
    with pytest.raises(ValueError, match="illegal actor transition"):
        advance_actor(actor(), event("act", actor_id="playwright", payload={"page_id": "page-1"}))
    with pytest.raises(ValueError, match="only in the act phase"):
        advance_actor(actor(), event("normalize", event_type="action.final_submit"))


def test_one_application_page_has_exactly_one_write_owner() -> None:
    state = to_policy(actor())

    with pytest.raises(ValueError, match="different write owner"):
        advance_actor(
            state,
            event("act", actor_id="helper-agent", payload={"page_id": "page-1"}),
        )
    with pytest.raises(ValueError, match="page identity"):
        advance_actor(
            state,
            event("act", actor_id="playwright", payload={"page_id": "page-2"}),
        )


def test_final_submit_can_be_attempted_at_most_once() -> None:
    state = to_policy(actor())
    state = advance_actor(
        state,
        event(
            "act",
            actor_id="playwright",
            event_type="action.final_submit",
            payload={"page_id": "page-1"},
        ),
    )
    state = advance_actor(state, event("verify"))
    state = advance_actor(state, event("recover"))
    state = advance_actor(state, event("observe"))
    state = to_policy(state)

    with pytest.raises(ValueError, match="at most once"):
        advance_actor(
            state,
            event(
                "act",
                actor_id="playwright",
                event_type="action.final_submit",
                payload={"page_id": "page-1"},
            ),
        )


def test_submission_uncertain_is_receipt_only_and_never_returns_to_act() -> None:
    state = to_policy(actor())
    state = advance_actor(
        state,
        event(
            "act",
            actor_id="playwright",
            event_type="action.final_submit",
            payload={"page_id": "page-1"},
        ),
    )
    state = advance_actor(state, event("verify", event_type="submission.uncertain"))

    decision = decide_recovery(state, "failed:provider_submission_error")
    reduced = apply_recovery(state, decision)

    assert decision.recovery_action is not None
    assert decision.recovery_action.action == "reconcile_receipt"
    assert decision.recovery_action.next_action == "reconcile_receipt_without_resubmitting"
    assert reduced.phase == "checkpoint"
    assert reduced.submission_uncertain is True
    with pytest.raises(ValueError, match="receipt reconciliation only"):
        advance_actor(reduced, event("observe"))


@pytest.mark.parametrize(
    ("result", "expected_action", "interruption_type"),
    [
        ("failed:resume_upload", "retry_same_application", None),
        ("failed:agent_runtime_timeout", "retry_same_application", None),
        ("failed:site_blocked", "retry_new_session", None),
        ("failed:browser_mcp_unavailable", "requires_capability", None),
        ("failed:required_document", "park", None),
        ("failed:unknown_new_failure", "park", None),
        ("captcha", "human_only", "captcha"),
        ("failed:mfa", "human_only", "security_challenge"),
        ("failed:assessment_required", "human_only", "assessment"),
        (
            "failed:identity_material_missing:passport",
            "human_only",
            "sensitive_identity_or_financial_material",
        ),
        (
            "failed:financial_document_required",
            "human_only",
            "sensitive_identity_or_financial_material",
        ),
        (
            "failed:unsupported_legal_declaration",
            "human_only",
            "unsupported_legal_declaration",
        ),
        ("expired", "no_retry", None),
        ("submission_uncertain", "reconcile_receipt", None),
    ],
)
def test_failure_taxonomy_has_one_deterministic_policy_projection(
    result: str,
    expected_action: str,
    interruption_type: str | None,
) -> None:
    decision = decide_recovery(actor(phase="verify"), result)

    assert decision.shadow_only is True
    assert decision.recovery_action is not None
    assert decision.recovery_action.action == expected_action
    assert (
        decision.human_interruption.interruption_type
        if decision.human_interruption is not None
        else None
    ) == interruption_type


def test_technical_recovery_consumes_budget_before_parking() -> None:
    state = actor(phase="verify", same_application_retries_remaining=1)
    decision = decide_recovery(state, "failed:resume_upload")

    reduced = apply_recovery(state, decision)
    exhausted = decide_recovery(
        replace(reduced, phase="verify"),
        "failed:resume_upload",
    )

    assert reduced.phase == "recover"
    assert reduced.same_application_retries_remaining == 0
    assert exhausted.recovery_action is not None
    assert exhausted.recovery_action.action == "park"


def test_real_turn_contract_yields_typed_human_request_without_free_text() -> None:
    request = AgentRunRequest(
        run_id="run-1",
        attempt_id="attempt-1",
        agent_role="browser-application-agent",
        phase="prepare",
        objective="Prepare application",
    )
    result = AgentTurnResult(
        run_id="run-1",
        status="captcha",
        summary="Synthetic boundary",
        requested_human_input="Do something vague",
    )

    decision = decision_for_turn(request, result, application_status="captcha")
    human_request = human_request_for_decision(decision)

    assert human_request is not None
    assert human_request.request_type == "captcha"
    assert human_request.context["recovery_action"] == "human_only"
    assert "Do something vague" not in str(contract_json(human_request))
    with pytest.raises(ValueError, match="run_id does not match"):
        decision_for_turn(
            request,
            replace(result, run_id="run-other"),
            application_status="captcha",
        )


def test_decision_envelope_cannot_claim_application_authority() -> None:
    with pytest.raises(ValueError, match="shadow-only"):
        DecisionEnvelope(
            run_id="run-1",
            attempt_id="attempt-1",
            phase="verify",
            disposition="checkpoint",
            next_phase="checkpoint",
            shadow_only=False,
        )
    with pytest.raises(ValueError, match="control flow"):
        DecisionEnvelope(
            run_id="run-1",
            attempt_id="attempt-1",
            phase="verify",
            disposition="complete",
            next_phase="recover",
            recovery_action=RecoveryAction(
                action="retry_new_session",
                failure_category="page_or_progress_failure",
                next_action="retry_with_fresh_observation",
            ),
        )
    with pytest.raises(ValueError, match="verify phase"):
        decision_for_status(actor(), "ready_to_submit")


def test_persisted_decision_envelope_round_trips_only_as_schema_v1() -> None:
    decision = decide_recovery(actor(phase="verify"), "failed:mfa")
    encoded = contract_json(decision)

    assert decision_envelope_from_mapping(encoded) == decision

    unknown_version = dict(encoded)
    unknown_version["schema_version"] = "2"
    with pytest.raises(ValueError, match="schema_version"):
        decision_envelope_from_mapping(unknown_version)

    contradictory = dict(encoded)
    contradictory["disposition"] = "recover"
    contradictory["next_phase"] = "recover"
    with pytest.raises(ValueError, match="control flow"):
        decision_envelope_from_mapping(contradictory)


def test_launcher_persists_typed_human_interruption_in_existing_control_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[object, object, object]] = []
    monkeypatch.setattr(
        "applypilot.database.record_agent_turn_control",
        lambda event_value, checkpoint_value, request_value: captured.append(
            (event_value, checkpoint_value, request_value)
        ),
    )
    request = AgentRunRequest(
        run_id="run-captcha",
        attempt_id="attempt-1",
        agent_role="browser-application-agent",
        phase="prepare",
        objective="Prepare application",
    )
    result = AgentTurnResult(
        run_id=request.run_id,
        status="captcha",
        summary="Synthetic CAPTCHA boundary",
        requested_human_input="Vague free-text request",
    )

    launcher._persist_agent_turn_completed(
        request,
        result,
        application_status="captcha",
        duration_ms=10,
        source="synthetic-test",
    )

    persisted_event, checkpoint, human_request = captured[0]
    assert persisted_event.payload["actor_decision"]["recovery_action"]["action"] == "human_only"
    assert checkpoint.state["actor_decision"] == persisted_event.payload["actor_decision"]
    assert human_request.request_type == "captcha"
    assert "Vague free-text request" not in str(contract_json(human_request))


def test_launcher_does_not_turn_recoverable_technical_failure_into_vague_human_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[object, object, object]] = []
    monkeypatch.setattr(
        "applypilot.database.record_agent_turn_control",
        lambda event_value, checkpoint_value, request_value: captured.append(
            (event_value, checkpoint_value, request_value)
        ),
    )
    request = AgentRunRequest(
        run_id="run-technical",
        attempt_id="attempt-1",
        agent_role="browser-application-agent",
        phase="prepare",
        objective="Prepare application",
    )
    result = AgentTurnResult(
        run_id=request.run_id,
        status="failed:stuck",
        summary="Synthetic technical failure",
        requested_human_input="Please tell me what to do",
    )

    launcher._persist_agent_turn_completed(
        request,
        result,
        application_status=result.status,
        duration_ms=10,
        source="synthetic-test",
    )

    persisted_event, _checkpoint, human_request = captured[0]
    assert persisted_event.payload["actor_decision"]["recovery_action"]["action"] == "park"
    assert human_request is None

    material_request = replace(request, run_id="run-material")
    material_result = AgentTurnResult(
        run_id=material_request.run_id,
        status="failed:manual_review_required:required_document",
        summary="Synthetic ordinary material requirement",
        requested_human_input="Attach the requested document",
    )
    launcher._persist_agent_turn_completed(
        material_request,
        material_result,
        application_status=material_result.status,
        duration_ms=10,
        source="synthetic-test",
    )

    material_event, _checkpoint, material_human_request = captured[1]
    assert material_event.payload["actor_decision"]["recovery_action"]["action"] == "park"
    assert material_human_request.request_type == "agent_clarification"


def test_submit_turn_cannot_advertise_a_fresh_browser_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[object, object, object]] = []
    monkeypatch.setattr(
        "applypilot.database.record_agent_turn_control",
        lambda event_value, checkpoint_value, request_value: captured.append(
            (event_value, checkpoint_value, request_value)
        ),
    )
    request = AgentRunRequest(
        run_id="run-submit-failure",
        attempt_id="attempt-1",
        agent_role="browser-application-agent",
        phase="submit",
        objective="Submit application",
        context={"actor_new_session_retries_remaining": 1},
    )
    result = AgentTurnResult(
        run_id=request.run_id,
        status="failed:cloudflare_blocked",
        summary="Synthetic submit-stage browser block",
        requested_human_input="Try the submission again",
    )

    launcher._persist_agent_turn_completed(
        request,
        result,
        application_status=result.status,
        duration_ms=10,
        source="synthetic-test",
    )

    persisted_event, checkpoint, human_request = captured[0]
    decision = persisted_event.payload["actor_decision"]
    assert decision["recovery_action"]["action"] == "park"
    assert checkpoint.state["actor_decision"] == decision
    assert human_request is None
