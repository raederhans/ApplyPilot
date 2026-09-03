from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from applypilot.apply import worker_orchestration
from applypilot.apply.application_episode import (
    EpisodeConflict,
    EpisodeParked,
    application_command,
    bounded_form_replan,
    build_job_evidence_bundle,
    command_result,
    create_episode,
    episode_from_job,
    execute_application_command,
    get_episode,
    persist_bounded_form_replan,
    persist_job_evidence_bundle,
)
from applypilot.apply.application_sessions import ContextBundle, EndpointDescriptor
from applypilot.apply.browser_broker import BrowserBroker
from applypilot.apply.contracts import application_actor_id
from applypilot.apply.control_descriptors import ControlDescriptor, FormInspection, FormSurface
from applypilot.apply.runtime_namespace import RuntimeNamespace
from applypilot.storage import agent_control

NOW = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)


def _runtime(tmp_path: Path, *, attempt_id: str = "attempt-p3"):
    actor_id = application_actor_id(attempt_id)
    broker = BrowserBroker()
    lease = broker.acquire_bundle(
        profile_id="profile-p3",
        page_id="page-p3",
        owner_id=actor_id,
        scope_id="scope-p3",
        attempt_id=attempt_id,
        runtime_id="runtime-p3",
    )
    context = ContextBundle(
        namespace=RuntimeNamespace(
            root=tmp_path,
            run_id="run-p3",
            session_id="session-p3",
            profile_id="profile-p3",
        ),
        worker_id=0,
        application_session_id="application-p3",
        actor_id=actor_id,
        attempt_id=attempt_id,
        phase="prepare",
        runtime_backend="test",
        browser_runtime="chromium",
        browser_profile_id="profile-p3",
        browser_generation=1,
        endpoint=EndpointDescriptor(
            endpoint_id="endpoint-p3",
            generation=1,
            transport="http",
            address="http://127.0.0.1:8931/mcp",
            reusable=True,
        ),
        root_target_ids=("target-p3",),
        page_binding=lease.page_binding.as_dict(),
    )
    job = {
        "company": "Example",
        "title": "Analyst",
        "application_url": "https://tenant.myworkdayjobs.com/apply",
        "source_url": "https://example.test/job/1",
        "_attempt_id": attempt_id,
        "_application_session_id": context.application_session_id,
        "_browser_lease_binding": lease.as_dict(),
        "_control_contract": {"contract": "current-page-only"},
        "_answer_provenance_binding": {
            "opaque_binding_seed": "a" * 64,
            "fact_scopes": ["global:candidate"],
        },
    }
    return broker, lease, context, job


def _evidence_and_episode(tmp_path: Path, *, attempt_id: str = "attempt-p3"):
    _broker, lease, context, job = _runtime(tmp_path, attempt_id=attempt_id)
    evidence = build_job_evidence_bundle(
        job,
        {},
        attempt_id=attempt_id,
        now=NOW,
    )
    assert evidence.status == "ready"
    episode = episode_from_job(job, run_id="run-p3", evidence=evidence, now=NOW)
    return lease, context, job, evidence, episode


def _descriptor(context: ContextBundle, lease, *, page_epoch: int = 0) -> ControlDescriptor:
    binding = replace(lease.page_binding, page_epoch=page_epoch)
    return ControlDescriptor(
        descriptor_id="d" * 64,
        actor_id=context.actor_id,
        attempt_id=context.attempt_id,
        application_session_id=context.application_session_id,
        browser_generation=context.browser_generation,
        provider="workday",
        page_binding=binding,
        surface_id="s" * 64,
        frame_path=(),
        frame_url="https://tenant.myworkdayjobs.com/apply",
        shadow_path=(),
        locator="#legal-name",
        kind="text",
        semantic="legal_name",
        label="Legal name",
        required=True,
        writable=True,
        stateful=False,
    )


def _inspection(context: ContextBundle, lease, descriptor: ControlDescriptor) -> FormInspection:
    bound_context = replace(context, page_binding=descriptor.page_binding.as_dict())
    return FormInspection(
        provider="workday",
        context=bound_context,
        page_binding=descriptor.page_binding,
        surfaces=(
            FormSurface(
                surface_id="s" * 64,
                frame_path=(),
                frame_url="https://tenant.myworkdayjobs.com/apply",
                origin="https://tenant.myworkdayjobs.com",
                control_count=1,
            ),
        ),
        controls=(descriptor,),
        proof_complete=True,
    )


