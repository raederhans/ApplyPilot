from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from applypilot.apply import application_jobs
from applypilot.apply.browser_context_runtime import (
    BrowserContextFeature,
    BrowserStateScope,
    HotBrowserContextRuntime,
    ScopedBrowserState,
)
from applypilot.apply.runtime_cell import RuntimeCellExecutionState
from applypilot.apply.runtime_cell_coordinator import (
    APP_SERVER_PRODUCTION_CELL_ADMITTED,
    RUNTIME_CELL_GATE_NAMES,
    RUNTIME_CELL_GATE_SCHEMAS,
    DiagnosticRuntimeCellCoordinator,
    RuntimeCellAdmissionDecision,
    RuntimeCellAdmissionManifest,
    RuntimeCellCoordinator,
    RuntimeCellGateReceipt,
    RuntimeCellHost,
    recovery_disposition,
    resolve_runtime_cell_admission,
    source_manifest_identity,
)
from applypilot.database import init_db
from applypilot.storage import runtime_cells

SOURCE = "a" * 64
RECEIPTS = {name: RuntimeCellGateReceipt(RUNTIME_CELL_GATE_SCHEMAS[name], "b" * 64) for name in RUNTIME_CELL_GATE_NAMES}


def _connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=10, check_same_thread=False)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def _register(connection: sqlite3.Connection, index: int = 0) -> runtime_cells.RuntimeCellGeneration:
    return runtime_cells.register_generation(
        connection,
        cell_id=f"runtime-cell-{index}",
        generation=1,
        runtime_id=f"runtime-{index}",
        source_identity=SOURCE,
        process_id=1000 + index,
        process_birth_time=2000 + index,
    )


def _claim(
    connection: sqlite3.Connection,
    index: int,
    suffix: str,
    hostname: str,
    *,
    now: datetime | None = None,
    ttl_seconds: int = 30,
) -> runtime_cells.RuntimeCellLease:
    attempt = f"attempt-{suffix}"
    return runtime_cells.claim_lease(
        connection,
        lease_id=f"lease-{suffix}",
        cell_id=f"runtime-cell-{index}",
        generation=1,
        runtime_id=f"runtime-{index}",
        source_identity=SOURCE,
        application_id=f"application-{suffix}",
        actor_id=f"application:{attempt}",
        attempt_id=attempt,
        hostname=hostname,
        now=now,
        ttl_seconds=ttl_seconds,
    )


def test_off_shadow_canary_and_local_manifest_fail_closed() -> None:
    assert APP_SERVER_PRODUCTION_CELL_ADMITTED is False
    production = RuntimeCellAdmissionManifest(
        source_identity=SOURCE,
        workers=2,
        gate_receipts=RECEIPTS,
        production_authority=True,
        authority_ref="release-gate:pending-upstream-app-server-fix",
    )
    for mode in ("off", "shadow", "canary"):
        decision = resolve_runtime_cell_admission(
            mode=mode,
            current_source_identity=SOURCE,
            requested_workers=2,
            manifest=production,
        )
        assert decision.effective_cells == 1
        assert decision.status == "NOT_ADMITTED"
    assert (
        "app_server_production_cell_not_admitted"
        in resolve_runtime_cell_admission(
            mode="canary",
            current_source_identity=SOURCE,
            requested_workers=2,
            manifest=production,
        ).reasons
    )

    local = RuntimeCellAdmissionManifest(
        source_identity=SOURCE,
        workers=2,
        gate_receipts=RECEIPTS,
        production_authority=False,
        authority_ref=None,
        local_diagnostic=True,
    )
    decision = resolve_runtime_cell_admission(
        mode="canary", current_source_identity=SOURCE, requested_workers=2, manifest=local
    )
    assert decision.effective_cells == 1
    assert "local_diagnostic_has_no_production_authority" in decision.reasons


