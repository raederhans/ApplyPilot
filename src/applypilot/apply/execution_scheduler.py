"""Pure resource-aware planning for application execution phases."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PhaseDemand:
    task_id: str
    phase: str
    browser_profile: str | None = None
    mailbox: bool = False
    submit_writer: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    requested_workers: int
    effective_workers: int
    phase_concurrency: Mapping[str, int]
    resource_capacities: Mapping[str, int]


def build_execution_plan(
    demands: Iterable[PhaseDemand],
    *,
    requested_workers: int,
    browser_capacity: int,
    mailbox_capacity: int = 1,
    submit_writer_capacity: int = 1,
) -> ExecutionPlan:
    if min(requested_workers, browser_capacity, mailbox_capacity, submit_writer_capacity) < 1:
        raise ValueError("worker and resource capacities must be positive")
    items = tuple(demands)
    profiles = {item.browser_profile for item in items if item.browser_profile}
    profile_capacity = max(1, len(profiles)) if profiles else browser_capacity
    phase_counts: dict[str, int] = {}
    for phase in {item.phase for item in items}:
        matching = [item for item in items if item.phase == phase]
        phase_profiles = {item.browser_profile for item in matching if item.browser_profile}
        unbound_profiles = sum(item.browser_profile is None for item in matching)
        profile_limit = len(phase_profiles) + unbound_profiles
        mailbox_tasks = sum(item.mailbox for item in matching)
        submit_tasks = sum(item.submit_writer for item in matching)
        mailbox_limit = len(matching) - mailbox_tasks + min(mailbox_tasks, mailbox_capacity)
        submit_limit = len(matching) - submit_tasks + min(submit_tasks, submit_writer_capacity)
        limit = min(
            requested_workers,
            len(matching),
            browser_capacity,
            profile_limit,
            mailbox_limit,
            submit_limit,
        )
        phase_counts[phase] = max(0, limit)
    effective = max(phase_counts.values(), default=0)
    return ExecutionPlan(
        requested_workers=requested_workers,
        effective_workers=effective,
        phase_concurrency=phase_counts,
        resource_capacities={
            "browser": browser_capacity,
            "browser_profile": profile_capacity,
            "mailbox": mailbox_capacity,
            "submit_writer": submit_writer_capacity,
        },
    )
