from __future__ import annotations

import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from applypilot.apply.background_runtime import (
    BackgroundWorkerPool,
    CancellationDispatcher,
    LeaseReaper,
    ResultEventPublisher,
    RetryableBackgroundError,
    production_specialist_runners,
)
from applypilot.apply.contracts import TaskSpec
from applypilot.apply.specialists import READ_ONLY_SPECIALIST_AUTHORITY
from applypilot.storage import task_journal


def _factory(path: Path):
    return lambda: sqlite3.connect(path, timeout=5)


def test_single_worker_runs_read_task_with_heartbeat_and_result_ref(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    connection = sqlite3.connect(path)
    task_journal.register(
        connection,
        TaskSpec(
            "task-1",
            "demo-read",
            "Read",
            authority_scope=("read:bounded_snapshot",),
        ),
    )

    def runner(spec, context):
        assert spec["task_id"] == "task-1"
        context.heartbeat({"stage": "working"})
        return {"ready": True}

    connection.close()
    with BackgroundWorkerPool(_factory(path), {"demo-read": runner}) as pool:
        outcome = pool.run_once()

    assert outcome is not None and outcome.status == "completed"
    connection = sqlite3.connect(path)
    entry = task_journal.load(connection, "task-1")
    assert entry is not None and entry.result_ref == "agent-task:task-1:epoch:1"
    assert entry.progress == {"stage": "working"}
    assert [event["event_type"] for event in ResultEventPublisher(connection).events("task-1")] == [
        "claimed",
        "progress",
        "progress",
        "result",
    ]


def test_pool_rejects_product_effect_authority_without_running_callback(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    connection = sqlite3.connect(path)
    task_journal.register(
        connection,
        TaskSpec("task-1", "unsafe", "Submit", effect_class="submit"),
    )
    called = False

    def unsafe(_spec, _context):
        nonlocal called
        called = True
        return {}

    connection.close()
    with BackgroundWorkerPool(_factory(path), {"unsafe": unsafe}) as pool:
        outcome = pool.run_once()

    assert outcome is not None and outcome.status == "dead_letter"
    assert called is False
    entry = task_journal.load(sqlite3.connect(path), "task-1")
    assert entry is not None and entry.dead_letter_reason == "background_task_not_admitted_read_only"


def test_retry_is_due_gated_and_epoch_increments(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    connection = sqlite3.connect(path)
    task_journal.register(
        connection,
        TaskSpec(
            "task-1",
            "retry-read",
            "Read",
            retry_budget=1,
            retry_categories=("transient",),
        ),
    )
    attempts = 0

    def runner(_spec, _context):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RetryableBackgroundError("transient")
        return {"ready": True}

    current = datetime(2026, 9, 4, tzinfo=UTC)
    connection.close()
    with BackgroundWorkerPool(
        _factory(path), {"retry-read": runner}, retry_delay_seconds=5
    ) as pool:
        first = pool.run_once(now=current)
        assert first is not None and first.status == "pending"
        assert pool.run_once(now=current + timedelta(seconds=4)) is None
        second = pool.run_once(now=current + timedelta(seconds=5))
    assert second is not None and second.status == "completed" and second.lease_epoch == 2


def test_running_cancel_is_cooperatively_acknowledged(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    connection = sqlite3.connect(path)
    task_journal.register(connection, TaskSpec("task-1", "read", "Read"))

    def runner(_spec, context):
        cancel_connection = sqlite3.connect(path)
        CancellationDispatcher(cancel_connection).request("task-1")
        cancel_connection.close()
        assert context.cancelled()
        return {"ignored": True}

    connection.close()
    with BackgroundWorkerPool(_factory(path), {"read": runner}) as pool:
        outcome = pool.run_once()
    assert outcome is not None and outcome.status == "cancelled"


def test_expired_deadline_times_out_without_running_callback(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    connection = sqlite3.connect(path)
    task_journal.register(
        connection,
        TaskSpec(
            "task-1",
            "read",
            "Read",
            deadline_at=datetime.now(UTC) - timedelta(seconds=1),
        ),
    )
    called = False

    def runner(_spec, _context):
        nonlocal called
        called = True
        return {}

    connection.close()
    with BackgroundWorkerPool(_factory(path), {"read": runner}) as pool:
        outcome = pool.run_once()
    assert outcome is not None and outcome.status == "timed_out"
    assert called is False


def test_lease_reaper_requeues_only_read_work() -> None:
    connection = sqlite3.connect(":memory:")
    current = datetime(2026, 9, 4, tzinfo=UTC)
    for task_id, effect in (("read", "read"), ("uncertain", "effect_uncertain")):
        task_journal.register(
            connection, TaskSpec(task_id, "kind", "Work", effect_class=effect)
        )
        assert task_journal.claim(connection, task_id, "worker", lease_seconds=1, now=current)

    assert LeaseReaper(connection).run_once(now=current + timedelta(seconds=2)) == (1, 1)
    assert task_journal.load(connection, "read").status == "pending"
    assert task_journal.load(connection, "uncertain").status == "dead_letter"


def test_p2_context_specialist_runs_from_only_its_durable_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    connection = sqlite3.connect(path)
    task_journal.register(
        connection,
        TaskSpec(
            "provider",
            "provider-classifier-v1",
            "Classify provider",
            inputs={
                "snapshot": {"url": "https://boards.greenhouse.io/acme/jobs/123"},
                "mode": "shadow",
            },
            authority_scope=READ_ONLY_SPECIALIST_AUTHORITY,
        ),
    )

    connection.close()
    with BackgroundWorkerPool(_factory(path), production_specialist_runners()) as pool:
        outcome = pool.run_once()

    assert outcome is not None and outcome.status == "completed"
    connection = sqlite3.connect(path)
    entry = task_journal.load(connection, "provider")
    assert entry is not None
    result = entry.result["output"]["result"]
    assert result["provider"] == "greenhouse"


def test_two_pools_coalesce_one_running_read_and_both_consume_completion(
    tmp_path: Path,
) -> None:
    path = tmp_path / "coalesce.db"
    connection = sqlite3.connect(path)
    task_journal.register(connection, TaskSpec("shared", "read", "Read"))
    connection.close()
    entered = threading.Barrier(2, timeout=2)
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def runner(_spec, _context):
        nonlocal calls
        with calls_lock:
            calls += 1
        entered.wait()
        assert release.wait(timeout=2)
        return {"ready": True}

    first_pool = BackgroundWorkerPool(
        _factory(path), {"read": runner}, worker_id="reader-1"
    )
    second_pool = BackgroundWorkerPool(
        _factory(path), {"read": runner}, worker_id="reader-2"
    )
    outcomes = []
    threads = [
        threading.Thread(target=lambda pool=pool: outcomes.append(pool.run_task("shared")))
        for pool in (first_pool, second_pool)
    ]
    for thread in threads:
        thread.start()
    entered.wait()
    release.set()
    for thread in threads:
        thread.join(timeout=3)
    first_pool.shutdown()
    second_pool.shutdown()

    assert not any(thread.is_alive() for thread in threads)
    assert calls == 1
    assert len(outcomes) == 2
    assert all(outcome is not None and outcome.status == "completed" for outcome in outcomes)


def test_shutdown_closes_submit_gate_and_waits_for_registered_future(
    tmp_path: Path,
) -> None:
    path = tmp_path / "shutdown.db"
    connection = sqlite3.connect(path)
    task_journal.register(connection, TaskSpec("shared", "read", "Read"))
    connection.close()
    entered = threading.Barrier(2, timeout=2)
    release = threading.Event()

    def runner(_spec, _context):
        entered.wait()
        assert release.wait(timeout=2)
        return {"ready": True}

    pool = BackgroundWorkerPool(_factory(path), {"read": runner})
    outcomes = []
    consumer = threading.Thread(target=lambda: outcomes.append(pool.run_task("shared")))
    consumer.start()
    entered.wait()
    shutdown = threading.Thread(target=pool.shutdown)
    shutdown.start()
    deadline = time.monotonic() + 1
    while not pool.is_shutdown and time.monotonic() < deadline:
        time.sleep(0.001)
    assert pool.is_shutdown
    try:
        pool.run_task("shared")
    except RuntimeError as exc:
        assert "shut down" in str(exc)
    else:  # pragma: no cover - the lifecycle gate must be closed before release
        raise AssertionError("shutdown accepted a new submission")
    release.set()
    consumer.join(timeout=3)
    shutdown.join(timeout=3)

    assert not consumer.is_alive() and not shutdown.is_alive()
    assert outcomes[0] is not None and outcomes[0].status == "completed"
