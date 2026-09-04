"""Fail-closed Greenhouse-first recipe routing without browser authority.

This module decides whether an already-observed, value-free Greenhouse page can
use a cached structural recipe. It never owns values, locators, browser writes,
Agent execution, navigation, Submit, receipt, or ledger authority. Production
execution remains disabled unless a caller injects an explicit M4 capability
registry that admits the exact provider and adapter version.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from applypilot.apply.provider_semantic_adapters import (
    ProviderPageRecipeObservation,
    ProviderSemanticRecipeRegistry,
    default_provider_semantic_recipe_registry,
)
from applypilot.apply.recipe_cache import (
    CachedProviderRecipe,
    RecipeCacheKey,
    ValueFreeRecipeCache,
)
from applypilot.apply.semantic_batch import (
    DEFAULT_SEMANTIC_BATCH_ADAPTER_REGISTRY,
    SemanticBatchAdapterRegistry,
    SemanticBatchDenied,
)

FastPathStatus = Literal["denied", "primed", "shadow_hit", "replay_ready"]


@dataclass(frozen=True, slots=True)
class ProviderRecipeFastPathDecision:
    status: FastPathStatus
    provider: str
    adapter_version: str | None
    recipe: CachedProviderRecipe | None
    cache_hit: bool
    execution_enabled: bool
    agent_fallback_required: bool
    reason_code: str
    submit_authority: bool = False

    def __post_init__(self) -> None:
        if self.submit_authority is not False:
            raise ValueError("provider recipe fast path must not grant submit authority")
        if self.status == "replay_ready":
            if not self.cache_hit or not self.execution_enabled or self.recipe is None:
                raise ValueError("replay-ready decisions require an admitted cache hit")
            if self.agent_fallback_required:
                raise ValueError("replay-ready decisions must not require Agent fallback")
        elif not self.agent_fallback_required:
            raise ValueError("non-ready decisions require a safe fallback")

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "provider": self.provider,
            "adapter_version": self.adapter_version,
            "recipe_identity": (self.recipe.key.template_identity_digest if self.recipe is not None else None),
            "cache_hit": self.cache_hit,
            "execution_enabled": self.execution_enabled,
            "agent_fallback_required": self.agent_fallback_required,
            "reason_code": self.reason_code,
            "browser_write_authority": False,
            "submit_authority": False,
        }


class GreenhouseRecipeFastPath:
    """Greenhouse-first cache resolver with explicit M4 admission."""

    def __init__(
        self,
        *,
        cache: ValueFreeRecipeCache | None = None,
        recipe_registry: ProviderSemanticRecipeRegistry | None = None,
        execution_registry: SemanticBatchAdapterRegistry = (DEFAULT_SEMANTIC_BATCH_ADAPTER_REGISTRY),
    ) -> None:
        self._cache = cache if cache is not None else ValueFreeRecipeCache()
        self._recipe_registry = (
            recipe_registry if recipe_registry is not None else default_provider_semantic_recipe_registry()
        )
        self._execution_registry = execution_registry

    @property
    def cache(self) -> ValueFreeRecipeCache:
        return self._cache

    def resolve(
        self,
        observation: ProviderPageRecipeObservation,
        *,
        validate_live: Callable[[RecipeCacheKey], bool] | None,
    ) -> ProviderRecipeFastPathDecision:
        if observation.provider != "greenhouse":
            return ProviderRecipeFastPathDecision(
                status="denied",
                provider=str(observation.provider),
                adapter_version=None,
                recipe=None,
                cache_hit=False,
                execution_enabled=False,
                agent_fallback_required=True,
                reason_code="greenhouse_priority_only",
            )
        try:
            candidate = self._recipe_registry.normalize(observation)
        except (SemanticBatchDenied, TypeError, ValueError):
            return ProviderRecipeFastPathDecision(
                status="denied",
                provider="greenhouse",
                adapter_version=None,
                recipe=None,
                cache_hit=False,
                execution_enabled=False,
                agent_fallback_required=True,
                reason_code="observation_not_recipe_safe",
            )
        hit = self._cache.get(candidate.key, validate_live=validate_live)
        if hit is None:
            self._cache.put(candidate)
            return ProviderRecipeFastPathDecision(
                status="primed",
                provider="greenhouse",
                adapter_version=candidate.key.adapter_version,
                recipe=candidate,
                cache_hit=False,
                execution_enabled=False,
                agent_fallback_required=True,
                reason_code="cache_miss_requires_fallback",
            )
        enabled = self._execution_registry.supports_execution("greenhouse", hit.key.adapter_version)
        if not enabled:
            return ProviderRecipeFastPathDecision(
                status="shadow_hit",
                provider="greenhouse",
                adapter_version=hit.key.adapter_version,
                recipe=hit,
                cache_hit=True,
                execution_enabled=False,
                agent_fallback_required=True,
                reason_code="provider_capability_disabled",
            )
        return ProviderRecipeFastPathDecision(
            status="replay_ready",
            provider="greenhouse",
            adapter_version=hit.key.adapter_version,
            recipe=hit,
            cache_hit=True,
            execution_enabled=True,
            agent_fallback_required=False,
            reason_code="exact_live_validated_recipe_hit",
        )


__all__ = [
    "FastPathStatus",
    "GreenhouseRecipeFastPath",
    "ProviderRecipeFastPathDecision",
]
