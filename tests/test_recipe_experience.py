from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from applypilot.apply.recipe_experience import (
    RecipeExperienceStore,
    RecipeExperienceTemplate,
    RoutineControlTemplate,
)


def _template(**changes: object) -> RecipeExperienceTemplate:
    base = RecipeExperienceTemplate(
        provider="greenhouse",
        adapter_version="greenhouse-semantic-recipe/v1",
        policy_version="routine-provider-recipe/v1",
        controls=(
            RoutineControlTemplate("email", "text", True, True, 0),
            RoutineControlTemplate(
                "country",
                "native_select",
                False,
                True,
                12,
                hashlib.sha256(b"public-control-shape").hexdigest(),
            ),
        ),
    )
    return replace(base, **changes)


def _validate(store: RecipeExperienceStore, template: RecipeExperienceTemplate) -> None:
    store.observe(template, event_id="observation-1")
    store.record_validation(template, event_id="validation-1", evidence="host_structure")


def test_restart_reuses_only_host_validated_exact_structure(tmp_path: Path) -> None:
    path = tmp_path / "experience.db"
    template = _template()
    _validate(RecipeExperienceStore(path), template)

    restarted = RecipeExperienceStore(path)
    assert restarted.lookup(
        template,
        adapter_version=template.adapter_version,
        policy_version=template.policy_version,
        validate_fresh=lambda candidate: candidate == template,
    ) == template


def test_exact_version_and_ordered_shape_are_isolated(tmp_path: Path) -> None:
    store = RecipeExperienceStore(tmp_path / "experience.db")
    template = _template()
    _validate(store, template)
    changed_version = replace(template, adapter_version="greenhouse-semantic-recipe/v2")
    changed_order = replace(template, controls=tuple(reversed(template.controls)))

    assert store.get(changed_version) is None
    assert store.get(changed_order) is None
    assert store.lookup(
        template,
        adapter_version="greenhouse-semantic-recipe/v2",
        policy_version=template.policy_version,
        validate_fresh=lambda _candidate: True,
    ) is None


def test_store_contains_no_values_pii_or_authority_and_event_ids_are_hashed(tmp_path: Path) -> None:
    path = tmp_path / "experience.db"
    store = RecipeExperienceStore(path)
    template = _template()
    secret_event = "job-934-private@example.test-lease-secret-Submit-authority"
    store.observe(template, event_id=secret_event)
    store.record_validation(template, event_id="host-proof-secret", evidence="host_postcondition")

    persisted = path.read_bytes()
    for forbidden in (
        b"private@example.test",
        b"lease-secret",
        b"Submit-authority",
        b"host-proof-secret",
        b"label",
        b"url",
        b"path",
        b"value",
    ):
        assert forbidden not in persisted
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(recipe_experiences)")}
    assert columns == {
        "content_key",
        "template_json",
        "state",
        "observation_count",
        "validation_count",
        "invalidation_count",
        "touched_ns",
    }


def test_events_are_idempotent_and_observations_never_validate(tmp_path: Path) -> None:
    store = RecipeExperienceStore(tmp_path / "experience.db")
    template = _template()
    first = store.observe(template, event_id="same-event")
    replay = store.observe(template, event_id="same-event")

    assert first.state == replay.state == "candidate"
    assert replay.observation_count == 1
    validated = store.record_validation(template, event_id="proof", evidence="host_structure")
    repeated = store.record_validation(template, event_id="proof", evidence="host_structure")
    assert repeated.state == "validated"
    assert repeated.validation_count == validated.validation_count == 1


def test_records_and_event_evidence_are_bounded(tmp_path: Path) -> None:
    path = tmp_path / "experience.db"
    store = RecipeExperienceStore(path, capacity=2)
    templates = [
        _template(),
        replace(_template(), adapter_version="greenhouse-semantic-recipe/v2"),
        replace(_template(), adapter_version="greenhouse-semantic-recipe/v3"),
    ]
    for template in templates:
        store.observe(template, event_id=f"observe-{template.adapter_version}")
    for number in range(100):
        store.observe(templates[-1], event_id=f"bounded-{number}")

    with sqlite3.connect(path) as connection:
        experience_count = connection.execute("SELECT COUNT(*) FROM recipe_experiences").fetchone()[0]
        event_count = connection.execute(
            "SELECT COUNT(*) FROM recipe_experience_events WHERE content_key=?", (templates[-1].content_key,)
        ).fetchone()[0]
    assert experience_count == 2
    assert event_count == 64
    assert store.get(templates[-1]).observation_count == 64  # type: ignore[union-attr]
    saturated_validation = store.record_validation(
        templates[-1], event_id="proof-after-saturation", evidence="host_structure"
    )
    saturated_invalidation = store.invalidate(templates[-1], event_id="failure-after-saturation")
    repeated_invalidation = store.invalidate(templates[-1], event_id="failure-after-saturation")
    assert saturated_validation.state == "validated"
    assert saturated_invalidation.state == repeated_invalidation.state == "invalidated"
    assert saturated_invalidation.invalidation_count == repeated_invalidation.invalidation_count == 1


def test_failed_fresh_validation_is_sticky_and_tainted_lookup_misses(tmp_path: Path) -> None:
    path = tmp_path / "experience.db"
    template = _template()
    store = RecipeExperienceStore(path)
    _validate(store, template)

    assert store.lookup(
        template,
        adapter_version=template.adapter_version,
        policy_version=template.policy_version,
        validate_fresh=lambda _candidate: False,
    ) is None
    assert store.get(template).state == "invalidated"  # type: ignore[union-attr]
    store.observe(template, event_id="later-observation")
    store.record_validation(template, event_id="later-proof", evidence="host_postcondition")
    assert RecipeExperienceStore(path).get(template).state == "invalidated"  # type: ignore[union-attr]
    assert store.lookup(
        template,
        adapter_version=template.adapter_version,
        policy_version=template.policy_version,
        validate_fresh=lambda _candidate: True,
        tainted=True,
    ) is None


def test_malformed_templates_and_non_host_evidence_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        RoutineControlTemplate("cover_letter", "text", False, True, 0)
    with pytest.raises(ValueError):
        RoutineControlTemplate("email", "textarea", True, True, 0)
    with pytest.raises(ValueError):
        replace(_template(), adapter_version="lever-semantic-recipe/v1")
    store = RecipeExperienceStore(tmp_path / "experience.db")
    with pytest.raises(ValueError):
        store.record_validation(_template(), event_id="proof", evidence="browser_write")  # type: ignore[arg-type]


def test_missing_store_and_table_reads_create_nothing(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "experience.db"
    assert RecipeExperienceStore(missing).get(_template()) is None
    assert not missing.exists()
    assert not missing.parent.exists()

    unrelated = tmp_path / "unrelated.db"
    with sqlite3.connect(unrelated) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")
    before = unrelated.read_bytes()
    assert RecipeExperienceStore(unrelated).get(_template()) is None
    assert unrelated.read_bytes() == before
