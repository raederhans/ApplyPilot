"""Small provider-neutral scheduler for specialist Agent proposals.

The current browser worker remains the default single Agent.  This module lets
future runtimes fan out independent analysis proposals while keeping dependent
or same-resource work ordered.  Concurrency modes are conventions supplied by
the caller, not a closed policy enum.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Collection, Iterable, Mapping, MutableMapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypeVar

from applypilot.apply.contracts import AgentProposal, TaskResult, TaskSpec

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class TaskExecutionContext:
    """Cooperative cancellation/deadline view passed to task runners."""

    deadline_at: datetime | None
    cancel_events: tuple[threading.Event, ...]
    attempt: int
    dependency_results: Mapping[str, TaskResult]
    reduced_state: Mapping[str, object]

    def cancelled(self) -> bool:
        if any(event.is_set() for event in self.cancel_events):
            return True
        return self.deadline_at is not None and datetime.now(UTC) >= self.deadline_at

    def remaining_seconds(self) -> float | None:
        if self.deadline_at is None:
            return None
        return max(0.0, (self.deadline_at - datetime.now(UTC)).total_seconds())


@dataclass(frozen=True, slots=True)
class CoordinatorOutcome:
    """All durable results plus the reducer-owned workflow state."""

    results: Mapping[str, TaskResult]
    reduced_state: Mapping[str, object]
    attempts: Mapping[str, int]
    target_reached: bool


def _terminal_result(task: TaskSpec, status: str, category: str) -> TaskResult:
    return TaskResult(
        task_id=task.task_id,
        status=status,
        failure_category=category,
        retryable=False,
        resume_cursor=task.resume_cursor,
    )


def execute_task_graph(
    tasks: Iterable[TaskSpec],
    runner: Callable[[TaskSpec, TaskExecutionContext], TaskResult],
    reducer: Callable[[MutableMapping[str, object], TaskSpec, TaskResult], None],
    *,
    max_workers: int = 1,
    resource_capacities: Mapping[str, int] | None = None,
    initial_state: Mapping[str, object] | None = None,
    cancel_event: threading.Event | None = None,
    target_successes: int | None = None,
) -> CoordinatorOutcome:
    """Run a recoverable task DAG with dependencies and capacity claims.

    Ready tasks share one executor, so an idle worker immediately takes the
    next compatible task instead of owning a static quota.  Every terminal
    result is synchronously passed through ``reducer`` before dependants may
    start; specialist output therefore cannot become an unconsumed side run.

    Cancellation and deadlines are cooperative for already-running work.  The
    coordinator never retries a task after its deadline and retries only a
    result explicitly marked ``retryable``.
    """
    if max_workers < 1:
        raise ValueError("max_workers must be positive")
    if target_successes is not None and target_successes < 1:
        raise ValueError("target_successes must be positive")
    ordered = list(tasks)
    by_id = {task.task_id: task for task in ordered}
    if len(by_id) != len(ordered):
        raise ValueError("task_id values must be unique")
    for task in ordered:
        missing = set(task.depends_on) - by_id.keys()
        if missing:
            raise ValueError(f"task {task.task_id} has unknown dependencies: {sorted(missing)}")

    # Validate the graph before starting work, so a cycle cannot leave a
    # partially executed workflow.
    unresolved = {task.task_id: set(task.depends_on) for task in ordered}
    visited: set[str] = set()
    while unresolved:
        ready_ids = [task_id for task_id, deps in unresolved.items() if deps <= visited]
        if not ready_ids:
            raise ValueError("task dependencies contain a cycle")
        visited.update(ready_ids)
        for task_id in ready_ids:
            unresolved.pop(task_id)

    capacities = {str(key): int(value) for key, value in (resource_capacities or {}).items()}
    if any(value < 1 for value in capacities.values()):
        raise ValueError("resource capacities must be positive")
    for task in ordered:
        for claim in task.resource_claims:
            if claim.units > capacities.get(claim.key, 1):
                raise ValueError(
                    f"task {task.task_id} claims {claim.units} units of {claim.key}, "
                    f"but capacity is {capacities.get(claim.key, 1)}"
                )

    state: dict[str, object] = dict(initial_state or {})
    results: dict[str, TaskResult] = {}
    attempts: dict[str, int] = {task.task_id: 0 for task in ordered}
    pending = set(by_id)
    in_use: dict[str, int] = {}
    target_count = 0
    target_stop = threading.Event()
    external_stop = cancel_event or threading.Event()

    def resources_available(task: TaskSpec) -> bool:
        return all(
            in_use.get(claim.key, 0) + claim.units <= capacities.get(claim.key, 1)
            for claim in task.resource_claims
        )

    def reserve(task: TaskSpec) -> None:
        for claim in task.resource_claims:
            in_use[claim.key] = in_use.get(claim.key, 0) + claim.units

    def release(task: TaskSpec) -> None:
        for claim in task.resource_claims:
            remaining = in_use.get(claim.key, 0) - claim.units
            if remaining > 0:
                in_use[claim.key] = remaining
            else:
                in_use.pop(claim.key, None)

    def consume(task: TaskSpec, result: TaskResult) -> None:
        nonlocal target_count
        if result.task_id != task.task_id:
            raise ValueError(
                f"runner returned result for {result.task_id}, expected {task.task_id}"
            )
        results[task.task_id] = result
        reducer(state, task, result)
        if task.counts_toward_target and result.succeeded:
            target_count += 1
            if target_successes is not None and target_count >= target_successes:
                target_stop.set()

    def call_runner(task: TaskSpec, context: TaskExecutionContext) -> TaskResult:
        try:
            return runner(task, context)
        except Exception as exc:  # noqa: BLE001 - provider boundary is normalized
            return TaskResult(
                task_id=task.task_id,
                status="failed",
                output={"error_type": type(exc).__name__, "error": str(exc)[:500]},
                failure_category="runner_exception",
                retryable=True,
                resume_cursor=task.resume_cursor,
            )

    running: dict[Future[TaskResult], TaskSpec] = {}
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="apply-task") as executor:
        while pending or running:
            made_progress = False

            # Convert tasks that can no longer run into reducer-visible results.
            for task_id in list(pending):
                task = by_id[task_id]
                if target_stop.is_set():
                    pending.remove(task_id)
                    consume(task, _terminal_result(task, "cancelled", "target_reached"))
                    made_progress = True
                    continue
                if external_stop.is_set():
                    pending.remove(task_id)
                    consume(task, _terminal_result(task, "cancelled", "cancelled"))
                    made_progress = True
                    continue
                if task.deadline_at is not None and datetime.now(UTC) >= task.deadline_at:
                    pending.remove(task_id)
                    consume(task, _terminal_result(task, "timed_out", "deadline_exceeded"))
                    made_progress = True
                    continue
                if not set(task.depends_on) <= results.keys():
                    continue
                failed_required = tuple(
                    dep for dep in task.required_results if not results[dep].succeeded
                )
                if failed_required:
                    pending.remove(task_id)
                    consume(
                        task,
                        TaskResult(
                            task_id=task.task_id,
                            status="blocked",
                            output={"blocked_by": list(failed_required)},
                            failure_category="required_result_failed",
                            resume_cursor=task.resume_cursor,
                        ),
                    )
                    made_progress = True

            ready = [
                by_id[task_id]
                for task_id in pending
                if set(by_id[task_id].depends_on) <= results.keys()
                and resources_available(by_id[task_id])
            ]
            original_index = {task.task_id: index for index, task in enumerate(ordered)}
            ready.sort(key=lambda task: (-task.priority, original_index[task.task_id]))
            for task in ready:
                if len(running) >= max_workers or target_stop.is_set() or external_stop.is_set():
                    break
                if not resources_available(task):
                    continue
                in_flight_target = sum(
                    int(running_task.counts_toward_target)
                    for running_task in running.values()
                )
                if (
                    task.counts_toward_target
                    and target_successes is not None
                    and target_count + in_flight_target >= target_successes
                ):
                    continue
                pending.remove(task.task_id)
                attempts[task.task_id] += 1
                reserve(task)
                context = TaskExecutionContext(
                    deadline_at=task.deadline_at,
                    cancel_events=(external_stop, target_stop),
                    attempt=attempts[task.task_id],
                    dependency_results={dep: results[dep] for dep in task.depends_on},
                    reduced_state=dict(state),
                )
                running[executor.submit(call_runner, task, context)] = task
                made_progress = True

            if not running:
                if pending and not made_progress:
                    raise RuntimeError("no task can make progress with the configured resources")
                continue

            done, _ = wait(tuple(running), return_when=FIRST_COMPLETED)
            for future in done:
                task = running.pop(future)
                release(task)
                result = future.result()
                can_retry = (
                    result.retryable
                    and attempts[task.task_id] <= task.retry_budget
                    and not external_stop.is_set()
                    and not target_stop.is_set()
                    and (task.deadline_at is None or datetime.now(UTC) < task.deadline_at)
                )
                if can_retry:
                    pending.add(task.task_id)
                else:
                    consume(task, result)

    return CoordinatorOutcome(
        results=dict(results),
        reduced_state=dict(state),
        attempts=dict(attempts),
        target_reached=target_stop.is_set(),
    )


def plan_proposal_waves(
    proposals: Iterable[AgentProposal],
    *,
    parallel_modes: Collection[str] = ("parallel", "parallel_safe", "adaptive"),
    can_share_wave: Callable[[AgentProposal, AgentProposal], bool] | None = None,
) -> tuple[tuple[AgentProposal, ...], ...]:
    """Topologically group proposals into configurable serial/parallel waves."""
    ordered = list(proposals)
    by_id = {proposal.proposal_id: proposal for proposal in ordered}
    if len(by_id) != len(ordered):
        raise ValueError("proposal_id values must be unique")
    for proposal in ordered:
        missing = set(proposal.depends_on) - by_id.keys()
        if missing:
            raise ValueError(
                f"proposal {proposal.proposal_id} has unknown dependencies: {sorted(missing)}"
            )

    mode_set = {str(mode).casefold() for mode in parallel_modes}
    original_index = {proposal.proposal_id: index for index, proposal in enumerate(ordered)}
    remaining = set(by_id)
    completed: set[str] = set()
    waves: list[tuple[AgentProposal, ...]] = []

    def compatible(left: AgentProposal, right: AgentProposal) -> bool:
        if left.concurrency_mode.casefold() not in mode_set:
            return False
        if right.concurrency_mode.casefold() not in mode_set:
            return False
        if (
            left.concurrency_key
            and right.concurrency_key
            and left.concurrency_key == right.concurrency_key
        ):
            return False
        return can_share_wave(left, right) if can_share_wave is not None else True

    while remaining:
        ready = [
            by_id[proposal_id]
            for proposal_id in remaining
            if set(by_id[proposal_id].depends_on) <= completed
        ]
        if not ready:
            raise ValueError("proposal dependencies contain a cycle")
        ready.sort(key=lambda item: (-item.priority, original_index[item.proposal_id]))

        layer_waves: list[list[AgentProposal]] = []
        for proposal in ready:
            placed = False
            for wave in layer_waves:
                if all(compatible(proposal, peer) for peer in wave):
                    wave.append(proposal)
                    placed = True
                    break
            if not placed:
                layer_waves.append([proposal])
        waves.extend(tuple(wave) for wave in layer_waves)
        completed.update(proposal.proposal_id for proposal in ready)
        remaining.difference_update(completed)

    return tuple(waves)


def execute_proposal_waves(
    proposals: Iterable[AgentProposal],
    runner: Callable[[AgentProposal], T],
    *,
    max_workers: int = 1,
    parallel_modes: Collection[str] = ("parallel", "parallel_safe", "adaptive"),
    can_share_wave: Callable[[AgentProposal, AgentProposal], bool] | None = None,
    dependency_succeeded: Callable[[T], bool] | None = None,
    blocked_result: Callable[[AgentProposal, tuple[str, ...]], T] | None = None,
) -> dict[str, T]:
    """Execute waves; callers explicitly opt into a parallel worker budget."""
    if max_workers < 1:
        raise ValueError("max_workers must be positive")
    waves = plan_proposal_waves(
        proposals,
        parallel_modes=parallel_modes,
        can_share_wave=can_share_wave,
    )
    results: dict[str, T] = {}
    for wave in waves:
        runnable: list[AgentProposal] = []
        for proposal in wave:
            blocked_by = (
                tuple(
                    dependency
                    for dependency in proposal.depends_on
                    if dependency in results
                    and not dependency_succeeded(results[dependency])
                )
                if dependency_succeeded is not None
                else ()
            )
            if blocked_by:
                if blocked_result is None:
                    raise RuntimeError(
                        "blocked_result is required when dependency_succeeded rejects a result"
                    )
                results[proposal.proposal_id] = blocked_result(proposal, blocked_by)
            else:
                runnable.append(proposal)
        if len(runnable) == 1 or max_workers == 1:
            for proposal in runnable:
                results[proposal.proposal_id] = runner(proposal)
            continue
        if not runnable:
            continue
        worker_count = min(len(runnable), max_workers)
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="agent-proposal") as executor:
            futures = {
                proposal.proposal_id: executor.submit(runner, proposal)
                for proposal in runnable
            }
            for proposal in runnable:
                results[proposal.proposal_id] = futures[proposal.proposal_id].result()
    return results