def test_runtime_cell_migration_is_idempotent_and_newer_schema_fails_closed(
    tmp_path: Path,
) -> None:
    connection = _connection(tmp_path / "migration.sqlite3")
    assert runtime_cells.ensure_schema(connection) == 2
    assert runtime_cells.ensure_schema(connection) == 2
    connection.execute("UPDATE runtime_cell_schema_version SET version=99 WHERE component='runtime_cells'")
    connection.commit()
    with pytest.raises(RuntimeError, match="newer than supported"):
        runtime_cells.ensure_schema(connection)


def test_v1_terminal_duplicate_process_identities_migrate_without_history_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = _connection(tmp_path / "v1-history.sqlite3")
    migrations = runtime_cells._MIGRATIONS
    monkeypatch.setattr(runtime_cells, "_MIGRATIONS", (runtime_cells._migration_v1,))
    assert runtime_cells.ensure_schema(connection) == 1
    now = datetime(2026, 9, 4, tzinfo=UTC).isoformat()
    for index, status in enumerate(("quarantined", "closed"), start=1):
        connection.execute(
            "INSERT INTO runtime_cell_generations("
            "cell_id,generation,runtime_id,source_identity,process_id,"
            "process_birth_time,status,created_at,updated_at,quarantine_reason) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                f"runtime-cell-{index}",
                1,
                f"historical-runtime-{index}",
                SOURCE,
                4242,
                8675309,
                status,
                now,
                now,
                "historical" if status == "quarantined" else None,
            ),
        )
    connection.commit()
    monkeypatch.setattr(runtime_cells, "_MIGRATIONS", migrations)

    assert runtime_cells.ensure_schema(connection) == 2
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM runtime_cell_generations WHERE process_id=4242 AND process_birth_time=8675309"
        ).fetchone()[0]
        == 2
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM runtime_cell_process_identities WHERE process_id=4242 AND process_birth_time=8675309"
        ).fetchone()[0]
        == 1
    )
    with pytest.raises(runtime_cells.RuntimeCellConflictError):
        runtime_cells.register_generation(
            connection,
            cell_id="runtime-cell-new",
            generation=1,
            runtime_id="new-runtime-on-historical-process",
            source_identity=SOURCE,
            process_id=4242,
            process_birth_time=8675309,
        )


