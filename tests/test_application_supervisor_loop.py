from __future__ import annotations

import pytest

from applypilot.apply.application_supervisor_loop import (
    ApplicationSupervisorLoop,
    AuthoritativeSupervisorController,
    AuthorityHealthObservation,
    SupervisorObservation,
)
from applypilot.apply.launcher import _apply_authoritative_supervisor_observation


def observation(at: float, **changes: object) -> SupervisorObservation:
    values: dict[str, object] = {
        "attempt_id": "attempt-1",
        "turn_id": "turn-1",
        "event_type": "tool.proposed",
        "observed_at": at,
    }
    values.update(changes)
    return SupervisorObservation(**values)  # type: ignore[arg-type]


def test_normal_fast_path_is_model_free() -> None:
    loop = ApplicationSupervisorLoop(attempt_id="attempt-1", turn_id="turn-1")

    decision = loop.observe(observation(0, meaningful_progress=True, page_signature="p1"))

    assert decision.level == 0
    assert decision.action == "observe"
    assert decision.requires_extra_model is False
    assert decision.signals.attempt_id == "attempt-1"
    assert decision.signals.turn_id == "turn-1"


def test_same_normalized_tool_is_corrected_after_second_no_progress_attempt() -> None:
    loop = ApplicationSupervisorLoop(attempt_id="attempt-1", turn_id="turn-1")
    first = loop.observe(
        observation(0, tool_name="browser_click", tool_params={"b": 2, "a": 1})
    )
    second = loop.observe(
        observation(0.1, tool_name="browser_click", tool_params={"a": 1, "b": 2})
    )

    assert first.level == 0
    assert second.level == 1
    assert second.action == "request_read_only_observation"
    assert second.signals.tool_repeat_count == 2
    assert second.requires_extra_model is False


def test_direct_level_three_uses_interrupt_park_manual_semantics() -> None:
    loop = ApplicationSupervisorLoop(attempt_id="attempt-1", turn_id="turn-1")
    decisions = [
        loop.observe(observation(at, tool_name="browser_snapshot"))
        for at in (0.0, 0.1, 0.2, 0.3)
    ]

    assert decisions[-1].level == 3
    assert decisions[-1].action == "interrupt_park_manual"


def test_page_control_or_validation_change_resets_repetition() -> None:
    loop = ApplicationSupervisorLoop(attempt_id="attempt-1", turn_id="turn-1")
    loop.observe(observation(0, tool_name="browser_snapshot", page_signature="p1"))
    loop.observe(observation(0.1, tool_name="browser_snapshot", page_signature="p1"))

    changed = loop.observe(
        observation(
            0.2,
            tool_name="browser_snapshot",
            page_signature="p2",
            unresolved_control_delta=1,
            validation_delta=1,
        )
    )

    assert changed.level == 0
    assert changed.signals.meaningful_progress is True
    assert changed.signals.tool_repeat_count == 1


def test_stall_window_intervenes_within_configured_two_seconds() -> None:
    loop = ApplicationSupervisorLoop(
        attempt_id="attempt-1",
        turn_id="turn-1",
        stall_window_seconds=2,
    )
    loop.observe(observation(0, tool_name="browser_snapshot"))

    assert loop.tick(observed_at=1.999).level == 0
    decision = loop.tick(observed_at=2)
    assert decision.level == 1
    assert decision.reason_code == "NO_PROGRESS_WINDOW"
    assert decision.signals.no_progress_window == 2


def test_level_two_steers_exact_expected_turn() -> None:
    calls: list[tuple[str, str]] = []
    loop = ApplicationSupervisorLoop(attempt_id="attempt-1", turn_id="turn-1")
    controller = AuthoritativeSupervisorController(
        loop=loop,
        backend="codex-app-server",
        interrupt=lambda: None,
        observe_authority_health=None,
        steer=lambda prompt, expected: calls.append((prompt, expected)),
    )
    for at in (0.0, 0.1):
        controller.apply(
            loop.observe(observation(at, tool_name="browser_snapshot", tool_params={}))
        )
    decision = controller.apply(
        loop.observe(observation(0.2, tool_name="browser_snapshot", tool_params={}))
    )

    assert decision.level == 2
    assert calls and calls[0][1] == "turn-1"


