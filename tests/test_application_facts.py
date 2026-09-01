from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from applypilot.apply.application_facts import (
    FactResolution,
    current_profile_facts,
    resolve_application_fact,
)

NOW = datetime(2026, 9, 1, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "apply" / "application_fact_cases.json"


def _fact(ref: str, value: object, **extra: object) -> dict[str, object]:
    return {
        "fact_ref": ref,
        "key": "answer",
        "value": value,
        "source": "profile.json",
        "scope": "job:example",
        "confirmed_at": "2026-08-31T00:00:00Z",
        "sensitivity": "medium",
        **extra,
    }


def test_current_profile_is_authority_and_false_and_zero_are_preserved() -> None:
    profile = {
        "application_facts": [_fact("false", False), _fact("zero", 0)],
        "application_fact_revisions": [_fact("history", "must-not-be-authority")],
    }
    facts = current_profile_facts(profile)

    assert [fact.value for fact in facts] == [False, 0]
    assert {fact.fact_ref for fact in facts} == {"false", "zero"}


def test_redacted_fact_fixture_preserves_false_and_zero() -> None:
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for case in cases:
        facts = current_profile_facts({"application_facts": case["facts"]})
        resolution = resolve_application_fact(
            facts, key="answer", scope=case["scope"], at=NOW
        )
        assert resolution.status == "resolved", case["name"]
        assert resolution.value == case["expected"], case["name"]


def test_scope_expiry_conflict_and_supersession_are_deterministic() -> None:
    expired = current_profile_facts(
        {"application_facts": [_fact("old", "No", expires_at="2026-08-01T00:00:00Z")]}
    )
    assert resolve_application_fact(expired, key="answer", scope="job:example", at=NOW).status == "expired"
    assert resolve_application_fact(expired, key="answer", scope="job:other", at=NOW).status == "out_of_scope"

    conflict = current_profile_facts(
        {"application_facts": [_fact("a", "Yes"), _fact("b", "No")]}
    )
    assert resolve_application_fact(conflict, key="answer", scope="job:example", at=NOW).status == "conflict"

    superseded = current_profile_facts(
        {
            "application_facts": [
                _fact("a", "Yes"),
                _fact("b", "No", supersedes=["a"], confirmed_at="2026-09-01T00:00:00Z"),
            ]
        }
    )
    resolution = resolve_application_fact(
        superseded, key="answer", scope="job:example", at=NOW
    )
    assert resolution.status == "resolved"
    assert resolution.fact_ref == "b"
    assert resolution.value == "No"


def test_expired_lineage_head_does_not_resurrect_predecessor() -> None:
    facts = current_profile_facts(
        {
            "application_facts": [
                _fact("old", "Yes"),
                _fact(
                    "new",
                    "No",
                    supersedes=["old"],
                    confirmed_at="2026-08-31T12:00:00Z",
                    expires_at="2026-09-01T00:00:00Z",
                ),
            ]
        }
    )
    resolution = resolve_application_fact(
        facts, key="answer", scope="job:example", at=NOW
    )
    assert resolution.status == "expired"
    assert resolution.value is None


def test_disconnected_supersession_cycle_fails_closed_even_with_valid_head() -> None:
    facts = current_profile_facts(
        {
            "application_facts": [
                _fact("a", "No", supersedes=["b"]),
                _fact("b", "No", supersedes=["a"]),
                _fact("c", "No"),
            ]
        }
    )
    resolution = resolve_application_fact(
        facts, key="answer", scope="job:example", at=NOW
    )
    assert resolution.status == "conflict"
    assert resolution.reason == "supersession_cycle"


def test_self_supersession_cycle_fails_closed() -> None:
    facts = current_profile_facts(
        {"application_facts": [_fact("self", "No", supersedes=["self"])]}
    )
    resolution = resolve_application_fact(
        facts, key="answer", scope="job:example", at=NOW
    )
    assert resolution.status == "conflict"
    assert resolution.reason == "supersession_cycle"


@pytest.mark.parametrize(
    "facts",
    [
        [
            _fact("a", "No"),
            _fact("b", "No", supersedes=["a"]),
            _fact("c", "No", supersedes=["b"]),
        ],
        [
            _fact("a", "No"),
            _fact("b", "No"),
            _fact("c", "No", supersedes=["a", "b"]),
        ],
    ],
    ids=("linear", "merge"),
)
def test_acyclic_linear_and_merge_lineages_resolve(facts: list[dict[str, object]]) -> None:
    resolution = resolve_application_fact(
        current_profile_facts({"application_facts": facts}),
        key="answer",
        scope="job:example",
        at=NOW,
    )
    assert resolution.status == "resolved"
    assert resolution.fact_ref == "c"


def test_invalid_expiry_is_rejected_instead_of_becoming_unbounded() -> None:
    with pytest.raises(ValueError, match="expires_at"):
        current_profile_facts(
            {"application_facts": [_fact("bad", "No", expires_at="not-a-time")]}
        )


def test_time_sensitive_medium_fact_requires_expiry() -> None:
    facts = current_profile_facts(
        {
            "application_facts": [
                {
                    **_fact("availability", False),
                    "key": "availability_window",
                }
            ]
        }
    )
    resolution = resolve_application_fact(
        facts, key="availability_window", scope="job:example", at=NOW
    )
    assert resolution.status == "missing"
    assert resolution.reason == "fact_lacks_scope_source_or_freshness"


def test_fact_resolution_seal_is_claim_bound() -> None:
    facts = current_profile_facts({"application_facts": [_fact("sealed", False)]})
    resolution = resolve_application_fact(
        facts, key="answer", scope="job:example", at=NOW
    )
    assert resolution.production_ready
    assert not replace(resolution, value=True).production_ready
    assert not replace(resolution, fact_ref="forged").production_ready
    with pytest.raises(TypeError):
        FactResolution(  # type: ignore[call-arg]
            "resolved", "answer", False, "forged", "medium", _validated=True
        )


def test_medium_fact_without_source_scope_or_freshness_cannot_be_promoted() -> None:
    facts = current_profile_facts(
        {"application_facts": [{"key": "answer", "value": False, "sensitivity": "medium"}]}
    )
    resolution = resolve_application_fact(facts, key="answer", scope="job:example", at=NOW)
    assert resolution.status == "out_of_scope"
    assert resolution.value is None
