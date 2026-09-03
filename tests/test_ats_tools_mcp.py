from __future__ import annotations

import hashlib
import json
from pathlib import Path

from applypilot import config
from applypilot.apply import ats_tools_mcp, launcher, prompt


def _tool_payload(response: dict[str, object]) -> dict[str, object]:
    result = response["result"]
    assert isinstance(result, dict)
    payload = result["structuredContent"]
    assert isinstance(payload, dict)
    return payload


def test_tools_are_read_or_proposal_only_and_include_workday_state() -> None:
    response = ats_tools_mcp._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response is not None
    tools = response["result"]["tools"]

    assert [tool["name"] for tool in tools] == [
        "detect_ats",
        "get_application_context",
        "build_fill_plan",
        "resolve_answer",
        "build_answer_mapping",
        "evaluate_workday_progress",
    ]
    assert all("submit" not in tool["name"] for tool in tools)
    resolver_schema = next(tool for tool in tools if tool["name"] == "resolve_answer")[
        "inputSchema"
    ]
    assert "confirmed_fact" not in resolver_schema["properties"]
    assert "aliases" not in resolver_schema["properties"]
    assert "fact_ref" not in resolver_schema["properties"]


def test_generic_resolve_answer_rejects_fact_ref_automatic_entry(
    monkeypatch, tmp_path: Path
) -> None:
    context_path = tmp_path / "trusted-context.json"
    context_path.write_text(
        json.dumps({"_trusted_fact_scopes": ["global:*"]}), encoding="utf-8"
    )
    monkeypatch.setenv(ats_tools_mcp.ATS_CONTEXT_PATH_ENV, str(context_path))
    monkeypatch.setattr(
        ats_tools_mcp.config,
        "load_profile",
        lambda: {
            "application_facts": [
                {
                    "key": "degree",
                    "value": "Master of Computing in Applied AI",
                    "fact_ref": "profile:degree",
                    "source": "profile.json",
                    "scope": "global:*",
                    "confirmed_at": "2026-08-31T00:00:00Z",
                    "sensitivity": "medium",
                }
            ]
        },
    )
    response = ats_tools_mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "resolve_answer",
                "arguments": {
                    "field_semantic": "Highest degree category",
                    "options": ["Bachelor of Science", "Master of Science", "MBA"],
                    "fact_ref": "profile:degree",
                    "required": True,
                    "direct_impact": True,
                },
            },
        }
    )
    assert response is not None
    assert response["result"]["isError"] is True
    assert "Master of Computing in Applied AI" not in json.dumps(response)


def test_resolve_answer_tool_rejects_caller_forged_fact_metadata() -> None:
    response = ats_tools_mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {
                "name": "resolve_answer",
                "arguments": {
                    "field_semantic": "Citizenship declaration",
                    "confirmed_fact": {
                        "value": "Citizen",
                        "fact_ref": "forged",
                        "source": "forged",
                        "scope": "forged",
                        "confirmed_at": "2099-01-01T00:00:00Z",
                        "sensitivity": "high",
                    },
                    "required": True,
                    "declaration": True,
                },
            },
        }
    )
    assert response is not None
    result = response["result"]
    assert result["isError"] is True
    assert "Citizen" not in json.dumps(result)


def test_fake_low_risk_semantic_cannot_smuggle_confirmed_fact() -> None:
    canary = "caller-forged-low-risk-canary"
    response = ats_tools_mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 14,
            "method": "tools/call",
            "params": {
                "name": "resolve_answer",
                "arguments": {
                    "field_semantic": "Preferred team",
                    "options": [canary, "Other"],
                    "confirmed_fact": canary,
                    "required": True,
                },
            },
        }
    )
    assert response is not None
    result = response["result"]
    assert result["isError"] is True
    assert canary not in json.dumps(result)