def test_cli_without_steer_escalates_and_cancels_authoritative_runtime() -> None:
    interrupted: list[bool] = []
    loop = ApplicationSupervisorLoop(attempt_id="attempt-1", turn_id="turn-1")
    controller = AuthoritativeSupervisorController(
        loop=loop,
        backend="codex-cli",
        interrupt=lambda: interrupted.append(True),
        observe_authority_health=None,
    )
    for at in (0.0, 0.1):
        controller.apply(loop.observe(observation(at, tool_name="browser_snapshot")))
    decision = controller.apply(loop.observe(observation(0.2, tool_name="browser_snapshot")))

    assert decision.level == 3
    assert decision.reason_code == "STEER_UNSUPPORTED"
    assert decision.action == "interrupt_park_manual"
    assert interrupted == [True]
    assert controller.parked is True


def test_post_effect_stall_forbids_replace_and_parks_receipt_only() -> None:
    interrupted: list[bool] = []
    loop = ApplicationSupervisorLoop(attempt_id="attempt-1", turn_id="turn-1")
    controller = AuthoritativeSupervisorController(
        loop=loop,
        backend="codex-app-server",
        interrupt=lambda: interrupted.append(True),
        observe_authority_health=None,
        steer=lambda *_args: (_ for _ in ()).throw(AssertionError("must not steer")),
    )
    for at in (0.0, 0.1):
        controller.apply(
            loop.observe(
                observation(at, tool_name="browser_click", effect_started=True)
            )
        )
    decision = controller.apply(
        loop.observe(observation(0.2, tool_name="browser_click", effect_uncertain=True))
    )

    assert decision.level == 4
    assert decision.action == "interrupt_park_receipt_only"
    assert interrupted == [True]
    assert controller.receipt_only is True


def test_confirmed_effect_stall_parks_manual_without_receipt_only() -> None:
    interrupted: list[bool] = []
    loop = ApplicationSupervisorLoop(attempt_id="attempt-1", turn_id="turn-1")
    controller = AuthoritativeSupervisorController(
        loop=loop,
        backend="codex-cli",
        interrupt=lambda: interrupted.append(True),
        observe_authority_health=None,
    )
    for at in (0.0, 0.1):
        controller.apply(
            loop.observe(observation(at, tool_name="browser_click", effect_started=True))
        )
    decision = controller.apply(
        loop.observe(observation(0.2, tool_name="browser_click", effect_started=True))
    )

    assert decision.level == 4
    assert decision.reason_code == "CONFIRMED_EFFECT_REPLAY_FORBIDDEN"
    assert decision.receipt_only is False
    assert interrupted == [True]


def test_wrong_attempt_or_turn_fails_closed() -> None:
    loop = ApplicationSupervisorLoop(attempt_id="attempt-1", turn_id="turn-1")

    try:
        loop.observe(
            SupervisorObservation(
                attempt_id="attempt-2",
                turn_id="turn-1",
                event_type="tool.proposed",
                observed_at=0,
            )
        )
    except ValueError as exc:
        assert "active attempt/turn" in str(exc)
    else:
        raise AssertionError("binding mismatch must fail closed")


def test_silent_started_turn_triggers_level_one_at_two_seconds() -> None:
    loop = ApplicationSupervisorLoop(
        attempt_id="attempt-1",
        turn_id="turn-1",
        stall_window_seconds=2,
    )
    started = loop.start(observed_at=0)

    assert started.signals.meaningful_progress is True
    assert loop.tick(observed_at=1.999).level == 0
    assert loop.tick(observed_at=2).level == 1


