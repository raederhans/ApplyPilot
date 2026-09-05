"""Host-owned first-turn fast path for routine ATS prepare repairs."""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from typing import Literal

FastPathDisposition = Literal["continue_agent", "ready_to_submit", "manual_review"]

_ADMITTED_PROVIDERS = frozenset({"workday", "smartrecruiters"})
_ADMITTED_SEMANTICS = frozenset(
    {
        "city",
        "country",
        "email",
        "phone",
        "portfolio_url",
        "preferred_name",
        "postal_code",
        "state",
    }
)
_TEXT_CONTROLS = frozenset({"text", "email", "tel", "url", "input"})
_SELECT_CONTROLS = frozenset({"select", "select-one", "native_select"})
_ATTEMPT_MARKER = "_prepare_fast_path_attempt_id"
_METADATA_KEY = "_prepare_fast_path"


@dataclass(frozen=True, slots=True)
class PrepareFastPathResult:
    """A launcher decision; it never carries Agent or submission authority."""

    status: str
    disposition: FastPathDisposition
    reason_code: str
    metadata: Mapping[str, object]


def _bounded_text(value: object, limit: int = 120) -> str:
    return " ".join(str(value or "").split())[:limit]


def _metadata(
    *,
    status: str,
    provider: str,
    reason_code: str,
    candidate_count: int = 0,
    effect_count: int = 0,
    plan_result: Mapping[str, object] | None = None,
) -> dict[str, object]:
    context = plan_result.get("context") if isinstance(plan_result, Mapping) else None
    context = context if isinstance(context, Mapping) else {}
    return {
        "schema_version": "prepare-fast-path-v1",
        "status": _bounded_text(status, 40),
        "provider": _bounded_text(provider, 40),
        "reason_code": _bounded_text(reason_code),
        "candidate_count": max(0, int(candidate_count)),
        "effect_count": max(0, int(effect_count)),
        "digest_refs": {
            "snapshot_ref": _bounded_text(context.get("snapshot_ref"), 100),
            "snapshot_sha256": _bounded_text(context.get("snapshot_sha256"), 64),
            "plan_sha256": _bounded_text(context.get("plan_sha256"), 64),
        },
        "no_submit_authority": True,
    }


def _result(
    job: MutableMapping[str, object],
    *,
    status: str,
    disposition: FastPathDisposition,
    provider: str,
    reason_code: str,
    candidate_count: int = 0,
    effect_count: int = 0,
    plan_result: Mapping[str, object] | None = None,
    record: bool = True,
) -> PrepareFastPathResult:
    metadata = _metadata(
        status=status,
        provider=provider,
        reason_code=reason_code,
        candidate_count=candidate_count,
        effect_count=effect_count,
        plan_result=plan_result,
    )
    if record:
        job[_METADATA_KEY] = metadata
    return PrepareFastPathResult(status, disposition, reason_code, metadata)


def _normalized_control(field: Mapping[str, object]) -> str | None:
    control = _bounded_text(field.get("control") or field.get("type") or field.get("tag"), 40).casefold()
    if control == "input":
        control = _bounded_text(field.get("type") or "text", 40).casefold()
    if any(field.get(flag) is True for flag in ("custom", "dynamic", "stateful", "sensitive")):
        return None
    if control in _TEXT_CONTROLS:
        return "text"
    if control in _SELECT_CONTROLS:
        return "native_select"
    return None


