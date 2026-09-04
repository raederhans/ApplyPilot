"""Synthetic-only provider recipe normalizers.

These adapters normalize already-observed, value-free DOM structure for the M4
semantic-batch recipe boundary. They intentionally provide no locator, browser,
navigation, file, Submit, receipt, recovery, or direct-email capability. Until
real provider DOM identities are admitted with mechanical evidence, all three
remain disabled in the M4 execution registry.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import unquote, urlsplit, urlunsplit

from applypilot.apply.provider_registry import provider_matches_host
from applypilot.apply.recipe_cache import (
    RECIPE_CACHE_SCHEMA_VERSION,
    RECIPE_POLICY_VERSION,
    CachedProviderRecipe,
    CachedRoutineControl,
    RecipeCacheKey,
    canonical_digest,
    private_binding_digest,
)
from applypilot.apply.semantic_batch import (
    DEFAULT_SEMANTIC_BATCH_ADAPTER_REGISTRY,
    SemanticBatchDenied,
    normalize_field_semantic,
)

ProviderName = Literal[
    "greenhouse", "lever", "ashby", "smartrecruiters", "workday"
]
RoutineKind = Literal["text", "native_select"]

GREENHOUSE_ADAPTER_VERSION = "greenhouse-semantic-recipe/v1"
LEVER_ADAPTER_VERSION = "lever-semantic-recipe/v1"
ASHBY_ADAPTER_VERSION = "ashby-semantic-recipe/v1"
SMARTRECRUITERS_ADAPTER_VERSION = "smartrecruiters-semantic-recipe/v1"
WORKDAY_ADAPTER_VERSION = "workday-semantic-recipe/v1"

_ROUTINE_SEMANTICS = frozenset(
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
_FORBIDDEN_MARKERS = frozenset(
    {
        "assessment",
        "captcha",
        "consent",
        "credential",
        "direct_email",
        "eeo",
        "final_submit",
        "file_upload",
        "financial",
        "identity",
        "legal",
        "receipt",
        "recovery",
        "verification",
    }
)
_FORBIDDEN_KINDS = frozenset(
    {
        "checkbox",
        "custom_combobox",
        "date",
        "file",
        "final_submit",
        "navigation",
        "radio",
        "resume_file",
        "switch",
        "textarea",
        "unknown",
    }
)


def _required(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _sha256(value: object, name: str) -> str:
    text = _required(value, name).casefold()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{name} must be a sha256 digest")
    return text


def _https_url(value: object, name: str) -> str:
    raw = _required(value, name)
    parsed = urlsplit(raw)
    if parsed.scheme.casefold() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{name} must be an HTTPS URL without credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{name} has an invalid port") from exc
    if port not in {None, 443}:
        raise ValueError(f"{name} must use the default HTTPS port")
    host = parsed.hostname.casefold().rstrip(".")
    return urlunsplit(("https", host, parsed.path or "/", parsed.query, parsed.fragment))


def _path_segments(url: str) -> tuple[str, ...]:
    segments: list[str] = []
    for raw in urlsplit(url).path.split("/"):
        if not raw:
            continue
        decoded = unquote(raw).strip()
        if not decoded or decoded in {".", ".."} or "/" in decoded or "\\" in decoded:
            raise SemanticBatchDenied("provider application target path is ambiguous")
        segments.append(decoded)
    return tuple(segments)


@dataclass(frozen=True, slots=True)
class ProviderControlStructure:
    """Ephemeral, already-value-free mechanical control evidence."""

    semantic: str
    kind: str
    required: bool
    writable: bool
    locator_digest: str
    dom_identity_digest: str
    option_count: int = 0
    option_digest: str = ""
    stateful: bool = False
    dynamic: bool = False
    custom: bool = False
    sensitive: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "semantic", normalize_field_semantic(self.semantic))
        object.__setattr__(self, "kind", _required(self.kind, "kind").casefold())
        object.__setattr__(self, "locator_digest", _sha256(self.locator_digest, "locator_digest"))
        object.__setattr__(self, "dom_identity_digest", _sha256(self.dom_identity_digest, "dom_identity_digest"))
        for name in ("required", "writable", "stateful", "dynamic", "custom", "sensitive"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a boolean")
        if isinstance(self.option_count, bool) or not isinstance(self.option_count, int) or self.option_count < 0:
            raise ValueError("option_count must be a non-negative integer")
        digest = self.option_digest or canonical_digest([])
        object.__setattr__(self, "option_digest", _sha256(digest, "option_digest"))


@dataclass(frozen=True, slots=True)
class ProviderPageRecipeObservation:
    """Fresh page/lease identity plus value-free structural evidence."""

    provider: ProviderName
    application_target_url: str
    page_url: str
    page_signature: str
    page_epoch: int
    page_lease_id: str
    browser_generation: int
    controls: tuple[ProviderControlStructure, ...]
    frame_path: tuple[int, ...] = ()
    frame_url: str | None = None
    taint_reason: str | None = None
    markers: tuple[str, ...] = ()
    control_schema_version: str = "1"

    def __post_init__(self) -> None:
        if self.provider not in {
            "greenhouse",
            "lever",
            "ashby",
            "smartrecruiters",
            "workday",
        }:
            raise ValueError("provider recipe observation is unsupported")
        object.__setattr__(
            self,
            "application_target_url",
            _https_url(self.application_target_url, "application_target_url"),
        )
        object.__setattr__(self, "page_url", _https_url(self.page_url, "page_url"))
        object.__setattr__(self, "page_signature", _sha256(self.page_signature, "page_signature"))
        object.__setattr__(self, "page_lease_id", _required(self.page_lease_id, "page_lease_id"))
        object.__setattr__(self, "control_schema_version", _required(self.control_schema_version, "control_schema_version"))
        if self.frame_url is not None:
            object.__setattr__(self, "frame_url", _https_url(self.frame_url, "frame_url"))
        if not isinstance(self.frame_path, tuple) or any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in self.frame_path
        ):
            raise ValueError("frame_path must be a tuple of non-negative integers")
        if isinstance(self.page_epoch, bool) or not isinstance(self.page_epoch, int) or self.page_epoch < 0:
            raise ValueError("page_epoch must be a non-negative integer")
        if (
            isinstance(self.browser_generation, bool)
            or not isinstance(self.browser_generation, int)
            or self.browser_generation < 1
        ):
            raise ValueError("browser_generation must be a positive integer")
        normalized_markers = tuple(sorted({_required(item, "marker").casefold() for item in self.markers}))
        object.__setattr__(self, "markers", normalized_markers)


@dataclass(frozen=True, slots=True)
class _TargetIdentity:
    domain: str
    tenant: str
    requisition: str
    canonical_target: str


class ProviderSemanticRecipeAdapter:
    """Base structural normalizer; never a browser-write adapter."""

    provider: ProviderName
    adapter_version: str

    @property
    def m4_execution_enabled(self) -> bool:
        return DEFAULT_SEMANTIC_BATCH_ADAPTER_REGISTRY.supports_execution(
            self.provider,
            self.adapter_version,
        )

    def normalize(self, observation: ProviderPageRecipeObservation) -> CachedProviderRecipe:
        if observation.provider != self.provider:
            raise SemanticBatchDenied("provider recipe adapter does not match the observation")
        if observation.taint_reason is not None:
            raise SemanticBatchDenied("tainted page observation cannot produce a provider recipe")
        if set(observation.markers) & _FORBIDDEN_MARKERS:
            raise SemanticBatchDenied("privileged or manual-gate page markers forbid a routine recipe")
        if observation.markers:
            raise SemanticBatchDenied("unclassified page markers forbid a routine recipe")
        target = self._target_identity(observation)
        structural_rows = [self._structural_row(control) for control in observation.controls]
        schema_policy_digest = private_binding_digest(
            "schema-policy",
            {
                "adapter_version": self.adapter_version,
                "cache_schema_version": RECIPE_CACHE_SCHEMA_VERSION,
                "control_schema_version": observation.control_schema_version,
                "controls": structural_rows,
                "markers": observation.markers,
                "policy_version": RECIPE_POLICY_VERSION,
                "provider": self.provider,
            }
        )
        controls = tuple(
            cached
            for control in observation.controls
            if (cached := self._routine_control(control)) is not None
        )
        if not controls:
            raise SemanticBatchDenied("no mechanically proven routine controls are cacheable")
        semantics = [control.semantic for control in controls]
        if len(semantics) != len(set(semantics)):
            raise SemanticBatchDenied("routine control semantics are ambiguous")
        key = RecipeCacheKey(
            provider=self.provider,
            domain=target.domain,
            adapter_version=self.adapter_version,
            page_signature=private_binding_digest("page-signature", observation.page_signature),
            schema_policy_digest=schema_policy_digest,
            page_digest=private_binding_digest("page", observation.page_url),
            frame_digest=private_binding_digest(
                "frame",
                {
                    "frame_path": observation.frame_path,
                    "frame_domain": (
                        urlsplit(observation.frame_url or observation.page_url).hostname
                        or ""
                    ).casefold(),
                },
            ),
            option_digest=private_binding_digest(
                "option-set",
                [(control.option_count, control.option_digest) for control in observation.controls]
            ),
            required_writable_digest=canonical_digest(
                [(control.required, control.writable) for control in observation.controls]
            ),
            locator_digest=private_binding_digest(
                "locator-set",
                [(control.locator_digest, control.dom_identity_digest) for control in observation.controls]
            ),
            taint_digest=private_binding_digest("taint", ""),
            lease_digest=private_binding_digest("lease", observation.page_lease_id),
            tenant_digest=private_binding_digest("tenant", target.tenant),
            requisition_digest=private_binding_digest("requisition", target.requisition),
            application_target_digest=private_binding_digest(
                "application-target",
                {
                    "canonical_target": target.canonical_target,
                    "observed_target": observation.application_target_url,
                },
            ),
            page_epoch=observation.page_epoch,
            browser_generation=observation.browser_generation,
        )
        return CachedProviderRecipe.build(key, controls)

    def _target_identity(self, observation: ProviderPageRecipeObservation) -> _TargetIdentity:
        raise NotImplementedError

    @staticmethod
    def _structural_row(control: ProviderControlStructure) -> dict[str, object]:
        return {
            "custom": control.custom,
            "dom_identity_digest": control.dom_identity_digest,
            "dynamic": control.dynamic,
            "kind": control.kind,
            "locator_digest": control.locator_digest,
            "option_count": control.option_count,
            "option_digest": control.option_digest,
            "required": control.required,
            "semantic": control.semantic,
            "sensitive": control.sensitive,
            "stateful": control.stateful,
            "writable": control.writable,
        }

    @staticmethod
    def _routine_control(control: ProviderControlStructure) -> CachedRoutineControl | None:
        if (
            control.semantic not in _ROUTINE_SEMANTICS
            or control.kind in _FORBIDDEN_KINDS
            or control.kind not in {"text", "native_select"}
            or not control.writable
            or control.stateful
            or control.dynamic
            or control.custom
            or control.sensitive
        ):
            return None
        if control.kind == "text" and control.option_count != 0:
            return None
        if control.kind == "native_select" and control.option_count < 1:
            return None
        structure_digest = private_binding_digest(
            "control-structure",
            ProviderSemanticRecipeAdapter._structural_row(control),
        )
        return CachedRoutineControl(
            structure_digest=structure_digest,
            semantic=control.semantic,
            kind=control.kind,
            required=control.required,
            writable=True,
            option_count=control.option_count,
            option_digest=private_binding_digest(
                "control-options",
                (control.option_count, control.option_digest),
            ),
            operation="set_text" if control.kind == "text" else "select_option",
        )


class ShadowOnlyProviderSemanticRecipeAdapter(ProviderSemanticRecipeAdapter):
    """Structural normalizer that explicitly refuses M4 execution admission."""

    @property
    def m4_execution_enabled(self) -> bool:
        return False


def _direct_target(
    provider: ProviderName,
    url: str,
    *,
    expected_tail: str | None = None,
) -> _TargetIdentity:
    parsed = urlsplit(url)
    domain = (parsed.hostname or "").casefold()
    if not provider_matches_host(provider, domain, "detection"):
        raise SemanticBatchDenied("provider application target host is not exact")
    segments = _path_segments(url)
    if expected_tail is not None and segments and segments[-1].casefold() == expected_tail:
        segments = segments[:-1]
    if provider == "greenhouse":
        if len(segments) != 3 or segments[1].casefold() != "jobs":
            raise SemanticBatchDenied("Greenhouse application target identity is unsupported")
        tenant, requisition = segments[0], segments[2]
        canonical_path = f"/{segments[0]}/jobs/{segments[2]}"
    else:
        if len(segments) != 2:
            raise SemanticBatchDenied(f"{provider} application target identity is unsupported")
        tenant, requisition = segments
        canonical_path = f"/{segments[0]}/{segments[1]}"
    return _TargetIdentity(
        domain=domain,
        tenant=tenant,
        requisition=requisition,
        canonical_target=urlunsplit(("https", domain, canonical_path, "", "")),
    )


class GreenhouseSemanticRecipeAdapter(ProviderSemanticRecipeAdapter):
    provider: ProviderName = "greenhouse"
    adapter_version = GREENHOUSE_ADAPTER_VERSION

    def _target_identity(self, observation: ProviderPageRecipeObservation) -> _TargetIdentity:
        if observation.frame_path:
            raise SemanticBatchDenied(
                "Greenhouse framed recipes require unavailable live frame-binding proof"
            )
        if observation.frame_url not in {None, observation.page_url}:
            raise SemanticBatchDenied("unbound Greenhouse frame URL")
        target = _direct_target("greenhouse", observation.application_target_url)
        page = _direct_target("greenhouse", observation.page_url)
        if (page.domain, page.tenant, page.requisition) != (
            target.domain,
            target.tenant,
            target.requisition,
        ):
            raise SemanticBatchDenied("Greenhouse live page changed application target")
        return target


class LeverSemanticRecipeAdapter(ProviderSemanticRecipeAdapter):
    provider: ProviderName = "lever"
    adapter_version = LEVER_ADAPTER_VERSION

    def _target_identity(self, observation: ProviderPageRecipeObservation) -> _TargetIdentity:
        if observation.frame_path or observation.frame_url not in {None, observation.page_url}:
            raise SemanticBatchDenied("Lever framed application targets are not admitted")
        target = _direct_target("lever", observation.application_target_url, expected_tail="apply")
        page = _direct_target("lever", observation.page_url, expected_tail="apply")
        if (page.domain, page.tenant, page.requisition) != (
            target.domain,
            target.tenant,
            target.requisition,
        ):
            raise SemanticBatchDenied("Lever live page changed application target")
        return target


class AshbySemanticRecipeAdapter(ProviderSemanticRecipeAdapter):
    provider: ProviderName = "ashby"
    adapter_version = ASHBY_ADAPTER_VERSION

    def _target_identity(self, observation: ProviderPageRecipeObservation) -> _TargetIdentity:
        if observation.frame_path or observation.frame_url not in {None, observation.page_url}:
            raise SemanticBatchDenied("Ashby framed application targets are not admitted")
        target = _direct_target("ashby", observation.application_target_url, expected_tail="application")
        page = _direct_target("ashby", observation.page_url, expected_tail="application")
        if (page.domain, page.tenant, page.requisition) != (
            target.domain,
            target.tenant,
            target.requisition,
        ):
            raise SemanticBatchDenied("Ashby live page changed application target")
        return target


def _opaque_direct_target(
    provider: Literal["smartrecruiters", "workday"],
    url: str,
) -> _TargetIdentity:
    """Bind dynamic-host providers to one exact, query-free application path."""

    parsed = urlsplit(url)
    domain = (parsed.hostname or "").casefold()
    if not provider_matches_host(provider, domain, "detection"):
        raise SemanticBatchDenied("provider application target host is not exact")
    segments = _path_segments(url)
    if segments and segments[-1].casefold() == "apply":
        segments = segments[:-1]
    if len(segments) < 2:
        raise SemanticBatchDenied(
            f"{provider} application target identity is unsupported"
        )
    canonical_path = "/" + "/".join(segments)
    return _TargetIdentity(
        domain=domain,
        tenant=f"{domain}:{segments[0]}",
        requisition=canonical_path,
        canonical_target=urlunsplit(("https", domain, canonical_path, "", "")),
    )


class SmartRecruitersSemanticRecipeAdapter(ShadowOnlyProviderSemanticRecipeAdapter):
    provider: ProviderName = "smartrecruiters"
    adapter_version = SMARTRECRUITERS_ADAPTER_VERSION

    def _target_identity(self, observation: ProviderPageRecipeObservation) -> _TargetIdentity:
        if observation.frame_path or observation.frame_url not in {
            None,
            observation.page_url,
        }:
            raise SemanticBatchDenied(
                "SmartRecruiters framed application targets are not admitted"
            )
        target = _opaque_direct_target("smartrecruiters", observation.application_target_url)
        page = _opaque_direct_target("smartrecruiters", observation.page_url)
        if page.canonical_target != target.canonical_target:
            raise SemanticBatchDenied(
                "SmartRecruiters live page changed application target"
            )
        return target


class WorkdaySemanticRecipeAdapter(ShadowOnlyProviderSemanticRecipeAdapter):
    provider: ProviderName = "workday"
    adapter_version = WORKDAY_ADAPTER_VERSION

    def _target_identity(self, observation: ProviderPageRecipeObservation) -> _TargetIdentity:
        if observation.frame_path or observation.frame_url not in {
            None,
            observation.page_url,
        }:
            raise SemanticBatchDenied("Workday framed application targets are not admitted")
        target = _opaque_direct_target("workday", observation.application_target_url)
        page = _opaque_direct_target("workday", observation.page_url)
        if page.canonical_target != target.canonical_target:
            raise SemanticBatchDenied("Workday live page changed application target")
        return target


class ProviderSemanticRecipeRegistry:
    """Injectable provider recipe registry linked to M4 capability admission."""

    def __init__(
        self,
        adapters: Iterable[ProviderSemanticRecipeAdapter] = (),
        *,
        execution_authority: bool = True,
    ) -> None:
        self._items: dict[str, ProviderSemanticRecipeAdapter] = {}
        self._execution_authority = execution_authority
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: ProviderSemanticRecipeAdapter, *, replace_existing: bool = False) -> None:
        provider = _required(getattr(adapter, "provider", ""), "provider").casefold()
        if provider in self._items and not replace_existing:
            raise ValueError(f"provider semantic recipe adapter already registered: {provider}")
        self._items[provider] = adapter

    def get(self, provider: object) -> ProviderSemanticRecipeAdapter | None:
        return self._items.get(str(provider or "").casefold().strip())

    def normalize(self, observation: ProviderPageRecipeObservation) -> CachedProviderRecipe:
        adapter = self.get(observation.provider)
        if adapter is None:
            raise SemanticBatchDenied("provider semantic recipe adapter is unavailable")
        return adapter.normalize(observation)

    def require_m4_execution(self, provider: object, adapter_version: object) -> None:
        if not self._execution_authority:
            raise SemanticBatchDenied("provider recipe registry is shadow-only")
        DEFAULT_SEMANTIC_BATCH_ADAPTER_REGISTRY.require_execution(provider, adapter_version)

    def providers(self) -> tuple[str, ...]:
        return tuple(self._items)


def default_provider_semantic_recipe_registry() -> ProviderSemanticRecipeRegistry:
    return ProviderSemanticRecipeRegistry(
        (
            GreenhouseSemanticRecipeAdapter(),
            LeverSemanticRecipeAdapter(),
            AshbySemanticRecipeAdapter(),
        )
    )


def default_provider_recipe_shadow_registry() -> ProviderSemanticRecipeRegistry:
    """Return read-only normalizers; this registry grants no execution capability."""

    return ProviderSemanticRecipeRegistry(
        (
            GreenhouseSemanticRecipeAdapter(),
            SmartRecruitersSemanticRecipeAdapter(),
            WorkdaySemanticRecipeAdapter(),
        ),
        execution_authority=False,
    )


__all__ = [
    "ASHBY_ADAPTER_VERSION",
    "GREENHOUSE_ADAPTER_VERSION",
    "LEVER_ADAPTER_VERSION",
    "SMARTRECRUITERS_ADAPTER_VERSION",
    "WORKDAY_ADAPTER_VERSION",
    "AshbySemanticRecipeAdapter",
    "GreenhouseSemanticRecipeAdapter",
    "LeverSemanticRecipeAdapter",
    "ProviderControlStructure",
    "ProviderPageRecipeObservation",
    "ProviderSemanticRecipeAdapter",
    "ProviderSemanticRecipeRegistry",
    "ShadowOnlyProviderSemanticRecipeAdapter",
    "SmartRecruitersSemanticRecipeAdapter",
    "WorkdaySemanticRecipeAdapter",
    "default_provider_recipe_shadow_registry",
    "default_provider_semantic_recipe_registry",
]
