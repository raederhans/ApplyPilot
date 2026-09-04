"""Single-worker durable runtime for bounded read-only production specialists.

This module intentionally has no browser, page, mailbox, ledger, SubmissionGate,
or submit dependency.  Its only mutable authority is the task journal itself.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, Self

from applypilot.apply.contracts import TaskResult, ensure_persistable
from applypilot.storage import task_journal

READ_ONLY_BACKGROUND_AUTHORITY = frozenset(
    {
        "read:bounded_snapshot",
        "write:control_heartbeat",
        "write:control_checkpoint",
        "emit:advisory_context",
    }
)


class BackgroundRunner(Protocol):
    def __call__(
        self, spec: Mapping[str, object], context: BackgroundTaskContext
    ) -> TaskResult | Mapping[str, object]: ...


class RetryableBackgroundError(RuntimeError):
    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


@dataclass(frozen=True, slots=True)
class WorkerOutcome:
    task_id: str
    status: str
    lease_epoch: int


class ResultEventPublisher:
    """Persist progress/results together with their idempotent event records."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def events(self, task_id: str) -> list[dict[str, object]]:
        return task_journal.list_events(self._connection, task_id)

    def progress(
        self,
        token: task_journal.LeaseToken,
        value: dict[str, object],
        *,
        lease_seconds: int,
    ) -> task_journal.JournalEntry:
        return task_journal.heartbeat(
            self._connection, token, progress=value, lease_seconds=lease_seconds
        )

    def result(
        self,
        token: task_journal.LeaseToken,
        value: TaskResult,
        *,
        result_ref: str | None = None,
    ) -> task_journal.JournalEntry:
        if value.succeeded:
            return task_journal.complete(
                self._connection,
                token.task_id,
                token,
                value,
                result_ref=result_ref,
            )
        return task_journal.fail(self._connection, token.task_id, token, value)


class CancellationDispatcher:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def request(self, task_id: str) -> task_journal.JournalEntry:
        return task_journal.request_cancel(self._connection, task_id)

    def requested(self, token: task_journal.LeaseToken) -> bool:
        return task_journal.cancellation_requested(self._connection, token)

    def acknowledge(self, token: task_journal.LeaseToken) -> task_journal.JournalEntry:
        return task_journal.acknowledge_cancel(self._connection, token)


class RetryScheduler:
    def __init__(self, connection: sqlite3.Connection, *, delay_seconds: int = 1) -> None:
        if delay_seconds < 0:
            raise ValueError("retry delay must be non-negative")
        self._connection = connection
        self._delay_seconds = delay_seconds

    def schedule(
        self,
        token: task_journal.LeaseToken,
        *,
        category: str,
        now: datetime | None = None,
    ) -> task_journal.JournalEntry:
        current = now or datetime.now(UTC)
        return task_journal.schedule_retry(
            self._connection,
            token,
            retry_at=current + timedelta(seconds=self._delay_seconds),
            failure_category=category,
            now=current,
        )


class LeaseReaper:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def run_once(self, *, now: datetime | None = None) -> tuple[int, int]:
        return task_journal.reap_expired(self._connection, now=now)