def test_episode_replays_typed_result_without_repeating_admitted_action(tmp_path: Path) -> None:
    _lease, _context, _job, evidence, episode = _evidence_and_episode(tmp_path)
    connection = sqlite3.connect(":memory:")
    assert persist_job_evidence_bundle(connection, evidence)
    assert create_episode(connection, episode)
    connection.commit()
    command = application_command(
        episode,
        kind="recovery",
        action="restart_endpoint",
        recovery_ref="recovery:p3",
        now=NOW + timedelta(seconds=1),
    )
    calls = 0

    def executor(current):
        nonlocal calls
        calls += 1
        return command_result(
            current,
            status="verified",
            outcome="endpoint_restarted_and_verified",
            effect_applied=True,
            occurred_at=NOW + timedelta(seconds=2),
        )

    first = execute_application_command(connection, command, executor)
    replay = execute_application_command(connection, command, executor)

    assert first.status == "verified"
    assert replay.replayed is True
    assert calls == 1
    events = agent_control.list_events(connection, attempt_id=episode.attempt_id)
    assert [item.event_type for item in events] == [
        "application.command.admitted",
        "application.command.result",
    ]
    stored = get_episode(connection, episode.episode_id)
    assert stored is not None
    assert (stored.command_sequence, stored.result_sequence) == (1, 1)


def test_crash_after_admission_parks_without_reexecuting_effect(tmp_path: Path) -> None:
    _lease, _context, _job, evidence, episode = _evidence_and_episode(tmp_path)
    connection = sqlite3.connect(":memory:")
    persist_job_evidence_bundle(connection, evidence)
    create_episode(connection, episode)
    connection.commit()
    command = application_command(
        episode,
        kind="recovery",
        action="restart_endpoint",
        recovery_ref="recovery:crash",
        now=NOW + timedelta(seconds=1),
    )
    calls = 0

    def crash_after_admission(_current):
        nonlocal calls
        calls += 1
        raise SystemExit("simulated process loss")

    with pytest.raises(SystemExit, match="simulated process loss"):
        execute_application_command(connection, command, crash_after_admission)

    replay = execute_application_command(connection, command, crash_after_admission)

    assert calls == 1
    assert replay.status == "effect_unknown"
    assert replay.outcome == "admitted_effect_outcome_unknown"
    assert get_episode(connection, episode.episode_id).state == "parked"  # type: ignore[union-attr]


def test_stale_episode_revision_is_rejected_before_executor(tmp_path: Path) -> None:
    _lease, _context, _job, evidence, episode = _evidence_and_episode(tmp_path)
    connection = sqlite3.connect(":memory:")
    persist_job_evidence_bundle(connection, evidence)
    create_episode(connection, episode)
    connection.commit()
    stale = application_command(
        episode,
        kind="recovery",
        action="restart_endpoint",
        recovery_ref="recovery:stale",
    )
    first = application_command(
        episode,
        kind="checkpoint",
        action="record_checkpoint",
    )
    execute_application_command(
        connection,
        first,
        lambda current: command_result(
            current,
            status="verified",
            outcome="checkpoint_recorded",
            effect_applied=False,
        ),
    )
    called = False

    def forbidden(_current):
        nonlocal called
        called = True
        raise AssertionError("must not execute")

    with pytest.raises(EpisodeConflict, match="stale"):
        execute_application_command(connection, stale, forbidden)
    assert called is False


def test_missing_and_conflicting_material_facts_require_human(tmp_path: Path) -> None:
    _broker, _lease, _context, job = _runtime(tmp_path)
    missing = build_job_evidence_bundle(
        job,
        {},
        attempt_id="attempt-p3",
        required_fact_keys=("legal_name",),
        now=NOW,
    )
    assert missing.status == "unavailable"
    assert "material_fact_unavailable:legal_name" in missing.unavailable
    assert episode_from_job(job, run_id="run-p3", evidence=missing, now=NOW).state == "human_required"

    job["_answer_provenance_binding"]["fact_scopes"] = [  # type: ignore[index]
        "global:candidate",
        "job:example",
    ]
    profile = {
        "application_facts": [
            {
                "fact_ref": "fact:legal:global",
                "key": "legal_name",
                "value": "Candidate A",
                "source": "candidate",
                "scope": "global:candidate",
                "confirmed_at": "2026-09-01T00:00:00+00:00",
            },
            {
                "fact_ref": "fact:legal:job",
                "key": "legal_name",
                "value": "Candidate B",
                "source": "candidate",
                "scope": "job:example",
                "confirmed_at": "2026-09-01T00:00:00+00:00",
            },
        ]
    }
    conflicted = build_job_evidence_bundle(
        job,
        profile,
        attempt_id="attempt-p3",
        required_fact_keys=("legal_name",),
        now=NOW,
    )
    assert conflicted.status == "conflicted"
    assert conflicted.conflicts == ("fact_conflict:legal_name",)


