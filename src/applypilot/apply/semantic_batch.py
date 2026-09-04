"""Fail-closed, provider-bound semantic patch batches.

This module is deliberately a small composition boundary.  It does not own a
browser process, final submission, credentials, sensitive material, or a
worker scheduler.  A caller may use it to bind a small set of routine form
patches to one application context and an exact adapter recipe before handing
the patches to a provider-specific browser driver.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal
from urllib.parse import urlparse

from applypilot.apply.provider_registry import provider_matches_host


class SemanticBatchDenied(RuntimeError):
    """A batch, authority, or context failed its fail-closed admission."""


class BrowserContextTainted(SemanticBatchDenied):
    """The application context observed a failed semantic operation."""


class SemanticBatchExecutionError(SemanticBatchDenied):
    """A provider driver failed; its application context was drained."""


RecipeOperation = Literal[
    "observe_form",
    "apply_semantic_patch",
    "resolve_validation_errors",
    "upload_bound_artifact",
]

_RECIPE_OPERATIONS: frozenset[str] = frozenset(
    {
        "observe_form",
        "apply_semantic_patch",
        "resolve_validation_errors",
        "upload_bound_artifact",
    }
)
_FORBIDDEN_SEMANTIC_TOKENS: frozenset[str] = frozenset(
    {
        "bank",
        "biometric",
        "authorization",
        "credential",
        "credentials",
        "criminal",
        "date_of_birth",
        "disability",
        "dob",
        "driver_license",
        "ethnicity",
        "financial",
        "fin",
        "gender",
        "identity",
        "immigration",
        "legal",
        "nric",
        "passport",
        "password",
        "pronoun",
        "payment",
        "race",
        "security_answer",
        "salary",
        "social_security",
        "sponsorship",
        "ssn",
        "tax",
        "veteran",
        "work_authorization",
    }
)


def _required(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _hostname(value: object) -> str:
    raw = _required(value, "domain").casefold().strip(".")
    parsed = urlparse(f"https://{raw}")
    if parsed.hostname != raw or ":" in raw or "/" in raw:
        raise ValueError("domain must be a hostname")
    return raw


def _sha256(value: object, name: str) -> str:
    text = _required(value, name).casefold()
    if len(text) != 64 or any(item not in "0123456789abcdef" for item in text):
        raise ValueError(f"{name} must be a sha256 digest")
    return text


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _recipe_key(recipe: ApplicationRecipe) -> tuple[str, str, str, str]:
    return (
        recipe.provider,
        recipe.domain,
        recipe.adapter_version,
        recipe.page_signature,
    )


@dataclass(frozen=True, slots=True)
class ApplicationRecipe:
    """A provider adapter's exact, non-submit recipe for one page signature."""

    provider: str
    domain: str
    adapter_version: str
    page_signature: str
    operations: tuple[RecipeOperation, ...]

    def __post_init__(self) -> None:
        provider = _required(self.provider, "provider").casefold()
        domain = _hostname(self.domain)
        adapter_version = _required(self.adapter_version, "adapter_version")
        page_signature = _sha256(self.page_signature, "page_signature")
        if not self.operations or len(set(self.operations)) != len(self.operations):
            raise ValueError("operations must be a non-empty unique sequence")
        if any(operation not in _RECIPE_OPERATIONS for operation in self.operations):
            raise ValueError("recipe contains an unsupported or privileged operation")
        if not provider_matches_host(provider, domain, "semantic_upload") and not provider_matches_host(
            provider, domain, "control_write"
        ):
            raise ValueError("recipe domain is not admitted for its provider semantic capability")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "domain", domain)
        object.__setattr__(self, "adapter_version", adapter_version)
        object.__setattr__(self, "page_signature", page_signature)


