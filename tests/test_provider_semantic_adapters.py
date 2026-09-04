from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread

import pytest

from applypilot.apply.ats import default_ats_registry
from applypilot.apply.provider_semantic_adapters import (
    ProviderControlStructure,
    ProviderPageRecipeObservation,
    default_provider_recipe_shadow_registry,
    default_provider_semantic_recipe_registry,
)
from applypilot.apply.recipe_cache import (
    ValueFreeRecipeCache,
    canonical_digest,
    payload_is_value_free,
)
from applypilot.apply.semantic_batch import (
    DEFAULT_SEMANTIC_BATCH_ADAPTER_REGISTRY,
    BatchPageBinding,
    BrowserResourceIdentity,
    SemanticBatchDenied,
    SemanticPatch,
)
from applypilot.apply.semantic_batch_runtime import (
    SemanticBatchRuntimeRequest,
    run_production_semantic_batch,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "apply"
JOBS = json.loads((FIXTURE_ROOT / "jobs.json").read_text(encoding="utf-8"))
ATS_CASES = json.loads((FIXTURE_ROOT / "ats_adapter_cases.json").read_text(encoding="utf-8"))


def _control(
    semantic: str = "email",
    *,
    kind: str = "text",
    token: str = "email",
    required: bool = True,
    writable: bool = True,
    option_count: int = 0,
    dynamic: bool = False,
    custom: bool = False,
    sensitive: bool = False,
    stateful: bool = False,
) -> ProviderControlStructure:
    return ProviderControlStructure(
        semantic=semantic,
        kind=kind,
        required=required,
        writable=writable,
        locator_digest=canonical_digest(["locator", token]),
        dom_identity_digest=canonical_digest(["dom", token]),
        option_count=option_count,
        option_digest=canonical_digest(["options", token, option_count]),
        dynamic=dynamic,
        custom=custom,
        sensitive=sensitive,
        stateful=stateful,
    )


def _observation(
    provider: str,
    url: str,
    *,
    controls: tuple[ProviderControlStructure, ...] | None = None,
    page_epoch: int = 7,
    page_lease_id: str = "lease-secret",
    browser_generation: int = 3,
    page_signature: str | None = None,
    frame_path: tuple[int, ...] = (),
    frame_url: str | None = None,
    markers: tuple[str, ...] = (),
) -> ProviderPageRecipeObservation:
    return ProviderPageRecipeObservation(
        provider=provider,  # type: ignore[arg-type]
        application_target_url=url,
        page_url=url,
        page_signature=page_signature or canonical_digest([provider, "page"]),
        page_epoch=page_epoch,
        page_lease_id=page_lease_id,
        browser_generation=browser_generation,
        controls=controls or (_control(),),
        frame_path=frame_path,
        frame_url=frame_url,
        markers=markers,
    )


@pytest.mark.parametrize("provider", ["greenhouse", "lever", "ashby"])
def test_fixture_providers_have_exact_value_free_recipe_cache_hits(provider: str) -> None:
    job = next(item for item in JOBS if item["site"] == provider)
    registry = default_provider_semantic_recipe_registry()
    recipe = registry.normalize(_observation(provider, job["application_url"]))
    cache = ValueFreeRecipeCache()
    cache.put(recipe)
    validations = 0

    def validate_live(key) -> bool:
        nonlocal validations
        validations += 1
        return key == recipe.key

    hit = cache.get(recipe.key, validate_live=validate_live)

    assert hit == recipe
    assert hit.key.provider == provider
    assert hit.key.domain in {
        "boards.greenhouse.io",
        "jobs.lever.co",
        "jobs.ashbyhq.com",
    }
    assert validations == 1


def test_structure_dimensions_miss_and_fresh_authority_dimensions_rebind() -> None:
    registry = default_provider_semantic_recipe_registry()
    recipe = registry.normalize(_observation("lever", "https://jobs.lever.co/example/1002"))
    cache = ValueFreeRecipeCache()
    cache.put(recipe)
    other = "f" * 64
    structural_mismatches = (
        replace(
            recipe.key,
            provider="ashby",
            domain="jobs.ashbyhq.com",
            adapter_version="ashby-semantic-recipe/v1",
        ),
        replace(recipe.key, domain="jobs.eu.lever.co"),
        replace(recipe.key, adapter_version="lever-semantic-recipe/v2"),
        replace(recipe.key, page_signature=other),
        replace(recipe.key, schema_policy_digest=other),
        replace(recipe.key, frame_digest=other),
        replace(recipe.key, option_digest=other),
        replace(recipe.key, required_writable_digest=other),
        replace(recipe.key, locator_digest=other),
        replace(recipe.key, taint_digest=other),
    )
    authority_variants = (
        replace(recipe.key, page_digest=other),
        replace(recipe.key, lease_digest=other),
        replace(recipe.key, tenant_digest=other),
        replace(recipe.key, requisition_digest=other),
        replace(recipe.key, application_target_digest=other),
        replace(recipe.key, page_epoch=recipe.key.page_epoch + 1),
        replace(recipe.key, browser_generation=recipe.key.browser_generation + 1),
    )

    assert all(
        cache.get(key, validate_live=lambda _key: True) is None
        for key in structural_mismatches
    )
    rebound = []
    for key in authority_variants:
        rebound.append(
            cache.get(
                key,
                validate_live=lambda fresh, expected=key: fresh == expected,
            )
        )
    assert all(item is not None for item in rebound)
    assert [item.key for item in rebound if item is not None] == list(authority_variants)
    assert all(item.controls == recipe.controls for item in rebound if item is not None)
    assert cache.get(recipe.key, validate_live=None) is None
    assert cache.get(recipe.key, validate_live=lambda _key: False) is None
    assert len(cache) == 0
    tainted_key = replace(recipe.key, taint_digest=other)
    with pytest.raises(ValueError, match="tainted"):
        cache.put(type(recipe).build(tainted_key, recipe.controls))


def test_fresh_provider_observation_rekeys_target_lease_generation_epoch_and_signature() -> None:
    registry = default_provider_semantic_recipe_registry()
    base = _observation("lever", "https://jobs.lever.co/example/1002")
    base_key = registry.normalize(base).key
    variants = (
        _observation("lever", "https://jobs.eu.lever.co/example/1002"),
        _observation("lever", "https://jobs.lever.co/another/1002"),
        _observation("lever", "https://jobs.lever.co/example/1003"),
        replace(base, page_lease_id="lease-other"),
        replace(base, browser_generation=base.browser_generation + 1),
        replace(base, page_epoch=base.page_epoch + 1),
        replace(base, page_signature="e" * 64),
    )

    assert all(registry.normalize(variant).key != base_key for variant in variants)


def test_option_required_writable_and_locator_changes_rebuild_distinct_keys() -> None:
    registry = default_provider_semantic_recipe_registry()
    base = _observation(
        "ashby",
        "https://jobs.ashbyhq.com/example/1003",
        controls=(_control("country", kind="native_select", option_count=3),),
    )
    variants = (
        replace(base, controls=(_control("country", kind="native_select", option_count=4),)),
        replace(base, controls=(_control("country", kind="native_select", option_count=3, required=False),)),
        replace(base, controls=(_control("country", kind="native_select", option_count=3, writable=False),)),
        replace(base, controls=(_control("country", kind="native_select", token="country-v2", option_count=3),)),
    )
    base_recipe = registry.normalize(base)
    cache = ValueFreeRecipeCache()
    cache.put(base_recipe)

    for variant in variants:
        if variant.controls[0].writable:
            key = registry.normalize(variant).key
            assert cache.get(key, validate_live=lambda _key: True) is None
        else:
            with pytest.raises(SemanticBatchDenied, match="no mechanically proven"):
                registry.normalize(variant)


def test_dynamic_custom_control_drift_forces_fresh_normalization_and_cache_miss() -> None:
    registry = default_provider_semantic_recipe_registry()
    url = "https://jobs.lever.co/example/1002"
    first = _observation(
        "lever",
        url,
        controls=(
            _control(),
            _control("unknown", kind="custom_combobox", token="dynamic-1", dynamic=True, custom=True),
        ),
    )
    changed = replace(
        first,
        controls=(
            _control(),
            _control("unknown", kind="custom_combobox", token="dynamic-2", dynamic=True, custom=True),
        ),
    )
    first_recipe = registry.normalize(first)
    changed_recipe = registry.normalize(changed)
    cache = ValueFreeRecipeCache()
    cache.put(first_recipe)

    assert first_recipe.controls[0].semantic == "email"
    assert first_recipe.key != changed_recipe.key
    assert cache.get(changed_recipe.key, validate_live=lambda _key: True) is None


def test_cache_payload_omits_values_secrets_targets_and_privileged_operations() -> None:
    registry = default_provider_semantic_recipe_registry()
    secret = "private-candidate-token-7f2e"
    url = f"https://jobs.ashbyhq.com/example/1003/application?candidate_token={secret}#private-fragment"
    recipe = registry.normalize(
        _observation(
            "ashby",
            url,
            page_lease_id=f"lease-{secret}",
            controls=(
                _control(),
                _control("country", kind="native_select", token="country", option_count=3),
                _control("final_submit", kind="final_submit", token="submit"),
                _control("unknown", kind="textarea", token="essay", sensitive=True),
            ),
        )
    )
    payload = recipe.as_dict()
    serialized = json.dumps(payload, sort_keys=True)

    assert payload_is_value_free(payload)
    assert secret not in serialized
    assert "jobs.ashbyhq.com/example/1003" not in serialized
    assert "candidate_token" not in serialized
    assert "private-fragment" not in serialized
    assert "final_submit" not in serialized
    assert "direct_email" not in serialized
    assert "receipt" not in serialized
    assert "nonce" not in serialized
    assert "authority" not in serialized
    assert "file_path" not in serialized
    assert recipe.operations == ("select_option", "set_text")


@pytest.mark.parametrize(
    "payload",
    [
        {"semantic": "https://jobs.lever.co/example/1002?token=private#fragment"},
        {"semantic": "private@example.test"},
        {"semantic": r"C:\private\resume.pdf"},
        {"semantic": "candidate_token"},
        {"unexpected": "email"},
    ],
)
def test_recursive_value_free_guard_rejects_urls_tokens_email_paths_and_unknown_fields(
    payload: dict[str, object],
) -> None:
    assert payload_is_value_free(payload) is False


def test_value_free_guard_allows_dynamic_workday_host_only_in_domain_field() -> None:
    host = "acme.wd5.myworkdayjobs.com"

    assert payload_is_value_free({"domain": host}) is True
    assert payload_is_value_free({"semantic": host}) is False


def test_caller_sha_fingerprints_are_rekeyed_with_process_private_hmac() -> None:
    registry = default_provider_semantic_recipe_registry()
    raw_locator = canonical_digest("email-locator")
    raw_dom = canonical_digest("input-email")
    raw_options = canonical_digest(["yes", "no"])
    raw_page = canonical_digest("small-page-template")
    control = ProviderControlStructure(
        semantic="country",
        kind="native_select",
        required=True,
        writable=True,
        locator_digest=raw_locator,
        dom_identity_digest=raw_dom,
        option_count=2,
        option_digest=raw_options,
    )
    recipe = registry.normalize(
        _observation(
            "lever",
            "https://jobs.lever.co/example/1002",
            controls=(control,),
            page_signature=raw_page,
        )
    )
    serialized = json.dumps(recipe.as_dict(), sort_keys=True)

    assert raw_locator not in serialized
    assert raw_dom not in serialized
    assert raw_options not in serialized
    assert raw_page not in serialized


def test_cache_rechecks_identity_after_live_validation_races_with_invalidation() -> None:
    registry = default_provider_semantic_recipe_registry()
    recipe = registry.normalize(_observation("lever", "https://jobs.lever.co/example/1002"))
    cache = ValueFreeRecipeCache()
    cache.put(recipe)
    validator_entered = Event()
    validator_release = Event()
    invalidated = Event()
    result: list[object] = []

    def validate_live(_key) -> bool:
        validator_entered.set()
        assert validator_release.wait(2)
        return True

    def read_cache() -> None:
        result.append(cache.get(recipe.key, validate_live=validate_live))

    def invalidate_cache() -> None:
        cache.invalidate(recipe.key)
        invalidated.set()

    reader = Thread(target=read_cache)
    invalidator = Thread(target=invalidate_cache)
    reader.start()
    assert validator_entered.wait(2)
    invalidator.start()
    try:
        assert invalidated.wait(2), "invalidate must not wait on the external live validator"
    finally:
        validator_release.set()
    reader.join(2)
    invalidator.join(2)

    assert not reader.is_alive()
    assert not invalidator.is_alive()
    assert result == [None]


@pytest.mark.parametrize(
    ("kind", "semantic", "kwargs"),
    [
        ("textarea", "email", {}),
        ("custom_combobox", "country", {"custom": True, "option_count": 3}),
        ("file", "resume", {}),
        ("checkbox", "consent", {"stateful": True, "sensitive": True}),
        ("text", "identity_number", {"sensitive": True}),
    ],
)
def test_unknown_dynamic_custom_stateful_and_sensitive_controls_fail_closed(
    kind: str,
    semantic: str,
    kwargs: dict[str, object],
) -> None:
    registry = default_provider_semantic_recipe_registry()
    control = _control(semantic, kind=kind, **kwargs)  # type: ignore[arg-type]

    with pytest.raises(SemanticBatchDenied, match="no mechanically proven"):
        registry.normalize(_observation("greenhouse", "https://boards.greenhouse.io/acme/jobs/123", controls=(control,)))


@pytest.mark.parametrize("marker", ["assessment", "captcha", "verification", "eeo", "legal", "financial"])
def test_page_level_manual_gate_markers_reject_entire_recipe(marker: str) -> None:
    registry = default_provider_semantic_recipe_registry()

    with pytest.raises(SemanticBatchDenied, match="manual-gate"):
        registry.normalize(
            _observation("lever", "https://jobs.lever.co/example/1002", markers=(marker,))
        )


def test_unknown_page_marker_fails_closed() -> None:
    registry = default_provider_semantic_recipe_registry()

    with pytest.raises(SemanticBatchDenied, match="unclassified"):
        registry.normalize(
            _observation("ashby", "https://jobs.ashbyhq.com/example/1003", markers=("new-widget",))
        )


def test_m4_registry_registers_new_providers_but_keeps_execution_default_off() -> None:
    recipe_registry = default_provider_semantic_recipe_registry()

    assert recipe_registry.providers() == ("greenhouse", "lever", "ashby")
    for provider in recipe_registry.providers():
        adapter = recipe_registry.get(provider)
        assert adapter is not None
        assert adapter.m4_execution_enabled is False
        assert default_ats_registry().get(provider).semantic_control_kinds() == frozenset()  # type: ignore[union-attr]
        with pytest.raises(SemanticBatchDenied, match="disabled"):
            recipe_registry.require_m4_execution(provider, adapter.adapter_version)
        assert not DEFAULT_SEMANTIC_BATCH_ADAPTER_REGISTRY.supports_execution(
            provider,
            adapter.adapter_version,
        )


def test_m4_runtime_returns_no_effect_fallback_before_disabled_provider_adapter_use(tmp_path: Path) -> None:
    registry = default_provider_semantic_recipe_registry()
    adapter_version = registry.get("greenhouse").adapter_version  # type: ignore[union-attr]
    request = SemanticBatchRuntimeRequest(
        mode="canary",
        attempt_id="attempt-greenhouse",
        actor_id="application:attempt-greenhouse",
        provider="greenhouse",
        adapter_version=adapter_version,
        page_binding=BatchPageBinding(
            "https://boards.greenhouse.io/acme/jobs/123",
            (),
            canonical_digest("greenhouse-page"),
            1,
        ),
        page_id="page-greenhouse",
        page_lease_id="lease-greenhouse",
        page_lease_epoch=1,
        resources=BrowserResourceIdentity(
            str(tmp_path / "state.sqlite3"),
            str(tmp_path / "profile"),
            9543,
        ),
        patches=(SemanticPatch("email", "private@example.test"),),
    )
    calls: list[str] = []

    with sqlite3.connect(":memory:") as connection:
        result = run_production_semantic_batch(
            request,
            adapter=object(),  # type: ignore[arg-type]
            connection=connection,
            close_resources=lambda: calls.append("close"),
            advance_page=lambda epoch: epoch + 1,
        )

    assert result.status == "not_applicable"
    assert result.reason_code == "provider_capability_disabled"
    assert result.effect_count == 0
    assert result.legacy_fallback_safe is True
    assert calls == []


def test_fixture_lookalike_hosts_are_rejected_by_recipe_normalization() -> None:
    registry = default_provider_semantic_recipe_registry()
    malicious = [case["url"] for case in ATS_CASES if case["adapter"] == "generic" and "example.com" not in case["url"]]

    for url in malicious:
        provider = "greenhouse" if "greenhouse" in url else "lever" if "lever" in url else "ashby"
        with pytest.raises(SemanticBatchDenied, match="host is not exact"):
            registry.normalize(_observation(provider, url))


@pytest.mark.parametrize("frame_path", [(0,), (1,), (0, 1)])
def test_greenhouse_workato_embed_stays_unsupported_without_live_frame_proof(
    frame_path: tuple[int, ...],
) -> None:
    registry = default_provider_semantic_recipe_registry()
    top_url = "https://www.workato.com/careers/software-engineer-123?gh_jid=123"
    frame_url = "https://job-boards.greenhouse.io/embed/job_app?for=workato&token=123"
    observation = ProviderPageRecipeObservation(
        provider="greenhouse",
        application_target_url=top_url,
        page_url=top_url,
        page_signature=canonical_digest("workato-greenhouse"),
        page_epoch=2,
        page_lease_id="lease-workato",
        browser_generation=1,
        controls=(_control(),),
        frame_path=frame_path,
        frame_url=frame_url,
    )

    with pytest.raises(SemanticBatchDenied, match="unavailable live frame-binding proof"):
        registry.normalize(observation)


def test_greenhouse_workato_evil_application_target_is_rejected() -> None:
    registry = default_provider_semantic_recipe_registry()
    observation = _observation(
        "greenhouse",
        "https://boards.greenhouse.io/acme/jobs/123",
    )

    with pytest.raises(SemanticBatchDenied, match="host is not exact"):
        registry.normalize(
            replace(
                observation,
                application_target_url="https://boards.greenhouse.io.evil.example/acme/jobs/123",
            )
        )


def test_greenhouse_unbound_frame_url_is_rejected_even_without_frame_path() -> None:
    registry = default_provider_semantic_recipe_registry()
    observation = _observation(
        "greenhouse",
        "https://boards.greenhouse.io/acme/jobs/123",
        frame_url="https://job-boards.greenhouse.io/embed/job_app?for=workato&token=123",
    )

    with pytest.raises(SemanticBatchDenied, match="unbound"):
        registry.normalize(observation)


def test_non_greenhouse_cross_origin_frames_are_rejected() -> None:
    registry = default_provider_semantic_recipe_registry()
    observation = _observation(
        "lever",
        "https://jobs.lever.co/example/1002",
        frame_path=(0,),
        frame_url="https://forms.example.test/apply",
    )

    with pytest.raises(SemanticBatchDenied, match="framed"):
        registry.normalize(observation)


@pytest.mark.parametrize(
    ("provider", "url"),
    [
        (
            "workday",
            "https://acme.wd5.myworkdayjobs.com/en-US/jobs/job/Singapore/Engineer_R123/apply",
        ),
        (
            "smartrecruiters",
            "https://jobs.smartrecruiters.com/Acme/744000012345678-engineer",
        ),
    ],
)
def test_shadow_registry_normalizes_workday_and_smartrecruiters_without_execution(
    provider: str,
    url: str,
) -> None:
    registry = default_provider_recipe_shadow_registry()

    recipe = registry.normalize(_observation(provider, url))

    assert recipe.key.provider == provider
    assert recipe.synthetic_only is True
    assert recipe.operations == ("set_text",)
    assert registry.get(provider).m4_execution_enabled is False  # type: ignore[union-attr]
    assert payload_is_value_free(recipe.as_dict())
    with pytest.raises(SemanticBatchDenied, match="shadow-only"):
        registry.require_m4_execution(provider, recipe.key.adapter_version)


@pytest.mark.parametrize(
    ("provider", "url", "lookalike"),
    [
        (
            "workday",
            "https://acme.wd5.myworkdayjobs.com/en-US/jobs/job/Engineer_R123",
            "https://acme.wd5.myworkdayjobs.com.evil.example/en-US/jobs/job/Engineer_R123",
        ),
        (
            "smartrecruiters",
            "https://jobs.smartrecruiters.com/Acme/744000012345678-engineer",
            "https://jobs.smartrecruiters.com.evil.example/Acme/744000012345678-engineer",
        ),
    ],
)
def test_shadow_provider_adapters_reject_lookalike_hosts_and_target_drift(
    provider: str,
    url: str,
    lookalike: str,
) -> None:
    registry = default_provider_recipe_shadow_registry()
    observation = _observation(provider, url)

    with pytest.raises(SemanticBatchDenied, match="host is not exact"):
        registry.normalize(replace(observation, application_target_url=lookalike))
    with pytest.raises(SemanticBatchDenied, match="changed application target"):
        registry.normalize(replace(observation, page_url=url + "-other"))


@pytest.mark.parametrize("provider", ["workday", "smartrecruiters"])
def test_shadow_provider_adapters_keep_frames_fail_closed(provider: str) -> None:
    url = (
        "https://acme.wd5.myworkdayjobs.com/en-US/jobs/job/Engineer_R123"
        if provider == "workday"
        else "https://jobs.smartrecruiters.com/Acme/744000012345678-engineer"
    )
    registry = default_provider_recipe_shadow_registry()

    with pytest.raises(SemanticBatchDenied, match="framed"):
        registry.normalize(
            _observation(
                provider,
                url,
                frame_path=(0,),
                frame_url="https://forms.example.test/apply",
            )
        )
