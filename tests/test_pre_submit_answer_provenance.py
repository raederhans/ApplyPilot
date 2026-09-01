from __future__ import annotations

from copy import deepcopy

import pytest

from applypilot.apply import page_observation
from applypilot.apply.answer_policy import SafeDefaultRegistry, SafeDefaultRule, context_binding
from applypilot.apply.answer_provenance import (
    adapter_contract,
    audit_pre_submit_answer_provenance,
    build_host_provenance_binding,
    envelope_binding,
    field_key_hash,
    selected_option_digest,
    structure_snapshot_digest,
)
from applypilot.apply.browser_broker import BrowserLease, BrowserLeaseBundle
from applypilot.apply.contracts import application_actor_id
from applypilot.apply.page_binding import PageBinding


def _lease(attempt_id: str = "attempt-1", *, page_epoch: int = 0) -> dict[str, object]:
    common = {
        "lease_id": "lease-1",
        "owner_id": application_actor_id(attempt_id),
        "scope_id": "worker:1",
        "attempt_id": attempt_id,
        "runtime_id": "codex:cdp:9432",
        "epoch": 1,
        "issued_at": 1.0,
        "heartbeat_at": 2.0,
        "expires_at": 9999999999.0,
    }
    profile = BrowserLease(resource_kind="profile", resource_id="profile-1", **common)
    page = BrowserLease(resource_kind="page", resource_id="application:attempt-1", **common)
    binding = PageBinding(
        page_id=page.resource_id,
        page_lease_id=page.lease_id,
        page_lease_epoch=page.epoch,
        page_epoch=page_epoch,
        profile_lease_id=profile.lease_id,
        owner_id=page.owner_id,
        attempt_id=attempt_id,
        runtime_id=page.runtime_id,
    )
    return BrowserLeaseBundle(profile, page, binding).as_dict()


def _snapshot(
    *,
    text: str = "Years of experience",
    selected: str = "5+",
    options: tuple[str, ...] = ("0 years", "1-2 years", "5+"),
    field_key: str = "experience",
    control: str = "select",
    protected: bool = False,
) -> dict[str, object]:
    return {
        "url": "https://boards.greenhouse.io/example/jobs/123",
        "provenance_fields": [
            {
                "field_key": field_key,
                "control": control,
                "text": text,
                "selected": selected,
                "required": True,
                "options": list(options),
                "protected_identifier": protected,
            }
        ],
    }


def _base_job(*, page_epoch: int = 0) -> dict[str, object]:
    return {
        "_attempt_id": "attempt-1",
        "_browser_lease_binding": _lease(page_epoch=page_epoch),
        "url": "https://boards.greenhouse.io/example/jobs/123",
        "application_url": "https://boards.greenhouse.io/example/jobs/123",
        "title": "Data role",
        "company_name": "Example",
        "location": "Singapore",
        "employment_type": "Internship",
        "job_family": "Data",
        "full_description": "A bounded test role.",
    }


def _profile(
    *,
    value: object = 6,
    scope: str = "global:*",
    expires_at: str | None = None,
    sensitivity: str = "medium",
) -> dict[str, object]:
    fact = {
        "fact_ref": "profile:answer",
        "key": "years_of_experience",
        "value": value,
        "source": "profile.json",
        "scope": scope,
        "confirmed_at": "2026-08-31T00:00:00Z",
        "sensitivity": sensitivity,
    }
    if expires_at is not None:
        fact["expires_at"] = expires_at
    return {"application_facts": [fact]}


