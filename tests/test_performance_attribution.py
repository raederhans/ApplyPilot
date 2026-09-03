from __future__ import annotations

import math

import pytest

from applypilot.apply.full_stack_no_submit_benchmark import (
    CohortMetricCollector,
    _attribution_summary,
)
from applypilot.apply.p4_no_submit_worker import (
    AGENT_RUNTIME_SPANS,
    ATTRIBUTION_SCHEMA_VERSION,
    TaskAttribution,
)


def test_task_attribution_reports_conserved_coverage_and_unavailable_runtime_spans() -> None:
    attribution = TaskAttribution(required_spans=("control_inspection_ms", "semantic_action_ms"))
    attribution.record("control_inspection_ms", 40)
    attribution.record("semantic_action_ms", 50)

    snapshot = attribution.snapshot(wall_clock_ms=100)

    assert snapshot == {
        "schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "spans_ms": {"control_inspection_ms": 40.0, "semantic_action_ms": 50.0},
        "attributed_wall_clock_ms": 90.0,
        "unattributed_wall_clock_ms": 10.0,
        "attribution_coverage_ratio": 0.9,
        "missing_required_spans": [],
        "unavailable_spans": list(AGENT_RUNTIME_SPANS),
        "attribution_complete": True,
    }


def test_task_attribution_fails_closed_for_unknown_invalid_and_overlapping_spans() -> None:
    with pytest.raises(ValueError, match="unknown required"):
        TaskAttribution(required_spans=("made_up_ms",))

    attribution = TaskAttribution(required_spans=("control_inspection_ms",))
    for invalid in (-1, math.nan, math.inf):
        with pytest.raises(ValueError, match="finite and non-negative"):
            attribution.record("control_inspection_ms", invalid)
    with pytest.raises(ValueError, match="unknown attribution span"):
        attribution.record("made_up_ms", 1)
    attribution.record("control_inspection_ms", 2)
    with pytest.raises(ValueError, match="exceed task wall clock"):
        attribution.snapshot(wall_clock_ms=1)

    with (
        attribution.measure("control_inspection_ms"),
        pytest.raises(RuntimeError, match="cannot overlap"),
        attribution.measure("semantic_action_ms"),
    ):
        pass


def test_attribution_summary_keeps_missing_evidence_incomplete() -> None:
    complete = TaskAttribution(required_spans=("control_inspection_ms",))
    complete.record("control_inspection_ms", 95)
    incomplete = TaskAttribution(required_spans=("semantic_action_ms",))
    incomplete.record("control_inspection_ms", 95)

    summary = _attribution_summary(
        (
            {"performance_attribution": complete.snapshot(wall_clock_ms=100)},
            {"performance_attribution": incomplete.snapshot(wall_clock_ms=100)},
            {},
        )
    )

    assert summary["task_count"] == 3
    assert summary["complete_tasks"] == 1
    assert summary["all_tasks_complete"] is False
    assert summary["coverage_ratio_min"] == 0.0
    assert summary["phase_p50_ms"]["control_inspection_ms"] == 95.0
    assert summary["unavailable_spans"] == sorted(AGENT_RUNTIME_SPANS)


def test_worker_lifecycle_collector_validates_and_sorts_spans() -> None:
    collector = CohortMetricCollector()
    collector.record_worker_span(1, "browser_launch_ms", 12.5)
    collector.record_worker_span(0, "playwright_start_ms", 3.25)

    assert collector.snapshot()["worker_lifecycle_spans_ms"] == {
        "0": {"playwright_start_ms": 3.25},
        "1": {"browser_launch_ms": 12.5},
    }
    with pytest.raises(ValueError, match="unknown worker"):
        collector.record_worker_span(0, "unknown_ms", 1)
    with pytest.raises(ValueError, match="finite and non-negative"):
        collector.record_worker_span(0, "browser_close_ms", math.nan)
