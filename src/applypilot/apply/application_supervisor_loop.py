"""Deterministic online supervision for one authoritative Agent turn.

The loop is deliberately model-free.  It consumes bounded host observations,
detects repeated/no-progress work, and emits an auditable intervention level.
It never grants browser or submission authority and never treats a shadow
runtime as evidence.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SupervisorObservation:
    attempt_id: str
    turn_id: str
    event_type: str
    observed_at: float
    meaningful_progress: bool = False
    tool_name: str | None = None
    tool_params: Mapping[str, object] | None = None
    page_signature: str | None = None
    unresolved_control_delta: int = 0
    validation_delta: int = 0
    last_successful_effect: str | None = None
    effect_started: bool = False
    submit_started: bool = False
    effect_uncertain: bool = False


@dataclass(frozen=True, slots=True)
class SupervisorSignals:
    attempt_id: str
    turn_id: str
    meaningful_progress: bool
    no_progress_window: float
    tool_repeat_count: int
    page_signature: str | None
    unresolved_control_delta: int
    validation_delta: int
    last_successful_effect: str | None


@dataclass(frozen=True, slots=True)
class SupervisorDecision:
    level: int
    action: str
    reason_code: str
    signals: SupervisorSignals
    expected_turn_id: str
    requires_extra_model: bool = False
    receipt_only: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AuthorityHealthObservation:
    """Stable read-only authority health; it is not a page-state observation."""

    observed_at: float
    authority_signature: str


def _normalized_params(params: Mapping[str, object] | None) -> str:
    try:
        return json.dumps(
            dict(params or {}),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda value: f"<{type(value).__name__}>",
        )
    except (TypeError, ValueError):
        return "<invalid>"


class ApplicationSupervisorLoop:
    """Pure per-turn state machine with a bounded deterministic stall policy."""

    def __init__(
        self,
        *,
        attempt_id: str,
        turn_id: str,
        stall_window_seconds: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not attempt_id or not turn_id:
            raise ValueError("attempt_id and turn_id are required")
        if isinstance(stall_window_seconds, bool) or not 0 < stall_window_seconds <= 2:
            raise ValueError("stall_window_seconds must be greater than zero and at most 2")
        self.attempt_id = attempt_id
        self.turn_id = turn_id
        self.stall_window_seconds = float(stall_window_seconds)
        self._clock = clock
        self._last_progress_at: float | None = None
        self._last_observed_at: float | None = None
        self._last_tool_key: tuple[str, str] | None = None
        self._tool_repeat_count = 0
        self._page_signature: str | None = None
        self._unresolved_control_delta = 0
        self._validation_delta = 0
        self._last_successful_effect: str | None = None
        self._effect_started = False
        self._submit_started = False
        self._effect_uncertain = False
        self._stall_level = 0

    def start(self, *, observed_at: float | None = None) -> SupervisorDecision:
        """Bind the turn-start progress baseline before any provider output."""

        now = self._clock() if observed_at is None else float(observed_at)
        if self._last_observed_at is not None:
            raise RuntimeError("supervisor turn is already started")
        self._last_observed_at = now
        self._last_progress_at = now
        return self._decision(0, "TURN_STARTED", now, meaningful_progress=True)

    def observe(self, observation: SupervisorObservation) -> SupervisorDecision:
        self._validate_binding(observation)
        now = float(observation.observed_at)
        if self._last_observed_at is not None and now < self._last_observed_at:
            raise ValueError("supervisor observations must be time ordered")
        self._last_observed_at = now
        self._effect_started = self._effect_started or observation.effect_started
        self._submit_started = self._submit_started or observation.submit_started
        self._effect_uncertain = self._effect_uncertain or observation.effect_uncertain

        changed = bool(
            observation.meaningful_progress
            or (
                observation.page_signature is not None
                and observation.page_signature != self._page_signature
            )
            or observation.unresolved_control_delta != 0
            or observation.validation_delta != 0
            or (
                observation.last_successful_effect is not None
                and observation.last_successful_effect != self._last_successful_effect
            )
        )
        self._page_signature = observation.page_signature or self._page_signature
        self._unresolved_control_delta = observation.unresolved_control_delta
        self._validation_delta = observation.validation_delta
        self._last_successful_effect = (
            observation.last_successful_effect or self._last_successful_effect
        )

        if changed:
            self._last_progress_at = now
            self._last_tool_key = None
            self._tool_repeat_count = 0
            self._stall_level = 0

        if observation.tool_name:
            tool_key = (observation.tool_name, _normalized_params(observation.tool_params))
            if not changed and tool_key == self._last_tool_key:
                self._tool_repeat_count += 1
            else:
                self._last_tool_key = tool_key
                self._tool_repeat_count = 1
                if self._last_progress_at is None:
                    self._last_progress_at = now

        if not changed and self._tool_repeat_count >= 2:
            candidate = min(3, self._tool_repeat_count - 1)
            return self._decision(candidate, "TOOL_REPEAT_NO_PROGRESS", now)
        return self._decision(0, "OBSERVING", now, meaningful_progress=changed)

    def tick(self, *, observed_at: float | None = None) -> SupervisorDecision:
        now = self._clock() if observed_at is None else float(observed_at)
        if self._last_observed_at is not None and now < self._last_observed_at:
            raise ValueError("supervisor ticks must be time ordered")
        self._last_observed_at = now
        if self._last_progress_at is None:
            return self._decision(0, "OBSERVING", now)
        elapsed = now - self._last_progress_at
        if elapsed < self.stall_window_seconds * (self._stall_level + 1):
            return self._decision(0, "OBSERVING", now)
        self._stall_level = min(3, self._stall_level + 1)
        return self._decision(self._stall_level, "NO_PROGRESS_WINDOW", now)

    def _validate_binding(self, observation: SupervisorObservation) -> None:
        if observation.attempt_id != self.attempt_id or observation.turn_id != self.turn_id:
            raise ValueError("supervisor observation is not bound to the active attempt/turn")

    def _signals(self, now: float, *, meaningful_progress: bool = False) -> SupervisorSignals:
        return SupervisorSignals(
            attempt_id=self.attempt_id,
            turn_id=self.turn_id,
            meaningful_progress=meaningful_progress,
            no_progress_window=(
                0.0 if self._last_progress_at is None else max(0.0, now - self._last_progress_at)
            ),
            tool_repeat_count=self._tool_repeat_count,
            page_signature=self._page_signature,
            unresolved_control_delta=self._unresolved_control_delta,
            validation_delta=self._validation_delta,
            last_successful_effect=self._last_successful_effect,
        )

    def _decision(
        self,
        level: int,
        reason_code: str,
        now: float,
        *,
        meaningful_progress: bool = False,
    ) -> SupervisorDecision:
        if level >= 2 and (self._submit_started or self._effect_uncertain):
            return SupervisorDecision(
                level=4,
                action="interrupt_park_receipt_only",
                reason_code="EFFECT_REPLAY_FORBIDDEN",
                signals=self._signals(now, meaningful_progress=meaningful_progress),
                expected_turn_id=self.turn_id,
                receipt_only=True,
            )
        if level >= 2 and self._effect_started:
            return SupervisorDecision(
                level=4,
                action="interrupt_park_manual",
                reason_code="CONFIRMED_EFFECT_REPLAY_FORBIDDEN",
                signals=self._signals(now, meaningful_progress=meaningful_progress),
                expected_turn_id=self.turn_id,
                receipt_only=False,
            )
        actions = {
            0: "observe",
            1: "request_read_only_observation",
            2: "steer_current_turn",
            3: "interrupt_park_manual",
            4: "park_human",
        }
        return SupervisorDecision(
            level=level,
            action=actions[level],
            reason_code=reason_code,
            signals=self._signals(now, meaningful_progress=meaningful_progress),
            expected_turn_id=self.turn_id,
            requires_extra_model=level >= 2,
            receipt_only=False,
        )


class AuthoritativeSupervisorController:
    """Apply loop decisions to the one authoritative runtime, never a shadow."""

    def __init__(
        self,
        *,
        loop: ApplicationSupervisorLoop,
        backend: str,
        interrupt: Callable[[], None],
        observe_authority_health: (
            Callable[[SupervisorSignals], AuthorityHealthObservation] | None
        ),
        steer: Callable[[str, str], None] | None = None,
        before_action: Callable[[SupervisorDecision], None] | None = None,
        after_action: Callable[[SupervisorDecision, str], None] | None = None,
    ) -> None:
        self.loop = loop
        self.backend = backend
        self._interrupt = interrupt
        self._observe_authority_health = observe_authority_health
        self._steer = steer
        self._before_action = before_action or (lambda _decision: None)
        self._after_action = after_action or (lambda _decision, _outcome: None)
        self.interventions: list[dict[str, Any]] = []
        self.parked = False
        self.receipt_only = False

    def apply(self, decision: SupervisorDecision) -> SupervisorDecision:
        if self.parked:
            return SupervisorDecision(
                level=4,
                action="parked_ignore_buffered_event",
                reason_code="ALREADY_PARKED",
                signals=decision.signals,
                expected_turn_id=decision.expected_turn_id,
                receipt_only=self.receipt_only,
            )
        if decision.level <= 0:
            return decision
        if decision.level == 1:
            audit = SupervisorDecision(
                level=1,
                action=(
                    "audit_only_authority_health"
                    if self._observe_authority_health is not None
                    else "audit_only_no_observer"
                ),
                reason_code="PAGE_OBSERVER_UNAVAILABLE",
                signals=decision.signals,
                expected_turn_id=decision.expected_turn_id,
                receipt_only=decision.receipt_only,
            )
            self._before_action(audit)
            if self._observe_authority_health is None:
                self._after_action(audit, "observer_unavailable")
                self._record(audit)
                return audit
            try:
                self._observe_authority_health(audit.signals)
            except (KeyError, OSError, RuntimeError, TimeoutError, TypeError, ValueError):
                self._after_action(audit, "authority_health_failed")
                self._record(audit)
                return self._interrupt_and_park(
                    SupervisorDecision(
                        level=4,
                        action="park_human",
                        reason_code="AUTHORITY_HEALTH_FAILED_CLOSED",
                        signals=decision.signals,
                        expected_turn_id=decision.expected_turn_id,
                        receipt_only=decision.receipt_only,
                    )
                )
            self._after_action(audit, "authority_health_observed")
            self._record(audit)
            return audit

        resolved = decision
        if decision.level == 2 and self._steer is None:
            resolved = SupervisorDecision(
                level=3,
                action="interrupt_park_manual",
                reason_code="STEER_UNSUPPORTED",
                signals=decision.signals,
                expected_turn_id=decision.expected_turn_id,
                requires_extra_model=True,
                receipt_only=False,
            )
        if resolved.level >= 3:
            return self._interrupt_and_park(resolved)

        self._before_action(resolved)
        assert self._steer is not None
        try:
            self._steer(
                "Re-observe the current page and choose a different bounded action; "
                "the previous tool path made no progress.",
                resolved.expected_turn_id,
            )
        except (KeyError, OSError, RuntimeError, TimeoutError, ValueError):
            self._after_action(resolved, "steer_failed")
            self._record(resolved)
            return self._interrupt_and_park(
                SupervisorDecision(
                    level=4,
                    action="park_human",
                    reason_code="STEER_FAILED_CLOSED",
                    signals=decision.signals,
                    expected_turn_id=decision.expected_turn_id,
                    receipt_only=decision.receipt_only,
                )
            )
        self._after_action(resolved, "steer_sent")
        self._record(resolved)
        return resolved

    def _interrupt_and_park(self, decision: SupervisorDecision) -> SupervisorDecision:
        self._before_action(decision)
        resolved = decision
        try:
            self._interrupt()
        except (KeyError, OSError, RuntimeError, TimeoutError, ValueError):
            resolved = SupervisorDecision(
                level=4,
                action="park_human",
                reason_code="INTERRUPT_FAILED_CLOSED",
                signals=decision.signals,
                expected_turn_id=decision.expected_turn_id,
                requires_extra_model=decision.requires_extra_model,
                receipt_only=decision.receipt_only,
            )
            outcome = "interrupt_failed"
        else:
            outcome = "runtime_interrupted"
        self.parked = True
        self.receipt_only = resolved.receipt_only
        self._after_action(decision, outcome)
        self._record(resolved)
        return resolved

    def _record(self, decision: SupervisorDecision) -> None:
        if decision.level <= 0:
            return
        payload = decision.as_dict()
        payload["backend"] = self.backend
        self.interventions.append(payload)


__all__ = [
    "ApplicationSupervisorLoop",
    "AuthoritativeSupervisorController",
    "AuthorityHealthObservation",
    "SupervisorDecision",
    "SupervisorObservation",
    "SupervisorSignals",
]
