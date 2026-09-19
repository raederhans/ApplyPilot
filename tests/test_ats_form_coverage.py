from __future__ import annotations

import json
from pathlib import Path

import pytest

from applypilot.apply import ats_tools_mcp
from applypilot.apply.ats import adapter_prompt_context, build_form_ir, propose_fill_plan


def _payload(response: dict[str, object]) -> dict[str, object]:
    result = response["result"]
    assert isinstance(result, dict)
    payload = result["structuredContent"]
    assert isinstance(payload, dict)
    return payload


def _call_build_fill_plan(arguments: dict[str, object], monkeypatch, tmp_path: Path) -> dict[str, object]:
    context_path = tmp_path / "ats-context.json"
    context_path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "adapter": "greenhouse",
                "target_url": "https://boards.greenhouse.io/example/jobs/1",
                "available_fact_names": ["email"],
                "side_effect": "proposal-only",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(ats_tools_mcp.ATS_CONTEXT_PATH_ENV, str(context_path))
    response = ats_tools_mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "build_fill_plan", "arguments": arguments},
        }
    )
    assert response is not None
    return _payload(response)


def test_large_form_pages_expose_required_late_field_with_stable_keys() -> None:
    fields = [
        {
            "id": f"field-{index}",
            "label": "Email" if index == 0 else "Custom question",
            "required": index == 94,
            "value": "private-value-must-not-leak" if index == 94 else None,
        }
        for index in range(100)
    ]
    form = build_form_ir("https://boards.greenhouse.io/example/jobs/1", fields)
    plan = propose_fill_plan(form, {"email"})

    page = adapter_prompt_context(form, plan, field_offset=80, field_limit=20)
    assert [item["field_key"] for item in page["fields"]] == [
        f"field-{index}" for index in range(80, 100)
    ]
    required = next(item for item in page["fields"] if item["field_key"] == "field-94")
    assert required["required"] is True
    action = next(item for item in page["actions"] if item["field_key"] == "field-94")
    assert action["action"] == "request_fact"
    assert page["pagination"] == {
        "field_offset": 80,
        "field_limit": 20,
        "field_returned": 20,
        "field_total": 100,
        "field_has_more": False,
        "field_next_offset": None,
        "option_offset": 0,
        "option_limit": 20,
    }
    assert "private-value-must-not-leak" not in json.dumps(page)

    repeated = adapter_prompt_context(form, plan, field_offset=80, field_limit=20)
    assert repeated["fields"] == page["fields"]
    assert repeated["actions"] == page["actions"]


def test_build_fill_plan_mcp_can_page_late_required_fields_without_values(
    monkeypatch, tmp_path: Path
) -> None:
    fields = [
        {
            "id": f"field-{index}",
            "label": "Custom question",
            "required": index == 94,
            "value": "secret-form-value" if index == 94 else None,
        }
        for index in range(100)
    ]
    payload = _call_build_fill_plan(
        {
            "fields": fields,
            "field_offset": 80,
            "field_limit": 20,
        },
        monkeypatch,
        tmp_path,
    )
    assert [item["field_key"] for item in payload["fields"]] == [
        f"field-{index}" for index in range(80, 100)
    ]
    assert any(
        item["field_key"] == "field-94" and item["action"] == "request_fact"
        for item in payload["actions"]
    )
    assert "secret-form-value" not in json.dumps(payload)


def test_option_windows_traverse_all_100_options_with_bounded_pages() -> None:
    options = [f"Choice {index}" for index in range(100)]
    form = build_form_ir(
        "https://jobs.example.test/apply",
        [{"id": "degree", "label": "Degree", "type": "select", "options": options}],
    )

    windows = [
        adapter_prompt_context(
            form, option_offset=offset, option_limit=20, include_paging=True
        )["fields"][0]
        for offset in range(0, 100, 20)
    ]
    assert [option for window in windows for option in window["options"]] == options
    assert all(window["option_count"] == 100 for window in windows)
    assert all(window["options_limit"] == 20 for window in windows)
    # Each page remains explicitly non-complete so a single page cannot be
    # mistaken for a complete trusted option set; the final page signals the
    # end of traversal separately.
    assert all(window["options_truncated"] for window in windows)
    assert windows[-1]["options_has_more"] is False
    assert windows[-1]["options_next_offset"] is None


def test_form_and_option_paging_reject_invalid_selectors_and_limits() -> None:
    form = build_form_ir(
        "https://jobs.example.test/apply",
        [{"id": "known", "label": "Question", "options": ["A"]}],
    )
    with pytest.raises(ValueError, match="unknown field key"):
        adapter_prompt_context(form, field_keys=["missing"])
    with pytest.raises(ValueError, match="field_limit"):
        adapter_prompt_context(form, field_limit=0)
    with pytest.raises(ValueError, match="field_limit"):
        adapter_prompt_context(form, field_limit=81)
    with pytest.raises(ValueError, match="option_limit"):
        adapter_prompt_context(form, option_limit=21)
    with pytest.raises(ValueError, match="field_offset"):
        adapter_prompt_context(form, field_offset=2)


def test_build_fill_plan_mcp_reports_invalid_page_request(monkeypatch, tmp_path: Path) -> None:
    context_path = tmp_path / "ats-context.json"
    context_path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "adapter": "greenhouse",
                "target_url": "https://boards.greenhouse.io/example/jobs/1",
                "available_fact_names": [],
                "side_effect": "proposal-only",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(ats_tools_mcp.ATS_CONTEXT_PATH_ENV, str(context_path))
    response = ats_tools_mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "build_fill_plan",
                "arguments": {
                    "fields": [{"id": "known", "label": "Question"}],
                    "field_keys": ["missing"],
                },
            },
        }
    )
    assert response is not None
    result = response["result"]
    assert isinstance(result, dict)
    assert result["isError"] is True
