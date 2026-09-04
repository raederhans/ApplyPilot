from __future__ import annotations

from copy import deepcopy

import pytest

from applypilot.apply.browser_authority import BrowserAuthorityHandle
from applypilot.apply.browser_broker import BrowserBroker
from applypilot.apply.contracts import application_actor_id
from applypilot.apply.provider_recipe_shadow import (
    ProviderRecipeShadowObserver,
    observe_prepare_recipe_shadow,
)


def _snapshot(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "form_fields": [
            {
                "field_key": "email",
                "label": "Email address",
                "control": "email",
                "required": True,
                "disabled": False,
                "readonly": False,
                "autocomplete": "email",
                "placeholder": "",
                "protected_identifier": False,
                "options": [],
                "option_count": 0,
                "options_truncated": False,
            },
            {
                "field_key": "submit",
                "label": "Submit application",
                "control": "submit",
                "required": False,
                "disabled": False,
                "readonly": False,
                "autocomplete": "",
                "placeholder": "",
                "protected_identifier": False,
                "options": [],
                "option_count": 0,
                "options_truncated": False,
            },
        ],
        "submit_control_count": 1,
        "captcha_visible": False,
        "assessment_visible": False,
        "verification_visible": False,
        "resume_field_present": False,
        "file_fields": [],
        "sensitive_required_unknown": [],
    }
    value.update(changes)
    return value


@pytest.mark.parametrize(
    ("provider", "url"),
    [
        ("greenhouse", "https://boards.greenhouse.io/acme/jobs/123"),
        (
            "workday",
            "https://acme.wd5.myworkdayjobs.com/en-US/jobs/job/Engineer_R123/apply",
        ),
        (
            "smartrecruiters",
            "https://jobs.smartrecruiters.com/Acme/744000012345678-engineer",
        ),
    ],
)
def test_each_provider_shadow_switch_is_independent_and_always_falls_back(
    provider: str,
    url: str,
) -> None:
    observer = ProviderRecipeShadowObserver()
    off = observer.observe(
        enabled_providers=(),
        application_target_url=url,
        page_url=url,
        surface_url=url,
        surface_is_main_frame=True,
        snapshot=_snapshot(),
        page_epoch=1,
        page_lease_id="lease-1",
        browser_generation=1,
    )
    miss = observer.observe(
        enabled_providers=(provider,),
        application_target_url=url,
        page_url=url,
        surface_url=url,
        surface_is_main_frame=True,
        snapshot=_snapshot(),
        page_epoch=1,
        page_lease_id="lease-1",
        browser_generation=1,
    )
    hit = observer.observe(
        enabled_providers=(provider,),
        application_target_url=url,
        page_url=url,
        surface_url=url,
        surface_is_main_frame=True,
        snapshot=_snapshot(),
        page_epoch=2,
        page_lease_id="lease-2",
        browser_generation=2,
    )

    assert off.outcome == "off"
    assert miss.outcome == "miss"
    assert hit.outcome == "hit"
    assert all(item.agent_fallback_required for item in (off, miss, hit))
    assert hit.as_dict()["browser_write_authority"] is False
    assert hit.as_dict()["file_upload_authority"] is False
    assert hit.as_dict()["submit_authority"] is False
    assert hit.as_dict()["throughput_admission_evidence"] is False


@pytest.mark.parametrize(
    ("snapshot_changes", "surface_url", "surface_is_main_frame", "reason"),
    [
        ({"captcha_visible": True}, None, True, "observation_not_recipe_safe"),
        ({"assessment_visible": True}, None, True, "observation_not_recipe_safe"),
        ({"verification_visible": True}, None, True, "observation_not_recipe_safe"),
        ({"resume_field_present": True}, None, True, "observation_not_recipe_safe"),
        ({"sensitive_required_unknown": ["Visa"]}, None, True, "observation_not_recipe_safe"),
        ({}, "https://forms.example.test/apply", True, "cross_origin_surface_not_admitted"),
        ({}, None, False, "framed_surface_not_admitted"),
    ],
)
def test_shadow_observation_fail_closes_manual_and_surface_boundaries(
    snapshot_changes: dict[str, object],
    surface_url: str | None,
    surface_is_main_frame: bool,
    reason: str,
) -> None:
    url = "https://boards.greenhouse.io/acme/jobs/123"
    decision = ProviderRecipeShadowObserver().observe(
        enabled_providers=("greenhouse",),
        application_target_url=url,
        page_url=url,
        surface_url=surface_url or url,
        surface_is_main_frame=surface_is_main_frame,
        snapshot=_snapshot(**snapshot_changes),
        page_epoch=1,
        page_lease_id="lease-1",
        browser_generation=1,
    )

    assert decision.outcome == "denied"
    assert decision.reason_code == reason
    assert decision.agent_fallback_required is True


