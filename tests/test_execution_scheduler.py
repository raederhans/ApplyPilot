from applypilot.apply.execution_scheduler import PhaseDemand, build_execution_plan


def test_plan_reports_effective_workers_by_resource() -> None:
    plan = build_execution_plan(
        [
            PhaseDemand("a", "prepare", "profile-a", mailbox=True),
            PhaseDemand("b", "prepare", "profile-b", mailbox=False),
            PhaseDemand("c", "submit", "profile-a", submit_writer=True),
            PhaseDemand("d", "submit", "profile-b", submit_writer=True),
        ],
        requested_workers=8,
        browser_capacity=4,
        mailbox_capacity=1,
        submit_writer_capacity=1,
    )
    assert plan.phase_concurrency == {"prepare": 2, "submit": 1}
    assert plan.effective_workers == 2
    assert plan.requested_workers == 8


def test_empty_plan_has_zero_effective_workers() -> None:
    plan = build_execution_plan([], requested_workers=3, browser_capacity=2)
    assert plan.effective_workers == 0
    assert plan.phase_concurrency == {}