@dataclass(frozen=True, slots=True)
class ProviderAdapter:
    """Versioned provider declaration containing only exact page recipes."""

    provider: str
    version: str
    recipes: tuple[ApplicationRecipe, ...]

    def __post_init__(self) -> None:
        provider = _required(self.provider, "provider").casefold()
        version = _required(self.version, "version")
        if not self.recipes:
            raise ValueError("ProviderAdapter requires at least one recipe")
        keys: set[tuple[str, str, str, str]] = set()
        for recipe in self.recipes:
            if recipe.provider != provider or recipe.adapter_version != version:
                raise ValueError("adapter recipes must bind the adapter provider and version")
            key = _recipe_key(recipe)
            if key in keys:
                raise ValueError("adapter recipes must be unique by provider/page identity")
            keys.add(key)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "version", version)

    def recipe_for(self, *, domain: str, page_signature: str) -> ApplicationRecipe:
        """Return the one exact recipe, never a domain-wide fallback."""

        requested_domain = _hostname(domain)
        requested_signature = _sha256(page_signature, "page_signature")
        matches = tuple(
            recipe
            for recipe in self.recipes
            if recipe.domain == requested_domain and recipe.page_signature == requested_signature
        )
        if len(matches) != 1:
            raise SemanticBatchDenied("no exact application recipe is admitted")
        return matches[0]


@dataclass(frozen=True, slots=True)
class SemanticPatch:
    """One routine, non-sensitive value bound to a declared field semantic."""

    field_semantic: str
    value: str

    def __post_init__(self) -> None:
        semantic = _required(self.field_semantic, "field_semantic").casefold()
        _required(self.value, "value")
        tokens = frozenset(part for part in semantic.replace("-", "_").split("_") if part)
        if tokens & _FORBIDDEN_SEMANTIC_TOKENS:
            raise ValueError("sensitive or credential field semantics are not batchable")
        object.__setattr__(self, "field_semantic", semantic)


@dataclass(frozen=True, slots=True)
class SemanticPatchBatch:
    """An immutable, exact-page group of routine semantic form patches."""

    batch_id: str
    attempt_id: str
    recipe: ApplicationRecipe
    patches: tuple[SemanticPatch, ...]
    expected_page_epoch: int = 0

    def __post_init__(self) -> None:
        _required(self.batch_id, "batch_id")
        _required(self.attempt_id, "attempt_id")
        if not self.patches:
            raise ValueError("SemanticPatchBatch requires at least one patch")
        if (
            isinstance(self.expected_page_epoch, bool)
            or not isinstance(self.expected_page_epoch, int)
            or self.expected_page_epoch < 0
        ):
            raise ValueError("expected_page_epoch must be a non-negative integer")
        names = tuple(patch.field_semantic for patch in self.patches)
        if len(set(names)) != len(names):
            raise ValueError("SemanticPatchBatch cannot write a field twice")
        if "apply_semantic_patch" not in self.recipe.operations:
            raise ValueError("recipe does not admit semantic patches")

    @property
    def digest(self) -> str:
        """Stable identity used to bind a one-shot authority to this exact batch."""

        return hashlib.sha256(
            _canonical_json(
                {
                    "attempt_id": self.attempt_id,
                    "batch_id": self.batch_id,
                    "patches": [{"field_semantic": item.field_semantic, "value": item.value} for item in self.patches],
                    "recipe": {
                        "adapter_version": self.recipe.adapter_version,
                        "domain": self.recipe.domain,
                        "operations": self.recipe.operations,
                        "page_signature": self.recipe.page_signature,
                        "provider": self.recipe.provider,
                    },
                    "expected_page_epoch": self.expected_page_epoch,
                    "submit_authority": False,
                }
            )
        ).hexdigest()


