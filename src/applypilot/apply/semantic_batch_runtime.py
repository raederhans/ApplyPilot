"""Feature-gated production orchestration for routine semantic patch batches."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from applypilot.apply.semantic_batch import (
    DEFAULT_SEMANTIC_BATCH_ADAPTER_REGISTRY,
    ApplicationRecipe,
    BatchPageBinding,
    BatchSemanticAuthorityIssuer,
    BrowserContext,
    BrowserContextRegistry,
    BrowserResourceIdentity,
    ProviderSemanticPatchAdapter,
    SemanticBatchAdapterRegistry,
    SemanticBatchDenied,
    SemanticBatchExecutionError,
    SemanticPatch,
    SemanticPatchBatch,
    SemanticPatchBatchRunner,
)
from applypilot.storage import semantic_patch_batches as journal

SemanticBatchMode = Literal["off", "shadow", "canary"]

# The repository has no cross-process durable authority secret.  Keeping this
# key process-local makes same-process retries comparable while guaranteeing
# that records from a previous process cannot be admitted as replay.
_REPLAY_DIGEST_KEY = secrets.token_bytes(32)


def _replay_key_id() -> str:
    return hmac.new(
        _REPLAY_DIGEST_KEY,
        b"applypilot.semantic-batch.replay-key-id/v1",
        hashlib.sha256,
    ).hexdigest()


def _keyed_replay_digest(label: str, value: object) -> str:
    payload = (
        label.encode("ascii")
        + b"\0"
        + json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return hmac.new(_REPLAY_DIGEST_KEY, payload, hashlib.sha256).hexdigest()


class ProductionSemanticPatchAdapter(ProviderSemanticPatchAdapter, Protocol):
    """Runtime adapter extension needed for crash-safe fallback decisions."""

    @property
    def effect_count(self) -> int: ...

    def bind_effect_sink(self, sink: Callable[[], None]) -> None: ...

    def pristine(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class SemanticBatchRuntimeRequest:
    mode: SemanticBatchMode
    attempt_id: str
    actor_id: str
    provider: str
    adapter_version: str
    page_binding: BatchPageBinding
    page_id: str
    page_lease_id: str
    page_lease_epoch: int
    resources: BrowserResourceIdentity
    patches: tuple[SemanticPatch, ...]
    replay_key_id: str = field(init=False, repr=False)
    page_identity_digest: str = field(init=False, repr=False)
    patch_payload_digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.mode not in {"off", "shadow", "canary"}:
            raise ValueError("semantic batch mode is invalid")
        if not self.attempt_id or not self.actor_id:
            raise ValueError("semantic batch runtime identity is incomplete")
        if DEFAULT_SEMANTIC_BATCH_ADAPTER_REGISTRY.registration_for(self.provider) is None:
            raise ValueError("semantic batch provider is unsupported")
        if not self.adapter_version:
            raise ValueError("semantic batch adapter version is required")
        if not self.page_id or not self.page_lease_id or self.page_lease_epoch < 1:
            raise ValueError("semantic batch page lease is incomplete")
        if not self.patches:
            raise ValueError("semantic batch runtime requires patches")
        object.__setattr__(self, "replay_key_id", _replay_key_id())
        object.__setattr__(
            self,
            "page_identity_digest",
            _keyed_replay_digest(
                "page-identity/v1",
                {
                    "page_url": self.page_binding.page_url,
                    "frame_path": self.page_binding.frame_path,
                },
            ),
        )
        object.__setattr__(
            self,
            "patch_payload_digest",
            _keyed_replay_digest(
                "patch-payload/v1",
                [
                    {
                        "semantic": patch.field_semantic,
                        "value": patch.value,
                    }
                    for patch in self.patches
                ],
            ),
        )

    @property
    def semantics(self) -> tuple[str, ...]:
        return tuple(sorted(patch.field_semantic for patch in self.patches))

    @property
    def batch_id(self) -> str:
        payload = {
            "mode": self.mode,
            "attempt_id": self.attempt_id,
            "provider": self.provider,
            "adapter_version": self.adapter_version,
            "page_id": self.page_id,
            "page_lease_id": self.page_lease_id,
            "page_lease_epoch": self.page_lease_epoch,
            "page_epoch": self.page_binding.page_epoch,
            "page_signature": self.page_binding.page_signature,
            "replay_key_id": self.replay_key_id,
            "page_identity_digest": self.page_identity_digest,
            "patch_payload_digest": self.patch_payload_digest,
            "semantics": self.semantics,
            "submit_authority": False,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"semantic-batch:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class SemanticBatchRuntimeResult:
    status: str
    mode: SemanticBatchMode
    batch_id: str | None
    candidate_count: int
    effect_count: int
    legacy_fallback_safe: bool
    reason_code: str
    submit_authority: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "mode": self.mode,
            "batch_id": self.batch_id,
            "candidate_count": self.candidate_count,
            "effect_count": self.effect_count,
            "legacy_fallback_safe": self.legacy_fallback_safe,
            "reason_code": self.reason_code,
            "submit_authority": False,
        }


def _result(
    request: SemanticBatchRuntimeRequest,
    status: str,
    *,
    effect_count: int = 0,
    safe: bool = False,
    reason: str,
) -> SemanticBatchRuntimeResult:
    return SemanticBatchRuntimeResult(
        status=status,
        mode=request.mode,
        batch_id=request.batch_id,
        candidate_count=len(request.patches),
        effect_count=effect_count,
        legacy_fallback_safe=safe,
        reason_code=reason,
    )


def _claims(request: SemanticBatchRuntimeRequest) -> journal.SemanticPatchBatchClaims:
    return journal.SemanticPatchBatchClaims(
        batch_id=request.batch_id,
        attempt_id=request.attempt_id,
        actor_id=request.actor_id,
        provider=request.provider,
        adapter_version=request.adapter_version,
        page_id=request.page_id,
        page_lease_id=request.page_lease_id,
        page_lease_epoch=request.page_lease_epoch,
        expected_page_epoch=request.page_binding.page_epoch,
        page_signature=request.page_binding.page_signature,
        replay_key_id=request.replay_key_id,
        page_identity_digest=request.page_identity_digest,
        patch_payload_digest=request.patch_payload_digest,
        semantics=request.semantics,
    )


def _batch(request: SemanticBatchRuntimeRequest) -> SemanticPatchBatch:
    recipe = ApplicationRecipe(
        provider=request.provider,
        domain=request.page_binding.page_url.split("/", 3)[2].split(":", 1)[0],
        adapter_version=request.adapter_version,
        page_signature=request.page_binding.page_signature,
        operations=("apply_semantic_patch",),
    )
    return SemanticPatchBatch(
        batch_id=request.batch_id,
        attempt_id=request.attempt_id,
        recipe=recipe,
        page_binding=request.page_binding,
        patches=request.patches,
    )


def _same_replay_authority(
    request: SemanticBatchRuntimeRequest,
    prior: journal.SemanticPatchBatchRecord,
) -> bool:
    if (
        prior.attempt_id,
        prior.actor_id,
        prior.provider,
        prior.adapter_version,
        prior.page_id,
        prior.page_lease_id,
        prior.page_lease_epoch,
        prior.page_signature,
    ) != (
        request.attempt_id,
        request.actor_id,
        request.provider,
        request.adapter_version,
        request.page_id,
        request.page_lease_id,
        request.page_lease_epoch,
        request.page_binding.page_signature,
    ):
        return False
    for stored, current in (
        (prior.replay_key_id, request.replay_key_id),
        (prior.page_identity_digest, request.page_identity_digest),
        (prior.patch_payload_digest, request.patch_payload_digest),
    ):
        if not isinstance(stored, str) or not hmac.compare_digest(stored, current):
            return False
    expected_epoch = prior.resulting_page_epoch if prior.state == "verified" else prior.expected_page_epoch
    return expected_epoch == request.page_binding.page_epoch


def _confirm_replay_page(
    request: SemanticBatchRuntimeRequest,
    batch: SemanticPatchBatch,
    adapter: ProductionSemanticPatchAdapter,
) -> None:
    if (adapter.provider.casefold(), adapter.adapter_version) != (
        batch.recipe.provider,
        batch.recipe.adapter_version,
    ):
        raise ValueError("semantic batch replay adapter mismatch")
    SemanticPatchBatchRunner._validate_page(
        request.page_binding,
        adapter.observe_page(),
    )


def _shadow_compare(
    request: SemanticBatchRuntimeRequest,
    batch: SemanticPatchBatch,
    adapter: ProductionSemanticPatchAdapter,
) -> None:
    if (adapter.provider.casefold(), adapter.adapter_version) != (
        batch.recipe.provider,
        batch.recipe.adapter_version,
    ):
        raise ValueError("semantic batch adapter recipe mismatch")
    for patch in batch.patches:
        SemanticPatchBatchRunner._validate_page(
            request.page_binding,
            adapter.observe_page(),
        )
        control = adapter.control_for(patch.field_semantic)
        SemanticPatchBatchRunner._validate_control(
            request.page_binding,
            patch,
            control,
        )


def run_production_semantic_batch(
    request: SemanticBatchRuntimeRequest,
    *,
    adapter: ProductionSemanticPatchAdapter,
    adapter_registry: SemanticBatchAdapterRegistry = DEFAULT_SEMANTIC_BATCH_ADAPTER_REGISTRY,
    connection: sqlite3.Connection,
    close_resources: Callable[[], None],
    advance_page: Callable[[int], int],
) -> SemanticBatchRuntimeResult:
    """Compare or execute one batch without ever granting submit authority."""

    if request.mode == "off":
        return SemanticBatchRuntimeResult(
            status="off",
            mode="off",
            batch_id=None,
            candidate_count=0,
            effect_count=0,
            legacy_fallback_safe=True,
            reason_code="feature_disabled",
        )
    try:
        adapter_registry.require_execution(request.provider, request.adapter_version)
    except SemanticBatchDenied:
        return _result(
            request,
            "not_applicable",
            safe=True,
            reason="provider_capability_disabled",
        )
    batch = _batch(request)
    claims = _claims(request)
    registry = BrowserContextRegistry()
    context = BrowserContext(
        context_id=request.batch_id,
        attempt_id=request.attempt_id,
        resources=request.resources,
        provider=request.provider,
        page_binding=request.page_binding,
    )
    lease = registry.acquire(context)
    resources_released = False

    def release() -> None:
        nonlocal resources_released
        if resources_released:
            return
        registry.release_after_close(
            context,
            lease,
            close_resources=close_resources,
        )
        resources_released = True

    if request.mode == "shadow":
        status = "shadow_match"
        reason = "shadow_parity"
        try:
            _shadow_compare(request, batch, adapter)
        except Exception as exc:  # noqa: BLE001 - shadow must remain effect-free
            status = "shadow_mismatch"
            reason = f"shadow_{type(exc).__name__.casefold()}"[:120]
        finally:
            release()
        journal.begin_batch(connection, claims, shadow=True)
        return _result(request, status, safe=True, reason=reason)

    prior = journal.latest_attempt_semantics(
        connection,
        attempt_id=request.attempt_id,
        semantics_digest=claims.semantics_digest,
    )
    if prior is not None:
        if not _same_replay_authority(request, prior):
            release()
            return _result(
                request,
                "parked",
                effect_count=prior.effect_count,
                reason="prior_semantics_replay_identity_mismatch",
            )
        if prior.state == "verified":
            try:
                _confirm_replay_page(request, batch, adapter)
            except Exception:  # noqa: BLE001 - replay requires current live proof
                release()
                return _result(
                    request,
                    "parked",
                    effect_count=prior.effect_count,
                    reason="replay_page_authority_unconfirmed",
                )
            release()
            return _result(
                request,
                "replayed",
                effect_count=prior.effect_count,
                reason="durable_verified_replay",
            )
        release()
        if prior.state == "failed_no_effect":
            return _result(
                request,
                "fallback",
                safe=True,
                reason="durable_no_effect_fallback",
            )
        if prior.state == "started" and prior.dispatch_count == 0:
            journal.claim_dispatch(connection, prior.batch_id)
            journal.finish_batch(
                connection,
                prior.batch_id,
                state="failed_no_effect",
                expected_effect_count=0,
                reason_code="interrupted_before_dispatch",
            )
            return _result(
                request,
                "fallback",
                safe=True,
                reason="interrupted_before_dispatch",
            )
        if prior.state == "started":
            journal.finish_batch(
                connection,
                prior.batch_id,
                state="parked_side_effect_unknown",
                expected_effect_count=prior.effect_count,
                reason_code="interrupted_after_dispatch",
            )
        return _result(
            request,
            "parked",
            effect_count=prior.effect_count,
            reason="durable_effect_state_not_replayable",
        )

    record = journal.begin_batch(connection, claims)
    if record.state != "started" or record.dispatch_count != 0:
        release()
        return _result(
            request,
            "parked",
            effect_count=record.effect_count,
            reason="batch_claim_not_pristine",
        )
    journal.claim_dispatch(connection, request.batch_id)

    def note_effect() -> None:
        journal.note_effect(
            connection,
            request.batch_id,
            expected_effect_count=adapter.effect_count - 1,
        )

    adapter.bind_effect_sink(note_effect)
    resulting_epoch: int | None = None
    execution_error: Exception | None = None
    safe_no_effect = False
    try:
        issuer = BatchSemanticAuthorityIssuer()
        authority = issuer.issue(context, batch)
        SemanticPatchBatchRunner(issuer).run(
            context=context,
            authority=authority,
            batch=batch,
            adapter=adapter,
        )
        resulting_epoch = (
            advance_page(request.page_binding.page_epoch) if adapter.effect_count else request.page_binding.page_epoch
        )
    except Exception as exc:  # noqa: BLE001 - result is classified below
        execution_error = exc
        try:
            safe_no_effect = adapter.effect_count == 0 and adapter.pristine()
        except Exception:  # noqa: BLE001 - unavailable proof is unsafe
            safe_no_effect = False
    try:
        release()
    except Exception as exc:  # noqa: BLE001 - teardown uncertainty parks the batch
        execution_error = execution_error or exc
        safe_no_effect = False

    if execution_error is None and resulting_epoch is not None:
        journal.finish_batch(
            connection,
            request.batch_id,
            state="verified",
            expected_effect_count=adapter.effect_count,
            resulting_page_epoch=resulting_epoch,
            reason_code="verified",
        )
        return _result(
            request,
            "verified",
            effect_count=adapter.effect_count,
            reason="verified",
        )
    if safe_no_effect:
        journal.finish_batch(
            connection,
            request.batch_id,
            state="failed_no_effect",
            expected_effect_count=0,
            reason_code="adapter_failed_pristine",
        )
        return _result(
            request,
            "fallback",
            safe=True,
            reason="adapter_failed_pristine",
        )
    state = "parked_stale_after_effect" if adapter.effect_count else "parked_side_effect_unknown"
    reason = (
        "page_cas_or_teardown_failed"
        if isinstance(execution_error, SemanticBatchExecutionError)
        else "adapter_effect_unknown"
    )
    journal.finish_batch(
        connection,
        request.batch_id,
        state=state,
        expected_effect_count=adapter.effect_count,
        reason_code=reason,
    )
    return _result(
        request,
        "parked",
        effect_count=adapter.effect_count,
        reason=reason,
    )


__all__ = [
    "ProductionSemanticPatchAdapter",
    "SemanticBatchMode",
    "SemanticBatchRuntimeRequest",
    "SemanticBatchRuntimeResult",
    "run_production_semantic_batch",
]
