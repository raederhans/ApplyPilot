"""Value-free, process-local cache for provider semantic recipes.

The cache deliberately stores no browser values, labels, locators, URLs, job
tokens, handles, authority material, paths, or submission data. Callers must
rebuild a key from a fresh observation and revalidate live authority on every
hit. A hit is only a structural optimization; it is never an audit or write
authorization.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

from applypilot.apply.semantic_batch import normalize_field_semantic

RECIPE_CACHE_SCHEMA_VERSION = "provider-recipe-cache/v1"
RECIPE_POLICY_VERSION = "routine-provider-recipe/v1"
_CACHE_BINDING_KEY = secrets.token_bytes(32)
_ALLOWED_OPERATIONS = frozenset({"set_text", "select_option"})
_ALLOWED_KINDS = frozenset({"text", "native_select"})
_ALLOWED_SEMANTICS = frozenset(
    {
        "city",
        "country",
        "email",
        "phone",
        "portfolio_url",
        "preferred_name",
        "postal_code",
        "state",
    }
)
_PROVIDER_DOMAINS = {
    "greenhouse": frozenset({"boards.greenhouse.io", "job-boards.greenhouse.io"}),
    "lever": frozenset({"jobs.lever.co", "jobs.eu.lever.co"}),
    "ashby": frozenset({"jobs.ashbyhq.com"}),
}
_ADAPTER_VERSION_RE = re.compile(r"^(greenhouse|lever|ashby)-semantic-recipe/v[1-9][0-9]*$")
_ALLOWED_PAYLOAD_KEYS = frozenset(
    {
        "adapter_version",
        "application_target_digest",
        "browser_generation",
        "controls",
        "domain",
        "frame_digest",
        "key",
        "kind",
        "lease_digest",
        "locator_digest",
        "operation",
        "operations",
        "option_count",
        "option_digest",
        "page_digest",
        "page_epoch",
        "page_signature",
        "policy_version",
        "provider",
        "required",
        "required_writable_digest",
        "requisition_digest",
        "schema_policy_digest",
        "schema_version",
        "semantic",
        "structure_digest",
        "synthetic_only",
        "taint_digest",
        "tenant_digest",
        "writable",
    }
)
_SAFE_PLAINTEXT_VALUES = frozenset(
    {
        RECIPE_CACHE_SCHEMA_VERSION,
        RECIPE_POLICY_VERSION,
        *_ALLOWED_OPERATIONS,
        *_ALLOWED_KINDS,
        *_ALLOWED_SEMANTICS,
        *_PROVIDER_DOMAINS,
        *(domain for domains in _PROVIDER_DOMAINS.values() for domain in domains),
    }
)


def canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def private_binding_digest(label: str, value: object) -> str:
    """Return a process-bound digest so low-entropy target tokens are not reusable."""

    payload = label.encode("ascii") + b"\0" + json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(_CACHE_BINDING_KEY, payload, hashlib.sha256).hexdigest()


def _required(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _digest(value: object, name: str) -> str:
    text = _required(value, name).casefold()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{name} must be a sha256 digest")
    return text


def _domain(value: object) -> str:
    raw = _required(value, "domain").casefold().strip(".")
    parsed = urlparse(f"https://{raw}")
    if parsed.hostname != raw or parsed.port is not None or ":" in raw or "/" in raw:
        raise ValueError("domain must be one normalized exact hostname")
    return raw


@dataclass(frozen=True, slots=True)
class RecipeCacheKey:
    """All authority and structure dimensions that must match for one hit."""

    provider: str
    domain: str
    adapter_version: str
    page_signature: str
    schema_policy_digest: str
    page_digest: str
    frame_digest: str
    option_digest: str
    required_writable_digest: str
    locator_digest: str
    taint_digest: str
    lease_digest: str
    tenant_digest: str
    requisition_digest: str
    application_target_digest: str
    page_epoch: int
    browser_generation: int

    def __post_init__(self) -> None:
        provider = _required(self.provider, "provider").casefold()
        domain = _domain(self.domain)
        adapter_version = _required(self.adapter_version, "adapter_version")
        domains = _PROVIDER_DOMAINS.get(provider)
        version_match = _ADAPTER_VERSION_RE.fullmatch(adapter_version)
        if (
            domains is None
            or domain not in domains
            or version_match is None
            or version_match.group(1) != provider
        ):
            raise ValueError("provider recipe cache binding is unsupported")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "domain", domain)
        object.__setattr__(self, "adapter_version", adapter_version)
        for name in (
            "page_signature",
            "schema_policy_digest",
            "page_digest",
            "frame_digest",
            "option_digest",
            "required_writable_digest",
            "locator_digest",
            "taint_digest",
            "lease_digest",
            "tenant_digest",
            "requisition_digest",
            "application_target_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name))
        if isinstance(self.page_epoch, bool) or not isinstance(self.page_epoch, int) or self.page_epoch < 0:
            raise ValueError("page_epoch must be a non-negative integer")
        if (
            isinstance(self.browser_generation, bool)
            or not isinstance(self.browser_generation, int)
            or self.browser_generation < 1
        ):
            raise ValueError("browser_generation must be a positive integer")

    @property
    def identity_digest(self) -> str:
        return canonical_digest(self.as_dict())

    @property
    def clean(self) -> bool:
        return hmac.compare_digest(self.taint_digest, private_binding_digest("taint", ""))

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "domain": self.domain,
            "adapter_version": self.adapter_version,
            "page_signature": self.page_signature,
            "schema_policy_digest": self.schema_policy_digest,
            "page_digest": self.page_digest,
            "frame_digest": self.frame_digest,
            "option_digest": self.option_digest,
            "required_writable_digest": self.required_writable_digest,
            "locator_digest": self.locator_digest,
            "taint_digest": self.taint_digest,
            "lease_digest": self.lease_digest,
            "tenant_digest": self.tenant_digest,
            "requisition_digest": self.requisition_digest,
            "application_target_digest": self.application_target_digest,
            "page_epoch": self.page_epoch,
            "browser_generation": self.browser_generation,
        }


@dataclass(frozen=True, slots=True)
class CachedRoutineControl:
    """One routine operation with only non-value structural metadata."""

    structure_digest: str
    semantic: str
    kind: str
    required: bool
    writable: bool
    option_count: int
    option_digest: str
    operation: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "structure_digest", _digest(self.structure_digest, "structure_digest"))
        semantic = normalize_field_semantic(self.semantic)
        if semantic not in _ALLOWED_SEMANTICS:
            raise ValueError("cached control semantic is not routine")
        object.__setattr__(self, "semantic", semantic)
        object.__setattr__(self, "option_digest", _digest(self.option_digest, "option_digest"))
        if self.kind not in _ALLOWED_KINDS:
            raise ValueError("cached control kind is not routine")
        if self.operation not in _ALLOWED_OPERATIONS:
            raise ValueError("cached control operation is not routine")
        if self.kind == "text" and self.operation != "set_text":
            raise ValueError("text controls require set_text")
        if self.kind == "native_select" and self.operation != "select_option":
            raise ValueError("native selects require select_option")
        if not isinstance(self.required, bool) or not isinstance(self.writable, bool):
            raise TypeError("required and writable must be booleans")
        if self.writable is not True:
            raise ValueError("cached routine controls must be writable")
        if isinstance(self.option_count, bool) or not isinstance(self.option_count, int) or self.option_count < 0:
            raise ValueError("option_count must be a non-negative integer")
        if self.kind == "text" and self.option_count != 0:
            raise ValueError("text controls cannot cache option metadata")
        if self.kind == "native_select" and self.option_count < 1:
            raise ValueError("native selects require observed option metadata")

    def as_dict(self) -> dict[str, object]:
        return {
            "structure_digest": self.structure_digest,
            "semantic": self.semantic,
            "kind": self.kind,
            "required": self.required,
            "writable": self.writable,
            "option_count": self.option_count,
            "option_digest": self.option_digest,
            "operation": self.operation,
        }


@dataclass(frozen=True, slots=True)
class CachedProviderRecipe:
    """Sanitized structural recipe. It deliberately carries no write authority."""

    key: RecipeCacheKey
    controls: tuple[CachedRoutineControl, ...]
    structure_digest: str
    schema_version: str = RECIPE_CACHE_SCHEMA_VERSION
    policy_version: str = RECIPE_POLICY_VERSION
    synthetic_only: bool = True

    def __post_init__(self) -> None:
        if not self.controls:
            raise ValueError("cached recipe requires at least one routine control")
        semantics = tuple(control.semantic for control in self.controls)
        if len(semantics) != len(set(semantics)):
            raise ValueError("cached recipe semantics must be unique")
        if self.schema_version != RECIPE_CACHE_SCHEMA_VERSION:
            raise ValueError("cached recipe schema version is unsupported")
        if self.policy_version != RECIPE_POLICY_VERSION:
            raise ValueError("cached recipe policy version is unsupported")
        if self.synthetic_only is not True:
            raise ValueError("provider recipes are synthetic-only")
        expected = canonical_digest(
            {
                "key": self.key.as_dict(),
                "controls": [control.as_dict() for control in self.controls],
                "schema_version": self.schema_version,
                "policy_version": self.policy_version,
                "synthetic_only": True,
            }
        )
        if not hmac.compare_digest(_digest(self.structure_digest, "structure_digest"), expected):
            raise ValueError("cached recipe structural digest is invalid")

    @classmethod
    def build(
        cls,
        key: RecipeCacheKey,
        controls: tuple[CachedRoutineControl, ...],
    ) -> CachedProviderRecipe:
        payload = {
            "key": key.as_dict(),
            "controls": [control.as_dict() for control in controls],
            "schema_version": RECIPE_CACHE_SCHEMA_VERSION,
            "policy_version": RECIPE_POLICY_VERSION,
            "synthetic_only": True,
        }
        return cls(key=key, controls=controls, structure_digest=canonical_digest(payload))

    @property
    def operations(self) -> tuple[str, ...]:
        return tuple(sorted({control.operation for control in self.controls}))

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key.as_dict(),
            "controls": [control.as_dict() for control in self.controls],
            "operations": list(self.operations),
            "structure_digest": self.structure_digest,
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "synthetic_only": True,
        }


class ValueFreeRecipeCache:
    """Bounded in-memory LRU with mandatory live validation on every hit."""

    def __init__(self, *, capacity: int = 128) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("recipe cache capacity must be positive")
        self._capacity = capacity
        self._items: OrderedDict[str, CachedProviderRecipe] = OrderedDict()
        self._lock = threading.RLock()

    def put(self, recipe: CachedProviderRecipe) -> None:
        if not isinstance(recipe, CachedProviderRecipe):
            raise TypeError("recipe must be a CachedProviderRecipe")
        if not recipe.key.clean:
            raise ValueError("tainted page observations cannot enter the recipe cache")
        if not payload_is_value_free(recipe.as_dict()):
            raise ValueError("recipe payload is not value-free")
        identity = recipe.key.identity_digest
        with self._lock:
            self._items[identity] = recipe
            self._items.move_to_end(identity)
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)

    def get(
        self,
        key: RecipeCacheKey,
        *,
        validate_live: Callable[[RecipeCacheKey], bool] | None,
    ) -> CachedProviderRecipe | None:
        if not isinstance(key, RecipeCacheKey) or not key.clean:
            return None
        identity = key.identity_digest
        with self._lock:
            recipe = self._items.get(identity)
        if recipe is None or recipe.key != key or validate_live is None:
            return None
        try:
            valid = validate_live(key) is True
        except Exception:  # noqa: BLE001 - missing live proof is a cache miss
            valid = False
        if not valid:
            return None
        with self._lock:
            if self._items.get(identity) is not recipe:
                return None
            self._items.move_to_end(identity)
            return recipe

    def invalidate(self, key: RecipeCacheKey) -> bool:
        with self._lock:
            return self._items.pop(key.identity_digest, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


def payload_is_value_free(payload: Mapping[str, object]) -> bool:
    """Recursively admit only the closed cache schema, enums, and opaque digests."""

    forbidden = {
        "value",
        "label",
        "url",
        "query",
        "fragment",
        "token",
        "credential",
        "handle",
        "authority",
        "nonce",
        "path",
        "content",
        "receipt",
        "recovery",
        "direct_email",
        "final_submit",
    }

    def walk(value: object) -> bool:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                normalized = str(key).casefold()
                if (
                    normalized not in _ALLOWED_PAYLOAD_KEYS
                    or normalized in forbidden
                    or any(normalized.startswith(f"{item}_") for item in forbidden)
                ):
                    return False
                if not walk(nested):
                    return False
        elif isinstance(value, (list, tuple)):
            return all(walk(item) for item in value)
        elif isinstance(value, str):
            return value in _SAFE_PLAINTEXT_VALUES or (
                len(value) == 64 and all(char in "0123456789abcdef" for char in value)
            ) or _ADAPTER_VERSION_RE.fullmatch(value) is not None
        elif not isinstance(value, (bool, int)):
            return False
        return True

    return walk(payload)


__all__ = [
    "RECIPE_CACHE_SCHEMA_VERSION",
    "RECIPE_POLICY_VERSION",
    "CachedProviderRecipe",
    "CachedRoutineControl",
    "RecipeCacheKey",
    "ValueFreeRecipeCache",
    "canonical_digest",
    "payload_is_value_free",
]
