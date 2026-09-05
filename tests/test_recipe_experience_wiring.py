from __future__ import annotations

from pathlib import Path

import pytest

from applypilot.apply.provider_recipe_shadow import ProviderRecipeShadowObserver


def _snapshot(**changes: object) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "form_fields": [
            {
                "field_key": "candidate-private@example.test",
                "label": "Email for private@example.test",
                "control": "email",
                "required": True,
                "disabled": False,
                "readonly": False,
                "autocomplete": "email",
                "placeholder": "private@example.test",
                "protected_identifier": False,
                "options": [],
                "option_count": 0,
                "options_truncated": False,
            }
        ],
        "captcha_visible": False,
        "assessment_visible": False,
        "verification_visible": False,
        "resume_field_present": False,
        "file_fields": [],
        "sensitive_required_unknown": [],
    }
    snapshot.update(changes)
    return snapshot


def _observe(observer: ProviderRecipeShadowObserver, snapshot: dict[str, object] | None = None):
    url = "https://boards.greenhouse.io/acme/jobs/123"
    return observer.observe(
        enabled_providers=("greenhouse",),
        application_target_url=url,
        page_url=url,
        surface_url=url,
        surface_is_main_frame=True,
        snapshot=snapshot or _snapshot(),
        page_epoch=1,
        page_lease_id="lease-private",
        browser_generation=1,
    )


def test_validated_public_shape_hits_across_observer_restart_without_pii(tmp_path: Path) -> None:
    path = tmp_path / "recipe-experience.db"
    first = _observe(ProviderRecipeShadowObserver(experience_db_path=path))
    restarted = _observe(ProviderRecipeShadowObserver(experience_db_path=path))

    assert first.persistent_status == "persistent_candidate"
    assert restarted.persistent_status == "persistent_hit"
    assert restarted.persistent_observation_count == 2
    assert restarted.persistent_validation_count == 2
    assert first.agent_fallback_required is restarted.agent_fallback_required is True
    assert first.outcome == restarted.outcome == "miss"
    persisted = path.read_bytes()
    assert b"private@example.test" not in persisted
    assert b"lease-private" not in persisted


@pytest.mark.parametrize(
    ("enabled", "snapshot"),
    [
        ((), _snapshot()),
        (("greenhouse",), _snapshot(captcha_visible=True)),
    ],
)
def test_disabled_or_tainted_shadow_does_not_create_experience_db(
    tmp_path: Path,
    enabled: tuple[str, ...],
    snapshot: dict[str, object],
) -> None:
    path = tmp_path / "missing" / "recipe-experience.db"
    observer = ProviderRecipeShadowObserver(experience_db_path=path)
    url = "https://boards.greenhouse.io/acme/jobs/123"
    decision = observer.observe(
        enabled_providers=enabled,
        application_target_url=url,
        page_url=url,
        surface_url=url,
        surface_is_main_frame=True,
        snapshot=snapshot,
        page_epoch=1,
        page_lease_id="lease-private",
        browser_generation=1,
    )

    assert decision.agent_fallback_required is True
    assert not path.exists()


def test_failed_fresh_check_invalidates_without_changing_shadow_fallback(tmp_path: Path) -> None:
    path = tmp_path / "recipe-experience.db"
    first_observer = ProviderRecipeShadowObserver(experience_db_path=path)
    _observe(first_observer)
    failed = _observe(
        ProviderRecipeShadowObserver(
            experience_db_path=path,
            validate_experience_fresh=lambda _stored, _fresh: False,
        )
    )

    assert failed.persistent_status == "invalidated"
    assert failed.agent_fallback_required is True
    assert failed.as_dict()["browser_write_authority"] is False
    assert failed.as_dict()["submit_authority"] is False


def test_database_failure_degrades_to_existing_process_cache(tmp_path: Path) -> None:
    invalid_db_path = tmp_path / "is-a-directory"
    invalid_db_path.mkdir()
    observer = ProviderRecipeShadowObserver(experience_db_path=invalid_db_path)

    first = _observe(observer)
    second = _observe(observer)

    assert first.persistent_status == second.persistent_status == "degraded"
    assert first.outcome == "miss"
    assert second.outcome == "hit"
    assert second.agent_fallback_required is True
