from types import SimpleNamespace

import pytest
from test_launcher_durable_runtime import _configure_launcher, _job
from test_prepare_fast_path import _contract

from applypilot.apply import launcher


@pytest.mark.parametrize(
    ("batch_result", "expected_status", "agent_invoked"),
    [
        ({"status": "verified", "effect_count": 1, "legacy_fallback_safe": False}, "ready_to_submit", False),
        ({"status": "fallback", "effect_count": 0, "legacy_fallback_safe": True}, "ready_to_submit", True),
        ({"status": "parked", "effect_count": 1, "legacy_fallback_safe": False}, "failed:manual_review_required", False),
    ],
)
def test_real_launcher_runs_bound_host_prepare_before_agent_and_releases_lease(
    monkeypatch, tmp_path, batch_result, expected_status, agent_invoked
) -> None:
    events = []
    _configure_launcher(monkeypatch, tmp_path, events)
    monkeypatch.setenv("APPLYPILOT_SEMANTIC_BATCH_MODE", "canary")
    audit, plan = _contract()
    job = _job(
        "host-prepare-attempt",
        application_url="https://example.wd3.myworkdayjobs.com/en-US/Careers/job/role",
    )
    supervisor = SimpleNamespace(
        attempt_id=job["_attempt_id"],
        application_session_id="host-prepare-session",
        browser_worker=SimpleNamespace(generation=1),
        bind_browser_authority=lambda bundle: events.append("bound"),
        context_bundle=lambda **kwargs: None,
    )

    def observe(*args):
        assert "_browser_lease_binding" in job
        assert events == ["bound"]
        events.append("audit")
        return "required_field_empty:email", audit

    def prepare(current_job, report):
        assert current_job is job and report is audit
        events.append("plan")
        return plan

    def execute(*args, **kwargs):
        assert kwargs["application_supervisor"] is supervisor
        events.append("batch")
        return batch_result

    monkeypatch.setattr(launcher, "_audit_live_pre_submit_page", observe)
    monkeypatch.setattr(launcher, "_prepare_ats_fill_plan_repair", prepare)
    monkeypatch.setattr(launcher, "_try_semantic_batch_fill", execute)
    release = launcher._release_application_browser_authority

    def release_lease(current_job):
        events.append("released")
        release(current_job)

    monkeypatch.setattr(launcher, "_release_application_browser_authority", release_lease)
    status, duration = launcher.run_job(
        job,
        port=9432,
        model="model",
        agent_backend="codex",
        submission_phase="prepare",
        application_supervisor=supervisor,
    )
    assert status.startswith(expected_status)
    assert duration >= 0
    assert events[:4] == ["bound", "audit", "plan", "batch"]
    assert ("popen" in events) is agent_invoked
    assert events[-1] == "released"
    assert job["_prepare_fast_path"]["agent_invoked"] is agent_invoked
    assert "_submission_gate" not in job
    if not agent_invoked:
        assert "_agent_turn_result" not in job


def test_raw_input_checkbox_cannot_enter_the_routine_writer() -> None:
    from test_prepare_fast_path import _run

    audit, plan = _contract(control="input")
    audit["ats_fill_plan_snapshot"]["form_fields"][0]["type"] = "checkbox"
    result, calls, _ = _run(audit=audit, plan=plan)
    assert result.disposition == "continue_agent"
    assert calls == ["audit", "plan"]