def test_fresh_database_concurrent_process_identity_registration_has_one_winner(
    tmp_path: Path,
) -> None:
    path = tmp_path / "concurrent-process-identity.sqlite3"
    setup = _connection(path)
    runtime_cells.ensure_schema(setup)
    setup.close()
    barrier = threading.Barrier(16)
    lock = threading.Lock()
    outcomes: list[str] = []

    def register(index: int) -> None:
        connection = _connection(path)
        try:
            barrier.wait()
            try:
                runtime_cells.register_generation(
                    connection,
                    cell_id=f"runtime-cell-{index}",
                    generation=1,
                    runtime_id=f"concurrent-runtime-{index}",
                    source_identity=SOURCE,
                    process_id=7777,
                    process_birth_time=8888,
                )
            except runtime_cells.RuntimeCellConflictError:
                outcome = "conflict"
            else:
                outcome = "registered"
            with lock:
                outcomes.append(outcome)
        finally:
            connection.close()

    threads = [threading.Thread(target=register, args=(index,)) for index in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("registered") == 1
    assert outcomes.count("conflict") == 15


def test_scheduler_microbenchmark_schema_is_rejected_as_admission_receipt() -> None:
    receipts = {name: receipt.as_dict() for name, receipt in RECEIPTS.items()}
    receipts["runtime_cell_host_lifecycle_benchmark"] = {
        "schema_version": "applypilot-runtime-cell-scheduler-microbenchmark/v1",
        "sha256": "c" * 64,
    }
    with pytest.raises(ValueError, match="schema is not admissible"):
        RuntimeCellAdmissionManifest.from_mapping(
            {
                "schema_version": "applypilot-runtime-cell-admission/v1",
                "source_identity": SOURCE,
                "workers": 2,
                "gate_receipts": receipts,
                "production_authority": True,
                "authority_ref": "release:forged",
            }
        )


def test_production_coordinator_self_evaluates_and_rejects_forged_decision(
    tmp_path: Path,
) -> None:
    forged = RuntimeCellAdmissionDecision(
        mode="canary",
        requested_workers=2,
        effective_cells=2,
        status="ADMITTED",
        reasons=(),
        source_identity=SOURCE,
        production_authority=True,
    )
    with pytest.raises(TypeError):
        RuntimeCellCoordinator(lambda: _connection(tmp_path / "forged.sqlite3"), decision=forged)  # type: ignore[call-arg]

    source_root = Path(__file__).resolve().parents[1]
    current_identity = source_manifest_identity(source_root)
    manifest = RuntimeCellAdmissionManifest(
        source_identity=current_identity,
        workers=2,
        gate_receipts=RECEIPTS,
        production_authority=True,
        authority_ref="release:still-blocked-by-code-gate",
    )
    manifest_path = tmp_path / "admission.json"
    manifest_path.write_text(json.dumps(manifest.as_dict()), encoding="utf-8")
    coordinator = RuntimeCellCoordinator(
        lambda: _connection(tmp_path / "production.sqlite3"),
        mode="canary",
        requested_workers=2,
        source_root=source_root,
        manifest_path=manifest_path,
    )
    assert coordinator.decision.effective_cells == 1
    assert coordinator.decision.status == "NOT_ADMITTED"

    diagnostic = DiagnosticRuntimeCellCoordinator(
        lambda: _connection(tmp_path / "diagnostic.sqlite3"),
        source_identity=current_identity,
        cells=1,
    )
    with pytest.raises(TypeError):
        diagnostic.register(  # type: ignore[call-arg]
            cell_index=0,
            generation=1,
            runtime_id="missing-verified-process-birth-identity",
        )


@pytest.mark.parametrize(
    ("state", "readable", "expected"),
    [
        (None, False, "park"),
        (RuntimeCellExecutionState(False, False, False, None), True, "fallback"),
        (RuntimeCellExecutionState(True, False, False, "codex-app-server"), True, "park"),
        (RuntimeCellExecutionState(True, True, False, "codex-app-server"), True, "park"),
        (RuntimeCellExecutionState(True, True, True, "codex-app-server"), True, "receipt_only"),
    ],
)
def test_accepted_effect_submit_failure_recovery_matrix(
    state: RuntimeCellExecutionState | None, readable: bool, expected: str
) -> None:
    assert recovery_disposition(state, state_readable=readable) == expected


def test_same_domain_100_concurrent_claims_peak_one(tmp_path: Path) -> None:
    path = tmp_path / "cells.sqlite3"
    setup = _connection(path)
    for index in range(100):
        _register(setup, index)
    setup.close()
    barrier = threading.Barrier(100)
    lock = threading.Lock()
    active = 0
    peak = 0
    successes = 0

    def contender(index: int) -> None:
        nonlocal active, peak, successes
        connection = _connection(path)
        try:
            barrier.wait()
            try:
                lease = _claim(connection, index, str(index), "same.example.test")
            except runtime_cells.RuntimeCellConflictError:
                return
            token = runtime_cells.token_from_lease(lease)
            with lock:
                active += 1
                peak = max(peak, active)
                successes += 1
            time.sleep(0.01)
            with lock:
                active -= 1
            runtime_cells.begin_drain(connection, token, reason="test_complete")
            runtime_cells.release_after_cleanup(
                connection,
                token,
                agent_stopped=True,
                context_cleanup_verified=True,
                residual_resources=0,
            )
        finally:
            connection.close()

    threads = [threading.Thread(target=contender, args=(index,)) for index in range(100)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert successes >= 1
    assert peak == 1


def test_different_domains_parallel_and_stale_cross_cell_tokens_rejected(tmp_path: Path) -> None:
    path = tmp_path / "cells.sqlite3"
    connection = _connection(path)
    _register(connection, 0)
    _register(connection, 1)
    first = _claim(connection, 0, "one", "one.example.test")
    second = _claim(connection, 1, "two", "two.example.test")
    assert first.status == second.status == "open"
    first_token = runtime_cells.token_from_lease(first)
    second_token = runtime_cells.token_from_lease(second)
    adopted = replace(first_token, cell_id=second_token.cell_id, runtime_id=second_token.runtime_id)
    with pytest.raises(runtime_cells.StaleRuntimeCellTokenError):
        runtime_cells.release_after_cleanup(
            connection,
            adopted,
            agent_stopped=True,
            context_cleanup_verified=True,
            residual_resources=0,
        )
    runtime_cells.begin_drain(connection, first_token, reason="done")
    runtime_cells.release_after_cleanup(
        connection,
        first_token,
        agent_stopped=True,
        context_cleanup_verified=True,
        residual_resources=0,
    )
    with pytest.raises(runtime_cells.StaleRuntimeCellTokenError):
        runtime_cells.heartbeat_lease(connection, first_token)
    runtime_cells.begin_drain(connection, second_token, reason="done")
    runtime_cells.release_after_cleanup(
        connection,
        second_token,
        agent_stopped=True,
        context_cleanup_verified=True,
        residual_resources=0,
    )


def test_ttl_only_marks_suspect_and_never_allows_takeover(tmp_path: Path) -> None:
    path = tmp_path / "cells.sqlite3"
    connection = _connection(path)
    _register(connection, 0)
    _register(connection, 1)
    now = datetime(2026, 9, 4, tzinfo=UTC)
    lease = _claim(connection, 0, "old", "same.example.test", now=now, ttl_seconds=1)
    assert runtime_cells.mark_expired_suspect(connection, now=now + timedelta(seconds=2)) == 1
    with pytest.raises(runtime_cells.RuntimeCellConflictError):
        _claim(
            connection,
            1,
            "new",
            "same.example.test",
            now=now + timedelta(seconds=2),
        )
    token = runtime_cells.token_from_lease(lease)
    with pytest.raises(runtime_cells.StaleRuntimeCellTokenError):
        runtime_cells.heartbeat_lease(connection, token, now=now + timedelta(seconds=2))


def test_old_cleanup_failure_cannot_quarantine_new_lease_same_generation(
    tmp_path: Path,
) -> None:
    connection = _connection(tmp_path / "old-token.sqlite3")
    _register(connection, 0)
    first = _claim(connection, 0, "first", "first.example.test")
    first_token = runtime_cells.token_from_lease(first)
    runtime_cells.begin_drain(connection, first_token, reason="first_done")
    runtime_cells.release_after_cleanup(
        connection,
        first_token,
        agent_stopped=True,
        context_cleanup_verified=True,
        residual_resources=0,
    )
    second = _claim(connection, 0, "second", "second.example.test")
    with pytest.raises(runtime_cells.StaleRuntimeCellTokenError):
        runtime_cells.release_after_cleanup(
            connection,
            first_token,
            agent_stopped=False,
            context_cleanup_verified=False,
            residual_resources=None,
        )
    current = connection.execute(
        "SELECT status FROM runtime_cell_leases WHERE lease_id=?", (second.lease_id,)
    ).fetchone()
    generation = runtime_cells.get_generation(connection, "runtime-cell-0", 1)
    assert current[0] == "open"
    assert generation is not None and generation.status == "active"


def test_quarantined_process_identity_can_never_register_new_generation(
    tmp_path: Path,
) -> None:
    connection = _connection(tmp_path / "process-fence.sqlite3")
    _register(connection, 0)
    lease = _claim(connection, 0, "quarantine", "q.example.test")
    with pytest.raises(runtime_cells.RuntimeCellQuarantinedError):
        runtime_cells.release_after_cleanup(
            connection,
            runtime_cells.token_from_lease(lease),
            agent_stopped=False,
            context_cleanup_verified=False,
            residual_resources=None,
        )
    with pytest.raises(runtime_cells.RuntimeCellConflictError):
        runtime_cells.register_generation(
            connection,
            cell_id="runtime-cell-0",
            generation=2,
            runtime_id="runtime-0-generation-2",
            source_identity=SOURCE,
            process_id=1000,
            process_birth_time=2000,
        )


class _Page:
    frames = (object(),)


class _Context:
    def __init__(self, *, residual: bool = False) -> None:
        self.pages: list[_Page] = []
        self.service_workers: list[object] = []
        self.residual = residual

    def new_page(self) -> _Page:
        page = _Page()
        self.pages.append(page)
        return page

    def close(self) -> None:
        if not self.residual:
            self.pages.clear()
            self.service_workers.clear()


class _Browser:
    def __init__(self, *, residual: bool = False) -> None:
        self.residual = residual
        self.contexts: list[_Context] = []
        self.closed = False

    def new_context(self, **_kwargs: object) -> _Context:
        context = _Context(residual=self.residual)
        self.contexts.append(context)
        return context

    def close(self) -> None:
        self.closed = True


def _host(tmp_path: Path, *, cell_index: int = 0, residual: bool = False) -> tuple[RuntimeCellHost, _Browser, Path]:
    path = tmp_path / "host.sqlite3"
    coordinator = DiagnosticRuntimeCellCoordinator(lambda: _connection(path), source_identity=SOURCE, cells=2)
    binding = coordinator.register(
        cell_index=cell_index,
        generation=1,
        runtime_id=f"runtime-host-{cell_index}",
        process_id=501 + cell_index,
        process_birth_time=601 + cell_index,
    )
    browser = _Browser(residual=residual)
    context_runtime = HotBrowserContextRuntime(feature=BrowserContextFeature(True), launch_browser=lambda: browser)
    return (
        RuntimeCellHost(
            coordinator=coordinator,
            binding=binding,
            context_runtime=context_runtime,
            connection_factory=lambda: _connection(path),
        ),
        browser,
        path,
    )


def _scope_state() -> tuple[BrowserStateScope, ScopedBrowserState]:
    scope = BrowserStateScope("workday", "tenant.example.test", "candidate")
    state = ScopedBrowserState(scope, {"cookies": [], "origins": []})
    return scope, state


def test_ten_contexts_close_zero_residual_after_agent_stop(tmp_path: Path) -> None:
    scope, state = _scope_state()
    for cell_index in range(2):
        host, browser, _ = _host(tmp_path, cell_index=cell_index)
        order: list[str] = []
        for index in range(10):
            attempt = f"attempt-host-{cell_index}-{index}"
            application = host.open_application(
                application_id=f"application-host-{cell_index}-{index}",
                actor_id=f"application:{attempt}",
                attempt_id=attempt,
                application_url="https://tenant.example.test/apply",
                scope=scope,
                state=state,
                agent_stop=lambda events=order: events.append("agent_stopped"),
                contain_runtime=lambda: None,
            )
            host.context_runtime.new_page(application.context_lease)
            host.close_application(application)
        metrics = host.context_runtime.metrics
        assert len(order) == 10
        assert metrics.contexts_created == metrics.contexts_closed == 10
        assert metrics.active_contexts == 0
        assert metrics.pages_after_close == 0
        assert metrics.frames_after_close == 0
        assert metrics.service_workers_after_close == 0
        assert all(not context.pages for context in browser.contexts)


def test_residual_context_quarantines_generation(tmp_path: Path) -> None:
    host, _browser, path = _host(tmp_path, residual=True)
    scope, state = _scope_state()
    attempt = "attempt-residual"
    application = host.open_application(
        application_id="application-residual",
        actor_id=f"application:{attempt}",
        attempt_id=attempt,
        application_url="https://tenant.example.test/apply",
        scope=scope,
        state=state,
        agent_stop=lambda: None,
        contain_runtime=lambda: None,
    )
    host.context_runtime.new_page(application.context_lease)
    with pytest.raises(runtime_cells.RuntimeCellQuarantinedError):
        host.close_application(application)
    connection = _connection(path)
    generation = runtime_cells.get_generation(connection, "runtime-cell-0", 1)
    connection.close()
    assert generation is not None and generation.status == "quarantined"


def test_agent_stop_failure_terminally_contains_context_and_browser(
    tmp_path: Path,
) -> None:
    host, browser, path = _host(tmp_path)
    scope, state = _scope_state()
    events: list[str] = []
    attempt = "attempt-stop-failure"

    def fail_stop() -> None:
        events.append("stop")
        raise RuntimeError("synthetic stop failure")

    application = host.open_application(
        application_id="application-stop-failure",
        actor_id=f"application:{attempt}",
        attempt_id=attempt,
        application_url="https://tenant.example.test/apply",
        scope=scope,
        state=state,
        agent_stop=fail_stop,
        contain_runtime=lambda: events.append("contain_runtime"),
    )
    host.context_runtime.new_page(application.context_lease)
    with pytest.raises(runtime_cells.RuntimeCellQuarantinedError):
        host.close_application(application)
    assert events == ["stop", "contain_runtime"]
    assert host.context_runtime.metrics.active_contexts == 0
    assert host.context_runtime.metrics.closed is True
    assert browser.closed is True
    connection = _connection(path)
    generation = runtime_cells.get_generation(connection, "runtime-cell-0", 1)
    connection.close()
    assert generation is not None and generation.status == "quarantined"


def test_generation_and_process_runtime_identity_cannot_be_adopted(tmp_path: Path) -> None:
    connection = _connection(tmp_path / "identity.sqlite3")
    _register(connection, 0)
    with pytest.raises(runtime_cells.RuntimeCellConflictError):
        runtime_cells.register_generation(
            connection,
            cell_id="runtime-cell-0",
            generation=1,
            runtime_id="other-runtime",
            source_identity=SOURCE,
            process_id=999,
            process_birth_time=999,
        )
    with pytest.raises(runtime_cells.RuntimeCellConflictError):
        runtime_cells.register_generation(
            connection,
            cell_id="runtime-cell-1",
            generation=1,
            runtime_id="runtime-1",
            source_identity=SOURCE,
            process_id=1000,
            process_birth_time=2000,
        )


def test_job_attempt_and_cell_claim_share_savepoint_and_skip_domain_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = init_db(tmp_path / "jobs.sqlite3")
    for name in ("alpha", "beta"):
        url = f"https://{name}.example.test/apply"
        connection.execute(
            "INSERT INTO jobs(url,application_url,title,company_name,fit_score,"
            "tailored_resume_path,tailor_status,cover_letter_status,eligibility_status) "
            "VALUES(?,?,?,'Example',9,'missing-resume.pdf','machine_validated',"
            "'not_required','eligible')",
            (url, url, name),
        )
    connection.commit()
    monkeypatch.setattr(application_jobs.config, "load_profile", lambda: {"submission_policy": {}})
    monkeypatch.setattr("applypilot.eligibility.refresh_job_eligibility", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "applypilot.apply.submission_admission.evaluate_submission_admission",
        lambda *_a, **_k: {"admitted": True},
    )
    attempts_seen: list[str] = []

    def claim(_connection: sqlite3.Connection, job: dict, attempt_id: str) -> object:
        attempts_seen.append(attempt_id)
        if "alpha.example.test" in job["url"]:
            raise runtime_cells.RuntimeCellConflictError("same domain busy")
        return {"lease": "beta"}

    acquired = application_jobs.acquire_job(
        connection,
        min_score=6,
        preview_only=True,
        load_blocked=lambda: ([], []),
        application_lease_minutes=45,
        runtime_cell_claim=claim,
    )
    assert acquired is not None
    assert acquired["url"] == "https://beta.example.test/apply"
    assert acquired["_runtime_cell_lease"] == {"lease": "beta"}
    persisted = connection.execute("SELECT job_url,status FROM application_attempts ORDER BY job_url").fetchall()
    assert [(row[0], row[1]) for row in persisted] == [("https://beta.example.test/apply", "in_progress")]
    assert len(attempts_seen) == 2
