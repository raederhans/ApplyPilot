"""Pure Workday state-machine tests; no browser, profile, or ledger is used."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from applypilot.apply.workday_state import (
    BoundedPageObservation,
    ControlKind,
    ProgressAction,
    WorkdayPageKind,
    WorkdayState,
    evaluate_page_progress,
    observation_from_mapping,
    page_signature,
    require_transition,
    resolve_post_submit,
    runtime_switch_allowed,
    state_for_observation,
    transition_allowed,
)

FIXTURE = Path(__file__).parent / "fixtures" / "apply" / "workday_observations.json"


def _observation(**changes: object) -> BoundedPageObservation:
    values: dict[str, object] = {
        "page_kind": WorkdayPageKind.MY_INFORMATION,
        "step_index": 2,
        "step_count": 4,
        "visible_controls": (ControlKind.TEXT, ControlKind.SELECT),
        "required_count": 2,
        "has_next": True,
    }
    values.update(changes)
    return BoundedPageObservation(**values)  # type: ignore[arg-type]


def test_fixture_matrix_classifies_workday_page_kinds() -> None:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))

    observed = {
        row["id"]: state_for_observation(observation_from_mapping(row["observation"])).value
        for row in rows
    }

    assert observed == {row["id"]: row["state"] for row in rows}


def test_observation_is_bounded_and_never_accepts_values_pii_handles_or_secrets() -> None:
    safe = _observation().as_dict()
    rendered = json.dumps(safe, sort_keys=True)

    assert set(safe) == {
        "page_kind",
        "step_index",
        "step_count",
        "visible_controls",
        "required_count",
        "invalid_count",
        "has_next",
        "has_review",
        "has_submit",
        "has_confirmation",
        "has_manual_gate",
        "repairable_validation",
    }
    assert "value" not in rendered
    assert "label" not in rendered
    assert "handle" not in rendered

    for forbidden in ("field_values", "email_address", "page_handle", "password", "token", "url"):
        with pytest.raises(ValueError, match="unsupported observation fields"):
            observation_from_mapping({"page_kind": "review", forbidden: "must-not-survive"})


def test_observation_rejects_unbounded_or_inconsistent_structure() -> None:
    with pytest.raises(ValueError, match="at most 128"):
        _observation(visible_controls=(ControlKind.TEXT,) * 129)
    with pytest.raises(TypeError, match="immutable tuple"):
        _observation(visible_controls=[ControlKind.TEXT])
    with pytest.raises(TypeError, match="has_next must be a boolean"):
        _observation(has_next=1)
    with pytest.raises(ValueError, match="supplied together"):
        BoundedPageObservation(WorkdayPageKind.REVIEW, step_index=1)
    with pytest.raises(ValueError, match="requires at least one"):
        _observation(repairable_validation=True)


def test_page_signature_is_stable_and_changes_with_structure_not_values() -> None:
    observation = _observation()

    assert page_signature(observation) == page_signature(_observation())
    assert page_signature(observation) != page_signature(_observation(required_count=1))
    assert len(page_signature(observation)) == 24


def test_transition_guard_rejects_skips_and_terminal_reentry() -> None:
    assert transition_allowed(WorkdayState.START, WorkdayState.UPLOAD)
    assert transition_allowed(WorkdayState.UPLOAD, WorkdayState.FORM)
    assert transition_allowed(WorkdayState.FORM, WorkdayState.REVIEW)
    assert transition_allowed(WorkdayState.REVIEW, WorkdayState.SUBMIT_STARTED)
    assert not transition_allowed(WorkdayState.FORM, WorkdayState.APPLIED)
    assert not transition_allowed(WorkdayState.APPLIED, WorkdayState.FORM)
    assert not transition_allowed(WorkdayState.SUBMIT_STARTED, WorkdayState.FORM)
    assert transition_allowed(
        WorkdayState.SUBMIT_STARTED,
        WorkdayState.FORM,
        repair_authorized=True,
    )

    with pytest.raises(ValueError, match="illegal Workday transition"):
        require_transition(WorkdayState.FORM, WorkdayState.APPLIED)


def test_repeated_page_allows_exactly_one_repair_before_stuck() -> None:
    observation = _observation()
    signature = page_signature(observation)

    first_repeat = evaluate_page_progress(signature, observation, repair_used=False)
    second_repeat = evaluate_page_progress(signature, observation, repair_used=True)

    assert (first_repeat.action, first_repeat.repair_used) == (ProgressAction.REPAIR_ONCE, True)
    assert (second_repeat.action, second_repeat.state) == (
        ProgressAction.STOP_STUCK,
        WorkdayState.FAILED_STUCK,
    )


def test_real_submit_confirmation_is_applied_without_runtime_switch() -> None:
    decision = resolve_post_submit(
        BoundedPageObservation(
            WorkdayPageKind.CONFIRMATION,
            has_confirmation=True,
        ),
        repair_used=False,
        runtime_switch_requested=True,
    )

    assert decision.state is WorkdayState.APPLIED
    assert decision.runtime_switch_allowed is False
    assert runtime_switch_allowed(submit_started=False)
    assert not runtime_switch_allowed(submit_started=True)


def test_real_submit_without_decisive_confirmation_is_uncertain() -> None:
    decision = resolve_post_submit(
        BoundedPageObservation(WorkdayPageKind.REVIEW, has_submit=True),
        repair_used=False,
    )

    assert decision.action is ProgressAction.MARK_UNCERTAIN
    assert decision.state is WorkdayState.SUBMISSION_UNCERTAIN
    assert decision.runtime_switch_allowed is False


def test_post_submit_repair_requires_visible_ordinary_validation_and_is_single_use() -> None:
    validation = _observation(
        page_kind=WorkdayPageKind.VALIDATION_ERROR,
        invalid_count=1,
        repairable_validation=True,
        has_next=False,
        has_submit=True,
    )

    first = resolve_post_submit(validation, repair_used=False)
    second = resolve_post_submit(validation, repair_used=True)

    assert (first.action, first.state, first.repair_used) == (
        ProgressAction.REPAIR_ONCE,
        WorkdayState.FORM,
        True,
    )
    assert (second.action, second.state) == (
        ProgressAction.STOP_VALIDATION,
        WorkdayState.FAILED_VALIDATION,
    )


def test_post_submit_manual_gate_is_uncertain_not_a_second_click() -> None:
    decision = resolve_post_submit(
        BoundedPageObservation(
            WorkdayPageKind.MANUAL_GATE,
            has_manual_gate=True,
        ),
        repair_used=False,
    )

    assert decision.action is ProgressAction.MARK_UNCERTAIN
    assert decision.state is WorkdayState.SUBMISSION_UNCERTAIN
    assert decision.repair_used is False
