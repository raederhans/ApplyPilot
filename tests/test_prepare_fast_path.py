from __future__ import annotations

import pytest

from applypilot.apply.prepare_fast_path import run_prepare_fast_path


def _contract(*, semantic: str = "email", control: str = "email"):
    action = "select" if control in {"select", "select-one", "native_select"} else "fill"
    audit = {
        "disposition": "retry_prepare",
        "repairable_issues": [f"required_field_empty:{semantic}"],
        "ats_fill_plan_snapshot": {
            "form_fields": [{"field_key": "field-1", "label": semantic, "control": control}],
        },
    }
    plan = {
        "context": {
            "snapshot_ref": "ats-form:abc",
            "snapshot_sha256": "a" * 64,
            "plan_sha256": "b" * 64,
            "submit_authority": False,
            "plan": {
                "fields": [
                    {
                        "field_key": "field-1",
                        "semantic": semantic,
                        "control": control,
                        "writable": True,
                    }
                ],
                "actions": [
                    {
                        "field_key": "field-1",
                        "semantic": semantic,
                        "source_key": semantic,
                        "action": action,
                        "requires_review": False,
                    }
                ],
            },
        }
    }
    return audit, plan


def _run(*, audit=None, plan=None, batch=None, **overrides):
    audit = audit or _contract()[0]
    plan = plan or _contract()[1]
    calls: list[str] = []
    job = overrides.pop("job", {"_attempt_id": "attempt-1"})

    def host_audit():
        calls.append("audit")
        return "pre_submit_repair", audit

    def prepare_plan(report):
        assert report is audit
        calls.append("plan")
        return plan

    def execute_batch(report, prepared):
        assert report is audit and prepared is plan
        calls.append("batch")
        if isinstance(batch, BaseException):
            raise batch
        return batch or {
            "status": "verified",
            "effect_count": 1,
            "legacy_fallback_safe": False,
        }

    result = run_prepare_fast_path(
        job,
        {"personal": {"email": "must-not-leak@example.test"}},
        mode=overrides.pop("mode", "canary"),
        phase=overrides.pop("phase", "prepare"),
        resume_existing_page=overrides.pop("resume_existing_page", False),
        dry_run=overrides.pop("dry_run", False),
        route=overrides.pop("route", "browser"),
        provider=overrides.pop("provider", "workday"),
        host_audit=host_audit,
        prepare_plan=prepare_plan,
        execute_batch=execute_batch,
    )
    assert not overrides
    return result, calls, job


@pytest.mark.parametrize("control", ["text", "email", "tel", "url", "select"])
def test_canary_verified_runs_audit_plan_batch_and_skips_agent(control: str) -> None:
    audit, plan = _contract(control=control)
    result, calls, job = _run(audit=audit, plan=plan)

    assert result.disposition == "ready_to_submit"
    assert result.status == "verified"
    assert calls == ["audit", "plan", "batch"]
    assert result.metadata["no_submit_authority"] is True
    assert "must-not-leak" not in repr(job["_prepare_fast_path"])


def test_shadow_match_continues_agent_without_effect() -> None:
    result, calls, _ = _run(
        mode="shadow",
        batch={"status": "shadow_match", "effect_count": 0, "legacy_fallback_safe": True},
    )

    assert result.disposition == "continue_agent"
    assert result.status == "shadow_match"
    assert calls == ["audit", "plan", "batch"]


def test_unrelated_form_fields_do_not_expand_the_repair_write_set() -> None:
    audit, plan = _contract()
    audit["ats_fill_plan_snapshot"]["form_fields"].append(
        {"field_key": "consent-1", "label": "Consent", "control": "checkbox"}
    )
    plan["context"]["plan"]["fields"].append(
        {
            "field_key": "consent-1",
            "semantic": "consent",
            "control": "checkbox",
            "writable": True,
        }
    )
    plan["context"]["plan"]["actions"].append(
        {
            "field_key": "consent-1",
            "semantic": "consent",
            "source_key": None,
            "action": "review",
            "requires_review": True,
        }
    )

    result, calls, _ = _run(audit=audit, plan=plan)

    assert result.disposition == "ready_to_submit"
    assert result.metadata["candidate_count"] == 1
    assert calls == ["audit", "plan", "batch"]


@pytest.mark.parametrize(
    ("semantic", "control"),
    [("work_authorization", "text"), ("email", "textarea"), ("email", "combobox"), ("email", "file")],
)
def test_forbidden_fields_fall_back_before_batch(semantic: str, control: str) -> None:
    audit, plan = _contract(semantic=semantic, control=control)
    result, calls, _ = _run(audit=audit, plan=plan)

    assert result.disposition == "continue_agent"
    assert result.status == "fallback"
    assert calls == ["audit", "plan"]


def test_safe_no_effect_failure_continues_agent() -> None:
    result, _, _ = _run(
        batch={
            "status": "failed_no_effect",
            "effect_count": 0,
            "legacy_fallback_safe": True,
            "reason_code": "control_absent",
        }
    )

    assert result.disposition == "continue_agent"
    assert result.status == "fallback"


@pytest.mark.parametrize(
    "batch",
    [
        {"status": "parked", "effect_count": 1, "legacy_fallback_safe": False},
        {"status": "invalid", "effect_count": 0, "legacy_fallback_safe": False},
        RuntimeError("after dispatch"),
    ],
)
def test_effect_or_unknown_batch_state_parks_without_agent_fallback(batch) -> None:
    result, calls, _ = _run(batch=batch)

    assert result.disposition == "manual_review"
    assert result.status == "parked"
    assert calls == ["audit", "plan", "batch"]


def test_invalid_batch_result_parks_after_dispatch() -> None:
    result, calls, _ = _run(batch={"status": "verified", "effect_count": 2})

    assert result.disposition == "manual_review"
    assert result.reason_code == "batch_effect_count_invalid"
    assert calls == ["audit", "plan", "batch"]


def test_attempt_marker_prevents_retry_host_writes() -> None:
    job = {"_attempt_id": "attempt-1"}
    first, first_calls, _ = _run(job=job)
    second, second_calls, _ = _run(job=job)

    assert first.disposition == "ready_to_submit"
    assert first_calls == ["audit", "plan", "batch"]
    assert second.reason_code == "attempt_already_tried"
    assert second_calls == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"mode": "off"},
        {"phase": "submit"},
        {"route": "direct_email"},
        {"dry_run": True},
        {"resume_existing_page": True},
        {"provider": "greenhouse"},
        {"job": {"_attempt_id": "attempt-1", "_submission_gate": {"id": "gate"}}},
    ],
)
def test_ineligible_invocations_do_not_observe_or_write(overrides) -> None:
    result, calls, job = _run(**overrides)

    assert result.disposition == "continue_agent"
    assert calls == []
    assert "_prepare_fast_path_attempt_id" not in job
