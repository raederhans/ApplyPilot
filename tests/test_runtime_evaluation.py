from __future__ import annotations

from pathlib import Path

import pytest

from applypilot.apply.contracts import AgentRunRequest, AgentTurnResult
from applypilot.apply.runtime_evaluation import (
    METRIC_NAMES,
    REPLAY_EVALUATION_LABEL,
    EvaluationScenario,
    RuntimeRegistry,
    ScriptedReplayRuntime,
    compare_runtimes,
    replay_registry_from_fixture,
)

FIXTURE = Path(__file__).parent / "fixtures" / "runtime" / "scenarios.json"


def _ready_answer_mapping_observations() -> dict[str, object]:
    """Minimal browser-ready strict-v2 provenance envelope for replay results."""
    return {
        "answer_mappings": {
            "schema_version": "2",
            "adapter": "replay",
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
        }
    }


def test_same_fixture_compares_dynamic_runtimes_without_provider_benchmark_claim() -> None:
    registry, scenarios = replay_registry_from_fixture(FIXTURE)

    report = compare_runtimes(scenarios, registry)

    assert registry.names() == ("provider-a-replay", "provider-b-replay")
    assert len(report.evaluations) == len(scenarios) * len(registry.names())
    assert report.passed
    assert report.label == REPLAY_EVALUATION_LABEL
    assert all(item.label == REPLAY_EVALUATION_LABEL for item in report.evaluations)
    assert all(item.latency_source == "recorded_replay" for item in report.evaluations)
    assert all(set(item.metrics) == set(METRIC_NAMES) for item in report.evaluations)


def test_conflicting_structured_and_legacy_receipts_fail_closed_for_every_runtime() -> None:
    registry, scenarios = replay_registry_from_fixture(FIXTURE)
    conflict = next(item for item in scenarios if item.scenario_id == "submit-conflict-fails-closed")

    report = compare_runtimes([conflict], registry)

    assert report.passed
    assert all(item.result_source == "conflict" for item in report.evaluations)
    assert all(item.status == "submission_uncertain" for item in report.evaluations)
    assert all(not item.receipt_admitted for item in report.evaluations)


def test_registry_supports_factories_replacement_and_removal() -> None:
    request = AgentRunRequest(
        run_id="run-dynamic",
        attempt_id="attempt-dynamic",
        agent_role="test",
        phase="prepare",
        objective="Exercise a dynamically registered runtime",
    )

    def factory() -> ScriptedReplayRuntime:
        return ScriptedReplayRuntime(
            {
                request.run_id: {
                    "result": {
                        "run_id": request.run_id,
                        "status": "ready_to_submit",
                        "summary": "ready",
                        "observations": _ready_answer_mapping_observations(),
                    }
                }
            }
        )

    registry = RuntimeRegistry()
    registry.register("dynamic", factory)
    assert registry.get("dynamic").run(request).status == "ready_to_submit"
    with pytest.raises(ValueError, match="already registered"):
        registry.register("dynamic", factory)
    registry.register("dynamic", factory, replace=True)
    registry.unregister("dynamic")
    assert registry.names() == ()


def test_tool_side_effect_and_latency_regressions_are_independent_metrics() -> None:
    request = AgentRunRequest(
        run_id="run-regression",
        attempt_id="attempt-regression",
        agent_role="test",
        phase="prepare",
        objective="Detect replay regression",
        available_tools=("browser_snapshot",),
    )
    scenario = EvaluationScenario(
        scenario_id="regression",
        request=request,
        expected_status="ready_to_submit",
        expected_tools=("browser_snapshot",),
        expected_side_effects=("read",),
        max_latency_ms=10,
    )
    runtime = ScriptedReplayRuntime(
        {
            request.run_id: {
                "result": AgentTurnResult(
                    run_id=request.run_id,
                    status="ready_to_submit",
                    summary="contract-valid but behaviorally different",
                    observations={
                        **_ready_answer_mapping_observations(),
                        "tool_calls": [
                            {"name": "browser_click", "side_effect": "write"}
                        ]
                    },
                ),
                "latency_ms": 11,
            }
        }
    )
    registry = RuntimeRegistry()
    registry.register("regressed", runtime)

    evaluation = compare_runtimes([scenario], registry).evaluations[0]

    assert evaluation.metrics["contract"]
    assert evaluation.metrics["status"]
    assert evaluation.metrics["receipt"]
    assert evaluation.metrics["conflict"]
    assert not evaluation.metrics["tool"]
    assert not evaluation.metrics["side_effect"]
    assert not evaluation.metrics["latency"]
    assert not evaluation.passed


def test_replay_runtime_cancel_is_local_and_has_no_external_recovery() -> None:
    request = AgentRunRequest(
        run_id="run-cancelled",
        attempt_id="attempt-cancelled",
        agent_role="test",
        phase="prepare",
        objective="Remain offline",
    )
    runtime = ScriptedReplayRuntime(
        {
            request.run_id: {
                "result": {
                    "run_id": request.run_id,
                    "status": "ready_to_submit",
                    "summary": "unused",
                }
            }
        }
    )

    runtime.cancel(request.run_id)

    with pytest.raises(RuntimeError, match="cancelled"):
        runtime.run(request)