@pytest.mark.parametrize("control", ["textarea", "combobox", "file", "checkbox", "radio", "date"])
def test_complex_and_file_controls_deny_the_entire_shadow_recipe(control: str) -> None:
    snapshot = _snapshot()
    fields = deepcopy(snapshot["form_fields"])
    assert isinstance(fields, list)
    fields.append(
        {
            "field_key": "unsafe",
            "label": "Additional information",
            "control": control,
            "required": False,
            "disabled": False,
            "readonly": False,
            "options": [],
            "option_count": 0,
            "options_truncated": False,
        }
    )
    snapshot["form_fields"] = fields
    url = "https://boards.greenhouse.io/acme/jobs/123"

    decision = ProviderRecipeShadowObserver().observe(
        enabled_providers=("greenhouse",),
        application_target_url=url,
        page_url=url,
        surface_url=url,
        surface_is_main_frame=True,
        snapshot=snapshot,
        page_epoch=1,
        page_lease_id="lease-1",
        browser_generation=1,
    )

    assert decision.outcome == "denied"
    assert decision.agent_fallback_required is True


def test_shadow_telemetry_never_records_structural_or_candidate_values() -> None:
    url = "https://boards.greenhouse.io/acme/jobs/123"
    snapshot = _snapshot()
    fields = deepcopy(snapshot["form_fields"])
    assert isinstance(fields, list)
    assert isinstance(fields[0], dict)
    fields[0]["label"] = "Candidate private@example.test email"
    snapshot["form_fields"] = fields

    payload = (
        ProviderRecipeShadowObserver()
        .observe(
            enabled_providers=("greenhouse",),
            application_target_url=url,
            page_url=url,
            surface_url=url,
            surface_is_main_frame=True,
            snapshot=snapshot,
            page_epoch=1,
            page_lease_id="lease-secret",
            browser_generation=1,
        )
        .as_dict()
    )

    assert "private@example.test" not in repr(payload)
    assert "lease-secret" not in repr(payload)
    assert url not in repr(payload)


def test_default_off_production_observation_does_not_parse_browser_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://boards.greenhouse.io/acme/jobs/123"

    def unexpected_rebuild(_job: object) -> None:
        raise AssertionError("disabled shadow observation must not parse authority")

    monkeypatch.setattr(
        "applypilot.apply.provider_recipe_shadow.BrowserAuthorityHandle.rebuild",
        unexpected_rebuild,
    )
    telemetry = observe_prepare_recipe_shadow(
        job={"application_url": url},
        page_url=url,
        surface_url=url,
        surface_is_main_frame=True,
        snapshot=_snapshot(),
        enabled_providers=(),
    )

    assert telemetry.outcome == "off"
    assert telemetry.agent_fallback_required is True


def test_enabled_production_observation_denies_without_fresh_browser_authority() -> None:
    url = "https://boards.greenhouse.io/acme/jobs/123"
    telemetry = observe_prepare_recipe_shadow(
        job={"application_url": url},
        page_url=url,
        surface_url=url,
        surface_is_main_frame=True,
        snapshot=_snapshot(),
        enabled_providers=("greenhouse",),
    )

    assert telemetry.outcome == "denied"
    assert telemetry.reason_code == "fresh_browser_authority_unavailable"


def test_enabled_production_observation_uses_current_read_only_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://boards.greenhouse.io/acme/jobs/987"
    attempt_id = "recipe-shadow-attempt"
    job: dict[str, object] = {"_attempt_id": attempt_id, "application_url": url}
    broker = BrowserBroker()
    handle = BrowserAuthorityHandle.create(
        job,
        broker=broker,
        browser_generation=3,
        application_session_id="recipe-shadow-session",
        actor_id=application_actor_id(attempt_id),
        attempt_id=attempt_id,
    )
    handle.acquire_or_continue(
        profile_id="edge:worker:shadow",
        page_id="application:recipe-shadow-attempt",
        scope_id="worker:shadow",
        runtime_id="test:edge:cdp:0",
        submit_started=False,
        resume_existing_page=False,
    )
    monkeypatch.setattr(
        "applypilot.apply.provider_recipe_shadow._PRODUCTION_SHADOW_OBSERVER",
        ProviderRecipeShadowObserver(),
    )
    telemetry = observe_prepare_recipe_shadow(
        job=job,
        page_url=url,
        surface_url=url,
        surface_is_main_frame=True,
        snapshot=_snapshot(),
        enabled_providers=("greenhouse",),
    )

    assert telemetry.outcome == "miss"
    assert telemetry.admission_enabled is True
    assert telemetry.agent_fallback_required is True