class BackgroundTaskContext:
    """Narrow cooperative context; no product-effect capability can cross it."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        token: task_journal.LeaseToken,
        *,
        lease_seconds: int,
        deadline_at: datetime | None,
    ) -> None:
        self._connection = connection
        self._token = token
        self._lease_seconds = lease_seconds
        self._deadline_at = deadline_at

    @property
    def token(self) -> task_journal.LeaseToken:
        return self._token

    def heartbeat(self, progress: Mapping[str, object] | None = None) -> None:
        copied = None
        if progress is not None:
            persisted = ensure_persistable(progress, path="$.background_progress")
            if not isinstance(persisted, dict):
                raise TypeError("background progress must be an object")
            copied = persisted
        ResultEventPublisher(self._connection).progress(
            self._token, copied or {}, lease_seconds=self._lease_seconds
        )

    def cancelled(self) -> bool:
        return task_journal.cancellation_requested(self._connection, self._token)

    def remaining_seconds(self) -> float | None:
        if self._deadline_at is None:
            return None
        return max(0.0, (self._deadline_at - datetime.now(UTC)).total_seconds())

    def deadline_reached(self) -> bool:
        remaining = self.remaining_seconds()
        return remaining is not None and remaining == 0


class BackgroundWorkerPool:
    """Staged one-worker executor for allowlisted read-only task kinds."""

    def __init__(
        self,
        connection_factory: Callable[[], sqlite3.Connection],
        runners: Mapping[str, BackgroundRunner],
        *,
        worker_id: str = "background-read-1",
        max_workers: int = 1,
        lease_seconds: int = 30,
        retry_delay_seconds: int = 1,
        coalesce_wait_seconds: float = 5.0,
    ) -> None:
        if max_workers != 1:
            raise ValueError("background runtime is staged at exactly one worker")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if coalesce_wait_seconds <= 0:
            raise ValueError("coalesce_wait_seconds must be positive")
        self._connection_factory = connection_factory
        self._runners = dict(runners)
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._retry_delay_seconds = retry_delay_seconds
        self._coalesce_wait_seconds = coalesce_wait_seconds
        self._executor: ThreadPoolExecutor | None = None
        self._lifecycle_lock = threading.Lock()
        self._active_futures: set[Future[WorkerOutcome | None]] = set()
        self._closed = False
        self._shutdown_started = False

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("background worker pool is shut down")
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix=self._worker_id
                )

    @property
    def is_shutdown(self) -> bool:
        with self._lifecycle_lock:
            return self._closed

    def shutdown(self) -> None:
        with self._lifecycle_lock:
            if self._shutdown_started:
                return
            self._shutdown_started = True
            self._closed = True
            executor = self._executor
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=False)
        with self._lifecycle_lock:
            if self._executor is executor:
                self._executor = None
            self._active_futures.clear()

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.shutdown()

    def _submit(self, operation: Callable[[], WorkerOutcome | None]) -> WorkerOutcome | None:
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("background worker pool is shut down")
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix=self._worker_id
                )
            executor = self._executor
            future = executor.submit(operation)
            self._active_futures.add(future)
        try:
            return future.result()
        finally:
            with self._lifecycle_lock:
                self._active_futures.discard(future)

    def run_once(self, *, now: datetime | None = None) -> WorkerOutcome | None:
        return self._submit(lambda: self._run_once_worker(now=now))

    def run_task(self, task_id: str, *, now: datetime | None = None) -> WorkerOutcome | None:
        return self._submit(lambda: self._run_task_worker(task_id, now=now))

    def _run_once_worker(self, *, now: datetime | None) -> WorkerOutcome | None:
        connection = self._connection_factory()
        try:
            entry = task_journal.claim_next(
                connection,
                self._worker_id,
                lease_seconds=self._lease_seconds,
                now=now,
            )
            return None if entry is None else self._execute(connection, entry, now=now)
        finally:
            connection.close()

    def _run_task_worker(
        self, task_id: str, *, now: datetime | None
    ) -> WorkerOutcome | None:
        connection = self._connection_factory()
        try:
            entry = task_journal.claim(
                connection,
                task_id,
                self._worker_id,
                lease_seconds=self._lease_seconds,
                now=now,
            )
            if entry is None:
                existing = task_journal.load(connection, task_id)
                if existing is not None and existing.status in {
                    "completed",
                    "failed",
                    "blocked",
                    "cancelled",
                    "timed_out",
                    "dead_letter",
                }:
                    return WorkerOutcome(task_id, existing.status, existing.lease_epoch)
                if existing is None or existing.effect_class != "read":
                    return None
                entry = self._coalesce_or_reclaim(connection, existing)
                if entry is None:
                    return None
                if entry.status != "running":
                    return WorkerOutcome(task_id, entry.status, entry.lease_epoch)
            return self._execute(connection, entry, now=now)
        finally:
            connection.close()

    def _coalesce_or_reclaim(
        self,
        connection: sqlite3.Connection,
        initial: task_journal.JournalEntry,
    ) -> task_journal.JournalEntry | None:
        """Wait for the current read owner or reclaim only after lease expiry."""
        deadline = time.monotonic() + self._coalesce_wait_seconds
        durable_deadline = self._deadline(task_journal.load_spec(connection, initial.task_id))
        while time.monotonic() < deadline:
            current = task_journal.load(connection, initial.task_id)
            if current is None:
                return None
            if current.status in {
                "completed",
                "failed",
                "blocked",
                "cancelled",
                "timed_out",
                "dead_letter",
            }:
                return current
            wall_now = datetime.now(UTC)
            if durable_deadline is not None and wall_now >= durable_deadline:
                return None
            if current.status == "running" and current.lease_expires_at is not None:
                lease_expires = datetime.fromisoformat(current.lease_expires_at)
                if lease_expires <= wall_now:
                    LeaseReaper(connection).run_once(now=wall_now)
                    continue
            if current.status == "pending":
                reclaimed = task_journal.claim(
                    connection,
                    current.task_id,
                    self._worker_id,
                    lease_seconds=self._lease_seconds,
                    now=wall_now,
                )
                if reclaimed is not None:
                    return reclaimed
            time.sleep(0.01)
        return None

    def _execute(
        self,
        connection: sqlite3.Connection,
        entry: task_journal.JournalEntry,
        *,
        now: datetime | None = None,
    ) -> WorkerOutcome:
        token = entry.lease_token
        assert token is not None
        spec = task_journal.load_spec(connection, entry.task_id)
        authority = spec.get("authority_scope", [])
        authority_set = set(authority) if isinstance(authority, list) else set()
        kind = str(spec.get("kind") or "")
        if (
            entry.effect_class != "read"
            or not authority_set <= READ_ONLY_BACKGROUND_AUTHORITY
            or kind not in self._runners
        ):
            reason = (
                "background_task_not_admitted_read_only"
                if entry.effect_class != "read" or not authority_set <= READ_ONLY_BACKGROUND_AUTHORITY
                else "background_task_kind_not_allowlisted"
            )
            finished = task_journal.dead_letter(connection, token, reason=reason)
            return WorkerOutcome(entry.task_id, finished.status, token.lease_epoch)

        deadline_at = self._deadline(spec)
        context = BackgroundTaskContext(
            connection,
            token,
            lease_seconds=self._lease_seconds,
            deadline_at=deadline_at,
        )
        if context.cancelled():
            finished = task_journal.acknowledge_cancel(connection, token)
            return WorkerOutcome(entry.task_id, finished.status, token.lease_epoch)
        if context.deadline_reached():
            finished = self._persist_result(
                connection,
                token,
                TaskResult(
                    task_id=entry.task_id,
                    status="timed_out",
                    failure_category="background_deadline_exceeded",
                ),
            )
            return WorkerOutcome(entry.task_id, finished.status, token.lease_epoch)
        context.heartbeat({"stage": "started"})
        try:
            produced = self._runners[kind](spec, context)
            if context.cancelled():
                finished = task_journal.acknowledge_cancel(connection, token)
            elif context.deadline_reached():
                finished = self._persist_result(
                    connection,
                    token,
                    TaskResult(
                        task_id=entry.task_id,
                        status="timed_out",
                        failure_category="background_deadline_exceeded",
                    ),
                )
            else:
                result = self._coerce_result(entry.task_id, produced, authority_set)
                if result.succeeded:
                    finished = self._persist_result(
                        connection,
                        token,
                        result,
                        result_ref=f"agent-task:{entry.task_id}:epoch:{token.lease_epoch}",
                    )
                else:
                    finished = self._persist_result(connection, token, result)
        except RetryableBackgroundError as exc:
            if self._retry_admitted(spec, entry, exc.category):
                try:
                    finished = RetryScheduler(
                        connection, delay_seconds=self._retry_delay_seconds
                    ).schedule(token, category=exc.category, now=now)
                except RuntimeError:
                    if task_journal.cancellation_requested(connection, token):
                        finished = task_journal.acknowledge_cancel(connection, token)
                    else:
                        raise
            else:
                finished = self._persist_result(
                    connection,
                    token,
                    TaskResult(
                        task_id=entry.task_id,
                        status="failed",
                        failure_category=exc.category,
                        retryable=False,
                    ),
                )
        except Exception as exc:  # noqa: BLE001 - runner boundary must become durable failure
            finished = self._persist_result(
                connection,
                token,
                TaskResult(
                    task_id=entry.task_id,
                    status="failed",
                    output={"error_type": type(exc).__name__},
                    failure_category="background_runner_failure",
                ),
            )
        return WorkerOutcome(entry.task_id, finished.status, token.lease_epoch)

    @staticmethod
    def _persist_result(
        connection: sqlite3.Connection,
        token: task_journal.LeaseToken,
        result: TaskResult,
        *,
        result_ref: str | None = None,
    ) -> task_journal.JournalEntry:
        try:
            return ResultEventPublisher(connection).result(
                token, result, result_ref=result_ref
            )
        except RuntimeError:
            if task_journal.cancellation_requested(connection, token):
                return task_journal.acknowledge_cancel(connection, token)
            raise

    @staticmethod
    def _deadline(spec: Mapping[str, object]) -> datetime | None:
        value = spec.get("deadline_at")
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("background deadline must be an ISO timestamp")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("background deadline must be timezone-aware")
        return parsed.astimezone(UTC)

    @staticmethod
    def _retry_admitted(
        spec: Mapping[str, object], entry: task_journal.JournalEntry, category: str
    ) -> bool:
        categories = spec.get("retry_categories", [])
        budget = spec.get("retry_budget", 0)
        return (
            isinstance(categories, list)
            and category in categories
            and isinstance(budget, int)
            and not isinstance(budget, bool)
            and entry.attempt_count <= budget
        )

    @staticmethod
    def _coerce_result(
        task_id: str,
        produced: TaskResult | Mapping[str, object],
        admitted_authority: set[object],
    ) -> TaskResult:
        if isinstance(produced, TaskResult):
            unexpected = set(produced.authority_scope) - admitted_authority
            if unexpected:
                raise PermissionError("background result exceeded admitted authority")
            return produced
        persisted = ensure_persistable(produced, path="$.background_result")
        if not isinstance(persisted, dict):
            raise TypeError("background result must be an object")
        return TaskResult(task_id=task_id, status="completed", output=persisted)


def production_specialist_background_runner(
    spec: Mapping[str, object], context: BackgroundTaskContext
) -> TaskResult:
    """Adapter connecting durable P2 snapshot specialists to the P3 worker."""
    from applypilot.apply.specialists import (
        dispatch_production_specialist,
        production_specialist_spec,
        run_context_specialist,
    )

    kind = str(spec.get("kind") or "")
    declared = production_specialist_spec(kind)
    if declared.effect_class != "read" or not declared.read_only:
        raise PermissionError("production specialist is not read-only")
    inputs = spec.get("inputs")
    if not isinstance(inputs, Mapping):
        raise TypeError("production specialist durable inputs are invalid")
    context.heartbeat({"stage": "specialist-dispatched", "kind": kind})
    if kind == "ats-fill-plan-v1":
        phase = inputs.get("phase")
        payload = inputs.get("payload")
        catalog = inputs.get("snapshot_catalog")
        if not isinstance(phase, str) or not isinstance(payload, Mapping) or not isinstance(catalog, Mapping):
            raise TypeError("ATS fill-plan durable inputs are invalid")
        output = dispatch_production_specialist(
            kind, phase=phase, payload=payload, snapshot_catalog=catalog
        )
    elif kind == "material-readiness-v1":
        # Existing material tasks intentionally persist only byte identities,
        # not the source document handles needed to recompute the result.
        raise PermissionError("material readiness is not background-recoverable from its durable spec")
    else:
        snapshot = inputs.get("snapshot")
        mode = inputs.get("mode", "shadow")
        if not isinstance(snapshot, Mapping) or not isinstance(mode, str):
            raise TypeError("context specialist durable inputs are invalid")
        run = run_context_specialist(kind, snapshot, mode=mode)
        output = {
            "result": {} if run is None else run.result,
            "enforced": False if run is None else run.enforced,
            "mode": mode if run is None else run.mode,
        }
    return TaskResult(
        task_id=str(spec.get("task_id") or ""),
        status="completed",
        output=(
            {"specialist_result": output}
            if kind == "ats-fill-plan-v1"
            else output
        ),
        authority_scope=declared.authority_scope,
    )


def production_specialist_runners() -> dict[str, BackgroundRunner]:
    """Return the explicit background allowlist; material work stays synchronous."""
    return {
        name: production_specialist_background_runner
        for name in (
            "ats-fill-plan-v1",
            "field-semantic-v1",
            "provider-classifier-v1",
            "application-facts-v1",
            "work-authorization-v1",
            "page-failure-v1",
        )
    }
