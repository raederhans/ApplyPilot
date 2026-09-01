from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from itertools import permutations

import pytest

from applypilot.apply.answer_policy import (
    SafeDefaultRegistry,
    SafeDefaultRule,
    context_binding,
    field_risk,
)
from applypilot.apply.answer_resolution import AnswerRequest, resolve_answer
from applypilot.apply.application_facts import (
    ApplicationFact,
    FactResolution,
    resolve_application_fact,
)


def _fact(key: str, value: object):  # type: ignore[no-untyped-def]
    return resolve_application_fact(
        (
            ApplicationFact(
                f"profile:{key}",
                key,
                value,
                source="profile.json",
                scope="test",
                confirmed_at=datetime(2026, 8, 31, tzinfo=UTC),
                expires_at=datetime(2027, 8, 31, tzinfo=UTC),
                sensitivity="medium",
            ),
        ),
        key=key,
        scope="test",
    )


def test_core_risk_cannot_be_lowered_by_adapter() -> None:
    assert field_risk("Citizenship declaration", adapter_risk="low") == "high"
    assert field_risk("Degree category", adapter_risk="low") == "medium"
    assert field_risk("Preferred team", adapter_risk="medium") == "medium"


def test_lower_sensitivity_fact_cannot_answer_high_risk_field() -> None:
    result = resolve_answer(
        AnswerRequest(
            field_semantic="Citizenship declaration",
            options=("Citizen", "Non-citizen"),
            fact_resolution=_fact("citizenship", "Non-citizen"),
            required=True,
            declaration=True,
        )
    )
    assert result.selected_option is None
    assert result.action == "review"


def test_time_sensitive_semantic_rejects_typed_fact_without_expiry() -> None:
    unbounded = resolve_application_fact(
        (
            ApplicationFact(
                "profile:generic-window",
                "generic_window",
                {"start": "2026-11-10", "end": "2027-06-30"},
                source="profile.json",
                scope="test",
                confirmed_at=datetime(2026, 8, 31, tzinfo=UTC),
                sensitivity="high",
            ),
        ),
        key="generic_window",
        scope="test",
    )
    assert unbounded.production_ready
    result = resolve_answer(
        AnswerRequest(
            field_semantic="Available November 2026 to June 2027?",
            options=("Yes", "No"),
            fact_resolution=unbounded,
            required=True,
            direct_impact=True,
        )
    )
    assert result.selected_option is None


def test_safe_default_requires_registration_and_exact_binding() -> None:
    context = {"job_id": "1", "field_id": "source"}
    raw = SafeDefaultRule(
        "greenhouse-source-v1",
        "greenhouse",
        "v1",
        "How did you hear about us?",
        context_binding(context),
        "Company website",
    )
    registry = SafeDefaultRegistry()
    with pytest.raises(TypeError):
        SafeDefaultRule(  # type: ignore[call-arg]
            "forged",
            "greenhouse",
            "v1",
            "How did you hear about us?",
            context_binding(context),
            "Company website",
            _registered=True,
        )
    registry.register(raw)
    rule = registry.get(raw.rule_id)
    assert rule is not None

    request = AnswerRequest(
        field_semantic="How did you hear about us?",
        options=("Referral", "Company website"),
        required=True,
        adapter="greenhouse",
        adapter_version="v1",
        context=context,
        safe_default_rule=rule,
    )
    result = resolve_answer(request)
    assert result.selected_option == "Company website"
    assert result.audit["safe_default_rule_id"] == raw.rule_id

    assert resolve_answer(replace(request, adapter_version="v2")).selected_option is None
    assert resolve_answer(
        replace(request, safe_default_rule=replace(rule, value="Referral"))
    ).selected_option is None
    assert resolve_answer(
        replace(request, safe_default_rule=replace(rule, context_digest="forged"))
    ).selected_option is None


def test_generic_adapter_and_medium_risk_have_zero_safe_defaults() -> None:
    context = {"field_id": "degree"}
    raw = SafeDefaultRule(
        "generic-degree",
        "generic",
        "v1",
        "Degree category",
        context_binding(context),
        "Master",
    )
    registry = SafeDefaultRegistry()
    try:
        registry.register(raw)
    except ValueError:
        pass
    else:
        raise AssertionError("generic safe default was accepted")


def test_legacy_medium_fact_cannot_auto_select_and_typed_fact_has_provenance() -> None:
    legacy = resolve_answer(
        AnswerRequest(
            field_semantic="Degree category",
            options=("Bachelor", "Master"),
            confirmed_fact="Master",
            required=True,
        )
    )
    assert legacy.action == "research_then_select"
    assert legacy.selected_option is None

    typed = resolve_answer(
        AnswerRequest(
            field_semantic="Degree category",
            options=("Bachelor", "Master"),
            fact_resolution=_fact("degree", "Master"),
            required=True,
        )
    )
    assert typed.selected_option == "Master"
    assert typed.audit["fact_ref"] == "profile:degree"
    assert "confirmed_fact" not in typed.audit

    forged = resolve_answer(
        AnswerRequest(
            field_semantic="Degree category",
            options=("Bachelor", "Master"),
            fact_resolution=FactResolution(
                "resolved", "degree", "Master", "profile:forged", "medium"
            ),
            required=True,
        )
    )
    assert forged.selected_option is None


def test_option_permutation_does_not_change_resolution_and_preference_has_no_first_choice() -> None:
    selected = {
        resolve_answer(
            AnswerRequest(
                field_semantic="Years of experience",
                options=tuple(options),
                fact_resolution=_fact("years", 6),
                required=True,
            )
        ).selected_option
        for options in permutations(("0 years", "5+", "1-2 years"))
    }
    assert selected == {"5+"}

    preference = resolve_answer(
        AnswerRequest(
            field_semantic="Preferred office",
            options=("London", "Singapore"),
            confirmed_fact="Tokyo",
            preference=True,
            required=True,
        )
    )
    assert preference.selected_option is None
    assert preference.action == "review"
