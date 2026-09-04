from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from applypilot.apply import semantic_batch
from applypilot.apply.semantic_batch import (
    ApplicationRecipe,
    BatchControlDescriptor,
    BatchPageBinding,
    BatchSemanticAuthorityIssuer,
    BrowserContext,
    BrowserContextLease,
    BrowserContextRegistry,
    BrowserPageObservation,
    BrowserResourceIdentity,
    ProviderAdapter,
    SemanticBatchDenied,
    SemanticBatchExecutionError,
    SemanticPatch,
    SemanticPatchBatch,
    SemanticPatchBatchRunner,
    normalize_field_semantic,
)

PAGE_SIGNATURE = "a" * 64
PAGE_URL = "https://tenant.myworkdayjobs.com/en-US/example/job/1"


def _recipe(*, operations: tuple[str, ...] = ("apply_semantic_patch",)) -> ApplicationRecipe:
    return ApplicationRecipe(
        provider="workday",
        domain="tenant.myworkdayjobs.com",
        adapter_version="workday-semantic/v1",
        page_signature=PAGE_SIGNATURE,
        operations=operations,  # type: ignore[arg-type]
    )


def _context(
    tmp_path: Path,
    *,
    context_id: str = "context-1",
    attempt_id: str = "attempt-1",
    sqlite_path: Path | None = None,
    profile_path: Path | None = None,
    debug_port: int = 9231,
    page_epoch: int = 0,
) -> BrowserContext:
    return BrowserContext(
        context_id=context_id,
        attempt_id=attempt_id,
        resources=BrowserResourceIdentity(
            str(sqlite_path or tmp_path / "application.sqlite"),
            str(profile_path or tmp_path / "profile"),
            debug_port,
        ),
        provider="workday",
        page_binding=BatchPageBinding(PAGE_URL, (), PAGE_SIGNATURE, page_epoch),
    )


def _batch(
    *,
    recipe: ApplicationRecipe | None = None,
    page_binding: BatchPageBinding | None = None,
) -> SemanticPatchBatch:
    return SemanticPatchBatch(
        batch_id="batch-1",
        attempt_id="attempt-1",
        recipe=recipe or _recipe(),
        page_binding=page_binding or BatchPageBinding(PAGE_URL, (), PAGE_SIGNATURE, 0),
        patches=(SemanticPatch("preferred_name", "Ada"), SemanticPatch("city", "Singapore")),
    )


class FakeAdapter:
    provider = "workday"
    adapter_version = "workday-semantic/v1"

    def __init__(
        self,
        *,
        classification: str = "routine",
        before_page: BrowserPageObservation | None = None,
        after_first_page: BrowserPageObservation | None = None,
    ) -> None:
        self.classification = classification
        self.page = before_page or BrowserPageObservation(PAGE_URL, (), PAGE_SIGNATURE, 0)
        self.after_first_page = after_first_page
        self.applied: list[str] = []

    def observe_page(self) -> BrowserPageObservation:
        return self.page

    def control_for(self, field_semantic: str) -> BatchControlDescriptor:
        return BatchControlDescriptor(
            control_id=f"control:{field_semantic}",
            field_semantic=field_semantic,
            classification=self.classification,  # type: ignore[arg-type]
            page=self.page,
        )

    def apply_routine_control(self, control: BatchControlDescriptor, _value: str) -> None:
        self.applied.append(control.field_semantic)
        if len(self.applied) == 1 and self.after_first_page is not None:
            self.page = self.after_first_page


def test_adapter_recipe_requires_exact_provider_domain_version_and_signature() -> None:
    recipe = _recipe()
    adapter = ProviderAdapter(provider="workday", version="workday-semantic/v1", recipes=(recipe,))

    assert adapter.recipe_for(domain="tenant.myworkdayjobs.com", page_signature=PAGE_SIGNATURE) == recipe
    with pytest.raises(SemanticBatchDenied, match="exact application recipe"):
        adapter.recipe_for(domain="other.myworkdayjobs.com", page_signature=PAGE_SIGNATURE)
    with pytest.raises(ValueError, match="provider and version"):
        ProviderAdapter(provider="smartrecruiters", version="workday-semantic/v1", recipes=(recipe,))


def test_sensitive_privileged_and_unrecognized_semantics_fail_closed_after_normalization() -> None:
    assert normalize_field_semantic("Preferred Name") == "preferred_name"
    assert normalize_field_semantic("dateOfBirth") == "date_of_birth"
    for semantic in (
        "passport number",
        "Expected monthly salary",
        "dateOfBirth",
        "OTP",
        "security code",
        "legal declaration",
        "sponsorship",
        "final_submit",
        "unclassified proprietary field",
    ):
        with pytest.raises(ValueError, match="not batchable"):
            SemanticPatch(semantic, "value")


