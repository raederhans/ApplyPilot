from __future__ import annotations

from dataclasses import replace

from applypilot.apply.browser_broker import BrowserLease, BrowserLeaseBundle
from applypilot.apply.contracts import (
    DecisionEnvelope,
    HumanInterruption,
    RecoveryAction,
)
from applypilot.apply.exception_queue import exception_for_command
from applypilot.apply.operator_binding import operator_resume_binding
from applypilot.apply.page_binding import PageBinding
from applypilot.apply.recovery_execution import admit_recovery_decision


def _decision() -> DecisionEnvelope:
    return DecisionEnvelope(
        run_id="turn-human",
        attempt_id="attempt-human",
        phase="verify",
        disposition="checkpoint",
        next_phase="checkpoint",
        recovery_action=RecoveryAction(
            action="human_only",
            failure_category="truth_or_security_boundary",
            next_action="wait_for_human",
        ),
        human_interruption=HumanInterruption(
            interruption_type="human_boundary",
            reason="confirmed fact required",
            next_action="wait_for_human",
        ),
    )


def _bundle(*, attempt_id: str = "attempt-human") -> BrowserLeaseBundle:
    common = {
        "lease_id": "lease-human",
        "owner_id": "application:attempt-human",
        "scope_id": "worker:1",
        "attempt_id": attempt_id,
        "runtime_id": "codex:edge:cdp:9515",
        "epoch": 3,
        "issued_at": 1.0,
        "heartbeat_at": 2.0,
        "expires_at": 300.0,
    }
    return BrowserLeaseBundle(
        profile=BrowserLease(
            resource_kind="profile",
            resource_id="edge:worker:1",
            **common,
        ),
        page=BrowserLease(
            resource_kind="page",
            resource_id="application:attempt-human",
            **common,
        ),
        page_binding=PageBinding(
            page_id="application:attempt-human",
            page_lease_id="lease-human",
            page_lease_epoch=3,
            page_epoch=7,
            profile_lease_id="lease-human",
            owner_id="application:attempt-human",
            attempt_id=attempt_id,
            runtime_id="codex:edge:cdp:9515",
        ),
    )


def _job(bundle: BrowserLeaseBundle) -> dict[str, object]:
    return {
        "url": "https://jobs.example.test/one",
        "_attempt_id": "attempt-human",
        "_parent_agent_run_id": "turn-human",
        "_parent_agent_checkpoint_id": "checkpoint-human",
        "_browser_lease_binding": bundle.as_dict(),
    }


def test_host_binding_reaches_exact_human_exception_without_raw_answer() -> None:
    decision = _decision()
    binding = operator_resume_binding(decision, _job(_bundle()))
    assert binding == {
        "request_id": "turn-human:human:1",
        "checkpoint_id": "checkpoint-human",
        "job_url": "https://jobs.example.test/one",
        "profile_id": "edge:worker:1",
        "browser_lease_id": "lease-human",
        "browser_lease_epoch": 3,
        "page_target_id": "application:attempt-human",
        "page_epoch": 7,
    }

    admission = admit_recovery_decision(
        decision,
        submit_started=False,
        operator_context=binding,
    )
    assert admission.admitted is True and admission.command is not None
    item = exception_for_command(admission.command)
    assert {key: item.context[key] for key in binding} == binding
    assert "answer" not in str(item.context).casefold()
    assert item.queue_kind == "human_only"


def test_incomplete_or_untrusted_binding_is_not_persisted_or_promoted() -> None:
    decision = _decision()
    wrong_attempt = operator_resume_binding(decision, _job(_bundle(attempt_id="attempt-other")))
    wrong_parent = operator_resume_binding(
        decision,
        {**_job(_bundle()), "_parent_agent_run_id": "turn-other"},
    )
    assert wrong_attempt is None and wrong_parent is None

    untrusted = {
        **(operator_resume_binding(decision, _job(_bundle())) or {}),
        "human_answer": "must never persist",
    }
    admission = admit_recovery_decision(
        decision,
        submit_started=False,
        operator_context=untrusted,
    )
    assert admission.command is not None
    assert not set(untrusted).intersection(admission.command.payload)
    assert "must never persist" not in str(exception_for_command(admission.command).context)

    non_human = replace(
        decision,
        disposition="recover",
        next_phase="recover",
        recovery_action=RecoveryAction(
            action="retry_same_application",
            failure_category="transient_browser_failure",
            next_action="retry",
            retry_budget_remaining=1,
        ),
        human_interruption=None,
    )
    assert operator_resume_binding(non_human, _job(_bundle())) is None
