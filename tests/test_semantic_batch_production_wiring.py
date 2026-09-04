from __future__ import annotations

import pytest
from test_apply_runtime_contract import _run_worker_contract

from applypilot.apply import launcher


def _repair_contract(*, semantics: tuple[str, ...] = ("email", "phone")):
    fields = [
        {
            "field_key": semantic,
            "label": semantic.title(),
            "type": semantic,
            "required": True,
        }
        for semantic in semantics
    ]
    plan_fields = [
        {
            "field_key": semantic,
            "semantic": semantic,
            "control": semantic,
            "required": True,
            "writable": True,
            "option_count": 0,
            "options_sha256": "a" * 64,
        }
        for semantic in semantics
    ]
    actions = [
        {
            "field_key": semantic,
            "semantic": semantic,
            "action": "fill",
            "source_key": semantic,
            "requires_review": False,
        }
        for semantic in semantics
    ]
    audit = {
        "status": "attention",
        "disposition": "retry_prepare",
        "page_url": "https://tenant.wd5.myworkdayjobs.com/apply/REQ-1",
        "repairable_issues": [f"required_field_empty:{semantic.title()}" for semantic in semantics],
        "ats_fill_plan_snapshot": {
            "schema_version": "ats-form-snapshot-v1",
            "target_url": "https://tenant.wd5.myworkdayjobs.com/apply/REQ-1",
            "form_fields": fields,
            "available_fact_names": list(semantics),
        },
    }
    specialist = {
        "context": {
            "snapshot_ref": "ats-form:abc",
            "snapshot_sha256": "a" * 64,
            "plan_sha256": "b" * 64,
            "plan": {"fields": plan_fields, "actions": actions},
            "submit_authority": False,
        },
        "feedback": {
            "task_id": "task:plan",
            "attempt_id": "attempt-1",
            "workflow_id": "workflow-1",
            "snapshot_ref": "ats-form:abc",
            "snapshot_sha256": "a" * 64,
            "plan_sha256": "b" * 64,
        },
    }
    return audit, specialist


def _accept_staged_plan(current_job: dict) -> None:
    context = current_job.get("_ats_fill_plan_context")
    if isinstance(context, dict):
        current_job["_ats_fill_plan_consumed"] = {
            "accepted": True,
            "snapshot_ref": context["snapshot_ref"],
            "snapshot_sha256": context["snapshot_sha256"],
            "plan_sha256": context["plan_sha256"],
        }


def test_candidate_builder_accepts_only_exact_routine_personal_facts() -> None:
    audit, specialist = _repair_contract()

    patches = launcher._semantic_batch_candidate_patches(
        {"personal": {"email": "private@example.test", "phone": "90000000"}},
        audit,
        specialist,
    )

    assert [patch.field_semantic for patch in patches] == ["email", "phone"]


@pytest.mark.parametrize(
    ("semantic", "action"),
    [
        ("work_authorization", "fill"),
        ("email", "upload"),
        ("email", "navigate"),
        ("email", "submit"),
    ],
)
def test_candidate_builder_rejects_sensitive_file_navigation_and_final_actions(
    semantic: str,
    action: str,
) -> None:
    audit, specialist = _repair_contract(semantics=(semantic,))
    specialist["context"]["plan"]["actions"][0]["action"] = action

    with pytest.raises(ValueError, match="not an admitted routine write"):
        launcher._semantic_batch_candidate_patches(
            {"personal": {semantic: "private"}},
            audit,
            specialist,
        )


def test_candidate_builder_rejects_email_textarea_before_runtime() -> None:
    audit, specialist = _repair_contract(semantics=("email",))
    specialist["context"]["plan"]["fields"][0]["control"] = "textarea"

    with pytest.raises(ValueError, match="not an admitted routine write"):
        launcher._semantic_batch_candidate_patches(
            {"personal": {"email": "private@example.test"}},
            audit,
            specialist,
        )


def test_direct_email_and_started_submit_are_rejected_before_browser_access(
    monkeypatch,
) -> None:
    audit, specialist = _repair_contract(semantics=("email",))
    monkeypatch.setattr(launcher, "_record_semantic_batch_telemetry", lambda *_a: None)
    direct_email = launcher._try_semantic_batch_fill(
        9432,
        0,
        {
            "_attempt_id": "attempt-email",
            "_agent_observations": {"email_application": {"route": "direct_email"}},
        },
        {"personal": {"email": "private@example.test"}},
        audit,
        specialist,
        mode="canary",
        application_supervisor=None,
    )
    submit_started = launcher._try_semantic_batch_fill(
        9432,
        0,
        {"_attempt_id": "attempt-submit", "_submission_gate": {"gate_id": "g"}},
        {"personal": {"email": "private@example.test"}},
        audit,
        specialist,
        mode="canary",
        application_supervisor=None,
    )

    assert direct_email["reason_code"] == "direct_email_route_forbidden"
    assert direct_email["legacy_fallback_safe"] is True
    assert submit_started["reason_code"] == "submit_or_final_action_started"
    assert submit_started["legacy_fallback_safe"] is False