def _candidate_count(audit_report: Mapping[str, object], plan_result: Mapping[str, object]) -> int:
    snapshot = audit_report.get("ats_fill_plan_snapshot")
    context = plan_result.get("context")
    plan = context.get("plan") if isinstance(context, Mapping) else None
    if not isinstance(snapshot, Mapping) or not isinstance(plan, Mapping):
        raise TypeError("bound_snapshot_and_plan_required")
    if context.get("submit_authority") is not False:
        raise ValueError("plan_submit_authority_forbidden")

    raw_fields = snapshot.get("form_fields")
    plan_fields = plan.get("fields")
    actions = plan.get("actions")
    if not all(isinstance(value, list) for value in (raw_fields, plan_fields, actions)):
        raise ValueError("plan_structure_incomplete")

    repairable = audit_report.get("repairable_issues")
    if not isinstance(repairable, list):
        raise TypeError("repairable_issues_required")
    issue_labels = {
        _bounded_text(issue.partition(":")[2], 160).casefold()
        for issue in repairable
        if isinstance(issue, str) and issue.startswith("required_field_empty:")
    }
    if not issue_labels or len(issue_labels) != len(repairable):
        raise ValueError("repairable_issue_binding_invalid")

    raw_by_key: dict[str, Mapping[str, object]] = {}
    issue_keys: dict[str, str] = {}
    for field in raw_fields:
        if not isinstance(field, Mapping):
            continue
        key = _bounded_text(
            field.get("field_key") or field.get("id") or field.get("name") or field.get("selector"),
            160,
        )
        if key:
            if key in raw_by_key:
                raise ValueError("snapshot_field_ambiguous")
            raw_by_key[key] = field
            label = _bounded_text(field.get("label") or field.get("aria_label"), 160).casefold()
            if label in issue_labels:
                if label in issue_keys:
                    raise ValueError("snapshot_issue_label_ambiguous")
                issue_keys[label] = key
    if set(issue_keys) != issue_labels:
        raise ValueError("snapshot_issue_binding_incomplete")

    fields_by_key = {
        _bounded_text(field.get("field_key"), 160): field
        for field in plan_fields
        if isinstance(field, Mapping) and _bounded_text(field.get("field_key"), 160)
    }
    actions_by_key = {
        _bounded_text(action.get("field_key"), 160): action
        for action in actions
        if isinstance(action, Mapping) and _bounded_text(action.get("field_key"), 160)
    }
    candidate_keys = set(issue_keys.values())
    if not candidate_keys <= set(fields_by_key) or not candidate_keys <= set(actions_by_key):
        raise ValueError("plan_field_action_binding_invalid")

    for key in candidate_keys:
        field = fields_by_key[key]
        raw = raw_by_key.get(key)
        action = actions_by_key[key]
        semantic = _bounded_text(field.get("semantic"), 80).casefold()
        plan_control = _normalized_control(field)
        raw_control = _normalized_control(raw) if raw is not None else None
        expected_action = "select" if plan_control == "native_select" else "fill"
        if (
            raw is None
            or semantic not in _ADMITTED_SEMANTICS
            or _bounded_text(action.get("semantic"), 80).casefold() != semantic
            or _bounded_text(action.get("source_key"), 80).casefold() != semantic
            or plan_control not in {"text", "native_select"}
            or raw_control != plan_control
            or action.get("action") != expected_action
            or action.get("requires_review") is not False
            or field.get("writable") is not True
        ):
            raise ValueError("non_routine_field_forbidden")
    return len(candidate_keys)