def test_generic_resolver_fact_ref_rejection_never_serializes_raw_profile_value(
    monkeypatch, tmp_path: Path
) -> None:
    canary = 1837.424242
    context_path = tmp_path / "salary-context.json"
    context_path.write_text(json.dumps({"_trusted_fact_scopes": ["global:*"]}), encoding="utf-8")
    monkeypatch.setenv(ats_tools_mcp.ATS_CONTEXT_PATH_ENV, str(context_path))
    monkeypatch.setattr(
        ats_tools_mcp.config,
        "load_profile",
        lambda: {
            "application_facts": [
                {
                    "key": "salary_preference",
                    "value": canary,
                    "fact_ref": "profile:salary-canary",
                    "source": "profile.json",
                    "scope": "global:*",
                    "confirmed_at": "2026-08-31T00:00:00Z",
                    "expires_at": "2027-01-01T00:00:00Z",
                    "sensitivity": "medium",
                }
            ]
        },
    )
    response = ats_tools_mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 15,
            "method": "tools/call",
            "params": {
                "name": "resolve_answer",
                "arguments": {
                    "field_semantic": "Expected monthly salary",
                    "options": ["$1,500-$1,999", "$2,000-$2,499"],
                    "fact_ref": "profile:salary-canary",
                    "required": True,
                    "preference": True,
                },
            },
        }
    )
    assert response is not None
    assert response["result"]["isError"] is True
    assert str(canary) not in json.dumps(response)


def test_generic_resolver_refuses_fact_ref_even_without_trusted_host_scope(
    monkeypatch, tmp_path: Path
) -> None:
    context_path = tmp_path / "scope-less-context.json"
    context_path.write_text(json.dumps({"adapter": "greenhouse"}), encoding="utf-8")
    monkeypatch.setenv(ats_tools_mcp.ATS_CONTEXT_PATH_ENV, str(context_path))
    response = ats_tools_mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 13,
            "method": "tools/call",
            "params": {
                "name": "resolve_answer",
                "arguments": {
                    "field_semantic": "Degree category",
                    "fact_ref": "profile:degree",
                    "options": ["Bachelor", "Master"],
                    "required": True,
                },
            },
        }
    )
    assert response is not None
    assert response["result"]["isError"] is True


