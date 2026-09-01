"""Fail-closed, read-only provenance verification for pre-submit answers."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from applypilot.apply import ats as ats_mod
from applypilot.apply.answer_policy import FieldRisk, SafeDefaultRegistry, field_risk
from applypilot.apply.answer_resolution import AnswerRequest, resolve_answer
from applypilot.apply.application_facts import (
    ApplicationFact,
    current_profile_facts,
    resolve_application_fact_ref,
)
from applypilot.apply.authorization import compute_job_fingerprint
from applypilot.apply.browser_broker import BrowserLeaseBundle
from applypilot.apply.contracts import application_actor_id

ANSWER_MAPPING_SCHEMA_VERSION: Final = "2"
MAX_PROVENANCE_FIELDS: Final = 128
SUPPORTED_CONTROLS: Final = frozenset(
    {"select", "radio", "text", "textarea", "email", "tel", "number", "date", "combobox", "checkbox"}
)
_TOKEN_RE = re.compile(r"[^a-z0-9]+")
_DECLARATION_RE = re.compile(
    r"\b(?:declare|declaration|attest|attestation|certify|certification|"
    r"acknowledge|consent|terms and conditions)\b",
    re.IGNORECASE,
)
PRE_SUBMIT_SAFE_DEFAULTS = SafeDefaultRegistry()


def _normalized(value: object) -> str:
    return " ".join(_TOKEN_RE.sub(" ", str(value or "").casefold()).split())


def _digest(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def selected_option_digest(value: object) -> str:
    return hashlib.sha256(_normalized(value).encode()).hexdigest()


def adapter_contract(snapshot: Mapping[str, object]) -> tuple[str, str]:
    url = str(snapshot.get("url") or snapshot.get("document_url") or "")
    adapter = ats_mod.default_ats_registry().detect(url)
    explicit_version = str(getattr(adapter, "version", "") or "").strip()
    return adapter.name, explicit_version or f"{adapter.name}/ats-ir-{ats_mod.ATS_SCHEMA_VERSION}"


def field_key_hash(
    *, adapter: str, adapter_version: str, control: str, field_key: object
) -> str:
    return _digest(
        {
            "adapter": adapter,
            "adapter_version": adapter_version,
            "control": _normalized(control),
            "field_key": str(field_key or "").strip(),
        }
    )


def _scope_token(kind: str, value: object) -> str:
    return f"{kind}:{_digest({'value': value})}"


def build_host_provenance_binding(job: Mapping[str, object]) -> dict[str, object]:
    """Derive an immutable, observation-only binding from host-owned state."""

    attempt_id = str(job.get("_attempt_id") or "").strip()
    raw_lease = job.get("_browser_lease_binding")
    if not attempt_id or not isinstance(raw_lease, Mapping):
        raise ValueError("answer provenance requires an attempt-bound browser lease")
    bundle = BrowserLeaseBundle.from_mapping(raw_lease)
    page = bundle.page_binding
    if page.attempt_id != attempt_id or page.owner_id != application_actor_id(attempt_id):
        raise ValueError("answer provenance browser lease is not owned by this attempt")
    target_url = str(job.get("application_url") or job.get("url") or "")
    adapter, adapter_version = adapter_contract({"url": target_url})
    fingerprint = compute_job_fingerprint(dict(job))
    fact_scopes = ["global:*"]
    for kind, value in (
        ("jurisdiction", job.get("location")),
        ("employment_type", job.get("employment_type") or job.get("job_type")),
        ("job_family", job.get("job_family")),
        ("employer", job.get("company_name")),
    ):
        if str(value or "").strip():
            fact_scopes.append(_scope_token(kind, value))
    fact_scopes.append(
        _scope_token("this_application", {"attempt_id": attempt_id, "job_fingerprint": fingerprint})
    )
    claims = {
        "schema_version": ANSWER_MAPPING_SCHEMA_VERSION,
        "attempt_id": attempt_id,
        "job_fingerprint": fingerprint,
        "adapter": adapter,
        "adapter_version": adapter_version,
        "page_id": page.page_id,
        "page_lease_id": page.page_lease_id,
        "page_lease_epoch": page.page_lease_epoch,
        "page_epoch": page.page_epoch,
        "fact_scopes": fact_scopes,
    }
    return {**claims, "opaque_binding_seed": _digest(claims)}


def public_provenance_context(binding: Mapping[str, object]) -> dict[str, object]:
    """Project only opaque, non-authorizing fields to the Agent context."""

    return {
        "schema_version": ANSWER_MAPPING_SCHEMA_VERSION,
        "adapter": binding["adapter"],
        "adapter_version": binding["adapter_version"],
        "opaque_binding_seed": binding["opaque_binding_seed"],
        "supported_controls": sorted(SUPPORTED_CONTROLS),
        "authority": "observation_only",
        "mapping_template": {
            "schema_version": ANSWER_MAPPING_SCHEMA_VERSION,
            "adapter": binding["adapter"],
            "adapter_version": binding["adapter_version"],
            "opaque_binding": "<tool-produced binding>",
            "snapshot_digest": "<tool-produced digest>",
            "mappings": [],
        },
    }


@dataclass(frozen=True, slots=True)
class _ObservedAnswer:
    field_ref: str
    semantic: str
    risk: FieldRisk
    control: str
    required: bool
    declaration: bool
    selected: str
    options: tuple[str, ...]
    label: str
    unsupported: bool = False
    option_overflow: bool = False


@dataclass(frozen=True, slots=True)
class AnswerProvenanceAudit:
    issues: tuple[str, ...]
    report: Mapping[str, object]


def _field_semantic(
    *, url: str, label: str, field_key: str, control: str, required: bool, options: Sequence[str]
) -> str:
    form = ats_mod.build_form_ir(
        url,
        ({"field_key": field_key, "label": label, "control": control, "required": required, "options": list(options)},),
    )
    return form.fields[0].semantic if form.fields else "unknown"


def _observed_answers(snapshot: Mapping[str, object]) -> tuple[_ObservedAnswer, ...]:
    adapter, version = adapter_contract(snapshot)
    url = str(snapshot.get("url") or snapshot.get("document_url") or "")
    raw_fields = snapshot.get("provenance_fields")
    if not isinstance(raw_fields, list):
        return ()
    answers: list[_ObservedAnswer] = []
    for index, raw in enumerate(raw_fields[:MAX_PROVENANCE_FIELDS]):
        if not isinstance(raw, Mapping):
            continue
        selected = str(raw.get("selected") or "").strip()
        if not selected:
            continue
        label = str(raw.get("text") or "").strip()
        control = _normalized(raw.get("control")) or "unknown"
        required = raw.get("required") is True
        options_raw = raw.get("options")
        options = (
            tuple(str(item).strip() for item in options_raw if str(item).strip())
            if isinstance(options_raw, (list, tuple))
            else ()
        )
        source_key = str(raw.get("field_key") or "").strip()
        if not source_key:
            source_key = "fallback:" + _digest({"label": _normalized(label), "index": index})
        declaration = bool(_DECLARATION_RE.search(label)) or control == "checkbox"
        semantic = _field_semantic(
            url=url,
            label=label,
            field_key=source_key,
            control=control,
            required=required,
            options=options,
        )
        answers.append(
            _ObservedAnswer(
                field_ref=field_key_hash(
                    adapter=adapter,
                    adapter_version=version,
                    control=control,
                    field_key=source_key,
                ),
                semantic=semantic,
                risk=field_risk(label or semantic, declaration=declaration),
                control=control,
                required=required,
                declaration=declaration,
                selected=selected,
                options=options,
                label=label,
                unsupported=control not in SUPPORTED_CONTROLS or raw.get("protected_identifier") is True,
                option_overflow=(
                    raw.get("options_truncated") is True
                    or (
                        isinstance(raw.get("option_count"), int)
                        and not isinstance(raw.get("option_count"), bool)
                        and raw["option_count"] > len(options)
                    )
                ),
            )
        )
    return tuple(answers)


def structure_snapshot_digest(snapshot: Mapping[str, object]) -> str:
    adapter, version = adapter_contract(snapshot)
    fields = [
        {
            "field_key_hash": answer.field_ref,
            "semantic": answer.semantic,
            "risk": answer.risk,
            "control": answer.control,
            "required": answer.required,
            "declaration": answer.declaration,
            "option_overflow": answer.option_overflow,
            "options": [selected_option_digest(option) for option in answer.options],
        }
        for answer in _observed_answers(snapshot)
    ]
    raw_count = snapshot.get("provenance_field_count")
    observed_count = (
        raw_count
        if isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count >= 0
        else len(snapshot.get("provenance_fields", []))
        if isinstance(snapshot.get("provenance_fields"), list)
        else 0
    )
    return _digest(
        {
            "adapter": adapter,
            "adapter_version": version,
            "page_url_sha256": _digest(
                str(snapshot.get("url") or snapshot.get("document_url") or "")
            ),
            "fields": fields,
            "overflow_count": max(0, observed_count - MAX_PROVENANCE_FIELDS),
        }
    )


def envelope_binding(binding: Mapping[str, object], snapshot_digest: str) -> str:
    return _digest(
        {"opaque_binding_seed": binding.get("opaque_binding_seed"), "snapshot_digest": snapshot_digest}
    )


def _mapping_object(job: Mapping[str, object]) -> Mapping[str, object] | None:
    observations = job.get("_agent_observations")
    raw = observations.get("answer_mappings") if isinstance(observations, Mapping) else None
    return raw if isinstance(raw, Mapping) else None


def _strict_mappings(
    raw: Mapping[str, object] | None,
    *,
    binding: Mapping[str, object],
    snapshot_digest: str,
) -> tuple[dict[str, Mapping[str, object]], str | None]:
    if raw is None:
        return {}, None
    allowed_top = {
        "schema_version", "adapter", "adapter_version", "opaque_binding", "snapshot_digest", "mappings"
    }
    if set(raw) != allowed_top:
        return {}, "answer_provenance_schema_invalid"
    if (
        raw.get("schema_version") != ANSWER_MAPPING_SCHEMA_VERSION
        or raw.get("adapter") != binding.get("adapter")
        or raw.get("adapter_version") != binding.get("adapter_version")
        or raw.get("snapshot_digest") != snapshot_digest
        or raw.get("opaque_binding") != envelope_binding(binding, snapshot_digest)
    ):
        return {}, "answer_provenance_binding_mismatch"
    items = raw.get("mappings")
    if not isinstance(items, list) or len(items) > MAX_PROVENANCE_FIELDS:
        return {}, "answer_provenance_schema_invalid"
    allowed_item = {
        "field_key_hash", "semantic", "risk", "selected_option_digest", "fact_ref", "safe_default_rule_id"
    }
    parsed: dict[str, Mapping[str, object]] = {}
    for item in items:
        if not isinstance(item, Mapping) or set(item) - allowed_item:
            return {}, "answer_provenance_schema_invalid"
        field_ref = item.get("field_key_hash")
        if not isinstance(field_ref, str) or not re.fullmatch(r"[0-9a-f]{64}", field_ref):
            return {}, "answer_provenance_schema_invalid"
        fact_ref = item.get("fact_ref")
        rule_id = item.get("safe_default_rule_id")
        if (isinstance(fact_ref, str) and bool(fact_ref.strip())) == (
            isinstance(rule_id, str) and bool(rule_id.strip())
        ):
            return {}, "answer_provenance_schema_invalid"
        if field_ref in parsed:
            return {}, "answer_provenance_duplicate_mapping"
        parsed[field_ref] = item
    return parsed, None


def _referenced_fact(facts: Sequence[ApplicationFact], fact_ref: object) -> ApplicationFact | None:
    matches = [fact for fact in facts if fact.fact_ref == fact_ref]
    return matches[0] if len(matches) == 1 else None


def _provenance_ref(value: object) -> str:
    return "ref:" + hashlib.sha256(str(value or "").encode()).hexdigest()[:20]


def audit_pre_submit_answer_provenance(
    snapshot: Mapping[str, object],
    profile: Mapping[str, object],
    job: Mapping[str, object],
    *,
    existing_issues: Sequence[str] = (),
    safe_defaults: SafeDefaultRegistry | None = None,
) -> AnswerProvenanceAudit:
    """Recompute every trust input and verify every filled supported control."""

    del existing_issues  # Absence of another issue is never positive evidence.
    observed = _observed_answers(snapshot)
    incomplete_snapshot = not isinstance(snapshot.get("provenance_fields"), list)
    raw_count = snapshot.get("provenance_field_count")
    observed_count = (
        raw_count
        if isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count >= 0
        else len(snapshot.get("provenance_fields", []))
        if isinstance(snapshot.get("provenance_fields"), list)
        else 0
    )
    overflow_count = max(0, observed_count - MAX_PROVENANCE_FIELDS)
    issues: list[str] = []
    try:
        binding = build_host_provenance_binding(job)
    except (TypeError, ValueError):
        binding = {}
        issues.append("answer_provenance_host_binding_invalid")
    current_digest = structure_snapshot_digest(snapshot)
    mappings, schema_issue = _strict_mappings(
        _mapping_object(job), binding=binding, snapshot_digest=current_digest
    )
    if schema_issue:
        issues.append(schema_issue)
    if incomplete_snapshot:
        issues.append("answer_provenance_snapshot_incomplete")
    if overflow_count:
        issues.append("answer_provenance_field_overflow")
    fields: list[dict[str, object]] = []
    verified = 0
    current_facts = current_profile_facts(profile)
    scopes = set(binding.get("fact_scopes", [])) if isinstance(binding.get("fact_scopes"), list) else set()
    registry = safe_defaults or PRE_SUBMIT_SAFE_DEFAULTS
    observed_refs = {answer.field_ref for answer in observed}
    if not schema_issue:
        for stale_ref in sorted(set(mappings) - observed_refs):
            issues.append(f"answer_provenance_stale_mapping:{stale_ref[:16]}")

    for answer in observed:
        field_issue: str | None = None
        provenance_ref: str | None = None
        mapping = mappings.get(answer.field_ref) if not schema_issue and binding else None
        if answer.option_overflow:
            field_issue = "answer_provenance_option_overflow"
        elif answer.unsupported:
            field_issue = "answer_provenance_unsupported_control"
        elif answer.declaration and answer.semantic == "unknown":
            field_issue = "answer_provenance_high_risk_unknown"
        elif mapping is None:
            field_issue = "answer_provenance_missing"
        elif mapping.get("semantic") != answer.semantic or mapping.get("risk") != answer.risk:
            field_issue = "answer_provenance_rule_drift"
        elif mapping.get("selected_option_digest") != selected_option_digest(answer.selected):
            field_issue = "answer_provenance_page_value_mismatch"
        elif "fact_ref" in mapping:
            fact = _referenced_fact(current_facts, mapping.get("fact_ref"))
            if fact is None:
                field_issue = "answer_provenance_fact_missing"
            elif fact.scope not in scopes:
                field_issue = "answer_provenance_fact_out_of_scope"
            else:
                resolution = resolve_application_fact_ref(
                    current_facts,
                    fact_ref=fact.fact_ref,
                    scope=str(fact.scope),
                    minimum_sensitivity=answer.risk,
                )
                if not resolution.production_ready:
                    field_issue = f"answer_provenance_fact_{resolution.status}"
                elif answer.control in {"text", "textarea", "email", "tel", "number", "date", "combobox"}:
                    if _normalized(resolution.value) != _normalized(answer.selected):
                        field_issue = "answer_provenance_fact_value_mismatch"
                    else:
                        provenance_ref = _provenance_ref(resolution.fact_ref)
                else:
                    recalculated = resolve_answer(
                        AnswerRequest(
                            field_semantic=answer.label,
                            options=answer.options,
                            fact_resolution=resolution,
                            required=answer.required,
                            declaration=answer.declaration,
                            adapter=str(binding.get("adapter") or ""),
                            adapter_version=str(binding.get("adapter_version") or ""),
                        )
                    )
                    if recalculated.selected_option is None or _normalized(
                        recalculated.selected_option
                    ) != _normalized(answer.selected):
                        field_issue = "answer_provenance_fact_value_mismatch"
                    else:
                        provenance_ref = _provenance_ref(resolution.fact_ref)
        else:
            rule_id = mapping.get("safe_default_rule_id")
            rule = registry.get(str(rule_id or ""))
            context = {
                "field_key_hash": answer.field_ref,
                "scope_binding": binding.get("opaque_binding_seed", ""),
            }
            if (
                answer.risk != "low"
                or rule is None
                or not rule.matches(
                    adapter=str(binding.get("adapter") or ""),
                    adapter_version=str(binding.get("adapter_version") or ""),
                    field_semantic=answer.label,
                    context=context,
                )
                or _normalized(rule.value) != _normalized(answer.selected)
            ):
                field_issue = "answer_provenance_safe_default_invalid"
            else:
                provenance_ref = _provenance_ref(rule.rule_id)

        if field_issue:
            issues.append(f"{field_issue}:{answer.field_ref[:16]}")
        else:
            verified += 1
        fields.append(
            {
                "field_ref": answer.field_ref[:20],
                "semantic": answer.semantic,
                "risk": answer.risk,
                "control": answer.control,
                "status": "blocked" if field_issue else "verified",
                **({"provenance_ref": provenance_ref} if provenance_ref else {}),
                "evidence_refs": ["current_page", "host_browser_lease", "current_profile_registry"],
            }
        )

    eligible = len(observed) + overflow_count
    if incomplete_snapshot and eligible == 0:
        eligible = 1
    ratio = 0.0 if eligible == 0 and issues else (1.0 if eligible == 0 else verified / eligible)
    adapter, adapter_version = adapter_contract(snapshot)
    report: dict[str, object] = {
        "schema_version": ANSWER_MAPPING_SCHEMA_VERSION,
        "adapter": adapter,
        "adapter_version": adapter_version,
        "snapshot_ref": _provenance_ref(current_digest),
        "snapshot_digest": current_digest,
        "eligible_count": eligible,
        "verified_count": verified,
        "exemption_count": 0,
        "blocked_count": max(0, eligible - verified),
        "coverage_ratio": round(ratio, 6),
        "fields": fields[:MAX_PROVENANCE_FIELDS],
    }
    return AnswerProvenanceAudit(tuple(dict.fromkeys(issues)), report)