def test_evidence_identity_is_stable_across_rebuild_time(tmp_path: Path) -> None:
    _broker, _lease, _context, job = _runtime(tmp_path)
    first = build_job_evidence_bundle(job, {}, attempt_id="attempt-p3", now=NOW)
    second = build_job_evidence_bundle(
        job,
        {},
        attempt_id="attempt-p3",
        now=NOW + timedelta(minutes=5),
    )
    assert first.bundle_id == second.bundle_id
    assert first.evidence_digest == second.evidence_digest
    connection = sqlite3.connect(":memory:")
    assert persist_job_evidence_bundle(connection, first)
    assert persist_job_evidence_bundle(connection, second) is False


def test_bounded_same_application_replan_is_durable_and_one_shot(tmp_path: Path) -> None:
    lease, context, _job, evidence, episode = _evidence_and_episode(tmp_path)
    before_descriptor = _descriptor(context, lease, page_epoch=0)
    after_descriptor = replace(
        _descriptor(context, lease, page_epoch=1),
        locator="#legal-name-current",
    )
    before = _inspection(context, lease, before_descriptor)
    after = _inspection(context, lease, after_descriptor)
    command = application_command(
        episode,
        kind="browser_control",
        action="set_text",
        descriptor=before_descriptor,
        value_ref="fact:legal-name",
        now=NOW + timedelta(seconds=1),
    )
    decision = bounded_form_replan(
        episode,
        command,
        before=before,
        after=after,
        now=NOW + timedelta(seconds=2),
    )
    assert decision.authorized is True
    assert decision.replacement is not None
    assert decision.replacement.descriptor_digest == after_descriptor.locator_digest

    connection = sqlite3.connect(":memory:")
    persist_job_evidence_bundle(connection, evidence)
    create_episode(connection, episode)
    connection.commit()
    advanced, replacement = persist_bounded_form_replan(connection, episode, decision)
    assert advanced.replan_count == 1
    assert advanced.page_epoch == 1
    result = execute_application_command(
        connection,
        replacement,
        lambda current: command_result(
            current,
            status="verified",
            outcome="control_verified",
            effect_applied=True,
            resulting_page_epoch=2,
        ),
    )
    assert result.status == "verified"
    with pytest.raises(EpisodeParked, match="replan_budget_exhausted"):
        persist_bounded_form_replan(connection, advanced, replace(decision, authorized=False, reason="replan_budget_exhausted"))


def test_replan_rejects_stale_or_different_application(tmp_path: Path) -> None:
    lease, context, _job, _evidence, episode = _evidence_and_episode(tmp_path)
    descriptor = _descriptor(context, lease, page_epoch=0)
    before = _inspection(context, lease, descriptor)
    stale_after = _inspection(
        context,
        lease,
        replace(descriptor, descriptor_id="e" * 64, page_binding=replace(lease.page_binding, page_epoch=1)),
    )
    command = application_command(
        episode,
        kind="browser_control",
        action="set_text",
        descriptor=descriptor,
        value_ref="fact:legal-name",
    )
    denied = bounded_form_replan(episode, command, before=before, after=stale_after)
    assert denied.authorized is False
    assert denied.reason == "replan_descriptor_absent_or_ambiguous"

    other_context = replace(context, application_session_id="application-other")
    other_descriptor = replace(
        descriptor,
        application_session_id="application-other",
        page_binding=replace(lease.page_binding, page_epoch=1),
    )
    other = _inspection(other_context, lease, other_descriptor)
    with pytest.raises(EpisodeParked, match="identity changed"):
        bounded_form_replan(episode, command, before=before, after=other)


def test_episode_remains_diagnostic_not_production_recovery_authority() -> None:
    assert not hasattr(worker_orchestration, "_execute_recovery_with_episode")
    assert "application_episode" not in Path(worker_orchestration.__file__).read_text(
        encoding="utf-8"
    )
