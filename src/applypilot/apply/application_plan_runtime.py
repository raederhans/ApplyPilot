"""Host-owned production assembly and deterministic audit for ApplicationPlan.

The runtime in this module is deliberately observation-only. It creates a
reference-only plan from host state and can issue a host-local audit receipt,
but it never creates reservation or SubmitAuthority objects. The established
durable SubmissionGate remains the only production submit executor boundary.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from applypilot.apply.application_facts import (
    current_profile_facts,
    resolve_application_fact_ref,
)
from applypilot.apply.application_plan import (
    FACT_KEY_CODES,
    MATERIAL_PURPOSE_CODES,
    PROVIDER_CODES,
    ApplicationPlan,
    FactRef,
    HostAuditReceipt,
    HostAuditReceiptIssuer,
    HostSubmitDenied,
    MaterialRef,
)
from applypilot.apply.browser_broker import BrowserLeaseBundle
from applypilot.apply.contracts import application_actor_id
from applypilot.apply.provider_registry import provider_for_url


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _content_ref(value: object) -> str:
    return f"sha256:{_digest(value)}"


def _required(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _fact_scope(scope: object) -> str | None:
    token = str(scope or "").strip().casefold()
    if token in {"application", "candidate_profile", "employer", "job", "jurisdiction"}:
        return token
    prefix = token.partition(":")[0].replace("-", "_")
    return {
        "global": "candidate_profile",
        "profile": "candidate_profile",
        "candidate": "candidate_profile",
        "this_application": "application",
        "application": "application",
        "employer": "employer",
        "company": "employer",
        "job": "job",
        "job_family": "job",
        "employment_type": "job",
        "jurisdiction": "jurisdiction",
        "location": "jurisdiction",
    }.get(prefix)


def _fact_refs(profile: Mapping[str, object]) -> tuple[FactRef, ...]:
    facts = current_profile_facts(profile)
    refs: list[FactRef] = []
    for fact in facts:
        key = fact.key.strip().casefold()
        scope = _fact_scope(fact.scope)
        if key not in FACT_KEY_CODES or scope is None:
            continue
        resolution = resolve_application_fact_ref(
            facts,
            fact_ref=fact.fact_ref,
            scope=str(fact.scope),
            minimum_sensitivity=fact.sensitivity,
        )
        if not resolution.production_ready:
            continue
        refs.append(
            FactRef(
                fact_ref=_content_ref({"kind": "application_fact", "ref": fact.fact_ref}),
                key=key,
                scope=scope,
                value_sha256=_digest(resolution.value),
            )
        )
    return tuple(refs)


def _material_refs(job: Mapping[str, object]) -> tuple[MaterialRef, ...]:
    candidates: list[tuple[str, object]] = [("resume", job.get("tailored_resume_sha256"))]
    refs: dict[tuple[str, str], MaterialRef] = {}
    for purpose, raw_digest in candidates:
        digest = str(raw_digest or "").strip().casefold()
        if purpose not in MATERIAL_PURPOSE_CODES:
            continue
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            continue
        key = (purpose, digest)
        refs[key] = MaterialRef(
            material_ref=_content_ref(
                {
                    "kind": "application_material",
                    "purpose": purpose,
                    "sha256": digest,
                }
            ),
            purpose=purpose,
            content_sha256=digest,
        )
    return tuple(refs[key] for key in sorted(refs))


def _browser_target_binding(job: Mapping[str, object], attempt_id: str) -> str:
    raw = job.get("_browser_lease_binding")
    if not isinstance(raw, Mapping):
        raise TypeError("browser ApplicationPlan requires a host browser lease")
    bundle = BrowserLeaseBundle.from_mapping(raw)
    page = bundle.page_binding
    if page.attempt_id != attempt_id or page.owner_id != application_actor_id(attempt_id):
        raise ValueError("browser ApplicationPlan lease is not owned by this attempt")
    return _content_ref(
        {
            "kind": "browser_application_target",
            "page_id": page.page_id,
            "page_lease_id": page.page_lease_id,
            "page_lease_epoch": page.page_lease_epoch,
            "attempt_id": page.attempt_id,
            "owner_id": page.owner_id,
        }
    )


def _mailbox_target_binding(job: Mapping[str, object], attempt_id: str) -> str:
    return _content_ref(
        {
            "kind": "direct_email_application_target",
            "attempt_id": attempt_id,
            "job_url": str(job.get("url") or ""),
            "application_url": str(job.get("application_url") or ""),
            "company": str(job.get("company_name") or ""),
            "title": str(job.get("title") or ""),
        }
    )


def _provider(job: Mapping[str, object], route: str) -> str:
    if route == "direct_email":
        return "direct_email"
    binding = job.get("_ats_application_binding")
    bound = str(binding.get("provider") or "").strip().casefold() if isinstance(binding, Mapping) else ""
    detected = provider_for_url(
        job.get("application_url") or job.get("url"),
        "detection",
    )
    if bound and detected and bound != detected:
        raise ValueError("host provider binding does not match the application target")
    provider = bound or detected or "generic"
    return provider if provider in PROVIDER_CODES else "generic"


def _same_plan_content(left: ApplicationPlan, right: ApplicationPlan) -> bool:
    return (
        left.plan_id,
        left.attempt_id,
        left.route,
        left.provider,
        left.target_semantic_code,
        left.target_binding_ref,
        left.fact_refs,
        left.material_refs,
        left.provenance_refs,
        left.schema_version,
    ) == (
        right.plan_id,
        right.attempt_id,
        right.route,
        right.provider,
        right.target_semantic_code,
        right.target_binding_ref,
        right.fact_refs,
        right.material_refs,
        right.provenance_refs,
        right.schema_version,
    )


def build_host_application_plan(
    job: Mapping[str, object],
    profile: Mapping[str, object],
    *,
    runtime_route: str,
    previous: ApplicationPlan | None = None,
) -> ApplicationPlan:
    """Build or revise one ref-only plan exclusively from current host state."""

    attempt_id = _required(job.get("_attempt_id"), "attempt_id")
    route = "direct_email" if runtime_route == "direct_email" else "browser_form"
    provider = _provider(job, route)
    target_binding_ref = (
        _mailbox_target_binding(job, attempt_id)
        if route == "direct_email"
        else _browser_target_binding(job, attempt_id)
    )
    plan_id = f"host-application-plan:{attempt_id}"
    candidate = ApplicationPlan(
        plan_id=plan_id,
        attempt_id=attempt_id,
        revision=1 if previous is None else previous.revision + 1,
        route=route,
        provider=provider,
        target_semantic_code=("direct_email_application" if route == "direct_email" else "application_form"),
        target_binding_ref=target_binding_ref,
        fact_refs=_fact_refs(profile),
        material_refs=_material_refs(job),
        parent_plan_sha256=None if previous is None else previous.digest,
    )
    if previous is None:
        return candidate
    if (
        previous.plan_id != plan_id
        or previous.attempt_id != attempt_id
        or previous.route != route
        or previous.provider != provider
        or previous.target_binding_ref != target_binding_ref
    ):
        raise ValueError("ApplicationPlan immutable target identity changed within an attempt")
    return previous if _same_plan_content(candidate, previous) else candidate


def verify_host_application_plan_audit(
    plan: ApplicationPlan,
    job: Mapping[str, object],
    profile: Mapping[str, object],
    audit_report: Mapping[str, object],
    *,
    issuer: HostAuditReceiptIssuer,
) -> HostAuditReceipt:
    """Replace model self-verification with a strict host recomputation boundary."""

    if plan.route != "browser_form":
        raise HostSubmitDenied("direct email requires the mailbox verifier")
    expected_target = _browser_target_binding(job, plan.attempt_id)
    if plan.target_binding_ref != expected_target:
        raise HostSubmitDenied("ApplicationPlan browser target binding drifted before audit")
    try:
        current_plan = build_host_application_plan(
            job,
            profile,
            runtime_route="browser",
            previous=plan,
        )
    except (TypeError, ValueError) as exc:
        raise HostSubmitDenied("ApplicationPlan host inputs drifted before audit") from exc
    if current_plan.digest != plan.digest:
        raise HostSubmitDenied("ApplicationPlan host references drifted before audit")
    provenance = audit_report.get("answer_provenance")
    if not isinstance(provenance, Mapping):
        raise HostSubmitDenied("deterministic answer provenance audit is required")
    if (
        audit_report.get("disposition") != "clear"
        or audit_report.get("submission_gate") is not True
        or audit_report.get("blocking_issues") not in ([], ())
        or audit_report.get("repairable_issues") not in ([], ())
        or audit_report.get("captcha_token_present") is True
        or audit_report.get("assessment_visible") is True
        or audit_report.get("verification_visible") is True
        or int(audit_report.get("required_unfilled_count") or 0) != 0
        or int(audit_report.get("submit_control_count") or 0) < 1
        or int(provenance.get("blocked_count") or 0) != 0
        or float(provenance.get("coverage_ratio") or 0.0) != 1.0
    ):
        raise HostSubmitDenied("deterministic pre-submit audit is not clear")
    receipt = issuer.issue(
        plan,
        audit_report_ref=_content_ref(audit_report),
        disposition="clear",
        observed_target_ref=expected_target,
    )
    issuer.validate(receipt, plan)
    return receipt


def application_plan_shadow_result(
    plan: ApplicationPlan,
    job: Mapping[str, object],
    profile: Mapping[str, object],
    audit_report: Mapping[str, object],
    *,
    issuer: HostAuditReceiptIssuer,
) -> dict[str, object]:
    """Return a PII-free observation; never return a capability or raw plan values."""

    base = {
        "plan_sha256": plan.digest,
        "route": plan.route,
        "provider": plan.provider,
        "revision": plan.revision,
        "submit_executor": "host_submit_executor",
        "submit_executor_enabled": False,
        "durable_submission_gate": "authoritative",
        "submit_authority": False,
        "legacy_path_unchanged": True,
    }
    if plan.route == "direct_email":
        return {
            **base,
            "status": "mailbox_owned",
            "reason_code": "DIRECT_EMAIL_REMAINS_MAILBOX_OWNED",
        }
    try:
        receipt = verify_host_application_plan_audit(
            plan,
            job,
            profile,
            audit_report,
            issuer=issuer,
        )
    except (HostSubmitDenied, TypeError, ValueError):
        return {
            **base,
            "status": "blocked",
            "reason_code": "DETERMINISTIC_HOST_AUDIT_BLOCKED",
        }
    return {
        **base,
        "status": "verified",
        "reason_code": "DETERMINISTIC_HOST_AUDIT_VERIFIED",
        "audit_receipt_sha256": receipt.digest,
    }


__all__ = [
    "application_plan_shadow_result",
    "build_host_application_plan",
    "verify_host_application_plan_audit",
]