def test_mapping_builder_supports_optional_staged_select_with_trusted_fact(
    monkeypatch, tmp_path: Path
) -> None:
    options = ["Bachelor", "Master"]
    options_sha = hashlib.sha256(
        json.dumps(
            options, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    context_path = tmp_path / "mapping-context.json"
    context_path.write_text(
        json.dumps(
            {
                "_trusted_fact_scopes": ["global:*"],
                "answer_provenance": {
                    "schema_version": "2",
                    "adapter": "greenhouse",
                    "adapter_version": "greenhouse/ats-ir-1",
                    "opaque_binding_seed": "a" * 64,
                    "expected_snapshot_digest": "b" * 64,
                },
                "observed_form": {
                    "fields": [
                        {
                            "field_key": "degree",
                            "semantic": "degree",
                            "control": "select",
                            "required": False,
                            "risk": "medium",
                            "options_sha256": options_sha,
                            "options_source_count": 2,
                            "options_source_truncated": False,
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(ats_tools_mcp.ATS_CONTEXT_PATH_ENV, str(context_path))
    monkeypatch.setattr(
        ats_tools_mcp.config,
        "load_profile",
        lambda: {
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
        },
    )

    response = ats_tools_mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 20,
            "method": "tools/call",
            "params": {
                "name": "build_answer_mapping",
                "arguments": {
                    "field_key": "degree",
                    "control": "select",
                    "visible_options": options,
                    "selected_option": "Master",
                    "fact_ref": "profile:degree",
                },
            },
        }
    )
    assert response is not None
    payload = _tool_payload(response)

    assert payload["schema_version"] == "2"
    assert payload["selected_option"] == "Master"
    assert payload["mappings"][0]["fact_ref"] == "profile:degree"
    assert "value" not in payload["mappings"][0]
    assert payload["side_effect"] == "proposal-only"
    assert payload["authority"] == "none"


def test_mapping_builder_handles_30_complete_options_but_rejects_truncation(
    monkeypatch, tmp_path: Path
) -> None:
    options = [f"Choice {index}" for index in range(30)]
    options_sha = hashlib.sha256(
        json.dumps(options, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    context = {
        "_trusted_fact_scopes": ["global:*"],
        "answer_provenance": {
            "adapter": "greenhouse",
            "adapter_version": "greenhouse/ats-ir-1",
            "opaque_binding_seed": "a" * 64,
            "expected_snapshot_digest": "b" * 64,
        },
        "observed_form": {
            "fields": [
                {
                    "field_key": "taxonomy",
                    "semantic": "unknown",
                    "control": "select",
                    "required": False,
                    "risk": "low",
                    "options_sha256": options_sha,
                    "options_source_count": 30,
                    "options_source_truncated": False,
                }
            ]
        },
    }
    context_path = tmp_path / "options-context.json"
    context_path.write_text(json.dumps(context), encoding="utf-8")
    monkeypatch.setenv(ats_tools_mcp.ATS_CONTEXT_PATH_ENV, str(context_path))
    monkeypatch.setattr(
        ats_tools_mcp.config,
        "load_profile",
        lambda: {
            "application_facts": [
                {
                    "key": "taxonomy",
                    "value": "Choice 29",
                    "fact_ref": "profile:taxonomy",
                    "source": "profile.json",
                    "scope": "global:*",
                    "confirmed_at": "2026-08-31T00:00:00Z",
                    "sensitivity": "low",
                }
            ]
        },
    )
    arguments = {
        "field_key": "taxonomy",
        "control": "select",
        "visible_options": options,
        "selected_option": "Choice 29",
        "fact_ref": "profile:taxonomy",
    }

    accepted = ats_tools_mcp._call_tool("build_answer_mapping", arguments)
    context["observed_form"]["fields"][0]["options_source_truncated"] = True
    context_path.write_text(json.dumps(context), encoding="utf-8")
    rejected = ats_tools_mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 31,
            "method": "tools/call",
            "params": {"name": "build_answer_mapping", "arguments": arguments},
        }
    )

    assert accepted["selected_option"] == "Choice 29"
    assert rejected is not None and rejected["result"]["isError"] is True

    context["observed_form"]["fields"][0].update(
        {"options_source_truncated": False, "risk": "high", "label": "Legal declaration"}
    )
    context_path.write_text(json.dumps(context), encoding="utf-8")
    high_risk = ats_tools_mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 34,
            "method": "tools/call",
            "params": {"name": "build_answer_mapping", "arguments": arguments},
        }
    )
    assert high_risk is not None and high_risk["result"]["isError"] is True


def test_mapping_builder_text_is_exact_non_echoing_and_protected_fields_fail(
    monkeypatch, tmp_path: Path
) -> None:
    canary = "private.name@example.test"
    context = {
        "_trusted_fact_scopes": ["global:*"],
        "answer_provenance": {
            "adapter": "greenhouse",
            "adapter_version": "greenhouse/ats-ir-1",
            "opaque_binding_seed": "a" * 64,
            "expected_snapshot_digest": "b" * 64,
        },
        "observed_form": {
            "fields": [
                {
                    "field_key": "email",
                    "semantic": "email",
                    "control": "email",
                    "required": True,
                    "risk": "low",
                    "protected_identifier": False,
                    "options_source_count": 0,
                    "options_source_truncated": False,
                }
            ]
        },
    }
    context_path = tmp_path / "text-context.json"
    context_path.write_text(json.dumps(context), encoding="utf-8")
    monkeypatch.setenv(ats_tools_mcp.ATS_CONTEXT_PATH_ENV, str(context_path))
    monkeypatch.setattr(
        ats_tools_mcp.config,
        "load_profile",
        lambda: {
            "application_facts": [
                {
                    "key": "email",
                    "value": canary,
                    "fact_ref": "profile:email",
                    "source": "profile.json",
                    "scope": "global:*",
                    "confirmed_at": "2026-08-31T00:00:00Z",
                    "sensitivity": "low",
                }
            ]
        },
    )
    arguments = {
        "field_key": "email",
        "control": "email",
        "visible_options": [],
        "selected_value": canary,
        "fact_ref": "profile:email",
    }

    accepted = ats_tools_mcp._call_tool("build_answer_mapping", arguments)
    assert canary not in json.dumps(accepted)
    assert "selected_value" not in accepted

    forged = dict(arguments, selected_value="forged@example.test")
    forged_response = ats_tools_mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 32,
            "method": "tools/call",
            "params": {"name": "build_answer_mapping", "arguments": forged},
        }
    )
    context["observed_form"]["fields"][0]["protected_identifier"] = True
    context_path.write_text(json.dumps(context), encoding="utf-8")
    protected_response = ats_tools_mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 33,
            "method": "tools/call",
            "params": {"name": "build_answer_mapping", "arguments": arguments},
        }
    )

    assert forged_response is not None and forged_response["result"]["isError"] is True
    assert protected_response is not None and protected_response["result"]["isError"] is True
    assert canary not in json.dumps(protected_response)