@dataclass(slots=True)
class BrowserContext:
    """One application-only browser context with terminal taint/drain state."""

    context_id: str
    attempt_id: str
    sqlite_path: str
    profile_id: str
    debug_port: int
    provider: str
    domain: str
    page_signature: str
    page_epoch: int = 0
    taint_reason: str | None = None
    drained: bool = False

    def __post_init__(self) -> None:
        self.context_id = _required(self.context_id, "context_id")
        self.attempt_id = _required(self.attempt_id, "attempt_id")
        self.sqlite_path = _required(self.sqlite_path, "sqlite_path")
        self.profile_id = _required(self.profile_id, "profile_id")
        if (
            isinstance(self.debug_port, bool)
            or not isinstance(self.debug_port, int)
            or not 1 <= self.debug_port <= 65535
        ):
            raise ValueError("debug_port must be a TCP port")
        self.provider = _required(self.provider, "provider").casefold()
        self.domain = _hostname(self.domain)
        self.page_signature = _sha256(self.page_signature, "page_signature")
        if isinstance(self.page_epoch, bool) or not isinstance(self.page_epoch, int) or self.page_epoch < 0:
            raise ValueError("page_epoch must be a non-negative integer")
        if self.taint_reason is not None:
            self.taint_reason = _required(self.taint_reason, "taint_reason")
        if self.drained and self.taint_reason is None:
            raise ValueError("only a tainted browser context may be drained")

    @property
    def active(self) -> bool:
        return self.taint_reason is None and not self.drained

    def require_active(self) -> None:
        if self.taint_reason is not None:
            raise BrowserContextTainted("browser context is tainted")
        if self.drained:
            raise SemanticBatchDenied("browser context is drained")

    def taint(self, reason: str) -> None:
        if self.drained:
            raise SemanticBatchDenied("cannot taint a drained browser context")
        self.taint_reason = _required(reason, "taint_reason")

    def drain(self) -> None:
        if self.taint_reason is None:
            raise SemanticBatchDenied("only a tainted browser context may be drained")
        self.drained = True


class BrowserContextRegistry:
    """Reserve testable application-local SQLite, profile, and port identities.

    This is a logical ownership boundary, not a browser launcher or a SQLite
    connection factory.  The composition root remains responsible for opening
    the resources after it has acquired them here.
    """

    def __init__(self) -> None:
        self._sqlite_owners: dict[str, str] = {}
        self._profile_owners: dict[str, str] = {}
        self._port_owners: dict[int, str] = {}

    def acquire(self, context: BrowserContext) -> None:
        context.require_active()
        requested = (
            (self._sqlite_owners, context.sqlite_path, "sqlite path"),
            (self._profile_owners, context.profile_id, "browser profile"),
            (self._port_owners, context.debug_port, "debug port"),
        )
        for owners, resource_id, resource_name in requested:
            owner = owners.get(resource_id)
            if owner is not None and owner != context.context_id:
                raise SemanticBatchDenied(f"{resource_name} is already owned by another application context")
        for owners, resource_id, _resource_name in requested:
            owners[resource_id] = context.context_id

    def release_drained(self, context: BrowserContext) -> None:
        if not context.drained:
            raise SemanticBatchDenied("only a drained browser context may release resources")
        for owners, resource_id in (
            (self._sqlite_owners, context.sqlite_path),
            (self._profile_owners, context.profile_id),
            (self._port_owners, context.debug_port),
        ):
            if owners.get(resource_id) == context.context_id:
                del owners[resource_id]


