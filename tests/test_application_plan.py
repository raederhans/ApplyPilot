from __future__ import annotations

from contextlib import contextmanager
from dataclasses import FrozenInstanceError, fields, replace

import pytest

from applypilot.apply.application_plan import (
    BROWSER_SUBMIT_STAGES,
    DIRECT_EMAIL_STAGES,
    ApplicationPlan,
    FactRef,
    HostAuditReceipt,
    HostAuditReceiptIssuer,
    HostObservation,
    HostReconciledReceipt,
    HostReservation,
    HostSubmitDenied,
    HostSubmitExecutor,
    HostSubmitParityTrace,
    MaterialRef,
    ProvenanceRef,
    SubmitAuthority,
    SubmitAuthorityIssuer,
    evaluate_host_submit_parity,
    render_application_plan_delta,
)

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64


def _fact(*, digest: str = DIGEST_A) -> FactRef:
    return FactRef("profile:preferred_name", "preferred_name", "application", digest)


def _material(*, digest: str = DIGEST_B) -> MaterialRef:
    return MaterialRef("material:resume", "resume", digest)


def _provenance(*, digest: str = DIGEST_C) -> ProvenanceRef:
    return ProvenanceRef("provenance:preferred_name", "preferred_name", digest)


def _plan(
    *,
    route: str = "browser_form",
    revision: int = 1,
    parent: str | None = None,
    facts: tuple[FactRef, ...] = (),
    materials: tuple[MaterialRef, ...] = (),
    provenance: tuple[ProvenanceRef, ...] = (),
) -> ApplicationPlan:
    return ApplicationPlan(
        plan_id="plan-1",
        attempt_id="attempt-1",
        revision=revision,
        route=route,  # type: ignore[arg-type]
        provider="workday" if route == "browser_form" else "direct_email",
        application_url="https://example.test/jobs/1",
        target_binding_ref=f"sha256:{DIGEST_A}",
        fact_refs=facts,
        material_refs=materials,
        provenance_refs=provenance,
        parent_plan_sha256=parent,
    )


def test_application_plan_is_deeply_immutable_and_carries_only_typed_refs() -> None:
    facts = [_fact()]
    plan = _plan(
        facts=facts,  # type: ignore[arg-type]
        materials=(_material(),),
        provenance=(_provenance(),),
    )
    facts.append(FactRef("profile:city", "city", "application", DIGEST_B))

    assert plan.fact_refs == (_fact(),)
    assert (
        plan.digest
        == _plan(
            facts=(_fact(),),
            materials=(_material(),),
            provenance=(_provenance(),),
        ).digest
    )
    with pytest.raises(FrozenInstanceError):
        plan.plan_id = "changed"  # type: ignore[misc]
    assert {
        "browser_handle",
        "submission_gate",
        "reservation",
        "submit_authority",
        "receipt_writer",
    }.isdisjoint(field.name for field in fields(plan))


def test_delta_prompt_is_deterministic_ref_only_and_parent_bound() -> None:
    initial = _plan(facts=(_fact(),), materials=(_material(),))
    current = _plan(
        revision=2,
        parent=initial.digest,
        facts=(_fact(digest=DIGEST_C),),
        materials=(_material(),),
        provenance=(_provenance(),),
    )

    rendered = render_application_plan_delta(current, previous=initial)

    assert rendered == render_application_plan_delta(current, previous=initial)
    assert '"submit":false' in rendered
    assert '"kind":"provenance"' in rendered
    assert '"before":{"key":"preferred_name"' in rendered
    assert '"after":{"key":"preferred_name"' in rendered
    assert "Ada Lovelace" not in rendered
    assert "C:\\private\\resume.pdf" not in rendered
    with pytest.raises(ValueError, match="exact previous plan"):
        render_application_plan_delta(replace(current, parent_plan_sha256=DIGEST_B), previous=initial)


def test_direct_email_delta_declares_mailbox_only_route() -> None:
    rendered = render_application_plan_delta(_plan(route="direct_email"))

    assert "mailbox-only" in rendered
    assert "browser HostSubmit is forbidden" in rendered


