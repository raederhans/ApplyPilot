"""Focused post-submit evidence and historical-duplicate contracts."""

from __future__ import annotations

import pytest
from test_apply_runtime_contract import _run_worker_contract

from applypilot.apply import launcher
from applypilot.apply.worker_orchestration import (
    _observer_screenshot_name,
)


@pytest.mark.parametrize(
    ("observation", "expected"),
    [
        (
            {
                "confirmed": True,
                "receipt_visible": True,
                "confirmation_text": "Your application has been submitted",
            },
            "confirmed",
        ),
        (
            {
                "confirmed": False,
                "historical_duplicate": True,
                "historical_duplicate_text": "Your application was already submitted",
            },
            "historical_duplicate",
        ),
        (
            {
                "confirmed": False,
                "historical_duplicate_text": "You have already applied",
            },
            "historical_duplicate",
        ),
        (
            {
                "confirmed": True,
                "receipt_visible": True,
                "confirmation_text": "Your application has been submitted",
                "historical_duplicate": True,
                "historical_duplicate_text": "Your application was already submitted",
            },
            "conflicting_post_submit_status",
        ),
        (
            {
                "confirmed": True,
                "receipt_visible": True,
                "confirmation_text": "Your application has been submitted",
                "historical_duplicate_text": "Already applied? Learn how application history works.",
            },
            "confirmed",
        ),
        (
            {
                "confirmed": False,
                "historical_duplicate_text": (
                    "If you have already applied, sign in to view your status"
                ),
            },
            "uncertain",
        ),
        (
            {"confirmed": False, "form_visible": True, "submit_control_count": 1},
            "uncertain",
        ),
    ],
)
def test_post_submit_disposition_distinguishes_receipt_history_and_form(
    observation: dict, expected: str
) -> None:
    assert launcher._classify_post_submit_observation(observation) == expected


def test_historical_duplicate_is_permanent_and_never_repaired_or_counted(
    monkeypatch,
) -> None:
    result, phases, ledger, marked = _run_worker_contract(
        monkeypatch,
        observer_results=[
            {
                "confirmed": False,
                "historical_duplicate": True,
                "historical_duplicate_text": "Your application was already submitted",
                "current_url": "https://jobs.example.test/role/apply",
            }
        ],
    )

    assert result == (0, 0)
    assert phases == ["prepare", "submit"]
    assert ledger[0][0] == "already_applied"
    assert marked[0][0][1] == "already_applied"
    assert marked[0][1]["permanent"] is True
    evidence = marked[0][1]["evidence"]
    assert evidence["historical_duplicate"] is True
    assert evidence["observer"]["historical_duplicate"] is True


def test_observer_screenshot_names_are_semantic() -> None:
    assert _observer_screenshot_name(1) == "post-submit-observer.png"
    assert _observer_screenshot_name(2) == "post-submit-observer-attempt-2.png"