@dataclass(frozen=True, slots=True)
class BatchSemanticAuthority:
    """Non-serializable, one-shot authority for one exact semantic patch batch."""

    context_id: str
    attempt_id: str
    batch_digest: str
    recipe: ApplicationRecipe
    expires_at: float
    nonce: str
    submit_authority: bool
    signature: str

    def __reduce__(self) -> object:
        raise TypeError("batch semantic authority cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("batch semantic authority cannot be serialized")


class BatchSemanticAuthorityIssuer:
    """Parent-owned HMAC issuer that binds one batch to one application context."""

    def __init__(self, *, ttl_seconds: float = 60.0, clock: Callable[[], float] = time.time) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._secret = secrets.token_bytes(32)
        self._issued: dict[str, float] = {}

    def issue(self, context: BrowserContext, batch: SemanticPatchBatch) -> BatchSemanticAuthority:
        context.require_active()
        self._validate_context_batch(context, batch)
        authority = BatchSemanticAuthority(
            context_id=context.context_id,
            attempt_id=context.attempt_id,
            batch_digest=batch.digest,
            recipe=batch.recipe,
            expires_at=self._clock() + self._ttl_seconds,
            nonce=secrets.token_hex(16),
            submit_authority=False,
            signature="",
        )
        signed = replace(authority, signature=self._sign(authority))
        self._issued[signed.nonce] = signed.expires_at
        return signed

    def consume(self, authority: BatchSemanticAuthority, context: BrowserContext, batch: SemanticPatchBatch) -> None:
        context.require_active()
        self._validate_context_batch(context, batch)
        if not isinstance(authority, BatchSemanticAuthority):
            raise SemanticBatchDenied("batch semantic authority has the wrong type")
        if authority.submit_authority is not False:
            raise SemanticBatchDenied("semantic batch authority must never authorize submit")
        expected = replace(authority, signature="")
        if not hmac.compare_digest(authority.signature, self._sign(expected)):
            raise SemanticBatchDenied("batch semantic authority signature is invalid")
        if self._clock() >= authority.expires_at:
            raise SemanticBatchDenied("batch semantic authority expired")
        if (
            authority.context_id,
            authority.attempt_id,
            authority.batch_digest,
            authority.recipe,
        ) != (context.context_id, context.attempt_id, batch.digest, batch.recipe):
            raise SemanticBatchDenied("batch semantic authority binding mismatch")
        expiry = self._issued.pop(authority.nonce, None)
        if expiry != authority.expires_at:
            raise SemanticBatchDenied("batch semantic authority was already consumed or was not issued here")

    @staticmethod
    def _validate_context_batch(context: BrowserContext, batch: SemanticPatchBatch) -> None:
        if context.attempt_id != batch.attempt_id:
            raise SemanticBatchDenied("batch does not belong to this application context")
        if (context.provider, context.domain, context.page_signature) != (
            batch.recipe.provider,
            batch.recipe.domain,
            batch.recipe.page_signature,
        ):
            raise SemanticBatchDenied("batch recipe does not match the current application page")
        if context.page_epoch != batch.expected_page_epoch:
            raise SemanticBatchDenied("batch page epoch is stale")

    def _sign(self, authority: BatchSemanticAuthority) -> str:
        return hmac.new(
            self._secret,
            _canonical_json(
                {
                    "attempt_id": authority.attempt_id,
                    "batch_digest": authority.batch_digest,
                    "context_id": authority.context_id,
                    "expires_at": authority.expires_at,
                    "nonce": authority.nonce,
                    "recipe": {
                        "adapter_version": authority.recipe.adapter_version,
                        "domain": authority.recipe.domain,
                        "operations": authority.recipe.operations,
                        "page_signature": authority.recipe.page_signature,
                        "provider": authority.recipe.provider,
                    },
                    "submit_authority": authority.submit_authority,
                }
            ),
            hashlib.sha256,
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class SemanticPatchBatchResult:
    """A successful local driver result; it carries no submission result."""

    batch_id: str
    batch_digest: str
    applied_fields: tuple[str, ...]
    next_page_epoch: int
    submit_authority: bool = False

    def __post_init__(self) -> None:
        if self.submit_authority is not False:
            raise ValueError("semantic batch results must not grant submit authority")


class SemanticPatchBatchRunner:
    """Run a batch via a provider driver and terminally drain on any driver error."""

    def __init__(self, issuer: BatchSemanticAuthorityIssuer) -> None:
        self._issuer = issuer

    def run(
        self,
        *,
        context: BrowserContext,
        authority: BatchSemanticAuthority,
        batch: SemanticPatchBatch,
        apply_patch: Callable[[SemanticPatch], None],
    ) -> SemanticPatchBatchResult:
        self._issuer.consume(authority, context, batch)
        try:
            for patch in batch.patches:
                apply_patch(patch)
            if context.page_epoch != batch.expected_page_epoch:
                raise RuntimeError("page_epoch_changed_after_effect")
        except Exception as exc:
            context.taint(f"effect_unknown:semantic_patch_failed:{type(exc).__name__}")
            context.drain()
            raise SemanticBatchExecutionError("semantic patch execution failed; context was drained") from exc
        context.page_epoch += 1
        return SemanticPatchBatchResult(
            batch_id=batch.batch_id,
            batch_digest=batch.digest,
            applied_fields=tuple(patch.field_semantic for patch in batch.patches),
            next_page_epoch=context.page_epoch,
        )


__all__ = [
    "ApplicationRecipe",
    "BatchSemanticAuthority",
    "BatchSemanticAuthorityIssuer",
    "BrowserContext",
    "BrowserContextRegistry",
    "BrowserContextTainted",
    "ProviderAdapter",
    "SemanticBatchDenied",
    "SemanticBatchExecutionError",
    "SemanticPatch",
    "SemanticPatchBatch",
    "SemanticPatchBatchResult",
    "SemanticPatchBatchRunner",
]