def test_host_audit_receipt_is_deterministic_authenticated_and_plan_bound() -> None:
    plan = _plan()
    issuer = HostAuditReceiptIssuer()
    first = issuer.issue(plan, audit_report_ref=f"sha256:{DIGEST_B}", disposition="clear")
    second = issuer.issue(plan, audit_report_ref=f"sha256:{DIGEST_B}", disposition="clear")

    assert first.digest == second.digest
    assert first.signature == second.signature
    issuer.validate(first, plan)
    with pytest.raises(HostSubmitDenied, match="signature"):
        issuer.validate(replace(first, signature="tampered"), plan)
    with pytest.raises(ValueError, match="blocked audits require blockers"):
        replace(first, disposition="blocked")


def test_shadow_parity_checks_browser_order_and_mailbox_ownership_without_effects() -> None:
    browser = _plan()
    browser_report = evaluate_host_submit_parity(
        browser,
        HostSubmitParityTrace("browser_form", "browser", "host", BROWSER_SUBMIT_STAGES),
    )
    email = _plan(route="direct_email")
    email_report = evaluate_host_submit_parity(
        email,
        HostSubmitParityTrace("direct_email", "mailbox", "mailbox", DIRECT_EMAIL_STAGES),
    )
    wrong_email = evaluate_host_submit_parity(
        email,
        HostSubmitParityTrace("direct_email", "browser", "host", DIRECT_EMAIL_STAGES),
    )

    assert browser_report.parity is True
    assert email_report.parity is True
    assert wrong_email.parity is False
    assert wrong_email.reason_code == "HOST_SUBMIT_PARITY_MISMATCH"


class FakeHooks:
    def __init__(
        self,
        plan: ApplicationPlan,
        audit: HostAuditReceipt,
        *,
        submit_error: bool = False,
    ) -> None:
        self.plan = plan
        self.audit = audit
        self.submit_error = submit_error
        self.calls: list[str] = []
        self.authority: SubmitAuthority | None = None
        self.reservation = HostReservation(
            reservation_id="reservation-1",
            plan_sha256=plan.digest,
            audit_receipt_sha256=audit.digest,
        )

    @contextmanager
    def global_submit_lane(self, _plan: ApplicationPlan):
        self.calls.append("lane_enter")
        try:
            yield
        finally:
            self.calls.append("lane_exit")

    def reserve(self, _plan: ApplicationPlan, _audit: HostAuditReceipt) -> HostReservation:
        self.calls.append("reserve")
        return self.reservation

    def mark_submit_started(self, _reservation: HostReservation) -> None:
        self.calls.append("submit_started")

    def submit_once(self, _plan: ApplicationPlan, authority: SubmitAuthority) -> None:
        self.calls.append("submit_once")
        self.authority = authority
        if self.submit_error:
            raise RuntimeError("synthetic submit transport ambiguity")

    def observe(self, plan: ApplicationPlan) -> HostObservation:
        self.calls.append("observe")
        return HostObservation(plan.digest, "confirmed", f"sha256:{DIGEST_B}")

    def reconcile(
        self,
        plan: ApplicationPlan,
        observation: HostObservation,
    ) -> HostReconciledReceipt:
        self.calls.append("reconcile")
        return HostReconciledReceipt(
            plan.digest,
            observation.evidence_ref,
            "admitted",
            f"sha256:{DIGEST_C}",
        )


def _audit(plan: ApplicationPlan, issuer: HostAuditReceiptIssuer) -> HostAuditReceipt:
    return issuer.issue(plan, audit_report_ref=f"sha256:{DIGEST_B}", disposition="clear")


