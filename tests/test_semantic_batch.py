from __future__ import annotations

import sqlite3

import pytest

from applypilot.apply.semantic_batch import (
    ApplicationRecipe,
    BatchSemanticAuthorityIssuer,
    BrowserContext,
    BrowserContextRegistry,
    BrowserContextTainted,
    ProviderAdapter,
    SemanticBatchDenied,
    SemanticBatchExecutionError,
    SemanticPatch,
    SemanticPatchBatch,
    SemanticPatchBatchRunner,
)

PAGE_SIGNATURE = "a" * 64


def _recipe(*, operations: tuple[str, ...] = ("apply_semantic_patch",)) -> ApplicationRecipe:
    return ApplicationRecipe(
        provider="workday",
        domain="tenant.myworkdayjobs.com",
        adapter_version="workday-semantic/v1",
        page_signature=PAGE_SIGNATURE,
        operations=operations,  # type: ignore[arg-type]
    )


def _context(
    *,
    context_id: str = "context-1",
    attempt_id: str = "attempt-1",
    sqlite_path: str = "application-1.sqlite",
    profile_id: str = "profile-1",
    debug_port: int = 9222,
) -> BrowserContext:
    return BrowserContext(
        context_id=context_id,
        attempt_id=attempt_id,
        sqlite_path=sqlite_path,
        profile_id=profile_id,
        debug_port=debug_port,
        provider="workday",
        domain="tenant.myworkdayjobs.com",
        page_signature=PAGE_SIGNATURE,
    )


def _batch(*, attempt_id: str = "attempt-1") -> SemanticPatchBatch:
    return SemanticPatchBatch(
        batch_id="batch-1",
        attempt_id=attempt_id,
        recipe=_recipe(),
        patches=(SemanticPatch("preferred_name", "Ada"), SemanticPatch("city", "Singapore")),
    )


def test_adapter_resolves_only_an_exact_provider_domain_version_and_page_signature() -> None:
    recipe = _recipe()
    adapter = ProviderAdapter(provider="workday", version="workday-semantic/v1", recipes=(recipe,))

    assert adapter.recipe_for(domain="tenant.myworkdayjobs.com", page_signature=PAGE_SIGNATURE) == recipe
    with pytest.raises(SemanticBatchDenied, match="exact application recipe"):
        adapter.recipe_for(domain="other.myworkdayjobs.com", page_signature=PAGE_SIGNATURE)
    with pytest.raises(ValueError, match="provider and version"):
        ProviderAdapter(provider="smartrecruiters", version="workday-semantic/v1", recipes=(recipe,))


def test_batch_excludes_submit_and_sensitive_or_credential_semantics() -> None:
    for privileged_operation in ("final_submit", "navigate", "switch_frame"):
        with pytest.raises(ValueError, match="unsupported or privileged"):
            _recipe(operations=(privileged_operation,))
    with pytest.raises(ValueError, match="not batchable"):
        SemanticPatch("passport_number", "P123")
    with pytest.raises(ValueError, match="not batchable"):
        SemanticPatch("bank_account", "123")
    with pytest.raises(ValueError, match="not batchable"):
        SemanticPatch("work_authorization", "yes")


def test_batch_authority_is_one_shot_and_bound_to_one_application_context() -> None:
    context = _context()
    batch = _batch()
    issuer = BatchSemanticAuthorityIssuer()
    authority = issuer.issue(context, batch)
    runner = SemanticPatchBatchRunner(issuer)
    applied: list[str] = []

    result = runner.run(
        context=context,
        authority=authority,
        batch=batch,
        apply_patch=lambda patch: applied.append(patch.field_semantic),
    )

    assert applied == ["preferred_name", "city"]
    assert result.next_page_epoch == 1
    assert result.submit_authority is False
    replayed_context = _context()
    with pytest.raises(SemanticBatchDenied, match="already consumed"):
        runner.run(
            context=replayed_context,
            authority=authority,
            batch=batch,
            apply_patch=lambda _patch: None,
        )

    other_context = _context(attempt_id="attempt-2")
    other_authority = issuer.issue(_context(), _batch())
    with pytest.raises(SemanticBatchDenied, match="application context"):
        runner.run(
            context=other_context,
            authority=other_authority,
            batch=_batch(),
            apply_patch=lambda _patch: None,
        )


def test_provider_failure_taints_and_drains_only_that_application_context() -> None:
    context = _context()
    batch = _batch()
    issuer = BatchSemanticAuthorityIssuer()
    authority = issuer.issue(context, batch)
    runner = SemanticPatchBatchRunner(issuer)

    with pytest.raises(SemanticBatchExecutionError, match="context was drained"):
        runner.run(
            context=context,
            authority=authority,
            batch=batch,
            apply_patch=lambda _patch: (_ for _ in ()).throw(RuntimeError("driver down")),
        )

    assert context.taint_reason == "effect_unknown:semantic_patch_failed:RuntimeError"
    assert context.drained is True
    with pytest.raises(BrowserContextTainted):
        context.require_active()


def test_context_registry_exclusively_reserves_temp_sqlite_profile_and_port(tmp_path) -> None:
    sqlite_path = tmp_path / "application-1.sqlite"
    sqlite3.connect(sqlite_path).close()
    registry = BrowserContextRegistry()
    first = _context(
        sqlite_path=str(sqlite_path),
        profile_id=str(tmp_path / "profile-1"),
        debug_port=9231,
    )
    competing_contexts = (
        _context(
            context_id="context-sqlite",
            attempt_id="attempt-sqlite",
            sqlite_path=str(sqlite_path),
            profile_id="profile-2",
            debug_port=9232,
        ),
        _context(
            context_id="context-profile",
            attempt_id="attempt-profile",
            sqlite_path="application-2.sqlite",
            profile_id=str(tmp_path / "profile-1"),
            debug_port=9232,
        ),
        _context(
            context_id="context-port",
            attempt_id="attempt-port",
            sqlite_path="application-2.sqlite",
            profile_id="profile-2",
            debug_port=9231,
        ),
    )

    registry.acquire(first)
    for competing, resource_name in zip(
        competing_contexts, ("sqlite path", "browser profile", "debug port"), strict=True
    ):
        with pytest.raises(SemanticBatchDenied, match=resource_name):
            registry.acquire(competing)

    first.taint("test_cleanup")
    first.drain()
    registry.release_drained(first)
    registry.acquire(competing_contexts[0])


def test_stale_page_after_effect_is_parked_and_never_replayed() -> None:
    context = _context()
    batch = _batch()
    issuer = BatchSemanticAuthorityIssuer()
    authority = issuer.issue(context, batch)
    runner = SemanticPatchBatchRunner(issuer)

    def navigation_side_effect(_patch: SemanticPatch) -> None:
        context.page_epoch += 1

    with pytest.raises(SemanticBatchExecutionError, match="context was drained"):
        runner.run(
            context=context,
            authority=authority,
            batch=batch,
            apply_patch=navigation_side_effect,
        )

    assert context.taint_reason == "effect_unknown:semantic_patch_failed:RuntimeError"
    assert context.drained is True
    with pytest.raises(SemanticBatchDenied, match="tainted"):
        runner.run(
            context=context,
            authority=authority,
            batch=batch,
            apply_patch=lambda _patch: None,
        )
