from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta

from applypilot.apply import launcher
from applypilot.apply.application_jobs import revalidate_duplicate_before_submit
from applypilot.apply.contracts import ResourceClaim, TaskResult, TaskSpec
from applypilot.apply.orchestration import execute_task_graph


def test_read_only_checks_share_snapshot_but_page_writes_are_serialized() -> None:
    barrier = threading.Barrier(2, timeout=2)
    active_page_writers = 0
    peak_page_writers = 0
    lock = threading.Lock()
    consumed: list[str] = []

    tasks = [
        TaskSpec(
            task_id="duplicate-check",
            kind="duplicate-check",
            objective="Check canonical URL, platform ID, email, and local receipt",
            inputs={"job_snapshot_ref": "snapshot:job-1"},
            effect_class="read",
        ),
        TaskSpec(
            task_id="field-map",
            kind="field-map",
            objective="Map fields from the same frozen job snapshot",
            inputs={"job_snapshot_ref": "snapshot:job-1"},
            effect_class="read",
        ),
        TaskSpec(
            task_id="page-write-a",
            kind="page-write",
            objective="Write one page section",
            effect_class="page_write",
            resource_claims=(ResourceClaim("page:attempt-1"),),
        ),
        TaskSpec(
            task_id="page-write-b",
            kind="page-write",
            objective="Write another page section",
            effect_class="page_write",
            resource_claims=(ResourceClaim("page:attempt-1"),),
        ),
    ]

    def runner(task: TaskSpec, _context) -> TaskResult:
        nonlocal active_page_writers, peak_page_writers
        if task.effect_class == "read":
            barrier.wait()
        else:
            with lock:
                active_page_writers += 1
                peak_page_writers = max(peak_page_writers, active_page_writers)
                active_page_writers -= 1
        return TaskResult(task_id=task.task_id, status="completed", output={"used": True})

    def reducer(state, task: TaskSpec, result: TaskResult) -> None:
        consumed.append(task.task_id)
        state[task.task_id] = dict(result.output)

    outcome = execute_task_graph(tasks, runner, reducer, max_workers=2)

    assert set(consumed) == {task.task_id for task in tasks}
    assert set(outcome.reduced_state) == set(consumed)
    assert peak_page_writers == 1


def test_required_result_blocks_dependent_and_preserves_resume_cursor() -> None:
    called: list[str] = []
    reduced: list[str] = []
    tasks = [
        TaskSpec(task_id="identity", kind="check", objective="Verify identity"),
        TaskSpec(
            task_id="claim",
            kind="claim",
            objective="Claim submission transition",
            depends_on=("identity",),
            required_results=("identity",),
            effect_class="submit",
            resume_cursor={"phase": "pre-submit", "node": "claim"},
        ),
    ]

    def runner(task: TaskSpec, _context) -> TaskResult:
        called.append(task.task_id)
        return TaskResult(
            task_id=task.task_id,
            status="failed",
            failure_category="duplicate_found",
        )

    outcome = execute_task_graph(
        tasks,
        runner,
        lambda _state, task, _result: reduced.append(task.task_id),
    )

    assert called == ["identity"]
    assert reduced == ["identity", "claim"]
    assert outcome.results["claim"].status == "blocked"
    assert outcome.results["claim"].resume_cursor == {"phase": "pre-submit", "node": "claim"}


def test_dependent_task_receives_reducer_state_and_dependency_results() -> None:
    tasks = [
        TaskSpec(task_id="lookup", kind="lookup", objective="Resolve reusable fact"),
        TaskSpec(
            task_id="decide",
            kind="decide",
            objective="Consume the resolved fact",
            depends_on=("lookup",),
            required_results=("lookup",),
        ),
    ]

    def runner(task: TaskSpec, context) -> TaskResult:
        if task.task_id == "lookup":
            return TaskResult(task_id="lookup", status="completed", output={"answer": "yes"})
        assert context.dependency_results["lookup"].output["answer"] == "yes"
        assert context.reduced_state["answer"] == "yes"
        return TaskResult(task_id="decide", status="completed")

    def reducer(state, _task: TaskSpec, result: TaskResult) -> None:
        if "answer" in result.output:
            state["answer"] = result.output["answer"]

    outcome = execute_task_graph(tasks, runner, reducer)

    assert outcome.results["decide"].succeeded


