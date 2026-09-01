"""Versioned, redacted accuracy corpus for deterministic answer resolution."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from itertools import permutations
from pathlib import Path
from typing import Any

import pytest

from applypilot.apply.answer_policy import SafeDefaultRegistry, SafeDefaultRule, context_binding, field_risk
from applypilot.apply.answer_resolution import AnswerRequest, AnswerResolution, resolve_answer
from applypilot.apply.application_facts import FactResolution, current_profile_facts, resolve_application_fact
from applypilot.apply.contracts import FailureObservation
from applypilot.apply.failure_taxonomy import classify_failure_observation

_CORPUS_PATH = Path(__file__).parent / "fixtures" / "apply" / "accuracy" / "answer-resolution-golden-v1.json"
_NOW = datetime(2026, 9, 1, tzinfo=UTC)
_CASE_KEYS = frozenset(
    {
        "case_id",
        "provider",
        "adapter_version",
        "page_type",
        "semantic",
        "type",
        "required",
        "visible_options",
        "synthetic_fact_ref",
        "scope",
        "freshness",
        "fact",
        "request",
        "safe_default",
        "expected",
    }
)
_FACT_KEYS = frozenset({"key", "entries"})
_FACT_ENTRY_KEYS = frozenset({"fact_ref", "value", "sensitivity", "scope", "expires_at"})
_REQUEST_KEYS = frozenset({"direct_impact", "declaration", "preference", "adapter_risk", "context"})
_DEFAULT_KEYS = frozenset({"rule_id", "adapter", "adapter_version", "value"})
_EXPECTED_KEYS = frozenset({"fact_status", "risk", "action", "selected_option", "provenance", "failure_code"})
_OPTION_CONTROL_TYPES = frozenset({"native_select", "combobox"})
_AUTOMATIC_ACTIONS = frozenset({"select", "select_and_record", "answer_negative_and_continue", "enter_value"})
_BANNED_KEY = re.compile(
    r"(?:url|screenshot|dom|raw_value|email|phone|name|identity|financial|legal_answer)", re.IGNORECASE
)
_BANNED_VALUE = re.compile(
    r"(?:https?://|www\\.|[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}|(?:\\+?\\d[ -]?){8,}|"
    r"(?<![A-Z0-9])[A-Z]\\d{7}[A-Z](?![A-Z0-9])|passport|nric|social security|bank account)",
    re.IGNORECASE,
)


def _load_corpus() -> dict[str, Any]:
    return json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))


def _redacted(value: object, *, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            assert not _BANNED_KEY.search(str(key)), f"forbidden corpus key at {path}.{key}"
            _redacted(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _redacted(child, path=f"{path}[{index}]")
    elif isinstance(value, str):
        assert not _BANNED_VALUE.search(value), f"forbidden corpus value at {path}"


def _assert_schema(corpus: dict[str, Any]) -> None:
    assert set(corpus) == {"schema_version", "cases"}
    assert corpus["schema_version"] == "applypilot-answer-accuracy-golden/v1"
    assert isinstance(corpus["cases"], list) and corpus["cases"]
    case_ids: set[str] = set()
    for case in corpus["cases"]:
        assert isinstance(case, dict) and set(case) == _CASE_KEYS
        assert case["case_id"] not in case_ids
        case_ids.add(case["case_id"])
        assert case["provider"] in {"workday", "smartrecruiters", "generic"}
        assert isinstance(case["adapter_version"], str) and case["adapter_version"]
        assert isinstance(case["page_type"], str) and case["page_type"]
        assert isinstance(case["semantic"], str) and case["semantic"]
        assert case["type"] in {*_OPTION_CONTROL_TYPES, "custom_text"}
        assert isinstance(case["required"], bool)
        assert isinstance(case["visible_options"], list)
        assert all(isinstance(option, str) and option for option in case["visible_options"])
        if case["type"] == "custom_text":
            assert not case["visible_options"]
        else:
            assert case["visible_options"]
        assert isinstance(case["synthetic_fact_ref"], str) and case["synthetic_fact_ref"].startswith("fact:")
        assert isinstance(case["scope"], str) and case["scope"]
        assert case["freshness"] in {"current", "missing", "expired", "out_of_scope", "conflict"}
        fact = case["fact"]
        assert isinstance(fact, dict) and set(fact) == _FACT_KEYS
        assert isinstance(fact["key"], str) and fact["key"]
        assert isinstance(fact["entries"], list)
        entry_refs = {entry["fact_ref"] for entry in fact["entries"] if isinstance(entry, dict)}
        if case["freshness"] == "missing":
            assert not fact["entries"]
        else:
            assert case["synthetic_fact_ref"] in entry_refs
        for entry in fact["entries"]:
            assert isinstance(entry, dict) and set(entry) == _FACT_ENTRY_KEYS
            assert entry["fact_ref"].startswith("fact:")
            assert entry["sensitivity"] in {"low", "medium", "high"}
            assert isinstance(entry["scope"], str) and entry["scope"]
            assert entry["expires_at"] is None or isinstance(entry["expires_at"], str)
        if case["freshness"] == "current":
            assert len(fact["entries"]) == 1
            assert fact["entries"][0]["scope"] == case["scope"]
            assert fact["entries"][0]["expires_at"] is None
        if case["freshness"] == "expired":
            assert len(fact["entries"]) == 1 and fact["entries"][0]["expires_at"] is not None
        if case["freshness"] == "out_of_scope":
            assert fact["entries"] and all(entry["scope"] != case["scope"] for entry in fact["entries"])
        if case["freshness"] == "conflict":
            assert len(fact["entries"]) >= 2 and all(entry["scope"] == case["scope"] for entry in fact["entries"])
        request = case["request"]
        assert isinstance(request, dict) and set(request) == _REQUEST_KEYS
        assert all(isinstance(request[key], bool) for key in ("direct_impact", "declaration", "preference"))
        assert request["adapter_risk"] in {None, "low", "medium", "high"}
        assert isinstance(request["context"], dict)
        safe_default = case["safe_default"]
        assert safe_default is None or (isinstance(safe_default, dict) and set(safe_default) == _DEFAULT_KEYS)
        expected = case["expected"]
        assert isinstance(expected, dict) and set(expected) == _EXPECTED_KEYS
        assert expected["fact_status"] in {"resolved", "missing", "expired", "out_of_scope", "conflict"}
        assert expected["risk"] in {"low", "medium", "high"}
        assert isinstance(expected["action"], str)
        assert expected["selected_option"] is None or expected["selected_option"] in case["visible_options"]
        assert expected["provenance"] == "none" or expected["provenance"].startswith(("fact:", "safe_default:"))
        assert expected["failure_code"] is None or expected["failure_code"] in {
            "answer_policy_unresolved",
            "unsupported_legal_declaration",
        }


def _fact_resolution(case: dict[str, Any]) -> FactResolution:
    facts = current_profile_facts(
        {
            "application_facts": [
                {
                    "fact_ref": entry["fact_ref"],
                    "key": case["fact"]["key"],
                    "value": entry["value"],
                    "source": "synthetic-corpus",
                    "scope": entry["scope"],
                    "confirmed_at": _NOW.isoformat(),
                    "expires_at": entry["expires_at"],
                    "sensitivity": entry["sensitivity"],
                }
                for entry in case["fact"]["entries"]
            ]
        }
    )
    return resolve_application_fact(facts, key=case["fact"]["key"], scope=case["scope"], at=_NOW)


def _safe_default(case: dict[str, Any]) -> SafeDefaultRule | None:
    spec = case["safe_default"]
    if spec is None:
        return None
    registry = SafeDefaultRegistry()
    raw = SafeDefaultRule(
        spec["rule_id"],
        spec["adapter"],
        spec["adapter_version"],
        case["semantic"],
        context_binding(case["request"]["context"]),
        spec["value"],
    )
    registry.register(raw)
    return registry.get(raw.rule_id)


def _resolve(case: dict[str, Any], options: tuple[str, ...] | None = None) -> AnswerResolution:
    request = case["request"]
    return resolve_answer(
        AnswerRequest(
            field_semantic=case["semantic"],
            options=tuple(case["visible_options"] if options is None else options),
            fact_resolution=_fact_resolution(case),
            required=case["required"],
            direct_impact=request["direct_impact"],
            declaration=request["declaration"],
            preference=request["preference"],
            context=request["context"],
            adapter=case["provider"],
            adapter_version=case["adapter_version"],
            adapter_risk=request["adapter_risk"],
            safe_default_rule=_safe_default(case),
        )
    )


def _provenance(result: AnswerResolution) -> str:
    if result.audit.get("safe_default_rule_id"):
        return f"safe_default:{result.audit['safe_default_rule_id']}"
    return str(result.audit.get("fact_ref") or "none")


def _enter_value_has_sealed_fact(case: dict[str, Any], result: AnswerResolution) -> bool:
    if result.action != "enter_value":
        return True
    fact = _fact_resolution(case)
    return fact.production_ready and result.value == fact.value and result.audit.get("safe_default_rule_id") is None


def _assert_expected(case: dict[str, Any], result: AnswerResolution) -> None:
    expected = case["expected"]
    assert _fact_resolution(case).status == expected["fact_status"], case["case_id"]
    assert (
        field_risk(
            case["semantic"],
            adapter_risk=case["request"]["adapter_risk"],
            direct_impact=case["request"]["direct_impact"],
            declaration=case["request"]["declaration"],
        )
        == expected["risk"]
    ), case["case_id"]
    assert result.action == expected["action"], case["case_id"]
    assert result.selected_option == expected["selected_option"], case["case_id"]
    assert _provenance(result) == expected["provenance"], case["case_id"]
    assert result.audit.get("failure_code") == expected["failure_code"], case["case_id"]
    assert _enter_value_has_sealed_fact(case, result), case["case_id"]


def test_accuracy_golden_corpus_is_versioned_strict_and_redacted() -> None:
    corpus = _load_corpus()
    _assert_schema(corpus)
    _redacted(corpus)


def test_accuracy_golden_corpus_covers_required_provider_and_failure_surfaces() -> None:
    cases = _load_corpus()["cases"]
    assert {case["provider"] for case in cases} == {"workday", "smartrecruiters", "generic"}
    assert {case["type"] for case in cases} == {"native_select", "combobox", "custom_text"}
    assert {case["freshness"] for case in cases} == {
        "current",
        "missing",
        "expired",
        "out_of_scope",
        "conflict",
    }
    risks_by_freshness = {
        (case["expected"]["risk"], case["freshness"])
        for case in cases
        if case["expected"]["risk"] in {"medium", "high"}
    }
    for risk in ("medium", "high"):
        assert {
            (risk, freshness) for freshness in ("missing", "expired", "out_of_scope", "conflict")
        } <= risks_by_freshness
    assert any(case["safe_default"] is not None and case["expected"]["selected_option"] for case in cases)
    assert any(
        case["provider"] == "generic"
        and case["safe_default"] is not None
        and case["expected"]["selected_option"] is None
        for case in cases
    )
    assert any(case["request"]["preference"] and case["expected"]["selected_option"] is None for case in cases)
    assert any(
        case["expected"]["risk"] == "high" and case["expected"]["failure_code"] == "unsupported_legal_declaration"
        for case in cases
    )


@pytest.mark.parametrize("case", _load_corpus()["cases"], ids=lambda case: case["case_id"])
def test_accuracy_golden_corpus_matches_expected_decisions(case: dict[str, Any]) -> None:
    _assert_expected(case, _resolve(case))


@pytest.mark.parametrize("case", _load_corpus()["cases"], ids=lambda case: case["case_id"])
def test_accuracy_golden_corpus_is_deterministic_for_every_option_permutation(case: dict[str, Any]) -> None:
    expected = _resolve(case)
    fingerprint = (expected.relation, expected.action, expected.selected_option, _provenance(expected))
    permutations_to_check = permutations(case["visible_options"]) if case["type"] in _OPTION_CONTROL_TYPES else ((),)
    for options in permutations_to_check:
        result = _resolve(case, options)
        assert (result.relation, result.action, result.selected_option, _provenance(result)) == fingerprint, case[
            "case_id"
        ]


def test_accuracy_golden_corpus_enforces_transparent_accuracy_and_safety_denominators() -> None:
    cases = _load_corpus()["cases"]
    results = [(case, _resolve(case)) for case in cases]
    for case, result in results:
        _assert_expected(case, result)

    high_unsupported = [
        (case, result)
        for case, result in results
        if case["expected"]["risk"] == "high" and case["expected"]["failure_code"] == "unsupported_legal_declaration"
    ]
    high_unsupported_denominator = len(high_unsupported)
    high_unsupported_numerator = sum(result.action in _AUTOMATIC_ACTIONS for _, result in high_unsupported)
    assert high_unsupported_denominator > 0
    assert high_unsupported_numerator == 0

    known_deterministic = [
        (case, result)
        for case, result in results
        if case["freshness"] == "current" and result.action in _AUTOMATIC_ACTIONS
    ]
    known_deterministic_denominator = len(known_deterministic)
    known_deterministic_numerator = sum(
        result.selected_option == case["expected"]["selected_option"]
        and result.action == case["expected"]["action"]
        and result.audit.get("failure_code") == case["expected"]["failure_code"]
        and _enter_value_has_sealed_fact(case, result)
        for case, result in known_deterministic
    )
    assert known_deterministic_denominator > 0
    assert known_deterministic_numerator / known_deterministic_denominator >= 0.99

    medium_cases = [(case, result) for case, result in results if case["expected"]["risk"] == "medium"]
    medium_denominator = len(medium_cases)
    medium_numerator = sum(
        result.selected_option == case["expected"]["selected_option"]
        and result.action == case["expected"]["action"]
        and result.audit.get("failure_code") == case["expected"]["failure_code"]
        and _enter_value_has_sealed_fact(case, result)
        for case, result in medium_cases
    )
    assert medium_denominator > 0
    assert medium_numerator / medium_denominator >= 0.97

    classified_failures = [
        (
            case,
            classify_failure_observation(
                FailureObservation(
                    code=str(result.audit["failure_code"]),
                    source="policy",
                    provider=case["provider"],
                    phase="prepare",
                    submit_started=False,
                    field_semantic="answer-resolution",
                    evidence_refs=("golden:case",),
                )
            ),
        )
        for case, result in results
        if result.audit.get("failure_code") is not None
    ]
    unclassified_denominator = len(classified_failures)
    unclassified_numerator = sum(
        descriptor.category == "unclassified_application_failure" for _, descriptor in classified_failures
    )
    assert unclassified_denominator > 0
    assert unclassified_numerator / unclassified_denominator < 0.02
    assert unclassified_numerator == 0

    automatic_results = [(case, result) for case, result in results if result.action in _AUTOMATIC_ACTIONS]
    automatic_provenance_denominator = len(automatic_results)
    automatic_provenance_numerator = sum(
        (_fact_resolution(case).production_ready and _provenance(result) == _fact_resolution(case).fact_ref)
        or (
            _safe_default(case) is not None
            and result.audit.get("safe_default_rule_id") == _safe_default(case).rule_id
            and _safe_default(case).matches(
                adapter=case["provider"],
                adapter_version=case["adapter_version"],
                field_semantic=case["semantic"],
                context=case["request"]["context"],
            )
        )
        for case, result in automatic_results
    )
    assert automatic_provenance_denominator > 0
    assert automatic_provenance_numerator == automatic_provenance_denominator