def test_feature_off_does_not_call_semantic_batch_port(monkeypatch) -> None:
    audit, specialist = _repair_contract(semantics=("email",))
    monkeypatch.setenv("APPLYPILOT_SEMANTIC_BATCH_MODE", "off")
    monkeypatch.setattr(
        launcher,
        "_prepare_ats_fill_plan_repair",
        lambda *_args: specialist,
    )
    monkeypatch.setattr(
        launcher,
        "_try_semantic_batch_fill",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("batch port called")),
    )

    result, phases, _ledger, _marked = _run_worker_contract(
        monkeypatch,
        prepare_results=["ready_to_submit", "ready_to_submit"],
        audit_results=[
            ("pre_submit_repair:required_field_empty:Email", audit),
            (None, {"status": "clear", "disposition": "clear", "repairable_issues": []}),
        ],
        profile_overrides={"personal": {"email": "private@example.test"}},
        prepare_hook=_accept_staged_plan,
    )

    assert result == (1, 0)
    assert phases == ["prepare", "prepare", "submit"]


def test_shadow_compares_then_preserves_same_actor_legacy_repair(monkeypatch) -> None:
    audit, specialist = _repair_contract(semantics=("email",))
    calls: list[str] = []
    monkeypatch.setenv("APPLYPILOT_SEMANTIC_BATCH_MODE", "shadow")
    monkeypatch.setattr(launcher, "_prepare_ats_fill_plan_repair", lambda *_args: specialist)
    monkeypatch.setattr(
        launcher,
        "_try_semantic_batch_fill",
        lambda *_a, **kwargs: (
            calls.append(kwargs["mode"])
            or {
                "status": "shadow_match",
                "legacy_fallback_safe": True,
            }
        ),
    )

    result, phases, _ledger, _marked = _run_worker_contract(
        monkeypatch,
        prepare_results=["ready_to_submit", "ready_to_submit"],
        audit_results=[
            ("pre_submit_repair:required_field_empty:Email", audit),
            (None, {"status": "clear", "disposition": "clear", "repairable_issues": []}),
        ],
        profile_overrides={"personal": {"email": "private@example.test"}},
        prepare_hook=_accept_staged_plan,
    )

    assert result == (1, 0)
    assert phases == ["prepare", "prepare", "submit"]
    assert calls == ["shadow"]


def test_verified_canary_reaudits_without_second_agent_write_turn(monkeypatch) -> None:
    audit, specialist = _repair_contract(semantics=("email",))
    feedback: list[str] = []
    monkeypatch.setenv("APPLYPILOT_SEMANTIC_BATCH_MODE", "canary")
    monkeypatch.setattr(launcher, "_prepare_ats_fill_plan_repair", lambda *_args: specialist)
    monkeypatch.setattr(
        launcher,
        "_record_ats_fill_plan_feedback",
        lambda _feedback, *, event, **_kwargs: feedback.append(event),
    )
    monkeypatch.setattr(
        launcher,
        "_try_semantic_batch_fill",
        lambda *_a, **_k: {
            "status": "verified",
            "legacy_fallback_safe": False,
        },
    )

    result, phases, _ledger, _marked = _run_worker_contract(
        monkeypatch,
        prepare_results=["ready_to_submit"],
        audit_results=[
            ("pre_submit_repair:required_field_empty:Email", audit),
            (None, {"status": "clear", "disposition": "clear", "repairable_issues": []}),
        ],
        profile_overrides={"personal": {"email": "private@example.test"}},
    )

    assert result == (1, 0)
    assert phases == ["prepare", "submit"]
    assert feedback == ["consumed", "changed_decision"]


def test_partial_canary_never_enters_legacy_repair_or_submit(monkeypatch) -> None:
    audit, specialist = _repair_contract(semantics=("email",))
    monkeypatch.setenv("APPLYPILOT_SEMANTIC_BATCH_MODE", "canary")
    monkeypatch.setattr(launcher, "_prepare_ats_fill_plan_repair", lambda *_args: specialist)
    monkeypatch.setattr(
        launcher,
        "_try_semantic_batch_fill",
        lambda *_a, **_k: {
            "status": "parked",
            "legacy_fallback_safe": False,
        },
    )

    result, phases, _ledger, marked = _run_worker_contract(
        monkeypatch,
        prepare_results=["ready_to_submit"],
        audit_results=[("pre_submit_repair:required_field_empty:Email", audit)],
        profile_overrides={"personal": {"email": "private@example.test"}},
    )

    assert result == (0, 1)
    assert phases == ["prepare"]
    assert "semantic_batch:parked" in marked[0][0][2]
