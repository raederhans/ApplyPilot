from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from applypilot.apply import launcher
from applypilot.apply.authorization import (
    compute_file_binding,
    compute_job_fingerprint,
)
from applypilot.apply.specialists import (
    production_specialist,
    run_durable_material_specialist,
    run_system_specialist,
)


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
    assert enforced.mode == "enforce"


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