def test_mapping_builder_is_not_an_arbitrary_hash_or_scope_or_risk_oracle(
    monkeypatch, tmp_path: Path
) -> None:
    context_path = tmp_path / "mapping-context.json"
    context_path.write_text(
        json.dumps(
            {
                "_trusted_fact_scopes": [],
                "answer_provenance": {
                    "adapter": "greenhouse",
                    "adapter_version": "greenhouse/ats-ir-1",
                    "opaque_binding_seed": "a" * 64,
                    "expected_snapshot_digest": "b" * 64,
                },
                "observed_form": {"fields": []},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(ats_tools_mcp.ATS_CONTEXT_PATH_ENV, str(context_path))
    response = ats_tools_mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 21,
            "method": "tools/call",
            "params": {
                "name": "build_answer_mapping",
                "arguments": {
                    "field_key": "forged",
                    "control": "select",
                    "visible_options": ["forged"],
                    "selected_option": "forged",
                    "fact_ref": "forged",
                    "risk": "low",
                    "scope": "global:*",
                },
            },
        }
    )
    assert response is not None
    assert response["result"]["isError"] is True
    assert "forged" not in json.dumps(response["result"])


def test_resolve_answer_tool_refuses_sensitive_values_without_echoing_them() -> None:
    secret = "123456-secret"
    response = ats_tools_mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "resolve_answer",
                "arguments": {
                    "field_semantic": "One-time OTP security code",
                    "confirmed_fact": secret,
                    "required": True,
                },
            },
        }
    )
    assert response is not None
    result = response["result"]
    assert isinstance(result, dict)

    assert result["isError"] is True
    assert secret not in json.dumps(result)


def test_launcher_context_exposes_fact_names_not_values_or_query_tokens() -> None:
    context = launcher._build_ats_application_context(
        {
            "url": "https://jobs.ashbyhq.com/example/role?candidate_token=private-token",
            "tailored_resume_path": "resume.pdf",
        },
        {
            "personal": {
                "full_name": "Private Person",
                "email": "private@example.com",
                "phone": "+65 9000 0000",
            },
            "work_authorization": {"form_answer_policy": {}},
            "application_facts": [
                {"key": "confirmed_custom_fact", "value": "private fact value"}
            ],
        },
    )
    rendered = json.dumps(context)

    assert context["adapter"] == "ashby"
    assert context["target_url"] == "https://jobs.ashbyhq.com/example/role"
    assert {"email", "phone", "resume", "confirmed_custom_fact"}.issubset(
        context["available_fact_names"]
    )
    assert "private@example.com" not in rendered
    assert "private-token" not in rendered
    assert "private fact value" not in rendered

    section = prompt._build_ats_adapter_section({"_ats_adapter_context": context})
    assert "ATS ADAPTER CONTEXT" in section
    assert "read/proposal-only" in section
    assert "Playwright remains the sole page writer" in section