def test_feature_disabled_host_submit_is_shadow_only_with_zero_hook_calls() -> None:
    plan = _plan()
    audit_issuer = HostAuditReceiptIssuer()
    audit = _audit(plan, audit_issuer)
    hooks = FakeHooks(plan, audit)

    result = HostSubmitExecutor(audit_issuer=audit_issuer).execute(
        plan=plan,
        audit=audit,
        hooks=hooks,
    )

    assert result.disposition == "shadow"
    assert result.reason_code == "HOST_SUBMIT_FEATURE_DISABLED"
    assert result.submit_effect_count == 0
    assert hooks.calls == []


def test_enabled_host_submit_requires_exact_order_and_single_use_authority() -> None:
    plan = _plan()
    audit_issuer = HostAuditReceiptIssuer()
    authority_issuer = SubmitAuthorityIssuer(audit_issuer=audit_issuer)
    audit = _audit(plan, audit_issuer)
    hooks = FakeHooks(plan, audit)
    executor = HostSubmitExecutor(
        feature_enabled=True,
        audit_issuer=audit_issuer,
        authority_issuer=authority_issuer,
    )

    result = executor.execute(plan=plan, audit=audit, hooks=hooks)

    assert result.disposition == "confirmed"
    assert result.stages == BROWSER_SUBMIT_STAGES
    assert result.submit_effect_count == 1
    assert hooks.calls == [
        "lane_enter",
        "reserve",
        "submit_started",
        "submit_once",
        "lane_exit",
        "observe",
        "reconcile",
    ]
    assert hooks.authority is not None
    with pytest.raises(HostSubmitDenied, match="already consumed"):
        authority_issuer.consume(hooks.authority, plan, audit, hooks.reservation)


def test_submit_transport_error_never_retries_and_still_observes_and_reconciles() -> None:
    plan = _plan()
    audit_issuer = HostAuditReceiptIssuer()
    audit = _audit(plan, audit_issuer)
    hooks = FakeHooks(plan, audit, submit_error=True)

    result = HostSubmitExecutor(feature_enabled=True, audit_issuer=audit_issuer).execute(
        plan=plan,
        audit=audit,
        hooks=hooks,
    )

    assert result.disposition == "uncertain"
    assert result.submit_effect_count == 1
    assert hooks.calls.count("submit_once") == 1
    assert hooks.calls[-2:] == ["observe", "reconcile"]


def test_direct_email_is_rejected_before_any_host_browser_hook() -> None:
    plan = _plan(route="direct_email")
    audit_issuer = HostAuditReceiptIssuer()
    audit = _audit(plan, audit_issuer)
    hooks = FakeHooks(plan, audit)

    with pytest.raises(HostSubmitDenied, match="mailbox route"):
        HostSubmitExecutor(feature_enabled=True, audit_issuer=audit_issuer).execute(
            plan=plan,
            audit=audit,
            hooks=hooks,
        )

    assert hooks.calls == []


def test_submit_authority_issuer_rejects_audit_from_another_host() -> None:
    plan = _plan()
    trusted_audit_issuer = HostAuditReceiptIssuer()
    foreign_audit_issuer = HostAuditReceiptIssuer()
    foreign_audit = _audit(plan, foreign_audit_issuer)
    reservation = HostReservation(
        "reservation-foreign",
        plan.digest,
        foreign_audit.digest,
    )

    with pytest.raises(HostSubmitDenied, match="not issued by this host"):
        SubmitAuthorityIssuer(audit_issuer=trusted_audit_issuer).issue(
            plan,
            foreign_audit,
            reservation,
            submit_started=True,
        )


def test_mismatched_reservation_fails_before_submit_started_or_submit() -> None:
    plan = _plan()
    audit_issuer = HostAuditReceiptIssuer()
    audit = _audit(plan, audit_issuer)
    hooks = FakeHooks(plan, audit)
    hooks.reservation = replace(hooks.reservation, plan_sha256=DIGEST_C)

    with pytest.raises(HostSubmitDenied, match="before submit_started"):
        HostSubmitExecutor(feature_enabled=True, audit_issuer=audit_issuer).execute(
            plan=plan,
            audit=audit,
            hooks=hooks,
        )

    assert "submit_started" not in hooks.calls
    assert "submit_once" not in hooks.calls
