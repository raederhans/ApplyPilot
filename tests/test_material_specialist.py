from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from applypilot.apply import launcher, prompt, worker_orchestration
from applypilot.apply.authorization import (
    compute_file_binding,
    compute_job_fingerprint,
)
from applypilot.apply.contracts import TaskResult, TaskSpec, contract_json
from applypilot.apply.material_readiness import material_snapshot_identity
from applypilot.apply.specialists import (
    READ_ONLY_SPECIALIST_AUTHORITY,
    ProductionSpecialistSpec,
    SpecialistCancelled,
    SpecialistDeadlineExceeded,
    production_specialist,
    production_specialist_spec,
    run_context_specialist,
    run_durable_material_specialist,
    run_system_specialist,
)
from applypilot.storage import task_journal


def _ready_job(tmp_path: Path) -> dict[str, object]:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-test")
    job = {
        "url": "https://example.test/job/1",
        "tailored_resume_path": str(resume),
        "tailor_status": "machine_validated",
        "cover_letter_status": "not_required",
    }
    digest, size = compute_file_binding(resume)
    job["_authorization_entry"] = {
        "resume_path": str(resume.resolve()),
        "resume_sha256": digest,
        "resume_size": size,
        "job_fingerprint": compute_job_fingerprint(job),
    }
    return job


def test_production_registry_rejects_unknown_specialist() -> None:
    with pytest.raises(ValueError, match="not allowlisted"):
        production_specialist("arbitrary-callable")


def test_shadow_reports_block_without_enforcing(tmp_path: Path) -> None:
    run = run_system_specialist("material-readiness-v1", {}, mode="shadow")
    assert run is not None
    assert run.result["ready"] is False
    assert run.enforced is False
    assert [item["event_type"] for item in run.telemetry] == [
        "agent.proposal.emitted",
        "agent.proposal.executed",
        "agent.proposal.consumed",
        "agent.proposal.changed_decision",
    ]


def test_enforce_changes_decision_for_missing_material() -> None:
    run = run_system_specialist("material-readiness-v1", {}, mode="enforce")
    assert run is not None and run.enforced is True
    assert run.telemetry[-1]["changed"] is True


