from __future__ import annotations

from dataclasses import replace

from applypilot.apply.provider_recipe_fast_path import GreenhouseRecipeFastPath
from applypilot.apply.provider_semantic_adapters import (
    GREENHOUSE_ADAPTER_VERSION,
    ProviderControlStructure,
    ProviderPageRecipeObservation,
)
from applypilot.apply.recipe_cache import canonical_digest
from applypilot.apply.semantic_batch import (
    SemanticBatchAdapterRegistration,
    SemanticBatchAdapterRegistry,
)


def _observation(**changes: object) -> ProviderPageRecipeObservation:
    base = ProviderPageRecipeObservation(
        provider="greenhouse",
        application_target_url="https://boards.greenhouse.io/acme/jobs/123",
        page_url="https://boards.greenhouse.io/acme/jobs/123",
        page_signature=canonical_digest("greenhouse-form-v1"),
        page_epoch=1,
        page_lease_id="lease-1",
        browser_generation=1,
        controls=(
            ProviderControlStructure(
                semantic="email",
                kind="text",
                required=True,
                writable=True,
                locator_digest=canonical_digest("email-locator"),
                dom_identity_digest=canonical_digest("email-dom"),
            ),
        ),
    )
    return replace(base, **changes)


def _greenhouse_execution_registry() -> SemanticBatchAdapterRegistry:
    return SemanticBatchAdapterRegistry(
        (
            SemanticBatchAdapterRegistration(
                "greenhouse",
                (GREENHOUSE_ADAPTER_VERSION,),
                execution_enabled=True,
            ),
        )
    )


def test_greenhouse_cache_hit_rebinds_fresh_authority_but_stays_shadow_by_default() -> None:
    fast_path = GreenhouseRecipeFastPath()
    first = fast_path.resolve(_observation(), validate_live=lambda _key: True)
    current = _observation(
        application_target_url="https://boards.greenhouse.io/acme/jobs/124",
        page_url="https://boards.greenhouse.io/acme/jobs/124",
        page_epoch=4,
        page_lease_id="lease-2",
        browser_generation=2,
    )
    second = fast_path.resolve(current, validate_live=lambda _key: True)

    assert first.status == "primed"
    assert first.agent_fallback_required is True
    assert second.status == "shadow_hit"
    assert second.cache_hit is True
    assert second.execution_enabled is False
    assert second.agent_fallback_required is True
    assert second.recipe is not None
    assert second.recipe.key.page_epoch == 4
    assert second.recipe.key.browser_generation == 2
    assert second.recipe.key.lease_digest != first.recipe.key.lease_digest  # type: ignore[union-attr]
    assert second.recipe.key.requisition_digest != first.recipe.key.requisition_digest  # type: ignore[union-attr]
    assert second.submit_authority is False
    assert second.as_dict()["browser_write_authority"] is False


def test_explicit_exact_execution_registry_can_make_validated_hit_replay_ready() -> None:
    fast_path = GreenhouseRecipeFastPath(execution_registry=_greenhouse_execution_registry())
    first = fast_path.resolve(_observation(), validate_live=lambda _key: True)
    second = fast_path.resolve(
        _observation(page_epoch=2, page_lease_id="lease-2"),
        validate_live=lambda _key: True,
    )

    assert first.status == "primed"
    assert second.status == "replay_ready"
    assert second.cache_hit is True
    assert second.execution_enabled is True
    assert second.agent_fallback_required is False
    assert second.as_dict()["submit_authority"] is False


def test_structure_drift_or_failed_live_validation_invalidates_and_reprime() -> None:
    fast_path = GreenhouseRecipeFastPath(execution_registry=_greenhouse_execution_registry())
    fast_path.resolve(_observation(), validate_live=lambda _key: True)
    changed = _observation(page_signature=canonical_digest("greenhouse-form-v2"))
    drift = fast_path.resolve(changed, validate_live=lambda _key: True)
    assert drift.status == "primed"

    denied_live = fast_path.resolve(changed, validate_live=lambda _key: False)
    assert denied_live.status == "primed"
    replay = fast_path.resolve(changed, validate_live=lambda _key: True)
    assert replay.status == "replay_ready"
    assert len(fast_path.cache) == 2


def test_greenhouse_fast_path_rejects_other_provider_and_manual_gate_observation() -> None:
    fast_path = GreenhouseRecipeFastPath()
    other = replace(
        _observation(),
        provider="lever",
        application_target_url="https://jobs.lever.co/acme/123",
        page_url="https://jobs.lever.co/acme/123",
    )
    manual = replace(_observation(), markers=("captcha",))

    assert fast_path.resolve(other, validate_live=lambda _key: True).reason_code == "greenhouse_priority_only"
    decision = fast_path.resolve(manual, validate_live=lambda _key: True)
    assert decision.status == "denied"
    assert decision.reason_code == "observation_not_recipe_safe"
    assert decision.agent_fallback_required is True
