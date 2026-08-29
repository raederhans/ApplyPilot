from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from applypilot.apply.ats import (
    AtsAdapterRegistry,
    GenericAtsAdapter,
    adapter_prompt_context,
    adapter_prompt_guidance,
    build_form_ir,
    default_ats_registry,
    detect_ats_site,
    propose_fill_plan,
)
from applypilot.apply.page_observation import _adapter_observation_context

_DETECTION_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "apply" / "ats_adapter_cases.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize(
    ("url", "adapter"),
    [(case["url"], case["adapter"]) for case in _DETECTION_CASES],
)
def test_detection_uses_exact_parsed_hostnames(url: str, adapter: str) -> None:
    assert detect_ats_site(url) == adapter


@dataclass(frozen=True)
class ExamplePluginAdapter(GenericAtsAdapter):
    name: str = "example-plugin"

    def matches(self, *, hostname: str, path: str) -> bool:
        return hostname == "careers.example.test" and path.startswith("/apply/")


def test_registry_is_dynamic_supports_replace_and_falls_back() -> None:
    registry = AtsAdapterRegistry()
    adapter = ExamplePluginAdapter()
    registry.register(adapter)

    assert registry.detect("https://careers.example.test/apply/1") is adapter
    assert registry.detect("https://careers.example.test/jobs/1").name == "generic"
    with pytest.raises(ValueError, match="already registered"):
        registry.register(ExamplePluginAdapter())

    replacement = ExamplePluginAdapter()
    registry.register(replacement, replace=True)
    assert registry.get("example-plugin") is replacement


def test_form_ir_is_provider_neutral_bounded_and_discards_values() -> None:
    form = build_form_ir(
        "https://jobs.lever.co/example/role",
        [
            {
                "id": "candidate-email",
                "label": "Email address",
                "type": "email",
                "required": True,
                "value": "private@example.com",
                "current_value": "private@example.com",
                "placeholder": "name@example.com",
            },
            {
                "name": "resume",
                "label": "Resume / CV",
                "type": "file",
                "files": ["C:/private/resume.pdf"],
            },
        ],
    )

    assert form.adapter == "lever"
    assert form.site == "jobs.lever.co"
    assert [field.semantic for field in form.fields] == ["email", "resume"]
    assert form.fields[1].control == "file"
    rendered = repr(form)
    assert "private@example.com" not in rendered
    assert "C:/private/resume.pdf" not in rendered


def test_semantic_fill_plan_only_proposes_source_references() -> None:
    form = build_form_ir(
        "https://jobs.ashbyhq.com/example/role",
        [
            {"id": "email", "label": "Email", "required": True},
            {"id": "cv", "label": "Resume", "type": "file", "required": True},
            {"id": "gender", "label": "Gender", "type": "select"},
            {"id": "unknown", "label": "Why us?", "required": True},
        ],
    )
    plan = propose_fill_plan(form, {"email", "resume", "gender"})

    assert [(item.semantic, item.action) for item in plan.actions] == [
        ("email", "fill"),
        ("resume", "upload"),
        ("gender", "review"),
        ("unknown", "request_fact"),
    ]
    assert plan.actions[0].source_key == "email"
    assert plan.actions[2].source_key is None
    assert not any(hasattr(item, "value") for item in plan.actions)


def test_prompt_guidance_and_context_are_bounded_json_safe_and_value_free() -> None:
    form = build_form_ir(
        "https://boards.greenhouse.io/example/jobs/1",
        [{"id": f"field-{index}", "label": "Email" if index == 0 else "Custom question"} for index in range(100)],
    )
    plan = propose_fill_plan(form, {"email"})
    context = adapter_prompt_context(form, plan)

    assert context["adapter"] == "greenhouse"
    assert context["field_count"] == 100
    assert context["truncated"] is True
    assert len(context["fields"]) == 80
    assert len(context["actions"]) == 80
    assert "value" not in json.dumps(context)
    assert json.loads(json.dumps(context)) == context
    assert any("Greenhouse" in item for item in adapter_prompt_guidance("https://boards.greenhouse.io/x"))


def test_prompt_context_exposes_bounded_option_text_for_answer_resolution() -> None:
    options = [f"Option {index} with a deliberately long suffix {'x' * 100}" for index in range(30)]
    form = build_form_ir(
        "https://jobs.example.test/apply",
        [{"id": "degree", "label": "Degree", "type": "select", "options": options}],
    )

    field = adapter_prompt_context(form)["fields"][0]

    assert field["option_count"] == 30
    assert len(field["options"]) == 20
    assert max(map(len, field["options"])) == 80
    assert field["options_truncated"] is True


@pytest.mark.parametrize(
    ("raw", "semantic"),
    [
        ({"label": "Account password", "type": "password"}, "password"),
        ({"label": "One-time OTP security code"}, "verification_code"),
        ({"label": "NRIC / FIN identification number"}, "identity_number"),
    ],
)
def test_sensitive_answer_fields_are_classified_before_planning(
    raw: dict[str, object], semantic: str
) -> None:
    form = build_form_ir("https://jobs.example.test/apply", [raw])
    plan = propose_fill_plan(form, {semantic})

    assert form.fields[0].semantic == semantic
    assert plan.actions[0].action == "review"
    assert plan.actions[0].source_key is None


def test_default_registry_is_open_and_does_not_encode_tenants_or_versions() -> None:
    registry = default_ats_registry()
    assert registry.names() == [
        "greenhouse",
        "lever",
        "ashby",
        "smartrecruiters",
        "workday",
        "generic",
    ]
    assert registry.detect("https://jobs.lever.co/any-tenant/1").name == "lever"
    assert (
        registry.detect("https://jobs.smartrecruiters.com/any-tenant/1").name
        == "smartrecruiters"
    )
    assert registry.detect("https://example.wd5.myworkdayjobs.com/job/1").name == "workday"


def test_smartrecruiters_guidance_targets_required_resume_not_autocomplete_upload() -> None:
    guidance = adapter_prompt_guidance(
        "https://jobs.smartrecruiters.com/example/role"
    )

    assert any("Easy Apply autocomplete" in item for item in guidance)
    assert any("required Resume" in item for item in guidance)


def test_observer_integrates_workday_signature_and_bounded_repair() -> None:
    snapshot = {
        "url": "https://example.wd5.myworkdayjobs.com/job/1/apply",
        "form_fields": [
            {"id": "email", "label": "Email", "type": "email", "required": True}
        ],
        "workday_observation": {
            "page_kind": "review",
            "visible_controls": ["email"],
            "required_count": 1,
            "invalid_count": 0,
            "has_submit": True,
        },
    }
    ats_context, first, issues = _adapter_observation_context(snapshot, {})

    assert ats_context["adapter"] == "workday"
    assert first is not None
    assert first["state"] == "review"
    assert first["action"] == "continue"
    assert issues == []

    _, repair, issues = _adapter_observation_context(
        snapshot,
        {"_browser_observation": {"workday_state": first}},
    )
    assert repair is not None
    assert repair["action"] == "repair_once"
    assert repair["repair_used"] is True
    assert issues == []

    _, stopped, issues = _adapter_observation_context(
        snapshot,
        {"_browser_observation": {"workday_state": repair}},
    )
    assert stopped is not None
    assert stopped["action"] == "stop_stuck"
    assert issues == ["workday_stuck"]
