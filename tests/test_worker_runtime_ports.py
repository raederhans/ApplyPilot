from __future__ import annotations

from dataclasses import fields

from applypilot.apply import launcher, worker_orchestration


def test_page_observation_ports_are_separate_from_application_execution() -> None:
    assert len(fields(worker_orchestration.WorkerApplicationPorts)) == 14
    assert len(fields(worker_orchestration.WorkerPageObservationPorts)) == 6
    assert not {
        "audit_live_pre_submit_page",
        "classify_post_submit_observation",
        "click_linkedin_main_apply_causally",
        "observe_post_submit_page",
    }.intersection(
        item.name for item in fields(worker_orchestration.WorkerApplicationPorts)
    )


def test_launcher_composes_migrated_public_observation_modules() -> None:
    runtime = launcher._worker_runtime_ports()

    assert (
        runtime.observation.click_linkedin_main_apply_causally
        is launcher.linkedin_page_observation_mod.click_linkedin_main_apply_causally
    )
    assert (
        runtime.observation.observe_post_submit_page
        is launcher.post_submit_observation_mod.observe_post_submit_page
    )
    assert (
        runtime.submission.submission_evidence_consistent
        is launcher.post_submit_observation_mod.submission_evidence_consistent
    )