def _mapped_job(
    snapshot: dict[str, object],
    *,
    job: dict[str, object] | None = None,
    fact_ref: str = "profile:answer",
    risk: str = "medium",
    semantic: str = "unknown",
    option_digest: str | None = None,
) -> dict[str, object]:
    current = deepcopy(job or _base_job())
    binding = build_host_provenance_binding(current)
    digest = structure_snapshot_digest(snapshot)
    field = snapshot["provenance_fields"][0]  # type: ignore[index]
    adapter, version = adapter_contract(snapshot)
    field_ref = field_key_hash(
        adapter=adapter,
        adapter_version=version,
        control=str(field["control"]),
        field_key=field["field_key"],
    )
    current["_agent_observations"] = {
        "answer_mappings": {
            "schema_version": "2",
            "adapter": adapter,
            "adapter_version": version,
            "opaque_binding": envelope_binding(binding, digest),
            "snapshot_digest": digest,
            "mappings": [
                {
                    "field_key_hash": field_ref,
                    "semantic": semantic,
                    "risk": risk,
                    "selected_option_digest": option_digest
                    or selected_option_digest(field["selected"]),
                    "fact_ref": fact_ref,
                }
            ],
        }
    }
    return current


def _codes(result) -> set[str]:  # type: ignore[no-untyped-def]
    return {str(issue).split(":", 1)[0] for issue in result.issues}


def test_current_sealed_fact_covers_visible_select_without_raw_report_values() -> None:
    snapshot = _snapshot()
    result = audit_pre_submit_answer_provenance(snapshot, _profile(), _mapped_job(snapshot))

    assert result.issues == ()
    assert result.report["verified_count"] == 1
    assert result.report["coverage_ratio"] == 1.0
    assert "5+" not in repr(result.report)
    assert "Years of experience" not in repr(result.report)


@pytest.mark.parametrize("control", ["text", "textarea", "combobox"])
def test_required_custom_free_text_controls_are_eligible_and_exact(control: str) -> None:
    snapshot = _snapshot(
        text="Custom portfolio answer",
        selected="Exact scoped answer",
        options=(),
        field_key=f"custom-{control}",
        control=control,
    )
    profile = _profile(value="Exact scoped answer")
    job = _mapped_job(snapshot, risk="low", semantic="website")

    accepted = audit_pre_submit_answer_provenance(snapshot, profile, job)
    changed = deepcopy(snapshot)
    changed["provenance_fields"][0]["selected"] = "Different answer"  # type: ignore[index]
    replayed = audit_pre_submit_answer_provenance(changed, profile, job)

    assert accepted.issues == ()
    assert accepted.report["eligible_count"] == 1
    assert "answer_provenance_page_value_mismatch" in _codes(replayed)


def test_missing_mapping_and_incomplete_snapshot_never_report_false_full_coverage() -> None:
    snapshot = _snapshot(text="Required custom answer", selected="Blue")
    missing = audit_pre_submit_answer_provenance(snapshot, {}, _base_job())
    incomplete = audit_pre_submit_answer_provenance(
        {"url": snapshot["url"], "text_fields": [{"required": True, "value": "Blue"}]},
        {},
        _base_job(),
    )

    assert "answer_provenance_missing" in _codes(missing)
    assert incomplete.report["eligible_count"] == 1
    assert incomplete.report["coverage_ratio"] == 0.0
    assert "answer_provenance_snapshot_incomplete" in _codes(incomplete)


def test_selected_optional_custom_select_is_still_provenance_eligible() -> None:
    snapshot = _snapshot(text="Optional custom taxonomy", selected="Other")
    snapshot["provenance_fields"][0]["required"] = False  # type: ignore[index]

    result = audit_pre_submit_answer_provenance(snapshot, {}, _base_job())

    assert result.report["eligible_count"] == 1
    assert "answer_provenance_missing" in _codes(result)


@pytest.mark.parametrize("control", ["text", "combobox"])
def test_filled_optional_custom_controls_cannot_bypass_provenance(control: str) -> None:
    snapshot = _snapshot(
        text="Optional portfolio answer",
        selected="Private selected value",
        options=(),
        field_key=f"optional-{control}",
        control=control,
    )
    snapshot["provenance_fields"][0]["required"] = False  # type: ignore[index]

    result = audit_pre_submit_answer_provenance(snapshot, {}, _base_job())

    assert result.report["eligible_count"] == 1
    assert result.report["verified_count"] == 0
    assert result.report["coverage_ratio"] == 0.0
    assert "answer_provenance_missing" in _codes(result)
    assert "Private selected value" not in repr(result.report)