def test_exact_routine_descriptor_batch_runs_without_submit_authority(tmp_path: Path) -> None:
    context = _context(tmp_path)
    batch = _batch()
    issuer = BatchSemanticAuthorityIssuer()
    adapter = FakeAdapter()
    authority = issuer.issue(context, batch)

    result = SemanticPatchBatchRunner(issuer).run(
        context=context,
        authority=authority,
        batch=batch,
        adapter=adapter,
    )

    assert adapter.applied == ["preferred_name", "city"]
    assert result.page_epoch == 0
    assert result.submit_authority is False
    with pytest.raises(SemanticBatchDenied, match="already consumed"):
        SemanticPatchBatchRunner(issuer).run(
            context=context,
            authority=authority,
            batch=batch,
            adapter=adapter,
        )


@pytest.mark.parametrize("classification", ("final_submit", "navigation", "frame", "sensitive"))
def test_nonroutine_controls_have_zero_side_effects(tmp_path: Path, classification: str) -> None:
    context = _context(tmp_path)
    batch = _batch()
    issuer = BatchSemanticAuthorityIssuer()
    adapter = FakeAdapter(classification=classification)

    with pytest.raises(SemanticBatchExecutionError, match="context was drained"):
        SemanticPatchBatchRunner(issuer).run(
            context=context,
            authority=issuer.issue(context, batch),
            batch=batch,
            adapter=adapter,
        )

    assert adapter.applied == []
    assert context.drained is True


@pytest.mark.parametrize(
    "changed_page",
    (
        BrowserPageObservation("https://tenant.myworkdayjobs.com/en-US/example/job/other", (), PAGE_SIGNATURE, 0),
        BrowserPageObservation(PAGE_URL, (0,), PAGE_SIGNATURE, 0),
        BrowserPageObservation(PAGE_URL, (), PAGE_SIGNATURE, 1),
    ),
)
def test_url_frame_or_epoch_change_after_first_effect_stops_remaining_patches(
    tmp_path: Path, changed_page: BrowserPageObservation
) -> None:
    context = _context(tmp_path)
    batch = _batch()
    issuer = BatchSemanticAuthorityIssuer()
    adapter = FakeAdapter(after_first_page=changed_page)
    authority = issuer.issue(context, batch)

    with pytest.raises(SemanticBatchExecutionError, match="context was drained"):
        SemanticPatchBatchRunner(issuer).run(
            context=context,
            authority=authority,
            batch=batch,
            adapter=adapter,
        )

    assert adapter.applied == ["preferred_name"]
    assert context.taint_reason == "effect_unknown:semantic_patch_batch:SemanticBatchDenied"
    with pytest.raises(SemanticBatchDenied, match="tainted"):
        SemanticPatchBatchRunner(issuer).run(
            context=context,
            authority=authority,
            batch=batch,
            adapter=adapter,
        )


@pytest.mark.parametrize(
    "changed_page",
    (
        BrowserPageObservation("https://tenant.myworkdayjobs.com/en-US/example/job/other", (), PAGE_SIGNATURE, 0),
        BrowserPageObservation(PAGE_URL, (0,), PAGE_SIGNATURE, 0),
    ),
)
def test_url_or_frame_change_after_signing_has_zero_writes(
    tmp_path: Path, changed_page: BrowserPageObservation
) -> None:
    context = _context(tmp_path)
    batch = _batch()
    issuer = BatchSemanticAuthorityIssuer()
    adapter = FakeAdapter(before_page=changed_page)
    authority = issuer.issue(context, batch)

    with pytest.raises(SemanticBatchExecutionError, match="context was drained"):
        SemanticPatchBatchRunner(issuer).run(
            context=context,
            authority=authority,
            batch=batch,
            adapter=adapter,
        )

    assert adapter.applied == []


