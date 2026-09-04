"""Foundation contracts for fail-closed, provider-bound semantic patch batches.

This module is intentionally not wired into the production worker. It owns
only a narrow future composition boundary: a batch can call an adapter's
routine-control capability after rechecking its exact page, frame, descriptor
classification, and one-shot authority. It cannot navigate, enter a frame,
handle credentials or sensitive fields, or submit an application.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable
from urllib.parse import urlparse, urlsplit, urlunsplit

from applypilot.apply.provider_registry import provider_matches_host


class SemanticBatchDenied(RuntimeError):
    """A batch, authority, adapter, or context failed a fail-closed check."""


class BrowserContextTainted(SemanticBatchDenied):
    """The application context observed an unsafe or uncertain operation."""


class SemanticBatchExecutionError(SemanticBatchDenied):
    """A semantic batch stopped and terminally drained its context."""


RecipeOperation = Literal[
    "observe_form",
    "apply_semantic_patch",
    "resolve_validation_errors",
    "upload_bound_artifact",
]
ControlClassification = Literal["routine", "sensitive", "navigation", "frame", "final_submit"]

_RECIPE_OPERATIONS = frozenset(
    {"observe_form", "apply_semantic_patch", "resolve_validation_errors", "upload_bound_artifact"}
)
_ROUTINE_FIELD_SEMANTICS = frozenset(
    {"city", "country", "email", "phone", "portfolio_url", "preferred_name", "postal_code", "state"}
)
_FORBIDDEN_SEMANTIC_TOKENS = frozenset(
    {
        "authorization",
        "bank",
        "biometric",
        "citizenship",
        "code",
        "credential",
        "criminal",
        "date",
        "declaration",
        "disability",
        "dob",
        "ethnicity",
        "financial",
        "fin",
        "frame",
        "gender",
        "identity",
        "immigration",
        "legal",
        "navigate",
        "navigation",
        "nric",
        "otp",
        "passport",
        "password",
        "payment",
        "pronoun",
        "race",
        "salary",
        "security",
        "sponsorship",
        "submit",
        "tax",
        "veteran",
    }
)
_CLASSIFICATIONS = frozenset({"routine", "sensitive", "navigation", "frame", "final_submit"})


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


def _https_url(value: object, name: str) -> str:
    url = _required(value, name)
    parsed = urlsplit(url)
    if parsed.scheme.casefold() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{name} must be an HTTPS URL without credentials")
    host = parsed.hostname.casefold().rstrip(".")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{name} has an invalid port") from exc
    netloc = host if port in {None, 443} else f"{host}:{port}"
    return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, parsed.fragment))


def _sha256(value: object, name: str) -> str:
    text = _required(value, name).casefold()
    if len(text) != 64 or any(item not in "0123456789abcdef" for item in text):
        raise ValueError(f"{name} must be a sha256 digest")
    return text


def _page_epoch(value: object, name: str = "page_epoch") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _frame_path(value: object) -> tuple[int, ...]:
    if not isinstance(value, tuple) or any(
        isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in value
    ):
        raise ValueError("frame_path must be a tuple of non-negative integers")
    return value


def _absolute_path(value: object, name: str) -> str:
    return os.path.normcase(str(Path(_required(value, name)).expanduser().resolve(strict=False)))


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def normalize_field_semantic(value: object) -> str:
    """Normalize whitespace, punctuation, and camelCase before policy checks."""

    text = unicodedata.normalize("NFKC", _required(value, "field_semantic"))
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").casefold()
    if not text:
        raise ValueError("field_semantic is required")
    return text


@dataclass(frozen=True, slots=True)
class ApplicationRecipe:
    """One adapter version's exact, explicitly non-submit page recipe."""

    provider: str
    domain: str
    adapter_version: str
    page_signature: str
    operations: tuple[RecipeOperation, ...]

    def __post_init__(self) -> None:
        provider = _required(self.provider, "provider").casefold()
        domain = _hostname(self.domain)
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
        object.__setattr__(self, "adapter_version", _required(self.adapter_version, "adapter_version"))
        object.__setattr__(self, "page_signature", _sha256(self.page_signature, "page_signature"))


