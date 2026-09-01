"""Pure shadow policy for application phases, recovery, and human boundaries."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal, cast

from applypilot.apply.contracts import (
    AgentRunRequest,
    AgentTurnResult,
    ApplicationEvent,
    DecisionEnvelope,
    FailureObservation,
    HumanInterruption,
    HumanRequest,
    RecoveryAction,
    application_actor_id,
)
from applypilot.apply.failure_taxonomy import (
    FailureDescriptor,
    classify_failure,
    classify_failure_observation,
)

ActorPhase = Literal[
    "observe",
    "normalize",
    "classify",
    "plan",
    "policy",
    "act",
    "verify",
    "recover",
    "checkpoint",
    "complete",
]

_ACTOR_PHASES = frozenset(
    {
        "observe",
        "normalize",
        "classify",
        "plan",
        "policy",
        "act",
        "verify",
        "recover",
        "checkpoint",
        "complete",
    }
)
_LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "observe": frozenset({"normalize"}),
    "normalize": frozenset({"classify"}),
    "classify": frozenset({"plan"}),
    "plan": frozenset({"policy"}),
    "policy": frozenset({"act", "checkpoint", "complete"}),
    "act": frozenset({"verify"}),
    "verify": frozenset({"recover", "checkpoint", "complete"}),
    "recover": frozenset({"observe", "checkpoint", "complete"}),
    "checkpoint": frozenset({"observe", "complete"}),
    "complete": frozenset(),
}
_COMPLETE_TURN_STATUSES = frozenset({"applied", "already_applied", "previewed"})
_CHECKPOINT_TURN_STATUSES = frozenset(
    {
        "ready_to_submit",
        "cover_not_required",
        "cover_letter_required",
        "linkedin_login_completed",
        "linkedin_external_handoff",
        "skipped",
    }
)


def _required(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")


@dataclass(frozen=True, slots=True)
class ApplicationActorState:
    """Immutable shadow state; it owns no browser, ledger, or submit authority."""

    run_id: str
    attempt_id: str
    application_id: str
    page_id: str
    write_owner: str
    phase: ActorPhase = "observe"
    final_submit_count: int = 0
    submission_uncertain: bool = False
    same_application_retries_remaining: int = 0
    new_session_retries_remaining: int = 0
    actor_id: str = ""
    turn_id: str = ""

    def __post_init__(self) -> None:
        actor_id = self.actor_id or application_actor_id(self.attempt_id)
        turn_id = self.turn_id or self.run_id
        object.__setattr__(self, "actor_id", actor_id)
        object.__setattr__(self, "turn_id", turn_id)
        for name in ("run_id", "attempt_id", "application_id", "page_id", "write_owner"):
            _required(getattr(self, name), name)
        _required(self.actor_id, "actor_id")
        _required(self.turn_id, "turn_id")
        if self.actor_id != application_actor_id(self.attempt_id):
            raise ValueError("actor_id must be the canonical identity for attempt_id")
        if self.turn_id != self.run_id:
            raise ValueError("run_id must remain the compatibility alias for turn_id")
        if self.phase not in _ACTOR_PHASES:
            raise ValueError(f"unsupported actor phase: {self.phase}")
        if self.final_submit_count not in {0, 1}:
            raise ValueError("final_submit_count must be zero or one")
        for name in (
            "same_application_retries_remaining",
            "new_session_retries_remaining",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


def advance_actor(state: ApplicationActorState, event: ApplicationEvent) -> ApplicationActorState:
    """Apply one legal phase event while enforcing identity and write invariants."""
    if event.run_id != state.run_id or event.attempt_id != state.attempt_id:
        raise ValueError("event identity does not match the application actor")
    if event.schema_version == "2" and (
        event.actor_id != state.actor_id or event.turn_id != state.turn_id
    ):
        raise ValueError("event durable identity does not match the application actor")
    next_phase = event.phase.strip().casefold()
    if next_phase not in _LEGAL_TRANSITIONS[state.phase]:
        raise ValueError(f"illegal actor transition: {state.phase} -> {next_phase}")
    if event.event_type == "action.final_submit" and next_phase != "act":
        raise ValueError("final submit events are valid only in the act phase")
    if event.event_type == "submission.uncertain" and next_phase != "verify":
        raise ValueError("submission uncertainty must be observed in the verify phase")

    application_id = event.payload.get("application_id")
    if application_id is not None and str(application_id) != state.application_id:
        raise ValueError("event application identity does not match the actor")
    page_id = event.payload.get("page_id")
    if page_id is not None and str(page_id) != state.page_id:
        raise ValueError("event page identity does not match the actor")

    if state.submission_uncertain:
        receipt_only = event.payload.get("intent") == "reconcile_receipt"
        terminal_control = event.event_type in {"checkpoint.created", "application.completed"}
        if next_phase == "act" or not (receipt_only or terminal_control):
            raise ValueError("submission_uncertain permits receipt reconciliation only")

    final_submit_count = state.final_submit_count
    if next_phase == "act":
        if event.actor != state.write_owner:
            raise ValueError("application page already has a different write owner")
        if str(page_id or "") != state.page_id:
            raise ValueError("write events must bind the exact application page")
        if event.event_type == "action.final_submit":
            if final_submit_count:
                raise ValueError("final submit may be attempted at most once")
            final_submit_count = 1

    return replace(
        state,
        phase=cast(ActorPhase, next_phase),
        final_submit_count=final_submit_count,
        submission_uncertain=(
            state.submission_uncertain or event.event_type == "submission.uncertain"
        ),
    )


def _human_interruption(
    result: str | FailureObservation, failure: FailureDescriptor
) -> HumanInterruption:
    reason = str(result or "").strip().casefold()
    typed_code = result.code if isinstance(result, FailureObservation) else ""
    if "captcha" in reason or typed_code == "captcha_required":
        interruption_type = "captcha"
    elif failure.category == "security_challenge_required":
        interruption_type = "security_challenge"
    elif failure.category == "assessment_required":
        interruption_type = "assessment"
    elif failure.category == "sensitive_identity_boundary":
        interruption_type = "sensitive_identity_or_financial_material"
    elif failure.category == "unsupported_legal_declaration" or "legal" in reason:
        interruption_type = "unsupported_legal_declaration"
    else:
        interruption_type = "human_boundary"
    return HumanInterruption(
        interruption_type=interruption_type,
        reason=failure.category,
        next_action=failure.next_action,
    )


def _decision(
    state: ApplicationActorState,
    recovery_action: RecoveryAction,
    *,
    human_interruption: HumanInterruption | None = None,
) -> DecisionEnvelope:
    if recovery_action.action in {"retry_same_application", "retry_new_session"}:
        disposition = "recover"
        next_phase = "recover"
    elif recovery_action.action == "no_retry":
        disposition = "complete"
        next_phase = "complete"
    else:
        disposition = "checkpoint"
        next_phase = "checkpoint"
    return DecisionEnvelope(
        run_id=state.run_id,
        attempt_id=state.attempt_id,
        phase=state.phase,
        disposition=disposition,
        next_phase=next_phase,
        recovery_action=recovery_action,
        human_interruption=human_interruption,
        actor_id=state.actor_id,
        turn_id=state.turn_id,
    )


def decide_recovery(
    state: ApplicationActorState, result: str | FailureObservation
) -> DecisionEnvelope:
    """Map the existing failure taxonomy to one deterministic canonical action."""
    if state.phase not in {"verify", "recover"}:
        raise ValueError("recovery decisions require a verified or recovering actor state")
    failure = (
        classify_failure_observation(result)
        if isinstance(result, FailureObservation)
        else classify_failure(result)
    )

    if state.submission_uncertain or state.final_submit_count:
        return _decision(
            state,
            RecoveryAction(
                action="reconcile_receipt",
                failure_category=failure.category,
                next_action="reconcile_receipt_without_resubmitting",
            ),
        )

    if failure.category == "unclassified_application_failure":
        action = "park"
        next_action = "park_unclassified_failure_for_bounded_diagnosis"
    elif failure.recoverability in {
        "retry_same_application",
        "retry_with_larger_runtime_budget",
    }:
        if state.same_application_retries_remaining:
            return _decision(
                state,
                RecoveryAction(
                    action="retry_same_application",
                    failure_category=failure.category,
                    next_action=failure.next_action,
                    retry_budget_remaining=state.same_application_retries_remaining - 1,
                    missing_capability=failure.missing_capability,
                    missing_material=failure.missing_material,
                ),
            )
        action = "park"
        next_action = "park_after_same_application_retry_budget_exhausted"
    elif failure.recoverability == "retry_new_session":
        if state.new_session_retries_remaining:
            return _decision(
                state,
                RecoveryAction(
                    action="retry_new_session",
                    failure_category=failure.category,
                    next_action=failure.next_action,
                    retry_budget_remaining=state.new_session_retries_remaining - 1,
                    missing_capability=failure.missing_capability,
                ),
            )
        action = "park"
        next_action = "park_after_new_session_retry_budget_exhausted"
    elif failure.recoverability == "requires_capability":
        action = "requires_capability"
        next_action = failure.next_action
    elif failure.recoverability == "requires_material":
        action = "park"
        next_action = failure.next_action
    elif failure.recoverability == "requires_human_boundary":
        interruption = _human_interruption(result, failure)
        return _decision(
            state,
            RecoveryAction(
                action="human_only",
                failure_category=failure.category,
                next_action=failure.next_action,
                missing_capability=failure.missing_capability,
                missing_material=failure.missing_material,
            ),
            human_interruption=interruption,
        )
    elif failure.recoverability == "submission_uncertain":
        action = "reconcile_receipt"
        next_action = failure.next_action
    else:
        action = "no_retry"
        next_action = failure.next_action

    return _decision(
        state,
        RecoveryAction(
            action=action,
            failure_category=failure.category,
            next_action=next_action,
            missing_capability=failure.missing_capability,
            missing_material=failure.missing_material,
        ),
    )


def apply_recovery(
    state: ApplicationActorState,
    decision: DecisionEnvelope,
) -> ApplicationActorState:
    """Reduce a shadow recovery decision without performing its proposed action."""
    if decision.upcast_from_schema_version == "1":
        raise ValueError("legacy DecisionEnvelope is read-only and cannot drive recovery")
    if (
        decision.run_id != state.run_id
        or decision.attempt_id != state.attempt_id
        or decision.phase != state.phase
        or decision.actor_id != state.actor_id
        or decision.turn_id != state.turn_id
    ):
        raise ValueError("decision identity does not match the application actor")
    if decision.next_phase not in _LEGAL_TRANSITIONS[state.phase]:
        raise ValueError(f"illegal recovery transition: {state.phase} -> {decision.next_phase}")
    recovery = decision.recovery_action
    if recovery is None:
        raise ValueError("recovery reduction requires a RecoveryAction")

    same_remaining = state.same_application_retries_remaining
    new_remaining = state.new_session_retries_remaining
    if recovery.action == "retry_same_application":
        if same_remaining < 1 or recovery.retry_budget_remaining != same_remaining - 1:
            raise ValueError("same-application retry budget does not match actor state")
        same_remaining -= 1
    elif recovery.action == "retry_new_session":
        if new_remaining < 1 or recovery.retry_budget_remaining != new_remaining - 1:
            raise ValueError("new-session retry budget does not match actor state")
        new_remaining -= 1

    return replace(
        state,
        phase=cast(ActorPhase, decision.next_phase),
        submission_uncertain=(
            state.submission_uncertain or recovery.action == "reconcile_receipt"
        ),
        same_application_retries_remaining=same_remaining,
        new_session_retries_remaining=new_remaining,
    )


def decision_for_status(
    state: ApplicationActorState,
    application_status: str,
) -> DecisionEnvelope:
    """Classify one normalized status without changing application authority."""
    if state.phase != "verify":
        raise ValueError("turn status decisions require an actor in the verify phase")
    status = str(application_status or "").strip().casefold()
    if status in _COMPLETE_TURN_STATUSES:
        return DecisionEnvelope(
            run_id=state.run_id,
            attempt_id=state.attempt_id,
            phase=state.phase,
            disposition="complete",
            next_phase="complete",
            actor_id=state.actor_id,
            turn_id=state.turn_id,
        )
    if status in _CHECKPOINT_TURN_STATUSES or status.startswith("deferred:"):
        return DecisionEnvelope(
            run_id=state.run_id,
            attempt_id=state.attempt_id,
            phase=state.phase,
            disposition="checkpoint",
            next_phase="checkpoint",
            actor_id=state.actor_id,
            turn_id=state.turn_id,
        )
    return decide_recovery(state, status)


def decision_for_turn(
    request: AgentRunRequest,
    result: AgentTurnResult,
    *,
    application_status: str,
) -> DecisionEnvelope:
    """Project one real Agent turn onto the shadow actor control contract."""
    if result.run_id != request.run_id:
        raise ValueError("AgentTurnResult run_id does not match AgentRunRequest")
    status = str(application_status or result.status).strip().casefold()

    def retry_budget(key: str) -> int:
        if request.phase.casefold() == "submit":
            return 0
        value = request.context.get(key, 0)
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0

    typed_failure = result.failure
    typed_application_status = None
    if typed_failure is not None:
        if typed_failure.submit_started or typed_failure.code == "submission_uncertain":
            typed_application_status = "submission_uncertain"
        elif typed_failure.code == "captcha_required":
            typed_application_status = "captcha"
        elif typed_failure.code == "expired":
            typed_application_status = "expired"
        else:
            typed_application_status = f"failed:{typed_failure.code}"
    submit_started = typed_failure is not None and typed_failure.submit_started
    state = ApplicationActorState(
        run_id=request.run_id,
        attempt_id=request.attempt_id,
        application_id=request.attempt_id,
        page_id=f"attempt:{request.attempt_id}",
        write_owner=request.agent_role,
        phase="verify",
        final_submit_count=int(
            submit_started
            or (
                request.phase.casefold() == "submit"
                and status in {"applied", "submission_uncertain"}
            )
        ),
        submission_uncertain=submit_started or status == "submission_uncertain",
        same_application_retries_remaining=retry_budget(
            "actor_same_application_retries_remaining"
        ),
        new_session_retries_remaining=retry_budget(
            "actor_new_session_retries_remaining"
        ),
        actor_id=request.actor_id,
        turn_id=request.turn_id,
    )
    if typed_failure is not None and status == typed_application_status:
        return decide_recovery(state, typed_failure)
    return decision_for_status(state, status)


def human_request_for_decision(decision: DecisionEnvelope) -> HumanRequest | None:
    """Convert a typed interruption to the existing durable HumanRequest contract."""
    if decision.upcast_from_schema_version is not None:
        raise ValueError("legacy DecisionEnvelope is read-only and cannot create a handoff")
    interruption = decision.human_interruption
    recovery = decision.recovery_action
    if interruption is None or recovery is None:
        return None
    prompt_by_type = {
        "captcha": (
            "Waiting for the visible CAPTCHA to be cleared on the bound ApplyPilot "
            "page; the launcher will re-observe and resume automatically."
        ),
        "security_challenge": (
            "Waiting for the account security challenge on the bound page; this is "
            "a security action, not a request to re-authorize the application."
        ),
        "assessment": (
            "The application reached an assessment boundary and remains paused; "
            "routine application authorization is already recorded."
        ),
        "sensitive_identity_or_financial_material": (
            "The application requires identity or financial material that ApplyPilot "
            "cannot supply automatically."
        ),
        "unsupported_legal_declaration": (
            "The application requires a material declaration that is not supported "
            "by confirmed facts."
        ),
        "human_boundary": (
            "The application reached a typed human-only boundary; routine application "
            "authorization is already recorded."
        ),
    }
    return HumanRequest(
        request_id=f"{decision.run_id}:human:1",
        run_id=decision.run_id,
        attempt_id=decision.attempt_id,
        request_type=interruption.interruption_type,
        prompt=prompt_by_type[interruption.interruption_type],
        context={
            "actor_id": decision.actor_id,
            "turn_id": decision.turn_id,
            "actor_phase": decision.phase,
            "actor_next_phase": decision.next_phase,
            "failure_category": recovery.failure_category,
            "recovery_action": recovery.action,
            "next_action": interruption.next_action,
            "shadow_only": decision.shadow_only,
        },
    )
