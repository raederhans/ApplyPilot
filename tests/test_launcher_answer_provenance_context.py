from __future__ import annotations

import hashlib
import json

from applypilot.apply import ats_tools_mcp, launcher, page_observation
from applypilot.apply.answer_provenance import audit_pre_submit_answer_provenance
from applypilot.apply.browser_broker import BrowserLease, BrowserLeaseBundle
from applypilot.apply.contracts import application_actor_id
from applypilot.apply.page_binding import PageBinding


def _job() -> dict[str, object]:
    attempt_id = "attempt-provenance"
    common = {
        "lease_id": "lease-provenance",
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
    page = BrowserLease(resource_kind="page", resource_id=f"application:{attempt_id}", **common)
    binding = PageBinding(
        page_id=page.resource_id,
        page_lease_id=page.lease_id,
        page_lease_epoch=1,
        page_epoch=3,
        profile_lease_id=profile.lease_id,
        owner_id=page.owner_id,
        attempt_id=attempt_id,
        runtime_id=page.runtime_id,
    )
    return {
        "_attempt_id": attempt_id,
        "_browser_lease_binding": BrowserLeaseBundle(profile, page, binding).as_dict(),
        "url": "https://boards.greenhouse.io/example/jobs/123",
        "application_url": "https://boards.greenhouse.io/example/jobs/123",
        "title": "Private job title",
        "company_name": "Private employer",
        "location": "Private jurisdiction",
        "employment_type": "Private employment type",
        "job_family": "Private job family",
        "full_description": "Private immutable description",
    }


def test_launcher_produces_opaque_host_binding_and_canonical_scope_allowlist() -> None:
    job = _job()
    job["_browser_observation"] = {
        "answer_provenance": {"snapshot_digest": "c" * 64},
        "ats_adapter_context": {
            "adapter": "greenhouse",
            "fields": [
                {
                    "field_key": "degree",
                    "semantic": "unknown",
                    "label": "Citizenship declaration",
                    "control": "select",
                    "required": True,
                    "options": ["Bachelor", "Master"],
                    "options_full_sha256": hashlib.sha256(
                        json.dumps(
                            ["Bachelor", "Master"],
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest(),
                    "options_source_count": 2,
                    "options_source_truncated": False,
                }
            ],
        },
    }

    public = launcher._install_answer_provenance_context(job)
    context = launcher._build_ats_application_context(job, {}, attempt_id=str(job["_attempt_id"]))

    assert public["authority"] == "observation_only"
    assert public["schema_version"] == "2"
    assert "attempt-provenance" not in repr(public)
    assert "Private employer" not in repr(public)
    assert all(
        context["answer_provenance"][key] == value for key, value in public.items()
    )
    assert context["answer_provenance"]["expected_snapshot_digest"] == "c" * 64
    assert context["observed_form"]["fields"][0]["options_sha256"]
    assert context["observed_form"]["fields"][0]["risk"] == "high"
    trusted = context["_trusted_fact_scopes"]
    assert trusted[0] == "global:*"
    assert {item.split(":", 1)[0] for item in trusted} == {
        "global",
        "jurisdiction",
        "employment_type",
        "job_family",
        "employer",
        "this_application",
    }


def test_launcher_rejects_stale_page_epoch_after_binding_installation() -> None:
    job = _job()
    launcher._install_answer_provenance_context(job)
    job["_browser_lease_binding"]["page_binding"]["page_epoch"] = 4  # type: ignore[index]

    try:
        launcher._build_ats_application_context(job, {}, attempt_id=str(job["_attempt_id"]))
    except ValueError as exc:
        assert "provenance binding drifted" in str(exc)
    else:
        raise AssertionError("stale provenance context was accepted")


def test_production_observation_preserves_complete_30_option_digest() -> None:
    job = _job()
    options = [f"Choice {index}" for index in range(30)]
    snapshot = {
        "url": job["url"],
        "form_fields": [
            {
                "field_key": "taxonomy",
                "label": "Optional taxonomy",
                "control": "select",
                "required": False,
                "options": options,
                "option_count": 30,
                "options_truncated": False,
            }
        ],
    }

    adapter_context, _, issues = page_observation._adapter_observation_context(
        snapshot, job
    )
    job["_browser_observation"] = {"ats_adapter_context": adapter_context}
    launcher._install_answer_provenance_context(job)
    staged = launcher._build_ats_application_context(job, {})["observed_form"]["fields"][0]

    assert issues == []
    assert adapter_context["fields"][0]["options_truncated"] is True
    assert adapter_context["fields"][0]["options_source_truncated"] is False
    assert staged["options_source_count"] == 30
    assert staged["options_source_truncated"] is False
    assert staged["options_sha256"] == hashlib.sha256(
        json.dumps(options, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_launcher_only_computes_legacy_option_digest_when_completeness_is_proven() -> None:
    options = ["Only visible option"]
    expected = hashlib.sha256(
        json.dumps(options, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    def staged(*, truncated: bool, count: int) -> dict[str, object]:
        job = _job()
        job["_browser_observation"] = {
            "ats_adapter_context": {
                "adapter": "greenhouse",
                "fields": [
                    {
                        "field_key": "legacy-select",
                        "semantic": "unknown",
                        "control": "select",
                        "required": False,
                        "options": options,
                        "option_count": count,
                        "options_truncated": truncated,
                    }
                ],
            }
        }
        launcher._install_answer_provenance_context(job)
        return launcher._build_ats_application_context(job, {})["observed_form"][
            "fields"
        ][0]

    complete = staged(truncated=False, count=1)
    truncated = staged(truncated=True, count=1)
    mismatched = staged(truncated=False, count=2)

    assert complete["options_sha256"] == expected
    assert complete["options_source_count"] == 1
    assert complete["options_source_truncated"] is False
    assert truncated["options_sha256"] is None
    assert truncated["options_source_truncated"] is True
    assert mismatched["options_sha256"] is None
    assert mismatched["options_source_truncated"] is True


def test_launcher_fact_ref_catalog_flows_through_public_context_to_builder(
    monkeypatch, tmp_path
) -> None:
    job = _job()
    options = ["Bachelor", "Master"]
    options_sha = hashlib.sha256(
        json.dumps(options, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    snapshot = {
        "url": job["url"],
        "provenance_fields": [
            {
                "field_key": "degree",
                "text": "Degree category",
                "control": "select",
                "required": True,
                "selected": "Master",
                "options": options,
                "option_count": 2,
                "options_truncated": False,
            }
        ],
    }
    first_audit = audit_pre_submit_answer_provenance(snapshot, profile := {
        "application_facts": [
            {
                "key": "degree",
                "value": "Master",
                "fact_ref": "profile:degree",
                "source": "profile.json",
                "scope": "global:*",
                "confirmed_at": "2026-08-31T00:00:00Z",
                "sensitivity": "medium",
            }
        ]
    }, job)
    job["_browser_observation"] = {
        "answer_provenance": first_audit.report,
        "ats_adapter_context": {
            "adapter": "greenhouse",
            "fields": [
                {
                    "field_key": "degree",
                    "label": "Degree category",
                        "semantic": "unknown",
                    "control": "select",
                    "required": True,
                    "options": options[:1],
                    "options_full_sha256": options_sha,
                    "options_source_count": 2,
                    "options_source_truncated": False,
                }
            ],
        },
    }
    launcher._install_answer_provenance_context(job)
    context = launcher._build_ats_application_context(job, profile)
    context_path = tmp_path / "launcher-context.json"
    context_path.write_text(json.dumps(context), encoding="utf-8")
    monkeypatch.setenv(ats_tools_mcp.ATS_CONTEXT_PATH_ENV, str(context_path))
    monkeypatch.setattr(ats_tools_mcp.config, "load_profile", lambda: profile)

    public = ats_tools_mcp._call_tool("get_application_context", {})
    mapping = ats_tools_mcp._call_tool(
        "build_answer_mapping",
        {
            "field_key": "degree",
            "control": "select",
            "visible_options": options,
            "selected_option": "Master",
            "fact_ref": public["available_fact_refs"][0]["fact_ref"],
        },
    )
    job["_agent_observations"] = {
        "answer_mappings": {
            key: mapping[key]
            for key in (
                "schema_version",
                "adapter",
                "adapter_version",
                "opaque_binding",
                "snapshot_digest",
                "mappings",
            )
        }
    }
    second_audit = audit_pre_submit_answer_provenance(snapshot, profile, job)

    assert any(str(issue).startswith("answer_provenance_missing:") for issue in first_audit.issues)
    assert public["available_fact_refs"] == [
        {"key": "degree", "fact_ref": "profile:degree", "sensitivity": "medium"}
    ]
    assert mapping["mappings"][0]["fact_ref"] == "profile:degree"
    assert second_audit.issues == ()
    assert second_audit.report["coverage_ratio"] == 1.0
    assert "Master" not in json.dumps(public)