def test_launcher_preflight_system_seeds_material_before_provider_work(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(launcher.config, "load_profile", lambda: {
        "agent_runtime": {"orchestration": {"material_specialist_mode": "enforce"}}
    })
    db_path = tmp_path / "preflight.db"
    monkeypatch.setattr(launcher, "get_connection", lambda: sqlite3.connect(db_path))
    result = launcher._run_read_only_preflight(_ready_job(tmp_path))
    assert result["material_readiness"]["ready"] is True
    assert result["material_enforced_block"] is False
    assert len(result["proposal_feedback"]) == 4


def test_enforce_preflight_fails_closed_when_specialist_runner_fails(
    monkeypatch, tmp_path: Path
) -> None:
    from applypilot.apply import specialists

    def broken_specialist(_job):
        raise PermissionError("material bytes unavailable")

    monkeypatch.setattr(launcher.config, "load_profile", lambda: {
        "agent_runtime": {"orchestration": {"material_specialist_mode": "enforce"}}
    })
    monkeypatch.setitem(
        specialists._PRODUCTION_SPECIALISTS,
        "material-readiness-v1",
        broken_specialist,
    )
    db_path = tmp_path / "preflight-failure.db"
    monkeypatch.setattr(launcher, "get_connection", lambda: sqlite3.connect(db_path))

    result = launcher._run_read_only_preflight(_ready_job(tmp_path))

    assert result["material_enforced_block"] is True
    assert result["material_readiness"]["state"] == "blocked"
    assert result["material_readiness"]["missing_kinds"] == [
        "material_specialist_unavailable"
    ]


def test_runtime_cover_discovery_does_not_false_block(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(launcher.config, "load_profile", lambda: {
        "submission_policy": {"allow_runtime_cover_letter_discovery": True},
        "agent_runtime": {"orchestration": {"material_specialist_mode": "enforce"}},
    })
    db_path = tmp_path / "preflight.db"
    monkeypatch.setattr(launcher, "get_connection", lambda: sqlite3.connect(db_path))
    job = _ready_job(tmp_path)
    job["cover_letter_status"] = None
    result = launcher._run_read_only_preflight(job)
    assert result["material_readiness"]["ready"] is True
    assert result["material_readiness"]["runtime_cover_discovery"] is True


def test_required_material_contract_separates_known_missing_and_unknown(
    tmp_path: Path,
) -> None:
    job = _ready_job(tmp_path)
    job["required_materials"] = ["Academic transcript", "Essay response attachment"]
    result = run_system_specialist("material-readiness-v1", job, mode="enforce")
    assert result is not None
    assert result.result["state"] == "blocked"
    assert result.result["missing_kinds"] == ["transcript"]
    assert result.result["unknown_required_labels"] == ["Essay response attachment"]


def test_optional_transcript_is_not_promoted_to_a_material_block(tmp_path: Path) -> None:
    job = _ready_job(tmp_path)
    job["required_materials"] = [
        {"label": "Academic transcript", "required": False}
    ]

    result = run_system_specialist("material-readiness-v1", job, mode="enforce")

    assert result is not None
    assert result.result["state"] == "ready"
    assert "transcript" not in result.result["missing_kinds"]


def test_byte_drift_after_manifest_binding_is_blocked(tmp_path: Path) -> None:
    job = _ready_job(tmp_path)
    Path(str(job["tailored_resume_path"])).write_bytes(b"%PDF-drift")
    result = run_system_specialist("material-readiness-v1", job, mode="enforce")
    assert result is not None
    assert "resume_byte_binding_mismatch" in result.result["missing_kinds"]


def test_durable_specialist_replays_without_second_runner_and_persists_feedback(
    monkeypatch, tmp_path: Path
) -> None:
    from applypilot.apply import specialists

    db_path = tmp_path / "journal.db"
    calls = 0
    original = specialists._PRODUCTION_SPECIALISTS["material-readiness-v1"]

    def counted(job):
        nonlocal calls
        calls += 1
        return original(job)

    monkeypatch.setitem(
        specialists._PRODUCTION_SPECIALISTS,
        "material-readiness-v1",
        counted,
    )
    first_connection = sqlite3.connect(db_path)
    first = run_durable_material_specialist(
        first_connection,
        _ready_job(tmp_path),
        mode="enforce",
        attempt_id="attempt-1",
        workflow_id="workflow-1",
    )
    first_connection.close()
    second_connection = sqlite3.connect(db_path)
    second = run_durable_material_specialist(
        second_connection,
        _ready_job(tmp_path),
        mode="enforce",
        attempt_id="attempt-1",
        workflow_id="workflow-1",
    )
    assert first is not None and first.replay is False
    assert second is not None and second.replay is True
    assert calls == 1
    assert second_connection.execute("SELECT COUNT(*) FROM agent_tasks").fetchone()[0] == 1
    event_types = [
        row[0]
        for row in second_connection.execute(
            "SELECT event_type FROM agent_events ORDER BY event_id"
        )
    ]
    assert {
        "agent.proposal.emitted",
        "agent.proposal.executed",
        "agent.proposal.consumed",
        "agent.proposal.changed_decision",
    } <= set(event_types)
    replay_payload = second_connection.execute(
        "SELECT payload_json FROM agent_events WHERE event_id LIKE '%:replay:%:executed'"
    ).fetchone()[0]
    assert '"replay":true' in replay_payload


def test_durable_specialist_can_replay_shadow_result_under_enforce_mode(
    tmp_path: Path,
) -> None:
    connection = sqlite3.connect(tmp_path / "journal.db")
    job = _ready_job(tmp_path)

    shadow = run_durable_material_specialist(
        connection,
        job,
        mode="shadow",
        attempt_id="attempt-shadow",
        workflow_id="workflow-shadow",
    )
    enforced = run_durable_material_specialist(
        connection,
        job,
        mode="enforce",
        attempt_id="attempt-enforce",
        workflow_id="workflow-enforce",
    )

    assert shadow is not None and shadow.replay is False
    assert enforced is not None and enforced.replay is True
    assert enforced.mode == "required"


def test_durable_material_replays_legacy_completed_read_spec(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "legacy.db")
    job = _ready_job(tmp_path)
    identity = material_snapshot_identity(job)
    fingerprint = identity["job_fingerprint"]
    snapshot = identity["material_snapshot_digest"]
    proposal_id = f"material-readiness-v1:{fingerprint[:12]}:{snapshot[:12]}"
    task_id = f"task:{proposal_id}"
    legacy_spec = TaskSpec(
        task_id=task_id,
        kind="material-readiness-v1",
        objective="Evaluate byte-bound submission material readiness.",
        inputs={
            "specialist_version": "material-readiness-v1",
            "job_fingerprint": fingerprint,
            "material_snapshot_digest": snapshot,
        },
        effect_class="read",
        idempotency_key=proposal_id,
    )
    legacy_result = TaskResult(
        task_id=task_id,
        status="completed",
        output={"material_readiness": {"state": "ready", "ready": True}},
    )
    task_journal.ensure_schema(connection)
    now = "2026-09-04T00:00:00+00:00"
    connection.execute(
        "INSERT INTO agent_tasks(task_id,idempotency_key,attempt_id,workflow_id,"
        "proposal_id,spec_digest,spec_json,effect_class,status,result_json,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            task_id,
            proposal_id,
            "legacy-attempt",
            "legacy-workflow",
            proposal_id,
            task_journal.spec_digest(legacy_spec),
            task_journal._json(contract_json(legacy_spec)),
            "read",
            "completed",
            task_journal._json(contract_json(legacy_result)),
            now,
            now,
        ),
    )
    connection.commit()

    replay = run_durable_material_specialist(
        connection,
        job,
        mode="required",
        attempt_id="new-attempt",
        workflow_id="new-workflow",
    )

    assert replay is not None and replay.replay is True
    assert replay.result == {"state": "ready", "ready": True}


@pytest.mark.parametrize(
    ("mode", "expected_mode", "enforced"),
    [
        ("shadow", "shadow", False),
        ("advisory", "advisory", False),
        ("required", "required", True),
        ("enforce", "required", True),
    ],
)
def test_material_specialist_modes_have_distinct_authority(
    mode: str, expected_mode: str, enforced: bool
) -> None:
    run = run_system_specialist("material-readiness-v1", {}, mode=mode)

    assert run is not None
    assert run.mode == expected_mode
    assert run.enforced is enforced


def test_production_specialist_rejects_browser_authority_and_live_handles() -> None:
    with pytest.raises(ValueError, match="authority"):
        ProductionSpecialistSpec(
            specialist_id="unsafe-v1",
            phases=("prepare",),
            effect_class="read",
            input_schema_version="in-v1",
            output_schema_version="out-v1",
            execution_budget_seconds=1,
            max_output_bytes=100,
            authority_scope=("browser:write",),
            read_only=True,
            capabilities=(),
            retry_categories=(),
            metadata={},
        )
    with pytest.raises(ValueError, match="may not be persisted"):
        run_system_specialist(
            "material-readiness-v1",
            {"browser_handle": "live-handle"},
            mode="advisory",
        )

    assert "submit" not in " ".join(READ_ONLY_SPECIALIST_AUTHORITY)


def test_preflight_shadow_is_telemetry_only_and_advisory_injects_context(
    monkeypatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "preflight-modes.db"
    monkeypatch.setattr(launcher, "get_connection", lambda: sqlite3.connect(db_path))
    job = _ready_job(tmp_path)

    monkeypatch.setattr(launcher.config, "load_profile", lambda: {
        "agent_runtime": {"orchestration": {"material_specialist_mode": "shadow"}}
    })
    shadow = launcher._run_read_only_preflight(job)
    assert shadow["material_readiness"] is None
    assert shadow["specialist_advisories"] == []
    assert shadow["proposal_feedback"]

    monkeypatch.setattr(launcher.config, "load_profile", lambda: {
        "agent_runtime": {"orchestration": {"material_specialist_mode": "advisory"}}
    })
    advisory = launcher._run_read_only_preflight(job)
    assert advisory["material_enforced_block"] is False
    assert advisory["specialist_advisories"][0]["result"]["ready"] is True


def test_preflight_advisory_reaches_the_actual_agent_prompt_reader(
    monkeypatch, tmp_path: Path
) -> None:
    job = _ready_job(tmp_path)
    job["application_url"] = "https://boards.greenhouse.io/acme/jobs/123"
    monkeypatch.setattr(launcher, "get_connection", lambda: sqlite3.connect(tmp_path / "seam.db"))
    monkeypatch.setattr(
        launcher.config,
        "load_profile",
        lambda: {
            "agent_runtime": {
                "orchestration": {
                    "material_specialist_mode": "shadow",
                    "production_specialist_modes": {
                        "provider-classifier-v1": "advisory"
                    },
                }
            }
        },
    )

    preflight = launcher._run_read_only_preflight(job)
    worker_orchestration._inject_preflight_specialist_context(job, preflight)

    rendered = prompt._build_specialist_context_section(job)
    assert "CONSUMED SPECIALIST CONTEXT" in rendered
    assert "provider=greenhouse" in rendered
    assert "submission authority" in rendered


@pytest.mark.parametrize(
    "specialist_id",
    [
        "field-semantic-v1",
        "provider-classifier-v1",
        "application-facts-v1",
        "work-authorization-v1",
        "material-readiness-v1",
        "page-failure-v1",
    ],
)
def test_first_production_specialists_are_read_only_allowlisted(
    specialist_id: str,
) -> None:
    spec = production_specialist_spec(specialist_id)
    assert spec.effect_class == "read"
    assert spec.read_only is True
    assert spec.capabilities == ()
    assert spec.execution_budget_seconds > 0
    assert spec.input_schema_version
    assert spec.output_schema_version


def test_context_specialist_reports_unavailable_and_required_fails_closed() -> None:
    advisory = run_context_specialist(
        "work-authorization-v1", {}, mode="advisory"
    )
    required = run_context_specialist(
        "work-authorization-v1", {}, mode="required"
    )

    assert advisory is not None and advisory.result["state"] == "insufficient"
    assert advisory.enforced is False
    assert required is not None and required.enforced is True


def test_required_unavailable_context_specialist_blocks_preflight(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        launcher.config,
        "load_profile",
        lambda: {
            "agent_runtime": {
                "orchestration": {
                    "material_specialist_mode": "off",
                    "production_specialist_modes": {
                        "work-authorization-v1": "required"
                    },
                }
            }
        },
    )
    monkeypatch.setattr(
        launcher, "get_connection", lambda: sqlite3.connect(tmp_path / "required.db")
    )

    result = launcher._run_read_only_preflight({"url": "https://example.test/job"})

    assert result["material_enforced_block"] is False
    assert result["specialist_required_block"] is True
    assert result["required_specialist_failures"] == ["work-authorization-v1"]


def test_preflight_failure_modes_fail_closed_for_every_applicable_required() -> None:
    material_mode, material_block, required = (
        worker_orchestration._preflight_failure_modes(
            {
                "agent_runtime": {
                    "orchestration": {
                        "material_specialist_mode": "shadow",
                        "production_specialist_modes": {
                            "provider-classifier-v1": "required",
                            "application-facts-v1": "enforce",
                            "field-semantic-v1": "required",
                        },
                    }
                }
            },
            {},
        )
    )

    assert material_mode == "shadow"
    assert material_block is False
    assert required == ["provider-classifier-v1", "application-facts-v1"]


def test_prepare_and_observe_specialists_are_not_run_during_preflight(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        launcher.config,
        "load_profile",
        lambda: {
            "agent_runtime": {
                "orchestration": {
                    "material_specialist_mode": "off",
                    "production_specialist_modes": {
                        "field-semantic-v1": "required",
                        "page-failure-v1": "required",
                    },
                }
            }
        },
    )
    monkeypatch.setattr(
        launcher, "get_connection", lambda: sqlite3.connect(tmp_path / "phases.db")
    )

    result = launcher._run_read_only_preflight({"url": "https://example.test/job"})

    assert result["specialist_required_block"] is False
    statuses = result["specialist_task_statuses"]
    assert statuses["context:field-semantic-v1"] == {
            "status": "skipped",
            "failure_category": "not_applicable_in_preflight",
            "metrics": {},
        }
    assert statuses["context:page-failure-v1"] == {
            "status": "skipped",
            "failure_category": "not_applicable_in_preflight",
            "metrics": {},
        }


@pytest.mark.parametrize(
    ("specialist_id", "snapshot"),
    [
        ("field-semantic-v1", {"field_semantics": {"email": "email"}}),
        ("provider-classifier-v1", {"url": "https://jobs.lever.co/acme/1"}),
        ("application-facts-v1", {"title": "Engineer"}),
        ("work-authorization-v1", {"requires_sponsorship": False}),
        ("page-failure-v1", {"failure_code": "captcha_required"}),
    ],
)
def test_context_specialist_advisory_mode_returns_bounded_context(
    specialist_id: str, snapshot: dict[str, object]
) -> None:
    run = run_context_specialist(specialist_id, snapshot, mode="advisory")

    assert run is not None
    assert run.mode == "advisory"
    assert run.enforced is False
    assert run.result["ready"] is True


def test_material_specialist_deadline_is_cooperative_and_input_is_frozen(
    monkeypatch,
) -> None:
    from applypilot.apply import specialists

    original: dict[str, object] = {"title": "original"}

    def slow(snapshot):
        snapshot["title"] = "mutated"
        time.sleep(0.02)
        return {"ready": True}

    monkeypatch.setitem(
        specialists._PRODUCTION_SPECIALISTS, "material-readiness-v1", slow
    )
    started = time.perf_counter()
    with pytest.raises(SpecialistDeadlineExceeded):
        run_system_specialist(
            "material-readiness-v1",
            original,
            mode="advisory",
            timeout_seconds=0.01,
        )
    assert time.perf_counter() - started >= 0.02
    assert original == {"title": "original"}

    with pytest.raises(SpecialistCancelled):
        run_system_specialist(
            "material-readiness-v1",
            {},
            mode="advisory",
            cancelled=lambda: True,
        )


def test_material_supply_change_invalidates_blocked_replay(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "journal.db")
    job = _ready_job(tmp_path)
    job["required_materials"] = ["Academic transcript"]

    blocked = run_durable_material_specialist(
        connection,
        job,
        mode="enforce",
        attempt_id="attempt-blocked",
        workflow_id="workflow-blocked",
    )
    job["provided_materials"] = {"transcript": True}
    ready = run_durable_material_specialist(
        connection,
        job,
        mode="enforce",
        attempt_id="attempt-ready",
        workflow_id="workflow-ready",
    )

    assert blocked is not None and blocked.result["state"] == "blocked"
    assert ready is not None and ready.result["state"] == "ready"
    assert ready.replay is False
    assert ready.task_id != blocked.task_id