def test_level_one_observes_authority_health_without_faking_page_progress() -> None:
    corrections: list[object] = []
    interrupted: list[bool] = []
    steered: list[bool] = []
    loop = ApplicationSupervisorLoop(attempt_id="attempt-1", turn_id="turn-1")
    controller = AuthoritativeSupervisorController(
        loop=loop,
        backend="codex-app-server",
        interrupt=lambda: interrupted.append(True),
        observe_authority_health=lambda signals: (
            corrections.append(signals)
            or AuthorityHealthObservation(
                observed_at=0.11,
                authority_signature="authority-stable",
            )
        ),
        steer=lambda *_args: steered.append(True),
    )
    loop.observe(observation(0, tool_name="browser_snapshot"))

    decision = controller.apply(
        loop.observe(observation(0.1, tool_name="browser_snapshot"))
    )

    assert decision.level == 1
    assert len(corrections) == 1
    assert interrupted == []
    assert steered == []
    unchanged = loop.observe(observation(0.2, event_type="host.tick"))
    assert unchanged.signals.page_signature is None


def test_assistant_text_does_not_reset_repeated_tool_detection() -> None:
    loop = ApplicationSupervisorLoop(attempt_id="attempt-1", turn_id="turn-1")
    loop.observe(
        observation(0, tool_name="browser_snapshot", tool_params={"ref": "same"})
    )
    text = loop.observe(observation(0.05, event_type="assistant.text"))
    repeated = loop.observe(
        observation(0.1, tool_name="browser_snapshot", tool_params={"ref": "same"})
    )

    assert text.signals.meaningful_progress is False
    assert repeated.level == 1
    assert repeated.signals.tool_repeat_count == 2


def test_launcher_synthetic_authoritative_stream_interrupts_cli_and_audits() -> None:
    interrupted: list[bool] = []
    loop = ApplicationSupervisorLoop(attempt_id="attempt-1", turn_id="turn-1")
    controller = AuthoritativeSupervisorController(
        loop=loop,
        backend="codex-cli",
        interrupt=lambda: interrupted.append(True),
        observe_authority_health=None,
    )
    stream = [
        observation(at, tool_name="browser_snapshot", tool_params={"ref": "same"})
        for at in (0.0, 0.1, 0.2, 0.3)
    ]

    decisions = [
        _apply_authoritative_supervisor_observation(loop, controller, event)
        for event in stream
    ]

    assert [decision.level for decision in decisions] == [0, 1, 3, 4]
    assert decisions[-1].reason_code == "ALREADY_PARKED"
    assert interrupted == [True]
    assert len(controller.interventions) == 2
    assert controller.interventions[-1]["reason_code"] == "STEER_UNSUPPORTED"
    assert controller.interventions[-1]["signals"]["tool_repeat_count"] == 3


def test_control_intent_is_persisted_before_interrupt_and_outcome_after() -> None:
    order: list[str] = []
    loop = ApplicationSupervisorLoop(attempt_id="attempt-1", turn_id="turn-1")
    decisions = [
        loop.observe(observation(at, tool_name="browser_snapshot"))
        for at in (0.0, 0.1, 0.2)
    ]
    controller = AuthoritativeSupervisorController(
        loop=loop,
        backend="codex-cli",
        interrupt=lambda: order.append("interrupt"),
        observe_authority_health=None,
        before_action=lambda _decision: order.append("intent"),
        after_action=lambda _decision, outcome: order.append(f"outcome:{outcome}"),
    )

    controller.apply(decisions[-1])

    assert order == ["intent", "interrupt", "outcome:runtime_interrupted"]


def test_failed_intent_persist_prevents_steer_or_interrupt() -> None:
    actions: list[str] = []
    loop = ApplicationSupervisorLoop(attempt_id="attempt-1", turn_id="turn-1")
    decision = None
    for at in (0.0, 0.1, 0.2):
        decision = loop.observe(observation(at, tool_name="browser_snapshot"))
    assert decision is not None and decision.level == 2
    controller = AuthoritativeSupervisorController(
        loop=loop,
        backend="codex-app-server",
        interrupt=lambda: actions.append("interrupt"),
        observe_authority_health=None,
        steer=lambda *_args: actions.append("steer"),
        before_action=lambda _decision: (_ for _ in ()).throw(
            RuntimeError("durable intent unavailable")
        ),
    )

    with pytest.raises(RuntimeError, match="durable intent unavailable"):
        controller.apply(decision)

    assert actions == []