def test_hmac_tamper_expiry_batch_recipe_and_epoch_fail_closed(tmp_path: Path) -> None:
    clock = [100.0]
    issuer = BatchSemanticAuthorityIssuer(ttl_seconds=5, clock=lambda: clock[0])
    context = _context(tmp_path)
    batch = _batch()
    authority = issuer.issue(context, batch)
    runner = SemanticPatchBatchRunner(issuer)

    with pytest.raises(SemanticBatchDenied, match="signature"):
        runner.run(context=context, authority=replace(authority, nonce="f" * 32), batch=batch, adapter=FakeAdapter())
    with pytest.raises(SemanticBatchDenied, match="signature"):
        runner.run(
            context=context,
            authority=replace(authority, page_binding=BatchPageBinding(PAGE_URL, (0,), PAGE_SIGNATURE, 0)),
            batch=batch,
            adapter=FakeAdapter(),
        )

    expired = issuer.issue(context, batch)
    clock[0] = 106.0
    with pytest.raises(SemanticBatchDenied, match="expired"):
        runner.run(context=context, authority=expired, batch=batch, adapter=FakeAdapter())

    clock[0] = 100.0
    different_batch = replace(batch, batch_id="batch-2")
    fresh = issuer.issue(context, batch)
    with pytest.raises(SemanticBatchDenied, match="binding mismatch"):
        runner.run(context=context, authority=fresh, batch=different_batch, adapter=FakeAdapter())

    different_recipe = replace(_recipe(), adapter_version="workday-semantic/v2")
    recipe_batch = replace(batch, recipe=different_recipe)
    fresh = issuer.issue(context, batch)
    with pytest.raises(SemanticBatchDenied, match="binding mismatch"):
        runner.run(context=context, authority=fresh, batch=recipe_batch, adapter=FakeAdapter())

    stale_context = _context(tmp_path, context_id="context-stale", page_epoch=1)
    with pytest.raises(SemanticBatchDenied, match="page binding"):
        issuer.issue(stale_context, batch)

    different_epoch = replace(batch, page_binding=BatchPageBinding(PAGE_URL, (), PAGE_SIGNATURE, 1))
    fresh = issuer.issue(context, batch)
    with pytest.raises(SemanticBatchDenied, match="binding mismatch"):
        runner.run(context=context, authority=fresh, batch=different_epoch, adapter=FakeAdapter())
    with pytest.raises(AttributeError, match="immutable"):
        context.page_binding = BatchPageBinding(PAGE_URL, (0,), PAGE_SIGNATURE, 0)


def test_context_registry_normalizes_paths_rejects_duplicate_context_and_closes_before_release(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "application.sqlite"
    connection = sqlite3.connect(sqlite_path)
    registry = BrowserContextRegistry()
    first = _context(tmp_path, sqlite_path=sqlite_path, profile_path=tmp_path / "profile", debug_port=9231)
    first_lease = registry.acquire(first)
    alias = _context(
        tmp_path,
        context_id="context-alias",
        sqlite_path=tmp_path / "nested" / ".." / "application.sqlite",
        profile_path=tmp_path / "nested" / ".." / "profile",
        debug_port=9232,
    )
    with pytest.raises(SemanticBatchDenied, match="sqlite path"):
        registry.acquire(alias)
    profile_alias = _context(
        tmp_path,
        context_id="context-profile-alias",
        sqlite_path=tmp_path / "other.sqlite",
        profile_path=tmp_path / "nested" / ".." / "profile",
        debug_port=9232,
    )
    with pytest.raises(SemanticBatchDenied, match="browser profile"):
        registry.acquire(profile_alias)
    port_alias = _context(
        tmp_path,
        context_id="context-port-alias",
        sqlite_path=tmp_path / "other.sqlite",
        profile_path=tmp_path / "other-profile",
        debug_port=9231,
    )
    with pytest.raises(SemanticBatchDenied, match="debug port"):
        registry.acquire(port_alias)
    with pytest.raises(SemanticBatchDenied, match="context_id"):
        registry.acquire(_context(tmp_path, debug_port=9233))
    close_attempts: list[str] = []
    with pytest.raises(SemanticBatchDenied, match="signature"):
        registry.release_after_close(
            first,
            BrowserContextLease(first.context_id, first.resources, first_lease.nonce, "bad"),
            close_resources=lambda: close_attempts.append("should-not-close"),
        )
    assert close_attempts == []
    with pytest.raises(SemanticBatchDenied, match="resources do not match"):
        registry.release_after_close(
            first,
            replace(
                first_lease,
                resources=BrowserResourceIdentity(str(tmp_path / "other.sqlite"), str(tmp_path / "profile"), 9231),
            ),
            close_resources=lambda: close_attempts.append("should-not-close"),
        )
    assert close_attempts == []
    registry._port_owners[first.resources.debug_port] = "other-context"  # prove ownership is checked before close
    with pytest.raises(SemanticBatchDenied, match="resources are not exactly owned"):
        registry.release_after_close(
            first,
            first_lease,
            close_resources=lambda: close_attempts.append("should-not-close"),
        )
    assert close_attempts == []
    registry._port_owners[first.resources.debug_port] = first.context_id

    closed: list[bool] = []

    def close_resources() -> None:
        connection.close()
        closed.append(True)

    registry.release_after_close(first, first_lease, close_resources=close_resources)
    assert closed == [True]
    with pytest.raises(SemanticBatchDenied, match="closed"):
        first.require_active()
    replacement = _context(tmp_path, context_id="context-replacement", debug_port=9231)
    registry.acquire(replacement)


def test_resource_identity_normalizes_windows_case_aliases_after_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(semantic_batch.os.path, "normcase", lambda value: value.casefold())
    first = BrowserResourceIdentity(
        str(tmp_path / "SQLite" / "Application.sqlite"),
        str(tmp_path / "Profiles" / "Default"),
        9231,
    )
    alias = BrowserResourceIdentity(
        str(tmp_path / "sqlite" / "application.SQLITE"),
        str(tmp_path / "profiles" / "default"),
        9231,
    )

    assert first == alias
