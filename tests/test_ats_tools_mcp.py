from __future__ import annotations

import json
from pathlib import Path

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
        "evaluate_workday_progress",
    ]
    assert all("submit" not in tool["name"] for tool in tools)


def test_resolve_answer_tool_is_proposal_only_and_returns_audit() -> None:
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
                    "confirmed_fact": "Master of Computing in Applied AI",
                    "required": True,
                    "direct_impact": True,
                },
            },
        }
    )
    assert response is not None
    payload = _tool_payload(response)

    assert payload["relation"] == "closest_non_equivalent"
    assert payload["action"] == "select_and_record"
    assert payload["selected_option"] == "Master of Science"
    assert payload["side_effect"] == "proposal-only"
    assert payload["audit"]["confirmed_fact"] == "Master of Computing in Applied AI"


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
