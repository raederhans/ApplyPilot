from __future__ import annotations

import math

import pytest

from applypilot.apply.performance_attribution import (
    SCHEMA_VERSION,
    attribution_snapshot,
    normalize_attribution,
    record_job_span,
    summarize_amplification,
)


def _job() -> dict[str, object]:
    return {
        "provider": "workday",
        "application_url": "https://careers.example.test/job/123",
        "_performance_application_index": 3,
    }


def test_no_submit_baseline_preserves_required_dimensions_and_nested_amplification() -> None:
    job = _job()
    for name, duration in (
        ("agent.turn", 100),
        ("agent.startup", 15),
        ("mcp.startup", 20),
        ("model.first_output", 30),
        ("model.first_tool_decision", 45),
        ("browser.prepare", 100),
        ("audit.pre_submit", 10),
        ("receipt.reconciliation", 5),
        ("recovery.agent", 25),
    ):
        record_job_span(job, name, duration)

    snapshot = attribution_snapshot(job)

    assert snapshot is not None
    assert snapshot["schema_version"] == SCHEMA_VERSION
    assert snapshot["measurement_model"] == "nested_advisory_spans"
    assert snapshot["dimensions"] == {
        "provider": "workday",
        "domain": "careers.example.test",
        "application_index": 3,
    }
    assert all(span["dimensions"] == snapshot["dimensions"] for span in snapshot["spans"])
    summary = summarize_amplification([snapshot])
    assert summary["primary_turn_ms"] == 100.0
    assert summary["groups"]["mcp"] == {
        "duration_ms": 20.0,
        "ratio_to_primary_turn": 0.2,
    }
    assert summary["groups"]["observation"] == {
        "duration_ms": 15.0,
        "ratio_to_primary_turn": 0.15,
    }
    assert summary["groups"]["recovery"] == {
        "duration_ms": 25.0,
        "ratio_to_primary_turn": 0.25,
    }


def test_invalid_or_unavailable_attribution_is_not_normalized_or_zero_filled() -> None:
    job = {"url": "not a URL"}
    record_job_span(job, "agent.turn", 1)
    snapshot = attribution_snapshot(job)

    assert snapshot is not None
    assert snapshot["dimensions"] == {
        "provider": "unavailable",
        "domain": "unavailable",
        "application_index": "unavailable",
    }
    assert normalize_attribution(snapshot) == snapshot
    assert normalize_attribution({"schema_version": SCHEMA_VERSION, "spans": []}) is None
    invalid = dict(snapshot)
    invalid["spans"] = [{"name": "agent.turn", "duration_ms": math.nan}]
    assert normalize_attribution(invalid) is None
    with pytest.raises(ValueError, match="unknown performance"):
        record_job_span(job, "submission.authority", 1)