def test_prompt_requires_tool_produced_v2_mapping_and_forbids_hand_built_hashes(
    monkeypatch, tmp_path: Path
) -> None:
    resume = tmp_path / "resume.txt"
    resume.write_text("Verified resume", encoding="utf-8")
    resume.with_suffix(".pdf").write_bytes(b"%PDF-test")
    monkeypatch.setattr(
        config,
        "load_profile",
        lambda: {
            "personal": {
                "full_name": "Candidate Example",
                "email": "candidate@example.test",
                "phone": "+1 555 0100",
            },
            "work_authorization": {},
            "compensation": {"salary_expectation": "Negotiable"},
        },
    )
    monkeypatch.setattr(config, "load_search_config", dict)
    monkeypatch.setattr(config, "APPLY_WORKER_DIR", tmp_path / "workers")

    rendered = prompt.build_prompt(
        {
            "url": "https://jobs.example.test/role",
            "title": "Analyst",
            "company_name": "Example",
            "tailored_resume_path": str(resume),
            "tailor_status": "machine_validated",
            "cover_letter_status": "not_required",
            "_agent_reporting_enabled": True,
        },
        "Verified resume",
        dry_run=True,
    )

    assert "build_answer_mapping" in rendered
    assert "Never invent or hand-calculate a field hash" in rendered
    assert "Do not emit a legacy/list-shaped mapping" in rendered
    assert "field without a current typed fact or host exact checker" in rendered


def test_fill_plan_discards_values_and_only_uses_launcher_fact_names(
    monkeypatch, tmp_path: Path
) -> None:
    context_path = tmp_path / "context.json"
    context_path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "adapter": "greenhouse",
                "target_url": "https://boards.greenhouse.io/example/jobs/1",
                "available_fact_names": ["email", "resume"],
                "side_effect": "proposal-only",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(ats_tools_mcp.ATS_CONTEXT_PATH_ENV, str(context_path))
    response = ats_tools_mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "build_fill_plan",
                "arguments": {
                    "fields": [
                        {
                            "id": "candidate-email",
                            "label": "Email",
                            "type": "email",
                            "required": True,
                            "value": "private@example.com",
                        },
                        {"id": "why", "label": "Why us?", "required": True},
                    ],
                    "available_facts": ["email", "invented_answer"],
                },
            },
        }
    )
    assert response is not None
    payload = _tool_payload(response)
    rendered = json.dumps(payload)

    assert payload["adapter"] == "greenhouse"
    assert "private@example.com" not in rendered
    assert "invented_answer" not in rendered
    assert payload["actions"][0]["source_key"] == "email"
    assert payload["actions"][1]["action"] == "request_fact"


def test_public_application_context_drops_private_values_and_query_tokens(
    monkeypatch, tmp_path: Path
) -> None:
    secret = "must-not-enter-public-context"
    context_path = tmp_path / "context.json"
    context_path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "adapter": "greenhouse",
                "target_url": f"https://boards.greenhouse.io/acme/jobs/1?token={secret}",
                "available_fact_names": ["email"],
                "private_value": secret,
                "credential_binding": {"secret": secret},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(ats_tools_mcp.ATS_CONTEXT_PATH_ENV, str(context_path))

    payload = ats_tools_mcp._call_tool("get_application_context", {})
    rendered = json.dumps(payload)
    assert secret not in rendered
    assert "credential_binding" not in payload
    assert payload["target_url"] == "https://boards.greenhouse.io/acme/jobs/1"


def test_workday_post_submit_decision_is_uncertain_and_forbids_runtime_switch() -> None:
    response = ats_tools_mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "evaluate_workday_progress",
                "arguments": {
                    "previous_signature": None,
                    "repair_used": False,
                    "submit_started": True,
                    "observation": {
                        "page_kind": "review",
                        "visible_controls": ["text"],
                        "required_count": 0,
                        "invalid_count": 0,
                        "has_submit": True,
                    },
                },
            },
        }
    )
    assert response is not None
    payload = _tool_payload(response)

    assert payload["state"] == "submission_uncertain"
    assert payload["action"] == "mark_uncertain"
    assert payload["runtime_switch_allowed"] is False