def test_retry_budget_and_deadline_are_bounded() -> None:
    attempts: list[int] = []
    tasks = [
        TaskSpec(
            task_id="repair",
            kind="repair",
            objective="Retry a recoverable technical failure",
            retry_budget=1,
        ),
        TaskSpec(
            task_id="expired",
            kind="lookup",
            objective="Do not start expired work",
            deadline_at=datetime.now(UTC) - timedelta(seconds=1),
            resume_cursor={"phase": "lookup"},
        ),
    ]

    def runner(task: TaskSpec, context) -> TaskResult:
        attempts.append(context.attempt)
        return TaskResult(task_id=task.task_id, status="failed", retryable=True)

    outcome = execute_task_graph(tasks, runner, lambda *_args: None)

    assert attempts == [1, 2]
    assert outcome.attempts["repair"] == 2
    assert outcome.results["expired"].status == "timed_out"
    assert outcome.results["expired"].resume_cursor == {"phase": "lookup"}


def test_shared_success_target_stops_claiming_more_work() -> None:
    tasks = [
        TaskSpec(
            task_id=f"job-{index}",
            kind="application",
            objective="Complete one shared-target item",
            counts_toward_target=True,
        )
        for index in range(4)
    ]

    outcome = execute_task_graph(
        tasks,
        lambda task, _context: TaskResult(task_id=task.task_id, status="completed"),
        lambda *_args: None,
        max_workers=1,
        target_successes=2,
    )

    assert outcome.target_reached is True
    assert sum(result.succeeded for result in outcome.results.values()) == 2
    assert sum(result.status == "cancelled" for result in outcome.results.values()) == 2


def test_submit_transition_revalidates_duplicate_identity_inside_transaction() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE jobs (
            url TEXT PRIMARY KEY,
            canonical_job_url TEXT,
            platform_job_id TEXT,
            apply_status TEXT
        );
        CREATE TABLE application_receipts (
            receipt_source TEXT,
            receipt_id TEXT,
            job_url TEXT,
            admitted_at TEXT
        );
        INSERT INTO jobs VALUES ('job:new', 'canonical:1', 'platform:1', 'in_progress');
        INSERT INTO jobs VALUES ('job:old', 'canonical:1', 'platform:1', 'applied');
        """
    )

    try:
        revalidate_duplicate_before_submit(conn, "job:new")
    except RuntimeError as exc:
        assert "claim transaction" in str(exc)
    else:  # pragma: no cover - explicit guard is part of the safety contract
        raise AssertionError("revalidation accepted a non-transactional check")

    conn.execute("BEGIN IMMEDIATE")
    result = revalidate_duplicate_before_submit(conn, "job:new")
    conn.rollback()

    assert result == {
        "clear": False,
        "reason": "duplicate_submission_identity",
        "matched_job_url": "job:old",
        "matched_status": "applied",
        "has_receipt": False,
    }


def test_smartrecruiters_duplicate_and_identity_preflight_run_in_parallel(
    monkeypatch,
) -> None:
    barrier = threading.Barrier(2, timeout=2)

    class Connection:
        in_transaction = False

        def execute(self, _sql: str) -> None:
            self.in_transaction = True

        def rollback(self) -> None:
            self.in_transaction = False

        def close(self) -> None:
            return None

    def duplicate(_connection, _url: str) -> dict[str, object]:
        barrier.wait()
        return {"clear": True, "reason": "no_duplicate_submission"}

    def binding(_job) -> dict[str, object]:
        barrier.wait()
        return {"provider": "smartrecruiters", "resolved": True}

    monkeypatch.setattr(launcher, "get_connection", Connection)
    monkeypatch.setattr(
        launcher.application_jobs_mod,
        "revalidate_duplicate_before_submit",
        duplicate,
    )
    monkeypatch.setattr(launcher, "_resolve_ats_application_binding", binding)

    result = launcher._run_read_only_preflight(
        {"url": "https://jobs.smartrecruiters.com/Example/123-role"}
    )

    assert result["duplicate"] == {
        "clear": True,
        "reason": "no_duplicate_submission",
    }
    assert result["ats_binding"] == {
        "provider": "smartrecruiters",
        "resolved": True,
    }
    assert set(result["task_statuses"]) == {"duplicate-snapshot", "ats-identity"}