@dataclass(frozen=True, slots=True)
class ProviderAdapter:
    """Versioned declaration of exact recipes; it has no browser capability."""

    provider: str
    version: str
    recipes: tuple[ApplicationRecipe, ...]

    def __post_init__(self) -> None:
        provider = _required(self.provider, "provider").casefold()
        version = _required(self.version, "version")
        if not self.recipes:
            raise ValueError("ProviderAdapter requires at least one recipe")
        identities: set[tuple[str, str, str, str]] = set()
        for recipe in self.recipes:
            if recipe.provider != provider or recipe.adapter_version != version:
                raise ValueError("adapter recipes must bind the adapter provider and version")
            identity = (recipe.provider, recipe.domain, recipe.adapter_version, recipe.page_signature)
            if identity in identities:
                raise ValueError("adapter recipes must be unique by exact page identity")
            identities.add(identity)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "version", version)

    def recipe_for(self, *, domain: str, page_signature: str) -> ApplicationRecipe:
        matches = tuple(
            recipe
            for recipe in self.recipes
            if recipe.domain == _hostname(domain) and recipe.page_signature == _sha256(page_signature, "page_signature")
        )
        if len(matches) != 1:
            raise SemanticBatchDenied("no exact application recipe is admitted")
        return matches[0]


@dataclass(frozen=True, slots=True)
class SemanticPatch:
    """One allowlisted routine field. Unknown semantics fail closed."""

    field_semantic: str
    value: str

    def __post_init__(self) -> None:
        semantic = normalize_field_semantic(self.field_semantic)
        _required(self.value, "value")
        tokens = frozenset(semantic.split("_"))
        if tokens & _FORBIDDEN_SEMANTIC_TOKENS or semantic not in _ROUTINE_FIELD_SEMANTICS:
            raise ValueError("sensitive, privileged, or unrecognized field semantic is not batchable")
        object.__setattr__(self, "field_semantic", semantic)


@dataclass(frozen=True, slots=True)
class BatchPageBinding:
    """Immutable full-page identity signed into a batch and its authority."""

    page_url: str
    frame_path: tuple[int, ...]
    page_signature: str
    page_epoch: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "page_url", _https_url(self.page_url, "page_url"))
        object.__setattr__(self, "frame_path", _frame_path(self.frame_path))
        object.__setattr__(self, "page_signature", _sha256(self.page_signature, "page_signature"))
        _page_epoch(self.page_epoch)


@dataclass(frozen=True, slots=True)
class BrowserResourceIdentity:
    """Immutable normalized resource tuple held by a registry lease."""

    sqlite_path: str
    profile_path: str
    debug_port: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "sqlite_path", _absolute_path(self.sqlite_path, "sqlite_path"))
        object.__setattr__(self, "profile_path", _absolute_path(self.profile_path, "profile_path"))
        if (
            isinstance(self.debug_port, bool)
            or not isinstance(self.debug_port, int)
            or not 1 <= self.debug_port <= 65535
        ):
            raise ValueError("debug_port must be a TCP port")