def test_more_than_128_filled_fields_fail_closed_without_raw_value_disclosure() -> None:
    snapshot = {
        "url": "https://boards.greenhouse.io/example/jobs/123",
        "provenance_fields": [
            {
                "field_key": f"custom-{index}",
                "control": "text",
                "text": f"Custom answer {index}",
                "selected": f"protected-value-{index}",
                "required": False,
                "options": [],
                "protected_identifier": False,
            }
            for index in range(129)
        ],
        "provenance_field_count": 129,
    }

    result = audit_pre_submit_answer_provenance(snapshot, {}, _base_job())

    assert "answer_provenance_field_overflow" in _codes(result)
    assert result.report["eligible_count"] == 129
    assert result.report["verified_count"] == 0
    assert result.report["blocked_count"] == 129
    assert result.report["coverage_ratio"] == 0.0
    assert len(result.report["fields"]) == 128
    assert "protected-value-128" not in repr(result.report)


def test_select_with_more_than_100_dom_options_fails_closed_as_overflow() -> None:
    snapshot = _snapshot(options=tuple(f"Choice {index}" for index in range(100)))
    field = snapshot["provenance_fields"][0]  # type: ignore[index]
    field["option_count"] = 101
    field["options_truncated"] = True

    result = audit_pre_submit_answer_provenance(snapshot, {}, _base_job())

    assert "answer_provenance_option_overflow" in _codes(result)
    assert result.report["eligible_count"] == 1
    assert result.report["blocked_count"] == 1


def test_only_regenerable_provenance_mapping_failures_are_repairable() -> None:
    blockers, repairable, _ = page_observation._partition_pre_submit_issues(
        [
            "answer_provenance_missing:abc",
            "answer_provenance_binding_mismatch",
            "answer_provenance_fact_out_of_scope:def",
            "answer_provenance_unsupported_control:ghi",
            "answer_provenance_high_risk_unknown:jkl",
        ]
    )

    assert repairable == [
        "answer_provenance_missing:abc",
        "answer_provenance_binding_mismatch",
    ]
    assert blockers == [
        "answer_provenance_fact_out_of_scope:def",
        "answer_provenance_unsupported_control:ghi",
        "answer_provenance_high_risk_unknown:jkl",
    ]


def test_public_scope_claim_is_ignored_and_host_allowlist_is_exact() -> None:
    snapshot = _snapshot()
    job = _mapped_job(snapshot)
    job["application_fact_scope"] = "attacker-selected-scope"
    job["_application_fact_scope"] = "global:*"
    result = audit_pre_submit_answer_provenance(
        snapshot, _profile(scope="attacker-selected-scope"), job
    )

    assert "answer_provenance_fact_out_of_scope" in _codes(result)


def test_expiry_rule_drift_and_agent_page_mismatch_are_blockers() -> None:
    snapshot = _snapshot()
    expired = audit_pre_submit_answer_provenance(
        snapshot,
        _profile(expires_at="2026-08-01T00:00:00Z"),
        _mapped_job(snapshot),
    )
    drift = audit_pre_submit_answer_provenance(
        snapshot, _profile(), _mapped_job(snapshot, risk="low")
    )
    mismatch = audit_pre_submit_answer_provenance(
        snapshot,
        _profile(),
        _mapped_job(snapshot, option_digest=selected_option_digest("1-2 years")),
    )

    assert "answer_provenance_fact_expired" in _codes(expired)
    assert "answer_provenance_rule_drift" in _codes(drift)
    assert "answer_provenance_page_value_mismatch" in _codes(mismatch)


