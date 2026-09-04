"""Read-only provider recipe observations for the normal prepare audit path.

The observer consumes the structural snapshot that the pre-submit audit already
read. It never evaluates the page, resolves live locators, supplies values,
writes controls, uploads files, navigates, or grants Submit authority. Every
outcome preserves the existing Agent fallback.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from time import perf_counter
from typing import Literal
from urllib.parse import urlsplit

from applypilot.apply.browser_authority import BrowserAuthorityHandle
from applypilot.apply.provider_registry import provider_for_url
from applypilot.apply.provider_semantic_adapters import (
    ProviderControlStructure,
    ProviderPageRecipeObservation,
    ProviderSemanticRecipeRegistry,
    default_provider_recipe_shadow_registry,
)
from applypilot.apply.recipe_cache import ValueFreeRecipeCache, canonical_digest
from applypilot.apply.semantic_batch import SemanticBatchDenied

RecipeShadowOutcome = Literal["off", "not_applicable", "denied", "miss", "hit"]
_ADMITTED_PROVIDERS = frozenset({"greenhouse", "smartrecruiters", "workday"})
_LEGAL_OR_SENSITIVE_RE = re.compile(
    r"work (?:authorization|authorisation)|right to work|visa|sponsorship|"
    r"citizenship|legal identity|passport|national id|nric|\bfin\b|"
    r"self identification|veteran|disability|eeo",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ProviderRecipeShadowTelemetry:
    provider: str | None
    outcome: RecipeShadowOutcome
    admission_enabled: bool
    cache_hit: bool
    agent_fallback_required: bool
    reason_code: str
    duration_ms: float
    routine_control_count: int = 0

    def __post_init__(self) -> None:
        if self.agent_fallback_required is not True:
            raise ValueError("recipe shadow observation must preserve Agent fallback")
        if self.cache_hit is not (self.outcome == "hit"):
            raise ValueError("cache_hit must match the shadow outcome")
        if self.duration_ms < 0:
            raise ValueError("duration_ms must be non-negative")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "provider-recipe-shadow/v1",
            "provider": self.provider,
            "outcome": self.outcome,
            "admission_enabled": self.admission_enabled,
            "cache_hit": self.cache_hit,
            "agent_fallback_required": True,
            "reason_code": self.reason_code,
            "duration_ms": self.duration_ms,
            "routine_control_count": self.routine_control_count,
            "browser_write_authority": False,
            "file_upload_authority": False,
            "submit_authority": False,
            "throughput_admission_evidence": False,
        }


def _telemetry(
    started: float,
    *,
    provider: str | None,
    outcome: RecipeShadowOutcome,
    admission_enabled: bool,
    reason_code: str,
    routine_control_count: int = 0,
) -> ProviderRecipeShadowTelemetry:
    return ProviderRecipeShadowTelemetry(
        provider=provider,
        outcome=outcome,
        admission_enabled=admission_enabled,
        cache_hit=outcome == "hit",
        agent_fallback_required=True,
        reason_code=reason_code,
        duration_ms=round(max(0.0, (perf_counter() - started) * 1000), 3),
        routine_control_count=routine_control_count,
    )


def _semantic(field: Mapping[str, object]) -> str:
    autocomplete = str(field.get("autocomplete") or "").strip().casefold()
    autocomplete_semantics = {
        "address-level1": "state",
        "address-level2": "city",
        "country": "country",
        "country-name": "country",
        "email": "email",
        "postal-code": "postal_code",
        "tel": "phone",
        "url": "portfolio_url",
    }
    if autocomplete in autocomplete_semantics:
        return autocomplete_semantics[autocomplete]
    descriptor = " ".join(str(field.get(key) or "") for key in ("label", "field_key", "placeholder")).casefold()
    patterns = (
        (r"\bpreferred (?:first )?name\b|\bdisplay name\b", "preferred_name"),
        (r"\be-?mail(?: address)?\b", "email"),
        (r"\b(?:phone|mobile|telephone)(?: number)?\b", "phone"),
        (r"\b(?:portfolio|personal website|website|linkedin)\b", "portfolio_url"),
        (r"\bpostal code\b|\bzip(?: code)?\b", "postal_code"),
        (r"\b(?:state|province|region)\b", "state"),
        (r"\bcountry\b", "country"),
        (r"\bcurrent location\b|\bcity\b", "city"),
    )
    for pattern, semantic in patterns:
        if re.search(pattern, descriptor):
            return semantic
    return "unknown"


def _snapshot_controls(
    snapshot: Mapping[str, object],
) -> tuple[tuple[ProviderControlStructure, ...], tuple[str, ...]]:
    raw_fields = snapshot.get("form_fields")
    fields = raw_fields if isinstance(raw_fields, list) else []
    controls: list[ProviderControlStructure] = []
    markers: set[str] = set()
    if snapshot.get("captcha_visible") is True:
        markers.add("captcha")
    if snapshot.get("assessment_visible") is True:
        markers.add("assessment")
    if snapshot.get("verification_visible") is True:
        markers.add("verification")
    if snapshot.get("resume_field_present") is True or snapshot.get("file_fields"):
        markers.add("file_upload")
    if snapshot.get("sensitive_required_unknown"):
        markers.add("legal")

    for index, raw_field in enumerate(fields):
        if not isinstance(raw_field, Mapping):
            markers.add("complex_control")
            continue
        raw_kind = str(raw_field.get("control") or "").strip().casefold()
        if raw_kind in {"submit", "button", "reset", "hidden"}:
            continue
        if raw_kind == "select":
            kind = "native_select"
        elif raw_kind in {"text", "email", "tel", "url", "search"}:
            kind = "text"
        else:
            kind = raw_kind or "unknown"
            markers.add("file_upload" if raw_kind == "file" else "complex_control")
        descriptor = " ".join(str(raw_field.get(key) or "") for key in ("label", "field_key", "placeholder"))
        sensitive = raw_field.get("protected_identifier") is True or bool(_LEGAL_OR_SENSITIVE_RE.search(descriptor))
        if sensitive:
            markers.add("legal")
        options = raw_field.get("options")
        option_values = options if isinstance(options, list) else []
        option_count = raw_field.get("option_count", 0)
        if isinstance(option_count, bool) or not isinstance(option_count, int):
            option_count = 0
            markers.add("complex_control")
        dynamic = raw_field.get("options_truncated") is True
        if dynamic:
            markers.add("complex_control")
        structural_identity = {
            "field_key": str(raw_field.get("field_key") or ""),
            "index": index,
            "kind": raw_kind,
            "label": str(raw_field.get("label") or ""),
            "placeholder": str(raw_field.get("placeholder") or ""),
        }
        controls.append(
            ProviderControlStructure(
                semantic=_semantic(raw_field),
                kind=kind,
                required=raw_field.get("required") is True,
                writable=not (raw_field.get("disabled") is True or raw_field.get("readonly") is True),
                locator_digest=canonical_digest(structural_identity),
                dom_identity_digest=canonical_digest(
                    {
                        "autocomplete": str(raw_field.get("autocomplete") or ""),
                        "index": index,
                        "kind": raw_kind,
                    }
                ),
                option_count=max(0, option_count),
                option_digest=canonical_digest(option_values),
                stateful=raw_kind in {"checkbox", "radio", "date"},
                dynamic=dynamic,
                custom=raw_kind == "combobox",
                sensitive=sensitive,
            )
        )
    return tuple(controls), tuple(sorted(markers))


def _origin(value: str) -> tuple[str, str, int | None] | None:
    try:
        parsed = urlsplit(value)
        return parsed.scheme.casefold(), (parsed.hostname or "").casefold(), parsed.port
    except ValueError:
        return None


class ProviderRecipeShadowObserver:
    """Process-local shadow cache that can never return an executable decision."""

    def __init__(
        self,
        *,
        cache: ValueFreeRecipeCache | None = None,
        registry: ProviderSemanticRecipeRegistry | None = None,
    ) -> None:
        self._cache = cache if cache is not None else ValueFreeRecipeCache()
        self._registry = registry if registry is not None else default_provider_recipe_shadow_registry()

    def observe(
        self,
        *,
        enabled_providers: Iterable[str],
        application_target_url: str,
        page_url: str,
        surface_url: str,
        surface_is_main_frame: bool,
        snapshot: Mapping[str, object],
        page_epoch: int,
        page_lease_id: str,
        browser_generation: int,
    ) -> ProviderRecipeShadowTelemetry:
        started = perf_counter()
        provider = provider_for_url(page_url, "detection")
        enabled = frozenset(str(item).strip().casefold() for item in enabled_providers)
        if provider not in _ADMITTED_PROVIDERS:
            return _telemetry(
                started,
                provider=provider,
                outcome="not_applicable",
                admission_enabled=False,
                reason_code="provider_not_shadow_supported",
            )
        if provider not in enabled:
            return _telemetry(
                started,
                provider=provider,
                outcome="off",
                admission_enabled=False,
                reason_code="provider_shadow_disabled",
            )
        if not surface_is_main_frame:
            return _telemetry(
                started,
                provider=provider,
                outcome="denied",
                admission_enabled=True,
                reason_code="framed_surface_not_admitted",
            )
        if _origin(surface_url) != _origin(page_url):
            return _telemetry(
                started,
                provider=provider,
                outcome="denied",
                admission_enabled=True,
                reason_code="cross_origin_surface_not_admitted",
            )
        controls, markers = _snapshot_controls(snapshot)
        observation = ProviderPageRecipeObservation(
            provider=provider,  # type: ignore[arg-type]
            application_target_url=application_target_url,
            page_url=page_url,
            page_signature=canonical_digest(
                {
                    "controls": [
                        {
                            "custom": control.custom,
                            "dynamic": control.dynamic,
                            "kind": control.kind,
                            "locator": control.locator_digest,
                            "options": control.option_digest,
                            "required": control.required,
                            "semantic": control.semantic,
                            "sensitive": control.sensitive,
                            "stateful": control.stateful,
                            "writable": control.writable,
                        }
                        for control in controls
                    ],
                    "markers": markers,
                }
            ),
            page_epoch=page_epoch,
            page_lease_id=page_lease_id,
            browser_generation=browser_generation,
            controls=controls,
            markers=markers,
            control_schema_version="prepare-shadow/v1",
        )
        try:
            candidate = self._registry.normalize(observation)
        except (SemanticBatchDenied, TypeError, ValueError):
            return _telemetry(
                started,
                provider=provider,
                outcome="denied",
                admission_enabled=True,
                reason_code="observation_not_recipe_safe",
            )
        hit = self._cache.get(
            candidate.key,
            validate_live=lambda fresh: fresh == candidate.key,
        )
        if hit is None:
            self._cache.put(candidate)
            return _telemetry(
                started,
                provider=provider,
                outcome="miss",
                admission_enabled=True,
                reason_code="cache_miss_observed",
                routine_control_count=len(candidate.controls),
            )
        return _telemetry(
            started,
            provider=provider,
            outcome="hit",
            admission_enabled=True,
            reason_code="cache_hit_observed",
            routine_control_count=len(hit.controls),
        )


_PRODUCTION_SHADOW_OBSERVER = ProviderRecipeShadowObserver()


def observe_prepare_recipe_shadow(
    *,
    job: dict,
    page_url: str,
    surface_url: str,
    surface_is_main_frame: bool,
    snapshot: Mapping[str, object],
    enabled_providers: Iterable[str],
) -> ProviderRecipeShadowTelemetry:
    """Observe one normal prepare snapshot using current read-only lease evidence."""

    started = perf_counter()
    provider = provider_for_url(page_url, "detection")
    enabled = frozenset(str(item).strip().casefold() for item in enabled_providers)
    if provider not in enabled:
        return _PRODUCTION_SHADOW_OBSERVER.observe(
            enabled_providers=enabled,
            application_target_url=str(job.get("application_url") or job.get("url") or ""),
            page_url=page_url,
            surface_url=surface_url,
            surface_is_main_frame=surface_is_main_frame,
            snapshot=snapshot,
            page_epoch=0,
            page_lease_id="shadow-disabled",
            browser_generation=1,
        )
    try:
        authority = BrowserAuthorityHandle.rebuild(job)
        bundle = authority.bundle
        target_url = str(job.get("application_url") or job.get("_discovered_application_url") or job.get("url") or "")
        return _PRODUCTION_SHADOW_OBSERVER.observe(
            enabled_providers=enabled,
            application_target_url=target_url,
            page_url=page_url,
            surface_url=surface_url,
            surface_is_main_frame=surface_is_main_frame,
            snapshot=snapshot,
            page_epoch=bundle.page_binding.page_epoch,
            page_lease_id=bundle.page.lease_id,
            browser_generation=authority.identity.browser_generation,
        )
    except Exception:  # noqa: BLE001 - missing authority must remain advisory and fail closed
        return _telemetry(
            started,
            provider=provider,
            outcome="denied",
            admission_enabled=True,
            reason_code="fresh_browser_authority_unavailable",
        )


__all__ = [
    "ProviderRecipeShadowObserver",
    "ProviderRecipeShadowTelemetry",
    "RecipeShadowOutcome",
    "observe_prepare_recipe_shadow",
]