@dataclass(frozen=True, slots=True)
class SemanticPatchBatch:
    """An immutable routine-only batch for one exact application page epoch."""

    batch_id: str
    attempt_id: str
    recipe: ApplicationRecipe
    page_binding: BatchPageBinding
    patches: tuple[SemanticPatch, ...]

    def __post_init__(self) -> None:
        _required(self.batch_id, "batch_id")
        _required(self.attempt_id, "attempt_id")
        if not self.patches:
            raise ValueError("SemanticPatchBatch requires at least one patch")
        if len({patch.field_semantic for patch in self.patches}) != len(self.patches):
            raise ValueError("SemanticPatchBatch cannot write a field twice")
        if "apply_semantic_patch" not in self.recipe.operations:
            raise ValueError("recipe does not admit semantic patches")

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            _canonical_json(
                {
                    "attempt_id": self.attempt_id,
                    "batch_id": self.batch_id,
                    "page_binding": {
                        "page_url": self.page_binding.page_url,
                        "frame_path": self.page_binding.frame_path,
                        "page_signature": self.page_binding.page_signature,
                        "page_epoch": self.page_binding.page_epoch,
                    },
                    "patches": [{"semantic": item.field_semantic, "value": item.value} for item in self.patches],
                    "recipe": {
                        "provider": self.recipe.provider,
                        "domain": self.recipe.domain,
                        "adapter_version": self.recipe.adapter_version,
                        "page_signature": self.recipe.page_signature,
                        "operations": self.recipe.operations,
                    },
                    "submit_authority": False,
                }
            )
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class BrowserPageObservation:
    """Current adapter-observed root URL, frame, page signature, and epoch."""

    page_url: str
    frame_path: tuple[int, ...]
    page_signature: str
    page_epoch: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "page_url", _https_url(self.page_url, "page_url"))
        object.__setattr__(self, "frame_path", _frame_path(self.frame_path))
        object.__setattr__(self, "page_signature", _sha256(self.page_signature, "page_signature"))
        _page_epoch(self.page_epoch)

    @property
    def binding(self) -> BatchPageBinding:
        return BatchPageBinding(self.page_url, self.frame_path, self.page_signature, self.page_epoch)


@dataclass(frozen=True, slots=True)
class BatchControlDescriptor:
    """Adapter-projected descriptor; only a matching ``routine`` control can run."""

    control_id: str
    field_semantic: str
    classification: ControlClassification
    page: BrowserPageObservation

    def __post_init__(self) -> None:
        object.__setattr__(self, "control_id", _required(self.control_id, "control_id"))
        object.__setattr__(self, "field_semantic", normalize_field_semantic(self.field_semantic))
        if self.classification not in _CLASSIFICATIONS:
            raise ValueError("unsupported control classification")


@runtime_checkable
class ProviderSemanticPatchAdapter(Protocol):
    """The only browser capability accepted by ``SemanticPatchBatchRunner``."""

    provider: str
    adapter_version: str

    def observe_page(self) -> BrowserPageObservation: ...

    def control_for(self, field_semantic: str) -> BatchControlDescriptor: ...

    def apply_routine_control(self, control: BatchControlDescriptor, value: str) -> None: ...


@dataclass(slots=True)
class BrowserContext:
    """One application-owned context; taint requires terminal draining."""

    context_id: str
    attempt_id: str
    resources: BrowserResourceIdentity
    provider: str
    page_binding: BatchPageBinding
    taint_reason: str | None = None
    drained: bool = False
    closed: bool = False

    def __setattr__(self, name: str, value: object) -> None:
        if name in {"resources", "page_binding"}:
            try:
                object.__getattribute__(self, name)
            except AttributeError:
                pass
            else:
                raise AttributeError(f"{name} is immutable for a browser context")
        object.__setattr__(self, name, value)

    def __post_init__(self) -> None:
        self.context_id = _required(self.context_id, "context_id")
        self.attempt_id = _required(self.attempt_id, "attempt_id")
        if not isinstance(self.resources, BrowserResourceIdentity):
            raise TypeError("resources must be a BrowserResourceIdentity")
        if not isinstance(self.page_binding, BatchPageBinding):
            raise TypeError("page_binding must be a BatchPageBinding")
        self.provider = _required(self.provider, "provider").casefold()
        if self.taint_reason is not None:
            self.taint_reason = _required(self.taint_reason, "taint_reason")
        if self.drained and self.taint_reason is None:
            raise ValueError("only a tainted browser context may be drained")
        if self.closed and self.taint_reason is not None:
            raise ValueError("a tainted browser context must be drained, not healthy-closed")

    def require_active(self) -> None:
        if self.taint_reason is not None:
            raise BrowserContextTainted("browser context is tainted")
        if self.drained:
            raise SemanticBatchDenied("browser context is drained")
        if self.closed:
            raise SemanticBatchDenied("browser context is closed")

    def taint_and_drain(self, reason: str) -> None:
        if self.closed:
            raise SemanticBatchDenied("cannot taint a healthy-closed browser context")
        if not self.drained:
            self.taint_reason = _required(reason, "taint_reason")
            self.drained = True

    def close_healthy(self) -> None:
        self.require_active()
        self.closed = True