def run_prepare_fast_path(
    job: MutableMapping[str, object],
    profile: Mapping[str, object],
    *,
    mode: str,
    phase: str,
    resume_existing_page: bool,
    dry_run: bool,
    route: str,
    provider: str,
    host_audit: Callable[[], tuple[str | None, Mapping[str, object]]],
    prepare_plan: Callable[[Mapping[str, object]], Mapping[str, object]],
    execute_batch: Callable[[Mapping[str, object], Mapping[str, object]], Mapping[str, object]],
) -> PrepareFastPathResult:
    """Run one bound, routine-only prepare attempt before an Agent is spawned.

    The launcher must call this only after binding the current attempt, browser
    lease and ApplicationSupervisor. ``profile`` is deliberately never copied
    into metadata; its presence makes the host-owned input boundary explicit.
    """

    del profile  # The bound launcher callbacks own all access to profile values.
    normalized_mode = _bounded_text(mode, 20).casefold()
    normalized_provider = _bounded_text(provider, 40).casefold().replace("_", "")
    if normalized_mode not in {"shadow", "canary"}:
        return _result(
            job,
            status="off",
            disposition="continue_agent",
            provider=normalized_provider,
            reason_code="feature_disabled",
            record=False,
        )
    eligible = (
        phase.strip().casefold() == "prepare"
        and route.strip().casefold() == "browser"
        and not dry_run
        and not resume_existing_page
        and normalized_provider in _ADMITTED_PROVIDERS
        and not job.get("_submission_gate")
        and not job.get("_submission_gate_binding")
    )
    observations = job.get("_agent_observations")
    if isinstance(observations, Mapping) and isinstance(observations.get("email_application"), Mapping):
        eligible = False
    attempt_id = _bounded_text(job.get("_attempt_id"), 160)
    if not eligible or not attempt_id:
        return _result(
            job,
            status="not_eligible",
            disposition="continue_agent",
            provider=normalized_provider,
            reason_code="preconditions_not_admitted",
            record=False,
        )
    if job.get(_ATTEMPT_MARKER) == attempt_id:
        return _result(
            job,
            status="not_eligible",
            disposition="continue_agent",
            provider=normalized_provider,
            reason_code="attempt_already_tried",
            record=False,
        )
    job[_ATTEMPT_MARKER] = attempt_id

    try:
        audit_reason, audit_report = host_audit()
        repairable = audit_report.get("repairable_issues")
        if (
            audit_report.get("disposition") != "retry_prepare"
            or not isinstance(repairable, list)
            or not repairable
            or any(not isinstance(issue, str) or not issue.startswith("required_field_empty:") for issue in repairable)
            or not isinstance(audit_report.get("ats_fill_plan_snapshot"), Mapping)
        ):
            return _result(
                job,
                status="fallback",
                disposition="continue_agent",
                provider=normalized_provider,
                reason_code="audit_not_routine_repair",
            )
        plan_result = prepare_plan(audit_report)
        candidates = _candidate_count(audit_report, plan_result)
    except Exception as exc:  # noqa: BLE001 - read-only preparation can safely fall back
        return _result(
            job,
            status="fallback",
            disposition="continue_agent",
            provider=normalized_provider,
            reason_code=f"pre_batch_{type(exc).__name__.casefold()}",
        )

    try:
        batch = execute_batch(audit_report, plan_result)
    except Exception as exc:  # noqa: BLE001 - callback may already have a browser effect
        return _result(
            job,
            status="parked",
            disposition="manual_review",
            provider=normalized_provider,
            reason_code=f"batch_unknown_{type(exc).__name__.casefold()}",
            candidate_count=candidates,
            plan_result=plan_result,
        )

    if not isinstance(batch, Mapping):
        return _result(
            job,
            status="parked",
            disposition="manual_review",
            provider=normalized_provider,
            reason_code="batch_result_invalid",
            candidate_count=candidates,
            plan_result=plan_result,
        )

    batch_status = _bounded_text(batch.get("status"), 40).casefold()
    effect_count = batch.get("effect_count", 0)
    effect_count = effect_count if isinstance(effect_count, int) and not isinstance(effect_count, bool) else -1
    reason_code = _bounded_text(batch.get("reason_code") or batch_status or audit_reason)
    if effect_count < 0 or effect_count > candidates:
        return _result(
            job,
            status="parked",
            disposition="manual_review",
            provider=normalized_provider,
            reason_code="batch_effect_count_invalid",
            candidate_count=candidates,
            effect_count=max(0, effect_count),
            plan_result=plan_result,
        )
    if normalized_mode == "shadow" and batch_status == "shadow_match" and effect_count == 0:
        return _result(
            job,
            status="shadow_match",
            disposition="continue_agent",
            provider=normalized_provider,
            reason_code=reason_code,
            candidate_count=candidates,
            plan_result=plan_result,
        )
    if normalized_mode == "canary" and batch_status in {"verified", "replayed"} and effect_count >= 0:
        return _result(
            job,
            status=batch_status,
            disposition="ready_to_submit",
            provider=normalized_provider,
            reason_code=reason_code,
            candidate_count=candidates,
            effect_count=effect_count,
            plan_result=plan_result,
        )
    if batch.get("legacy_fallback_safe") is True and effect_count == 0:
        return _result(
            job,
            status="fallback",
            disposition="continue_agent",
            provider=normalized_provider,
            reason_code=reason_code or "safe_no_effect",
            candidate_count=candidates,
            plan_result=plan_result,
        )
    return _result(
        job,
        status="parked",
        disposition="manual_review",
        provider=normalized_provider,
        reason_code=reason_code or "batch_side_effect_uncertain",
        candidate_count=candidates,
        effect_count=max(0, effect_count),
        plan_result=plan_result,
    )