def test_navigation_epoch_and_same_field_replay_break_the_v2_binding() -> None:
    snapshot = _snapshot()
    job = _mapped_job(snapshot)
    advanced = deepcopy(job)
    advanced["_browser_lease_binding"]["page_binding"]["page_epoch"] = 1  # type: ignore[index]
    advanced["_browser_lease_binding"]["page"]["epoch"] = 1  # type: ignore[index]
    navigation = audit_pre_submit_answer_provenance(snapshot, _profile(), advanced)
    navigated_snapshot = deepcopy(snapshot)
    navigated_snapshot["url"] = "https://boards.greenhouse.io/example/jobs/other"
    route_replay = audit_pre_submit_answer_provenance(
        navigated_snapshot, _profile(), job
    )
    changed = deepcopy(snapshot)
    changed["provenance_fields"][0]["selected"] = "1-2 years"  # type: ignore[index]
    same_field = audit_pre_submit_answer_provenance(changed, _profile(), job)

    assert "answer_provenance_binding_mismatch" in _codes(navigation)
    assert "answer_provenance_binding_mismatch" in _codes(route_replay)
    assert "answer_provenance_page_value_mismatch" in _codes(same_field)


def test_legacy_mapping_and_unsupported_or_protected_controls_fail_closed() -> None:
    snapshot = _snapshot(control="contenteditable")
    job = _mapped_job(snapshot)
    result = audit_pre_submit_answer_provenance(snapshot, _profile(), job)
    protected = _snapshot(control="text", protected=True, selected="[redacted-present]")
    protected_result = audit_pre_submit_answer_provenance(
        protected, _profile(value="secret"), _mapped_job(protected, risk="low")
    )
    legacy = _mapped_job(_snapshot())
    legacy["_agent_observations"]["answer_mappings"]["schema_version"] = "1"  # type: ignore[index]
    legacy_result = audit_pre_submit_answer_provenance(_snapshot(), _profile(), legacy)

    assert "answer_provenance_unsupported_control" in _codes(result)
    assert "answer_provenance_unsupported_control" in _codes(protected_result)
    assert "answer_provenance_binding_mismatch" in _codes(legacy_result)


def test_declaration_education_is_never_exempted() -> None:
    snapshot = _snapshot(
        text="I certify my education declaration",
        selected="Master's Degree",
        options=("Bachelor's Degree", "Master's Degree"),
        field_key="education-declaration",
    )
    result = audit_pre_submit_answer_provenance(
        snapshot,
        {"education": [{"institution": "Example", "degree": "Master's Degree"}]},
        _base_job(),
    )

    assert result.report["exemption_count"] == 0
    assert result.report["verified_count"] == 0
    assert _codes(result) & {"answer_provenance_high_risk_unknown", "answer_provenance_missing"}


def test_specific_low_risk_safe_default_is_bound_to_provider_version_and_host_context() -> None:
    snapshot = _snapshot(
        text="Application source",
        selected="Company website",
        options=("Referral", "Company website"),
        field_key="source",
    )
    job = _mapped_job(snapshot, risk="low")
    mapping = job["_agent_observations"]["answer_mappings"]["mappings"][0]  # type: ignore[index]
    mapping.pop("fact_ref")
    mapping["safe_default_rule_id"] = "greenhouse-source-v1"
    binding = build_host_provenance_binding(job)
    adapter, version = adapter_contract(snapshot)
    context = {
        "field_key_hash": mapping["field_key_hash"],
        "scope_binding": binding["opaque_binding_seed"],
    }
    registry = SafeDefaultRegistry()
    registry.register(
        SafeDefaultRule(
            "greenhouse-source-v1",
            adapter,
            version,
            "Application source",
            context_binding(context),
            "Company website",
        )
    )

    accepted = audit_pre_submit_answer_provenance(snapshot, {}, job, safe_defaults=registry)

    assert accepted.issues == ()
    assert accepted.report["verified_count"] == 1


def test_audit_is_read_only_and_has_no_submit_or_browser_write_authority() -> None:
    snapshot = _snapshot()
    profile = _profile()
    job = _mapped_job(snapshot)
    before = deepcopy((snapshot, profile, job))

    result = audit_pre_submit_answer_provenance(snapshot, profile, job)

    assert (snapshot, profile, job) == before
    assert "submission_gate" not in result.report
    assert "submit" not in repr(result.report).casefold()