@dataclass(frozen=True, slots=True)
class BrowserContextLease:
    """Registry-issued non-serializable capability for exact context release."""

    context_id: str
    resources: BrowserResourceIdentity
    nonce: str
    signature: str

    def __reduce__(self) -> object:
        raise TypeError("browser context lease cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("browser context lease cannot be serialized")


class BrowserContextRegistry:
    """Reserve normalized SQLite/profile/port identities until exact teardown."""

    def __init__(self) -> None:
        self._secret = secrets.token_bytes(32)
        self._sqlite_owners: dict[str, str] = {}
        self._profile_owners: dict[str, str] = {}
        self._port_owners: dict[int, str] = {}
        self._leases: dict[str, BrowserContextLease] = {}

    def acquire(self, context: BrowserContext) -> BrowserContextLease:
        context.require_active()
        if context.context_id in self._leases:
            raise SemanticBatchDenied("browser context_id is already leased")
        resources = self._resources(context)
        for owners, resource_id, name in resources:
            if resource_id in owners:
                raise SemanticBatchDenied(f"{name} is already owned by another application context")
        lease = BrowserContextLease(context.context_id, context.resources, secrets.token_hex(16), "")
        lease = replace(lease, signature=self._sign(lease))
        for owners, resource_id, _name in resources:
            owners[resource_id] = context.context_id
        self._leases[context.context_id] = lease
        return lease

    def release_after_close(
        self,
        context: BrowserContext,
        lease: BrowserContextLease,
        *,
        close_resources: Callable[[], None],
    ) -> None:
        self._validate_lease(context, lease)
        try:
            close_resources()
        except Exception as exc:
            context.taint_and_drain(f"effect_unknown:resource_close_failed:{type(exc).__name__}")
            raise SemanticBatchExecutionError("browser context resource close failed") from exc
        if context.taint_reason is None:
            context.close_healthy()
        for owners, resource_id, _name in self._resources(context):
            if owners.get(resource_id) != context.context_id:
                raise SemanticBatchDenied("browser context resource ownership changed before release")
            del owners[resource_id]
        del self._leases[context.context_id]

    def _validate_lease(self, context: BrowserContext, lease: BrowserContextLease) -> None:
        if not isinstance(lease, BrowserContextLease) or lease.context_id != context.context_id:
            raise SemanticBatchDenied("browser context lease does not match the context")
        if lease.resources != context.resources:
            raise SemanticBatchDenied("browser context lease resources do not match the context")
        if not hmac.compare_digest(lease.signature, self._sign(replace(lease, signature=""))):
            raise SemanticBatchDenied("browser context lease signature is invalid")
        if self._leases.get(context.context_id) != lease:
            raise SemanticBatchDenied("browser context lease is stale or was not issued here")
        for owners, resource_id, _name in self._resources(context):
            if owners.get(resource_id) != context.context_id:
                raise SemanticBatchDenied("browser context resources are not exactly owned by this lease")

    def _resources(self, context: BrowserContext) -> tuple[tuple[dict[object, str], object, str], ...]:
        return (
            (self._sqlite_owners, context.resources.sqlite_path, "sqlite path"),
            (self._profile_owners, context.resources.profile_path, "browser profile"),
            (self._port_owners, context.resources.debug_port, "debug port"),
        )

    def _sign(self, lease: BrowserContextLease) -> str:
        return hmac.new(
            self._secret,
            _canonical_json(
                {
                    "context_id": lease.context_id,
                    "resources": {
                        "sqlite_path": lease.resources.sqlite_path,
                        "profile_path": lease.resources.profile_path,
                        "debug_port": lease.resources.debug_port,
                    },
                    "nonce": lease.nonce,
                }
            ),
            hashlib.sha256,
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class BatchSemanticAuthority:
    """One-shot, non-serializable authority for exactly one semantic batch."""

    context_id: str
    attempt_id: str
    batch_digest: str
    recipe: ApplicationRecipe
    page_binding: BatchPageBinding
    expires_at: float
    nonce: str
    submit_authority: bool
    signature: str

    def __reduce__(self) -> object:
        raise TypeError("batch semantic authority cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("batch semantic authority cannot be serialized")


class BatchSemanticAuthorityIssuer:
    """Parent-owned HMAC issuer; authority is never submit authority."""

    def __init__(self, *, ttl_seconds: float = 60.0, clock: Callable[[], float] = time.time) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._secret = secrets.token_bytes(32)
        self._issued: dict[str, float] = {}

    def issue(self, context: BrowserContext, batch: SemanticPatchBatch) -> BatchSemanticAuthority:
        context.require_active()
        self._validate_issue_context(context, batch)
        authority = BatchSemanticAuthority(
            context_id=context.context_id,
            attempt_id=context.attempt_id,
            batch_digest=batch.digest,
            recipe=batch.recipe,
            page_binding=batch.page_binding,
            expires_at=self._clock() + self._ttl_seconds,
            nonce=secrets.token_hex(16),
            submit_authority=False,
            signature="",
        )
        authority = replace(authority, signature=self._sign(authority))
        self._issued[authority.nonce] = authority.expires_at
        return authority

    def consume(self, authority: BatchSemanticAuthority, context: BrowserContext, batch: SemanticPatchBatch) -> None:
        context.require_active()
        if not isinstance(authority, BatchSemanticAuthority) or authority.submit_authority is not False:
            raise SemanticBatchDenied("semantic batch authority must not grant submit")
        if not hmac.compare_digest(authority.signature, self._sign(replace(authority, signature=""))):
            raise SemanticBatchDenied("batch semantic authority signature is invalid")
        if self._clock() >= authority.expires_at:
            raise SemanticBatchDenied("batch semantic authority expired")
        if (context.context_id, context.attempt_id) != (authority.context_id, authority.attempt_id):
            raise SemanticBatchDenied("batch semantic authority does not belong to this application context")
        if (
            authority.context_id,
            authority.attempt_id,
            authority.batch_digest,
            authority.recipe,
            authority.page_binding,
        ) != (
            context.context_id,
            context.attempt_id,
            batch.digest,
            batch.recipe,
            batch.page_binding,
        ):
            raise SemanticBatchDenied("batch semantic authority binding mismatch")
        if self._issued.pop(authority.nonce, None) != authority.expires_at:
            raise SemanticBatchDenied("batch semantic authority was already consumed or was not issued here")

    @staticmethod
    def _validate_issue_context(context: BrowserContext, batch: SemanticPatchBatch) -> None:
        page_host = (urlparse(context.page_binding.page_url).hostname or "").casefold()
        if context.attempt_id != batch.attempt_id:
            raise SemanticBatchDenied("batch does not belong to this application context")
        if (context.provider, page_host, context.page_binding.page_signature) != (
            batch.recipe.provider,
            batch.recipe.domain,
            batch.recipe.page_signature,
        ):
            raise SemanticBatchDenied("batch recipe does not match the current application context")
        if context.page_binding != batch.page_binding:
            raise SemanticBatchDenied("batch page binding does not match the current application context")

    def _sign(self, authority: BatchSemanticAuthority) -> str:
        return hmac.new(
            self._secret,
            _canonical_json(
                {
                    "context_id": authority.context_id,
                    "attempt_id": authority.attempt_id,
                    "batch_digest": authority.batch_digest,
                    "recipe": {
                        "provider": authority.recipe.provider,
                        "domain": authority.recipe.domain,
                        "adapter_version": authority.recipe.adapter_version,
                        "page_signature": authority.recipe.page_signature,
                        "operations": authority.recipe.operations,
                    },
                    "page_binding": {
                        "page_url": authority.page_binding.page_url,
                        "frame_path": authority.page_binding.frame_path,
                        "page_signature": authority.page_binding.page_signature,
                        "page_epoch": authority.page_binding.page_epoch,
                    },
                    "expires_at": authority.expires_at,
                    "nonce": authority.nonce,
                    "submit_authority": authority.submit_authority,
                }
            ),
            hashlib.sha256,
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class SemanticPatchBatchResult:
    batch_id: str
    batch_digest: str
    applied_fields: tuple[str, ...]
    page_epoch: int
    submit_authority: bool = False

    def __post_init__(self) -> None:
        if self.submit_authority is not False:
            raise ValueError("semantic batch results must not grant submit authority")


class SemanticPatchBatchRunner:
    """Execute only verified routine descriptors and park any unexpected effect."""

    def __init__(self, issuer: BatchSemanticAuthorityIssuer) -> None:
        self._issuer = issuer

    def run(
        self,
        *,
        context: BrowserContext,
        authority: BatchSemanticAuthority,
        batch: SemanticPatchBatch,
        adapter: ProviderSemanticPatchAdapter,
    ) -> SemanticPatchBatchResult:
        self._issuer.consume(authority, context, batch)
        try:
            if (adapter.provider.casefold(), adapter.adapter_version) != (
                batch.recipe.provider,
                batch.recipe.adapter_version,
            ):
                raise SemanticBatchDenied("provider adapter does not match the admitted recipe")
            applied: list[str] = []
            for patch in batch.patches:
                self._validate_page(authority.page_binding, adapter.observe_page())
                control = adapter.control_for(patch.field_semantic)
                self._validate_control(authority.page_binding, patch, control)
                adapter.apply_routine_control(control, patch.value)
                self._validate_page(authority.page_binding, adapter.observe_page())
                applied.append(patch.field_semantic)
        except Exception as exc:
            context.taint_and_drain(f"effect_unknown:semantic_patch_batch:{type(exc).__name__}")
            raise SemanticBatchExecutionError("semantic patch batch stopped; context was drained") from exc
        return SemanticPatchBatchResult(
            batch.batch_id,
            batch.digest,
            tuple(applied),
            authority.page_binding.page_epoch,
        )

    @staticmethod
    def _validate_page(binding: BatchPageBinding, page: BrowserPageObservation) -> None:
        if page.binding != binding:
            raise SemanticBatchDenied("adapter page URL, frame, signature, or epoch changed")

    @staticmethod
    def _validate_control(
        binding: BatchPageBinding,
        patch: SemanticPatch,
        control: BatchControlDescriptor,
    ) -> None:
        if control.classification != "routine":
            raise SemanticBatchDenied("final, navigation, frame, or sensitive control is outside batch authority")
        if control.field_semantic != patch.field_semantic:
            raise SemanticBatchDenied("adapter control semantic does not match the batch patch")
        SemanticPatchBatchRunner._validate_page(binding, control.page)


__all__ = [
    "ApplicationRecipe",
    "BatchControlDescriptor",
    "BatchPageBinding",
    "BatchSemanticAuthority",
    "BatchSemanticAuthorityIssuer",
    "BrowserContext",
    "BrowserContextLease",
    "BrowserContextRegistry",
    "BrowserContextTainted",
    "BrowserPageObservation",
    "BrowserResourceIdentity",
    "ProviderAdapter",
    "ProviderSemanticPatchAdapter",
    "SemanticBatchDenied",
    "SemanticBatchExecutionError",
    "SemanticPatch",
    "SemanticPatchBatch",
    "SemanticPatchBatchResult",
    "SemanticPatchBatchRunner",
    "normalize_field_semantic",
]
