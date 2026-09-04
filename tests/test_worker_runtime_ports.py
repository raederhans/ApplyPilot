from __future__ import annotations

import sqlite3
from dataclasses import fields
from pathlib import Path

from applypilot.apply import launcher, worker_orchestration
from applypilot.apply.runtime_cell_coordinator import RuntimeCellCoordinator
from applypilot.storage import runtime_cells


def test_page_observation_ports_are_separate_from_application_execution() -> None:
    assert len(fields(worker_orchestration.WorkerApplicationPorts)) == 15
    assert len(fields(worker_orchestration.WorkerPageObservationPorts)) == 6
    assert "try_semantic_batch_fill" in {item.name for item in fields(worker_orchestration.WorkerApplicationPorts)}
    assert not {
        "audit_live_pre_submit_page",
        "classify_post_submit_observation",
        "click_linkedin_main_apply_causally",
        "observe_post_submit_page",
    }.intersection(item.name for item in fields(worker_orchestration.WorkerApplicationPorts))


def test_launcher_composes_migrated_public_observation_modules() -> None:
    runtime = launcher._worker_runtime_ports()

    assert (
        runtime.observation.click_linkedin_main_apply_causally
        is launcher.linkedin_page_observation_mod.click_linkedin_main_apply_causally
    )
    assert runtime.observation.observe_post_submit_page is launcher.post_submit_observation_mod.observe_post_submit_page
    assert (
        runtime.submission.submission_evidence_consistent
        is launcher.post_submit_observation_mod.submission_evidence_consistent
    )
    assert runtime.runtime_cells.production_enabled is False
    assert runtime.runtime_cells.coordinator_factory is RuntimeCellCoordinator
    assert "submit" not in {item.name for item in fields(worker_orchestration.WorkerRuntimeCellPorts)}
    assert "receipt" not in {item.name for item in fields(worker_orchestration.WorkerRuntimeCellPorts)}


def test_production_shadow_session_claims_and_closes_exact_cell_lifecycle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "runtime-cell-shadow.sqlite3"
    monkeypatch.setenv("APPLYPILOT_RUNTIME_CELL_MODE", "shadow")
    monkeypatch.delenv("APPLYPILOT_RUNTIME_CELL_ADMISSION_MANIFEST", raising=False)

    def connection_factory() -> sqlite3.Connection:
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        return connection

    runtime = launcher._worker_runtime_ports()
    runtime = worker_orchestration.WorkerRuntimePorts(
        host=runtime.host,
        browser=runtime.browser,
        jobs=runtime.jobs,
        application=runtime.application,
        observation=runtime.observation,
        submission=runtime.submission,
        operator=runtime.operator,
        runtime_cells=worker_orchestration.WorkerRuntimeCellPorts(
            resolve_admission=runtime.runtime_cells.resolve_admission,
            coordinator_factory=runtime.runtime_cells.coordinator_factory,
            host_factory=runtime.runtime_cells.host_factory,
            connection_factory=connection_factory,
            source_root=Path(__file__).resolve().parents[1],
            process_identity=lambda: (12345, 67890),
            production_enabled=False,
        ),
    )
    session = worker_orchestration._open_runtime_cell_shadow_session(
        runtime,
        requested_workers=2,
    )
    assert session is not None
    assert session.coordinator.decision.effective_cells == 1
    assert session.coordinator.decision.production_authority is False

    job = {
        "url": "https://shadow.example.test/apply",
        "application_url": "https://shadow.example.test/apply",
        "site": "example",
        "_attempt_id": "attempt-shadow",
    }
    connection = connection_factory()
    token = session.claim(connection, job, "attempt-shadow")
    connection.commit()
    connection.close()
    job["_runtime_cell_lease"] = token
    stopped: list[str] = []
    session.open_job(
        job,
        agent_stop=lambda: stopped.append("agent"),
        contain_runtime=lambda: stopped.append("contain"),
    )
    session.close_application()
    session.close()

    assert stopped == ["agent"]
    assert job["_runtime_cell_shadow"]["context_evidence"] == "logical_no_io"
    connection = connection_factory()
    generation = runtime_cells.get_generation(connection, "runtime-cell-0", 1)
    lease_status = connection.execute(
        "SELECT status FROM runtime_cell_leases WHERE lease_id=?",
        (token.lease_id,),
    ).fetchone()[0]
    connection.close()
    assert generation is not None and generation.status == "closed"
    assert lease_status == "released"
