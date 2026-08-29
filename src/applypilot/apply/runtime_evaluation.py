"""Provider-neutral, side-effect-free runtime replay evaluation.

This harness evaluates contract conformance from recorded or scripted turns.  It
does not open a browser, access the network, submit an application, or benchmark
a live provider.  In particular, replay latency is fixture metadata rather than
a measurement of provider performance.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from applypilot.apply.agent_output import reconcile_agent_turn_outputs
from applypilot.apply.contracts import (
    AgentRunRequest,
    AgentRuntime,
    AgentTurnResult,
    agent_turn_result_from_mapping,
)
from applypilot.apply.orchestration import plan_proposal_waves
from applypilot.apply.router import ControlRoute, prompt_control_contract

REPLAY_EVALUATION_LABEL = "replay_conformance_only_not_a_real_provider_benchmark"
METRIC_NAMES = (
    "contract",
    "status",
    "receipt",
    "conflict",
    "tool",
    "side_effect",
    "latency",
)


@dataclass(frozen=True, slots=True)
class ReplayTurn:
    """One recorded turn consumed by :class:`ScriptedReplayRuntime`."""

    result: AgentTurnResult | Mapping[str, object]
    legacy_output: str = ""
    latency_ms: float = 0.0

    def __post_init__(self) -> None:
        if self.latency_ms < 0:
            raise ValueError("latency_ms must be non-negative")


class ScriptedReplayRuntime:
    """Deterministic ``AgentRuntime`` backed by recorded, in-memory turns."""

    evaluation_label = REPLAY_EVALUATION_LABEL
    latency_source = "recorded_replay"

    def __init__(
        self,
        turns: Mapping[str, ReplayTurn | Mapping[str, object]],
        *,
        name: str = "scripted-replay",
    ) -> None:
        self.name = name
        self._turns = {run_id: self._coerce_turn(value) for run_id, value in turns.items()}
        self._cancelled: set[str] = set()
        self._last_run_id: str | None = None

    @staticmethod
    def _coerce_turn(value: ReplayTurn | Mapping[str, object]) -> ReplayTurn:
        if isinstance(value, ReplayTurn):
            return value
        result = value.get("result")
        if not isinstance(result, (AgentTurnResult, Mapping)):
            raise TypeError("a replay turn requires an AgentTurnResult or result object")
        return ReplayTurn(
            result=result,
            legacy_output=str(value.get("legacy_output") or ""),
            latency_ms=float(value.get("latency_ms") or 0.0),
        )

    def _turn(self, run_id: str) -> ReplayTurn:
        if run_id in self._cancelled:
            raise RuntimeError(f"run is cancelled: {run_id}")
        try:
            return self._turns[run_id]
        except KeyError as exc:
            raise KeyError(f"no scripted turn for run_id: {run_id}") from exc

    def run(self, request: AgentRunRequest) -> AgentTurnResult:
        turn = self._turn(request.run_id)
        self._last_run_id = request.run_id
        if isinstance(turn.result, AgentTurnResult):
            if turn.result.run_id != request.run_id:
                raise ValueError("scripted result run_id does not match the request")
            return turn.result
        return agent_turn_result_from_mapping(turn.result, expected_run_id=request.run_id)

    def resume(self, run_id: str, human_input: Mapping[str, object]) -> AgentTurnResult:
        del human_input
        turn = self._turn(run_id)
        self._last_run_id = run_id
        if isinstance(turn.result, AgentTurnResult):
            return turn.result
        return agent_turn_result_from_mapping(turn.result, expected_run_id=run_id)

    def cancel(self, run_id: str) -> None:
        self._cancelled.add(run_id)

    def legacy_output(self, run_id: str) -> str:
        return self._turn(run_id).legacy_output

    def recorded_latency_ms(self, run_id: str) -> float:
        return self._turn(run_id).latency_ms


RuntimeFactory = Callable[[], AgentRuntime]


class RuntimeRegistry:
    """Mutable registry for runtime instances or zero-argument factories."""

    def __init__(self) -> None:
        self._items: dict[str, AgentRuntime | RuntimeFactory] = {}

    def register(
        self,
        name: str,
        runtime: AgentRuntime | RuntimeFactory,
        *,
        replace: bool = False,
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("runtime name is required")
        if name in self._items and not replace:
            raise ValueError(f"runtime already registered: {name}")
        if not callable(runtime) and not isinstance(runtime, AgentRuntime):
            raise TypeError("runtime must implement AgentRuntime or be a factory")
        self._items[name] = runtime

    def unregister(self, name: str) -> None:
        if name not in self._items:
            raise KeyError(f"runtime is not registered: {name}")
        del self._items[name]

    def names(self) -> tuple[str, ...]:
        return tuple(self._items)

    def get(self, name: str) -> AgentRuntime:
        try:
            registered = self._items[name]
        except KeyError as exc:
            raise KeyError(f"runtime is not registered: {name}") from exc
        runtime = registered() if callable(registered) and not isinstance(registered, AgentRuntime) else registered
        if not isinstance(runtime, AgentRuntime):
            raise TypeError(f"registered factory did not return an AgentRuntime: {name}")
        return cast(AgentRuntime, runtime)


@dataclass(frozen=True, slots=True)
class EvaluationScenario:
    """One fixture applied unchanged to every registered runtime."""

    scenario_id: str
    request: AgentRunRequest
    expected_status: str
    dry_run: bool = False
    expected_receipt: bool = False
    expected_conflict: bool = False
    expected_tools: tuple[str, ...] = ()
    expected_side_effects: tuple[str, ...] = ()
    max_latency_ms: float | None = None

    def __post_init__(self) -> None:
        if not self.scenario_id.strip():
            raise ValueError("scenario_id is required")
        if not self.expected_status.strip():
            raise ValueError("expected_status is required")
        if self.max_latency_ms is not None and self.max_latency_ms < 0:
            raise ValueError("max_latency_ms must be non-negative")


@dataclass(frozen=True, slots=True)
class RuntimeEvaluation:
    scenario_id: str
    runtime_name: str
    label: str
    metrics: Mapping[str, bool]
    status: str
    result_source: str
    receipt_admitted: bool
    observed_tools: tuple[str, ...]
    observed_side_effects: tuple[str, ...]
    latency_ms: float | None
    latency_source: str
    error: str | None = None

    @property
    def passed(self) -> bool:
        return all(self.metrics.get(name) is True for name in METRIC_NAMES)


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    label: str
    evaluations: tuple[RuntimeEvaluation, ...]

    @property
    def passed(self) -> bool:
        return bool(self.evaluations) and all(item.passed for item in self.evaluations)

    def scorecard(self) -> dict[str, dict[str, int]]:
        scorecard: dict[str, dict[str, int]] = {}
        for item in self.evaluations:
            runtime = scorecard.setdefault(item.runtime_name, {name: 0 for name in METRIC_NAMES})
            for name in METRIC_NAMES:
                runtime[name] += int(item.metrics.get(name) is True)
        return scorecard


def _strings(value: object, *, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise TypeError(f"{name} must be an array of strings")
    return tuple(value)


def _request_from_mapping(value: Mapping[str, object]) -> AgentRunRequest:
    context = value.get("context") or {}
    if not isinstance(context, Mapping):
        raise TypeError("request context must be an object")
    copied_context = dict(context)
    route_raw = value.get("control_route")
    if route_raw is not None:
        if not isinstance(route_raw, Mapping):
            raise TypeError("control_route must be an object")
        route = ControlRoute(**dict(route_raw))
        copied_context["control_contract"] = prompt_control_contract(
            route,
            interaction_mode=str(value.get("interaction_mode") or "playwright"),
            resume_existing_page=value.get("resume_existing_page") is True,
        )
    return AgentRunRequest(
        run_id=str(value.get("run_id") or ""),
        attempt_id=str(value.get("attempt_id") or ""),
        agent_role=str(value.get("agent_role") or "runtime-evaluator"),
        phase=str(value.get("phase") or ""),
        objective=str(value.get("objective") or "Evaluate a recorded agent turn"),
        context=copied_context,
        available_tools=_strings(value.get("available_tools"), name="available_tools"),
        parent_run_id=None if value.get("parent_run_id") is None else str(value["parent_run_id"]),
        proposal_group_id=(
            None if value.get("proposal_group_id") is None else str(value["proposal_group_id"])
        ),
        concurrency_mode=str(value.get("concurrency_mode") or "adaptive"),
    )


def scenario_from_mapping(value: Mapping[str, object]) -> EvaluationScenario:
    request = value.get("request")
    expected = value.get("expected")
    if not isinstance(request, Mapping) or not isinstance(expected, Mapping):
        raise TypeError("a scenario requires request and expected objects")
    max_latency = expected.get("max_latency_ms")
    return EvaluationScenario(
        scenario_id=str(value.get("scenario_id") or ""),
        request=_request_from_mapping(request),
        expected_status=str(expected.get("status") or ""),
        dry_run=value.get("dry_run") is True,
        expected_receipt=expected.get("receipt") is True,
        expected_conflict=expected.get("conflict") is True,
        expected_tools=_strings(expected.get("tools"), name="expected.tools"),
        expected_side_effects=_strings(
            expected.get("side_effects"), name="expected.side_effects"
        ),
        max_latency_ms=None if max_latency is None else float(max_latency),
    )


def load_scenarios(path: Path) -> tuple[EvaluationScenario, ...]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    scenarios = raw.get("scenarios") if isinstance(raw, Mapping) else raw
    if not isinstance(scenarios, list):
        raise TypeError("runtime fixture must contain a scenarios array")
    result = []
    for value in scenarios:
        if not isinstance(value, Mapping):
            raise TypeError("each runtime scenario must be an object")
        result.append(scenario_from_mapping(value))
    return tuple(result)


def _replay_metadata(runtime: AgentRuntime, run_id: str) -> tuple[str, float | None, str]:
    output_reader = getattr(runtime, "legacy_output", None)
    latency_reader = getattr(runtime, "recorded_latency_ms", None)
    output = str(output_reader(run_id)) if callable(output_reader) else ""
    latency = float(latency_reader(run_id)) if callable(latency_reader) else None
    latency_source = "recorded_replay" if latency is not None else "not_recorded"
    return output, latency, latency_source


def _observed_activity(result: AgentTurnResult) -> tuple[tuple[str, ...], tuple[str, ...]]:
    raw_calls = result.observations.get("tool_calls") or ()
    if not isinstance(raw_calls, (list, tuple)):
        return (), ()
    tools: list[str] = []
    derived_effects: list[str] = []
    for call in raw_calls:
        if isinstance(call, str):
            tools.append(call)
        elif isinstance(call, Mapping):
            if call.get("name"):
                tools.append(str(call["name"]))
            if call.get("side_effect"):
                derived_effects.append(str(call["side_effect"]))
    raw_effects = result.observations.get("side_effects") or derived_effects
    effects = (
        tuple(str(item) for item in raw_effects)
        if isinstance(raw_effects, (list, tuple))
        else ()
    )
    return tuple(tools), effects


def evaluate_scenario(
    scenario: EvaluationScenario,
    *,
    runtime_name: str,
    runtime: AgentRuntime,
) -> RuntimeEvaluation:
    metrics = {name: False for name in METRIC_NAMES}
    status = "failed:evaluation_error"
    source = "none"
    receipt = False
    tools: tuple[str, ...] = ()
    side_effects: tuple[str, ...] = ()
    latency: float | None = None
    latency_source = "not_recorded"
    error: str | None = None
    try:
        result = runtime.run(scenario.request)
        if not isinstance(result, AgentTurnResult):
            raise TypeError("runtime did not return AgentTurnResult")
        if result.run_id != scenario.request.run_id:
            raise ValueError("runtime result run_id does not match request")
        plan_proposal_waves(result.proposals)
        metrics["contract"] = True

        legacy_output, latency, latency_source = _replay_metadata(
            runtime, scenario.request.run_id
        )
        status, evidence, source = reconcile_agent_turn_outputs(
            legacy_output,
            result,
            dry_run=scenario.dry_run,
            submission_phase=scenario.request.phase,
        )
        receipt = evidence is not None
        tools, side_effects = _observed_activity(result)
        metrics["status"] = status == scenario.expected_status
        metrics["receipt"] = receipt is scenario.expected_receipt
        metrics["conflict"] = (source == "conflict") is scenario.expected_conflict
        metrics["tool"] = (
            tools == scenario.expected_tools
            and set(tools) <= set(scenario.request.available_tools)
        )
        metrics["side_effect"] = side_effects == scenario.expected_side_effects
        metrics["latency"] = (
            scenario.max_latency_ms is None
            or (latency is not None and latency <= scenario.max_latency_ms)
        )
    except (KeyError, RuntimeError, TypeError, ValueError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    return RuntimeEvaluation(
        scenario_id=scenario.scenario_id,
        runtime_name=runtime_name,
        label=REPLAY_EVALUATION_LABEL,
        metrics=metrics,
        status=status,
        result_source=source,
        receipt_admitted=receipt,
        observed_tools=tools,
        observed_side_effects=side_effects,
        latency_ms=latency,
        latency_source=latency_source,
        error=error,
    )


def compare_runtimes(
    scenarios: Iterable[EvaluationScenario],
    registry: RuntimeRegistry,
) -> EvaluationReport:
    """Evaluate each registered runtime against the same immutable scenarios."""
    frozen_scenarios = tuple(scenarios)
    evaluations = tuple(
        evaluate_scenario(scenario, runtime_name=name, runtime=registry.get(name))
        for scenario in frozen_scenarios
        for name in registry.names()
    )
    return EvaluationReport(label=REPLAY_EVALUATION_LABEL, evaluations=evaluations)


def replay_registry_from_fixture(path: Path) -> tuple[RuntimeRegistry, tuple[EvaluationScenario, ...]]:
    """Load test-only replay runtimes and their shared scenarios from JSON."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise TypeError("runtime fixture must be an object")
    scenarios = load_scenarios(path)
    runtimes_raw = raw.get("runtimes")
    if not isinstance(runtimes_raw, Mapping):
        raise TypeError("runtime fixture must contain a runtimes object")
    registry = RuntimeRegistry()
    for runtime_name, turns in runtimes_raw.items():
        if not isinstance(runtime_name, str) or not isinstance(turns, Mapping):
            raise TypeError("fixture runtimes must map names to scripted turns")
        registry.register(
            runtime_name,
            ScriptedReplayRuntime(turns, name=runtime_name),
        )
    return registry, scenarios
