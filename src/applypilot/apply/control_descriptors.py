"""Page-versioned semantic control inspection and bounded browser writes.

The contracts in this module consume the P1 ``ContextBundle`` and the exact
``BrowserLeaseBundle`` owned by its ``ApplicationSupervisor``.  They never own
a browser process, grant final-submit authority, or write application state
outside the currently bound page.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from typing import Literal, Protocol
from urllib.parse import urlparse

from applypilot.apply.application_sessions import ContextBundle
from applypilot.apply.browser_broker import (
    BrowserAuthorityDenied,
    BrowserLeaseBundle,
    StalePageBinding,
)
from applypilot.apply.page_binding import PageBinding
from applypilot.apply.provider_registry import provider_for_url as registry_provider_for_url

Provider = Literal["workday", "smartrecruiters"]
ControlKind = Literal[
    "text",
    "textarea",
    "native_select",
    "custom_combobox",
    "radio",
    "checkbox",
    "switch",
    "date",
    "resume_file",
    "navigation",
    "final_submit",
]
SemanticOperation = Literal[
    "set_text",
    "select_option",
    "set_checked",
    "set_date",
    "activate_navigation",
]

CONTROL_DESCRIPTOR_SCHEMA_VERSION = "1"
SEMANTIC_CONTROL_POLICY = "semantic-control-write/v1"
MAX_CONTROLS = 200
MAX_OPTIONS = 100

_SUPPORTED_KINDS = frozenset(
    {
        "text",
        "textarea",
        "native_select",
        "custom_combobox",
        "radio",
        "checkbox",
        "switch",
        "date",
        "resume_file",
        "navigation",
        "final_submit",
    }
)


class ControlInspectionDenied(BrowserAuthorityDenied):
    """A complete, stable, supported control census could not be proven."""


class SemanticControlDenied(BrowserAuthorityDenied):
    """The exact semantic control capability is absent, stale, or forbidden."""


class SemanticControlUncertain(RuntimeError):
    """A browser write may have occurred but its postcondition is unproven."""


def provider_for_url(value: object) -> Provider | None:
    """Resolve only exact HTTPS Workday and SmartRecruiters provider hosts."""

    resolved = registry_provider_for_url(value, "control_write")
    return resolved if resolved in {"workday", "smartrecruiters"} else None  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class FormSurface:
    """One same-origin document surface owned by the bound root page."""

    surface_id: str
    frame_path: tuple[int, ...]
    frame_url: str
    origin: str
    control_count: int

    def __post_init__(self) -> None:
        if not self.surface_id or not self.frame_url or not self.origin:
            raise ValueError("form surface identity is incomplete")
        if any(isinstance(item, bool) or item < 0 for item in self.frame_path):
            raise ValueError("form surface frame path is invalid")
        if isinstance(self.control_count, bool) or self.control_count < 0:
            raise ValueError("form surface control_count is invalid")


@dataclass(frozen=True, slots=True)
class ControlDescriptor:
    """Stable visible locator and classification bound to one P1 page epoch."""

    descriptor_id: str
    actor_id: str
    attempt_id: str
    application_session_id: str
    browser_generation: int
    provider: Provider
    page_binding: PageBinding
    surface_id: str
    frame_path: tuple[int, ...]
    frame_url: str
    shadow_path: tuple[str, ...]
    locator: str
    kind: ControlKind
    semantic: str
    label: str
    required: bool
    writable: bool
    stateful: bool
    options: tuple[str, ...] = ()
    options_truncated: bool = False
    schema_version: str = CONTROL_DESCRIPTOR_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in (
            "descriptor_id",
            "actor_id",
            "attempt_id",
            "application_session_id",
            "surface_id",
            "frame_url",
            "locator",
            "kind",
            "semantic",
            "schema_version",
        ):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"ControlDescriptor {name} is required")
        if self.actor_id != self.page_binding.owner_id:
            raise ValueError("descriptor actor does not own the page binding")
        if self.attempt_id != self.page_binding.attempt_id:
            raise ValueError("descriptor attempt does not own the page binding")
        if isinstance(self.browser_generation, bool) or self.browser_generation < 1:
            raise ValueError("descriptor browser_generation is invalid")
        if self.kind not in _SUPPORTED_KINDS:
            raise ValueError("descriptor kind is unsupported")
        if len(self.options) > MAX_OPTIONS:
            raise ValueError("descriptor options exceed the bounded contract")

    @property
    def locator_digest(self) -> str:
        return _digest(
            {
                "frame_path": self.frame_path,
                "shadow_path": self.shadow_path,
                "locator": self.locator,
                "surface_id": self.surface_id,
            }
        )


@dataclass(frozen=True, slots=True)
class FormInspection:
    """Complete supported control census for one exact page epoch."""

    provider: Provider
    context: ContextBundle
    page_binding: PageBinding
    surfaces: tuple[FormSurface, ...]
    controls: tuple[ControlDescriptor, ...]
    proof_complete: bool
    schema_version: str = CONTROL_DESCRIPTOR_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_context_binding(self.context, self.page_binding)
        if not self.surfaces:
            raise ValueError("FormInspection requires at least the main surface")
        if len(self.controls) > MAX_CONTROLS:
            raise ValueError("FormInspection exceeds the control limit")
        if self.proof_complete is not True:
            raise ValueError("incomplete FormInspection cannot be constructed")
        descriptor_ids = [item.descriptor_id for item in self.controls]
        if len(descriptor_ids) != len(set(descriptor_ids)):
            raise ValueError("FormInspection descriptor identities are ambiguous")

    def require(self, descriptor_id: str) -> ControlDescriptor:
        matches = [item for item in self.controls if item.descriptor_id == descriptor_id]
        if len(matches) != 1:
            raise ControlInspectionDenied("control descriptor is absent or ambiguous")
        return matches[0]


_INSPECT_SCRIPT = r"""() => {
  const limit = 200;
  const optionLimit = 100;
  const controls = [];
  const blocked = [];
  const roots = [{root: document, shadowPath: []}];
  const escapeAttr = (value) => String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  const rendered = (element) => {
    const view = element.ownerDocument && element.ownerDocument.defaultView;
    if (!view) return false;
    const style = view.getComputedStyle(element);
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      style.opacity !== '0' && element.getClientRects().length > 0;
  };
  const selected = (element) => element.checked === true ||
    ['true', 'mixed'].includes(String(element.getAttribute('aria-checked') || '').toLowerCase()) ||
    ['true', 'mixed'].includes(String(element.getAttribute('aria-pressed') || '').toLowerCase());
  const required = (element, label) => element.required === true ||
    String(element.getAttribute('aria-required') || '').toLowerCase() === 'true' ||
    /[✱*]/.test(label);
  const uniqueSelector = (element, root) => {
    const candidates = [];
    if (element.id) candidates.push(`#${CSS.escape(element.id)}`);
    for (const attribute of ['data-automation-id', 'data-testid', 'name']) {
      const value = element.getAttribute(attribute);
      if (value) candidates.push(`${element.tagName.toLowerCase()}[${attribute}="${escapeAttr(value)}"]`);
    }
    for (const candidate of candidates) {
      try {
        if (root.querySelectorAll(candidate).length === 1) return candidate;
      } catch (_error) {}
    }
    return '';
  };
  const labelText = (element, root) => {
    const explicit = element.id
      ? [...root.querySelectorAll('label[for]')].find((label) => label.getAttribute('for') === element.id)
      : null;
    const wrapping = element.closest('label');
    const legend = element.closest('fieldset')?.querySelector('legend');
    return String(
      (explicit && explicit.innerText) || element.getAttribute('aria-label') ||
      (wrapping && wrapping.innerText) || (legend && legend.innerText) ||
      element.placeholder || element.innerText || element.value || element.name || element.id || ''
    ).replace(/\s+/g, ' ').trim().slice(0, 240);
  };
  const semantic = (element, label, kind) => {
    const text = `${label} ${element.name || ''} ${element.id || ''} ${element.autocomplete || ''}`.toLowerCase();
    if (kind === 'navigation') return 'page_progress';
    if (kind === 'final_submit') return 'final_submit';
    if (kind === 'date') return 'date';
    if (kind === 'resume_file') return 'resume';
    if (/email/.test(text)) return 'email';
    if (/first.?name|given.?name/.test(text)) return 'first_name';
    if (/last.?name|family.?name|surname/.test(text)) return 'last_name';
    if (/full.?name|candidate.?name/.test(text)) return 'full_name';
    if (/phone|mobile|telephone/.test(text)) return 'phone';
    if (/address|location|city|country/.test(text)) return 'location';
    if (['checkbox', 'radio', 'switch'].includes(kind)) return label ? 'boolean_choice' : 'unknown';
    return label ? 'ordinary_text' : 'unknown';
  };
  const classify = (element, label) => {
    const tag = element.tagName;
    const role = String(element.getAttribute('role') || '').toLowerCase();
    const type = String(element.type || '').toLowerCase();
    const action = String(element.innerText || element.value || element.getAttribute('aria-label') || '')
      .replace(/\s+/g, ' ').trim();
    if ((tag === 'BUTTON' || role === 'button' || ['submit', 'button'].includes(type)) &&
        /^(submit|submit application|send application|finish|complete application)$/i.test(action)) return 'final_submit';
    if ((tag === 'BUTTON' || role === 'button' || ['submit', 'button'].includes(type)) &&
        /^(next|continue|review|review application)$/i.test(action)) return 'navigation';
    if (tag === 'TEXTAREA') return 'textarea';
    if (tag === 'SELECT') return 'native_select';
    if (role === 'combobox' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) return 'custom_combobox';
    if (role === 'switch') return 'switch';
    if (role === 'checkbox' || type === 'checkbox') return 'checkbox';
    if (role === 'radio' || type === 'radio') return 'radio';
    if (tag === 'INPUT' && type === 'date') return 'date';
    if (tag === 'INPUT' && type === 'file' && /\b(?:resume|résumé|cv|curriculum vitae)\b/i.test(label)) return 'resume_file';
    if (tag === 'INPUT' && ['text', 'search', 'email', 'tel', 'number', 'url', ''].includes(type)) return 'text';
    if (element.hasAttribute('aria-checked') || element.hasAttribute('aria-pressed')) return 'unknown_stateful';
    if (tag === 'INPUT' || element.isContentEditable || role) return 'unknown';
    return '';
  };
  for (let index = 0; index < roots.length; index += 1) {
    const {root, shadowPath} = roots[index];
    const descendants = [...root.querySelectorAll('*')];
    for (const element of descendants) {
      if (element.hasAttribute('data-applypilot-closed-shadow')) {
        blocked.push('closed_shadow_root');
      }
      if (element.shadowRoot) {
        const hostSelector = uniqueSelector(element, root);
        if (!hostSelector) blocked.push('unstable_shadow_host_locator');
        else roots.push({root: element.shadowRoot, shadowPath: [...shadowPath, hostSelector]});
      }
    }
    const candidates = [...root.querySelectorAll(
      'input,textarea,select,button,[role="button"],[role="combobox"],[role="radio"],'+
      '[role="checkbox"],[role="switch"],[aria-checked],[aria-pressed],[contenteditable="true"]'
    )];
    for (const element of candidates) {
      const label = labelText(element, root);
      const kind = classify(element, label);
      if (!kind) continue;
      const isStateful = ['radio', 'checkbox', 'switch', 'unknown_stateful'].includes(kind);
      const active = rendered(element) || selected(element) || required(element, label);
      if (!active) continue;
      if (kind === 'unknown' || kind === 'unknown_stateful') {
        blocked.push(kind === 'unknown_stateful' ? 'stateful_control_unclassified' : 'control_kind_unknown');
        continue;
      }
      const locator = uniqueSelector(element, root);
      if (!locator) {
        blocked.push('unstable_or_ambiguous_locator');
        continue;
      }
      let rawOptions = [];
      if (kind === 'native_select') {
        rawOptions = [...element.options].map((option) =>
          String(option.textContent || '').replace(/\s+/g, ' ').trim()
        );
      } else if (kind === 'custom_combobox') {
        const controlledId = String(element.getAttribute('aria-controls') || '').trim();
        let container = null;
        if (controlledId) {
          try {
            container = root.querySelector(`#${CSS.escape(controlledId)}`) ||
              document.querySelector(`#${CSS.escape(controlledId)}`);
          } catch (_error) {}
        }
        rawOptions = container
          ? [...container.querySelectorAll('[role="option"]')].map((option) =>
              String(option.innerText || option.textContent || '').replace(/\s+/g, ' ').trim()
            ).filter(Boolean)
          : [];
        if (!rawOptions.length) blocked.push('custom_combobox_options_unproven');
      }
      controls.push({
        shadow_path: shadowPath,
        locator,
        kind,
        semantic: semantic(element, label, kind),
        label,
        required: required(element, label),
        writable: !element.disabled && !element.readOnly &&
          String(element.getAttribute('aria-disabled') || '').toLowerCase() !== 'true' && kind !== 'final_submit',
        stateful: isStateful,
        options: rawOptions.slice(0, optionLimit),
        options_truncated: rawOptions.length > optionLimit
      });
      if (controls.length > limit) blocked.push('control_coverage_overflow');
    }
  }
  return {controls: controls.slice(0, limit), blocked: [...new Set(blocked)]};
}"""


def inspect_form_surfaces(
    page: object,
    context: ContextBundle,
    *,
    provider: Provider | None = None,
) -> FormInspection:
    """Inspect all same-origin frames and open shadow roots without mutation."""

    binding = PageBinding.from_mapping(context.page_binding)
    _validate_context_binding(context, binding)
    page_url = str(getattr(page, "url", "") or "")
    resolved_provider = provider_for_url(page_url)
    if resolved_provider is None or (provider is not None and provider != resolved_provider):
        raise ControlInspectionDenied("page provider is unsupported or changed")
    from applypilot.apply.ats import default_ats_registry

    adapter = default_ats_registry().get(resolved_provider)
    if adapter is None:
        raise ControlInspectionDenied("provider adapter is unavailable")
    admitted_kinds = adapter.semantic_control_kinds()
    target_id = _require_no_closed_shadow_roots(page)
    if target_id not in context.root_target_ids:
        raise ControlInspectionDenied("page target is outside the P1 ContextBundle")

    main_frame = getattr(page, "main_frame", page)
    main_origin = _origin(page_url or getattr(main_frame, "url", ""))
    if main_origin is None:
        raise ControlInspectionDenied("main application origin is unproven")

    frames = list(getattr(page, "frames", ()) or (main_frame,))
    surfaces: list[FormSurface] = []
    descriptors: list[ControlDescriptor] = []
    for frame in frames:
        path = _frame_path(main_frame, frame)
        frame_url = str(getattr(frame, "url", "") or "")
        frame_origin = _inherited_origin(frame, main_frame)
        if frame_origin != main_origin:
            raise ControlInspectionDenied("cross_origin_frame_inaccessible")
        try:
            observed = frame.evaluate(_INSPECT_SCRIPT)
        except Exception as exc:
            raise ControlInspectionDenied("application_frame_inaccessible") from exc
        if not isinstance(observed, Mapping):
            raise ControlInspectionDenied("control inspection result is invalid")
        blocked = observed.get("blocked")
        if not isinstance(blocked, list) or any(not isinstance(item, str) for item in blocked):
            raise ControlInspectionDenied("control coverage proof is invalid")
        if blocked:
            raise ControlInspectionDenied(str(blocked[0]))
        raw_controls = observed.get("controls")
        if not isinstance(raw_controls, list):
            raise ControlInspectionDenied("control coverage proof is incomplete")
        for raw in raw_controls:
            if not isinstance(raw, Mapping):
                raise ControlInspectionDenied("control descriptor payload is invalid")
            kind = str(raw.get("kind") or "")
            if kind != "final_submit" and kind not in admitted_kinds:
                raise ControlInspectionDenied("provider_control_kind_unclassified")
        surface_id = _digest(
            {
                "application_session_id": context.application_session_id,
                "frame_path": path,
                "frame_url": frame_url,
                "page_binding": binding.as_dict(),
            }
        )
        surface_descriptors = [
            _descriptor_from_raw(
                raw,
                context=context,
                binding=binding,
                provider=resolved_provider,
                surface_id=surface_id,
                frame_path=path,
                frame_url=frame_url,
            )
            for raw in raw_controls
        ]
        surfaces.append(
            FormSurface(
                surface_id=surface_id,
                frame_path=path,
                frame_url=frame_url,
                origin=main_origin,
                control_count=len(surface_descriptors),
            )
        )
        descriptors.extend(surface_descriptors)
        if len(descriptors) > MAX_CONTROLS:
            raise ControlInspectionDenied("control_coverage_overflow")
    return FormInspection(
        provider=resolved_provider,
        context=context,
        page_binding=binding,
        surfaces=tuple(surfaces),
        controls=tuple(descriptors),
        proof_complete=True,
    )


def _descriptor_from_raw(
    raw: object,
    *,
    context: ContextBundle,
    binding: PageBinding,
    provider: Provider,
    surface_id: str,
    frame_path: tuple[int, ...],
    frame_url: str,
) -> ControlDescriptor:
    if not isinstance(raw, Mapping):
        raise ControlInspectionDenied("control descriptor payload is invalid")
    kind = str(raw.get("kind") or "")
    if kind not in _SUPPORTED_KINDS:
        raise ControlInspectionDenied("control_kind_unknown")
    shadow_path = raw.get("shadow_path")
    options = raw.get("options")
    if not isinstance(shadow_path, list) or not all(isinstance(item, str) for item in shadow_path):
        raise ControlInspectionDenied("shadow locator path is invalid")
    if not isinstance(options, list) or not all(isinstance(item, str) for item in options):
        raise ControlInspectionDenied("control option proof is invalid")
    locator = str(raw.get("locator") or "")
    semantic = str(raw.get("semantic") or "")
    identity = {
        "surface_id": surface_id,
        "shadow_path": shadow_path,
        "locator": locator,
        "kind": kind,
        "page_binding": binding.as_dict(),
    }
    return ControlDescriptor(
        descriptor_id=_digest(identity),
        actor_id=context.actor_id,
        attempt_id=context.attempt_id,
        application_session_id=context.application_session_id,
        browser_generation=context.browser_generation,
        provider=provider,
        page_binding=binding,
        surface_id=surface_id,
        frame_path=frame_path,
        frame_url=frame_url,
        shadow_path=tuple(shadow_path),
        locator=locator,
        kind=kind,  # type: ignore[arg-type]
        semantic=semantic,
        label=str(raw.get("label") or ""),
        required=raw.get("required") is True,
        writable=raw.get("writable") is True,
        stateful=raw.get("stateful") is True,
        options=tuple(options),
        options_truncated=raw.get("options_truncated") is True,
    )


def _origin(value: object) -> str | None:
    try:
        parsed = urlparse(str(value or ""))
        scheme = parsed.scheme.casefold()
        host = (parsed.hostname or "").casefold()
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None
    if scheme not in {"http", "https"} or not host:
        return None
    return f"{scheme}://{host}:{port}"


def _inherited_origin(frame: object, main_frame: object) -> str | None:
    value = str(getattr(frame, "url", "") or "").strip()
    direct = _origin(value)
    if direct is not None:
        return direct
    if value.casefold() not in {"about:blank", "about:srcdoc", ""}:
        return None
    parent = getattr(frame, "parent_frame", None)
    seen: set[int] = set()
    while parent is not None and id(parent) not in seen:
        direct = _origin(getattr(parent, "url", ""))
        if direct is not None:
            return direct
        if parent is main_frame:
            break
        seen.add(id(parent))
        parent = getattr(parent, "parent_frame", None)
    return _origin(getattr(main_frame, "url", ""))


def _frame_path(main_frame: object, frame: object) -> tuple[int, ...]:
    if frame is main_frame:
        return ()
    path: list[int] = []
    current = frame
    seen: set[int] = set()
    while current is not main_frame:
        if id(current) in seen:
            raise ControlInspectionDenied("frame ancestry is cyclic")
        seen.add(id(current))
        parent = getattr(current, "parent_frame", None)
        if parent is None:
            raise ControlInspectionDenied("frame ancestry is detached")
        children = list(getattr(parent, "child_frames", ()) or ())
        matches = [index for index, child in enumerate(children) if child is current]
        if len(matches) != 1:
            raise ControlInspectionDenied("frame ancestry is ambiguous")
        path.append(matches[0])
        current = parent
    return tuple(reversed(path))


def _require_no_closed_shadow_roots(page: object) -> str:
    """Use Chromium's pierced DOM census; unavailable evidence fails closed."""

    context = getattr(page, "context", None)
    factory = getattr(context, "new_cdp_session", None)
    if not callable(factory):
        raise ControlInspectionDenied("closed_shadow_observability_unproven")
    session = None
    try:
        session = factory(page)
        target_payload = session.send("Target.getTargetInfo")
        payload = session.send("DOM.getDocument", {"depth": -1, "pierce": True})
    except Exception as exc:
        raise ControlInspectionDenied("closed_shadow_observability_unproven") from exc
    finally:
        if session is not None:
            detach = getattr(session, "detach", None)
            if callable(detach):
                detach()
    root = payload.get("root") if isinstance(payload, Mapping) else None
    target_info = (
        target_payload.get("targetInfo")
        if isinstance(target_payload, Mapping)
        else None
    )
    target_id = (
        str(target_info.get("targetId") or "")
        if isinstance(target_info, Mapping)
        else ""
    )
    if not target_id:
        raise ControlInspectionDenied("page_target_observability_unproven")
    if not isinstance(root, Mapping):
        raise ControlInspectionDenied("closed_shadow_observability_unproven")
    stack: list[Mapping[str, object]] = [root]
    while stack:
        node = stack.pop()
        roots = node.get("shadowRoots", [])
        if isinstance(roots, list):
            for shadow in roots:
                if not isinstance(shadow, Mapping):
                    continue
                if str(shadow.get("shadowRootType") or "").casefold() == "closed":
                    raise ControlInspectionDenied("closed_shadow_root")
                stack.append(shadow)
        children = node.get("children", [])
        if isinstance(children, list):
            stack.extend(child for child in children if isinstance(child, Mapping))
    return target_id


@dataclass(frozen=True, slots=True)
class SemanticControlRequest:
    """One exact non-submit semantic operation against a descriptor."""

    descriptor: ControlDescriptor
    operation: SemanticOperation
    value: str | bool

    def __post_init__(self) -> None:
        allowed: dict[str, frozenset[str]] = {
            "set_text": frozenset({"text", "textarea"}),
            "select_option": frozenset({"native_select", "custom_combobox"}),
            "set_checked": frozenset({"radio", "checkbox", "switch"}),
            "set_date": frozenset({"date"}),
            "activate_navigation": frozenset({"navigation"}),
        }
        if self.descriptor.kind == "final_submit":
            raise SemanticControlDenied("final Submit is outside semantic control authority")
        if self.descriptor.kind == "resume_file":
            raise SemanticControlDenied(
                "resume file writes require the existing bound resume capability"
            )
        if self.descriptor.kind not in allowed.get(self.operation, frozenset()):
            raise ValueError("semantic operation does not match the control kind")
        if self.operation == "set_checked":
            if not isinstance(self.value, bool):
                raise TypeError("set_checked requires a boolean")
            if self.descriptor.kind == "radio" and self.value is not True:
                raise ValueError("radio operations may only select the exact option")
        elif self.operation == "activate_navigation":
            if self.value != self.descriptor.label:
                raise ValueError("navigation value must match the visible label")
        elif self.operation == "select_option":
            if (
                not isinstance(self.value, str)
                or self.descriptor.options_truncated
                or self.descriptor.options.count(self.value) != 1
            ):
                raise ValueError("select_option requires one exact visible option")
        elif not isinstance(self.value, str) or not self.value:
            raise ValueError("semantic control value is required")

    @property
    def value_digest(self) -> str:
        return hashlib.sha256(_canonical_json(self.value)).hexdigest()


@dataclass(frozen=True, slots=True)
class SemanticControlAuthority:
    """Opaque, single-use, parent-issued capability for one exact operation."""

    actor_id: str
    attempt_id: str
    application_session_id: str
    browser_generation: int
    page_binding: PageBinding
    provider: Provider
    descriptor_id: str
    locator_digest: str
    operation: SemanticOperation
    value_digest: str
    operation_digest: str
    expires_at: float
    nonce: str
    submit_authority: bool
    signature: str

    def __reduce__(self) -> object:
        raise TypeError("semantic control authority cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("semantic control authority cannot be serialized")


class SemanticControlAuthorityIssuer:
    """Private HMAC capability issuer tied to P1 context and page generation."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._secret = secrets.token_bytes(32)
        self._nonces: dict[str, float] = {}
        self._operation_digests: set[str] = set()
        self._lock = threading.Lock()

    def issue(
        self,
        *,
        context: ContextBundle,
        bundle: BrowserLeaseBundle,
        request: SemanticControlRequest,
        submit_started: bool,
    ) -> SemanticControlAuthority:
        if submit_started:
            raise SemanticControlDenied("semantic controls are pre-submit only")
        _validate_request_binding(context, bundle, request)
        operation_digest = _digest(
            {
                "policy": SEMANTIC_CONTROL_POLICY,
                "application_session_id": context.application_session_id,
                "browser_generation": context.browser_generation,
                "page_binding": bundle.page_binding.as_dict(),
                "provider": request.descriptor.provider,
                "descriptor_id": request.descriptor.descriptor_id,
                "locator_digest": request.descriptor.locator_digest,
                "operation": request.operation,
                "value_digest": request.value_digest,
                "submit_authority": False,
            }
        )
        nonce = secrets.token_hex(16)
        unsigned = SemanticControlAuthority(
            actor_id=context.actor_id,
            attempt_id=context.attempt_id,
            application_session_id=context.application_session_id,
            browser_generation=context.browser_generation,
            page_binding=bundle.page_binding,
            provider=request.descriptor.provider,
            descriptor_id=request.descriptor.descriptor_id,
            locator_digest=request.descriptor.locator_digest,
            operation=request.operation,
            value_digest=request.value_digest,
            operation_digest=operation_digest,
            expires_at=self._clock() + self._ttl_seconds,
            nonce=nonce,
            submit_authority=False,
            signature="",
        )
        authority = replace(unsigned, signature=self._sign(unsigned))
        with self._lock:
            if operation_digest in self._operation_digests:
                raise SemanticControlDenied(
                    "semantic control operation was already issued for this page epoch"
                )
            now = self._clock()
            self._nonces = {key: expiry for key, expiry in self._nonces.items() if expiry > now}
            self._nonces[nonce] = authority.expires_at
            self._operation_digests.add(operation_digest)
        return authority

    def verify_and_consume(
        self,
        authority: SemanticControlAuthority,
        *,
        context: ContextBundle,
        bundle: BrowserLeaseBundle,
        request: SemanticControlRequest,
    ) -> None:
        if not isinstance(authority, SemanticControlAuthority):
            raise SemanticControlDenied("semantic control authority has the wrong type")
        if not hmac.compare_digest(
            authority.signature,
            self._sign(replace(authority, signature="")),
        ):
            raise SemanticControlDenied("semantic control authority signature is invalid")
        if self._clock() >= authority.expires_at:
            raise SemanticControlDenied("semantic control authority expired")
        _validate_request_binding(context, bundle, request)
        expected = (
            context.actor_id,
            context.attempt_id,
            context.application_session_id,
            context.browser_generation,
            bundle.page_binding,
            request.descriptor.provider,
            request.descriptor.descriptor_id,
            request.descriptor.locator_digest,
            request.operation,
            request.value_digest,
            _digest(
                {
                    "policy": SEMANTIC_CONTROL_POLICY,
                    "application_session_id": context.application_session_id,
                    "browser_generation": context.browser_generation,
                    "page_binding": bundle.page_binding.as_dict(),
                    "provider": request.descriptor.provider,
                    "descriptor_id": request.descriptor.descriptor_id,
                    "locator_digest": request.descriptor.locator_digest,
                    "operation": request.operation,
                    "value_digest": request.value_digest,
                    "submit_authority": False,
                }
            ),
            False,
        )
        actual = (
            authority.actor_id,
            authority.attempt_id,
            authority.application_session_id,
            authority.browser_generation,
            authority.page_binding,
            authority.provider,
            authority.descriptor_id,
            authority.locator_digest,
            authority.operation,
            authority.value_digest,
            authority.operation_digest,
            authority.submit_authority,
        )
        if actual != expected:
            raise SemanticControlDenied("semantic control authority binding mismatch")
        with self._lock:
            expiry = self._nonces.pop(authority.nonce, None)
        if expiry != authority.expires_at:
            raise SemanticControlDenied("semantic control authority was already consumed")

    def _sign(self, authority: SemanticControlAuthority) -> str:
        payload = asdict(authority)
        payload["page_binding"] = authority.page_binding.as_dict()
        payload["signature"] = ""
        return hmac.new(self._secret, _canonical_json(payload), hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class ControlObservation:
    """Sanitized postcondition observation; no raw locator or secret metadata."""

    descriptor_id: str
    value: str | None = None
    checked: bool | None = None
    page_signature: str | None = None


@dataclass(frozen=True, slots=True)
class SemanticControlResult:
    bundle: BrowserLeaseBundle
    descriptor_id: str
    operation: SemanticOperation
    observation: ControlObservation


class SemanticControlDriver(Protocol):
    def perform(self, request: SemanticControlRequest) -> ControlObservation: ...

    def observe(self, request: SemanticControlRequest) -> ControlObservation: ...


class SemanticControlBroker(Protocol):
    def validate_page(self, binding: PageBinding) -> PageBinding: ...

    def advance_page(
        self,
        bundle: BrowserLeaseBundle,
        *,
        expected_page_epoch: int,
    ) -> BrowserLeaseBundle: ...


def execute_semantic_control(
    broker: SemanticControlBroker,
    driver: SemanticControlDriver,
    issuer: SemanticControlAuthorityIssuer,
    *,
    bundle: BrowserLeaseBundle,
    context: ContextBundle,
    authority: SemanticControlAuthority,
    request: SemanticControlRequest,
) -> SemanticControlResult:
    """Execute once, require two matching postcondition reads, then epoch CAS."""

    issuer.verify_and_consume(
        authority,
        context=context,
        bundle=bundle,
        request=request,
    )
    broker.validate_page(bundle.page_binding)
    if not request.descriptor.writable:
        raise SemanticControlDenied("control is not writable")
    try:
        first = driver.perform(request)
        second = driver.observe(request)
    except Exception as exc:
        raise SemanticControlUncertain(
            "semantic control write occurred but observation failed"
        ) from exc
    if not _postcondition_matches(request, first) or first != second:
        raise SemanticControlUncertain("semantic control postcondition is unproven")
    try:
        updated = broker.advance_page(
            bundle,
            expected_page_epoch=bundle.page_binding.page_epoch,
        )
    except Exception as exc:
        raise SemanticControlUncertain(
            "semantic control postcondition passed but page epoch CAS failed"
        ) from exc
    return SemanticControlResult(
        bundle=updated,
        descriptor_id=request.descriptor.descriptor_id,
        operation=request.operation,
        observation=second,
    )


def _postcondition_matches(
    request: SemanticControlRequest,
    observation: ControlObservation,
) -> bool:
    if observation.descriptor_id != request.descriptor.descriptor_id:
        return False
    if request.operation in {"set_text", "set_date", "select_option"}:
        return observation.value == request.value
    if request.operation == "set_checked":
        return observation.checked is request.value
    if request.operation == "activate_navigation":
        return bool(observation.page_signature)
    return False


_CONTROL_SCRIPT = r"""(request) => {
  const resolve = () => {
    let root = document;
    for (const hostSelector of request.shadow_path) {
      const hosts = root.querySelectorAll(hostSelector);
      if (hosts.length !== 1 || !hosts[0].shadowRoot) throw new Error('shadow_locator_stale');
      root = hosts[0].shadowRoot;
    }
    const matches = root.querySelectorAll(request.locator);
    if (matches.length !== 1) throw new Error('control_locator_stale');
    return matches[0];
  };
  const deepElements = () => {
    const roots = [document];
    const result = [];
    for (let index = 0; index < roots.length; index += 1) {
      const elements = [...roots[index].querySelectorAll('*')];
      result.push(...elements);
      for (const element of elements) if (element.shadowRoot) roots.push(element.shadowRoot);
    }
    return result;
  };
  const observe = (element) => {
    const aria = String(element.getAttribute('aria-checked') || '').toLowerCase();
    const selected = element.tagName === 'SELECT' && element.selectedOptions[0]
      ? String(element.selectedOptions[0].textContent || '').replace(/\s+/g, ' ').trim()
      : String(element.getAttribute('aria-valuetext') || element.value || '')
          .replace(/\s+/g, ' ').trim();
    const signature = JSON.stringify({
      url: location.href,
      title: document.title,
      text: (document.body ? document.body.innerText : '').replace(/\s+/g, ' ').trim(),
      controls: document.querySelectorAll('input,select,textarea,button,[role]').length
    });
    return {
      descriptor_id: request.descriptor_id,
      value: selected || null,
      checked: typeof element.checked === 'boolean'
        ? element.checked
        : (aria === 'true' ? true : (aria === 'false' ? false : null)),
      page_signature: signature
    };
  };
  const element = resolve();
  if (request.action === 'observe') return observe(element);
  if (request.operation === 'set_text' || request.operation === 'set_date') {
    element.focus();
    const prototype = element.tagName === 'TEXTAREA'
      ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(prototype, 'value').set;
    setter.call(element, request.value);
    element.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: null}));
    element.dispatchEvent(new Event('change', {bubbles: true}));
  } else if (request.operation === 'select_option') {
    if (element.tagName === 'SELECT') {
      const options = [...element.options].filter((option) =>
        String(option.textContent || '').replace(/\s+/g, ' ').trim() === request.value ||
        String(option.value) === request.value
      );
      if (options.length !== 1) throw new Error('option_ambiguous_or_absent');
      element.value = options[0].value;
      element.dispatchEvent(new Event('input', {bubbles: true}));
      element.dispatchEvent(new Event('change', {bubbles: true}));
    } else {
      element.click();
      const options = deepElements().filter((candidate) =>
        candidate.getAttribute('role') === 'option' &&
        String(candidate.innerText || candidate.textContent || '').replace(/\s+/g, ' ').trim() === request.value
      );
      if (options.length !== 1) throw new Error('custom_option_ambiguous_or_absent');
      options[0].click();
    }
  } else if (request.operation === 'set_checked') {
    const current = typeof element.checked === 'boolean'
      ? element.checked
      : String(element.getAttribute('aria-checked') || '').toLowerCase() === 'true';
    if (current !== request.value) element.click();
  } else if (request.operation === 'activate_navigation') {
    element.click();
  } else {
    throw new Error('operation_forbidden');
  }
  return observe(element);
}"""


class PlaywrightSemanticControlDriver:
    """Real Playwright driver restricted to descriptors from one inspection."""

    def __init__(self, page: object, inspection: FormInspection) -> None:
        self._page = page
        self._inspection = inspection
        self._navigation_before: dict[str, str] = {}

    def perform(self, request: SemanticControlRequest) -> ControlObservation:
        descriptor = self._require_descriptor(request)
        frame = self._frame(descriptor.frame_path)
        if request.operation == "activate_navigation":
            before = self._frame_signature(frame)
            self._navigation_before[descriptor.descriptor_id] = before
        raw = frame.evaluate(_CONTROL_SCRIPT, self._payload(request, action="perform"))
        observed = _observation(raw)
        if request.operation == "activate_navigation":
            return self._navigation_observation(frame, request, observed)
        return observed

    def observe(self, request: SemanticControlRequest) -> ControlObservation:
        descriptor = self._require_descriptor(request)
        frame = self._frame(descriptor.frame_path)
        if request.operation == "activate_navigation":
            signature = self._frame_signature(frame)
            before = self._navigation_before.get(descriptor.descriptor_id)
            if not before or signature == before:
                return ControlObservation(descriptor_id=descriptor.descriptor_id)
            return ControlObservation(
                descriptor_id=descriptor.descriptor_id,
                page_signature=signature,
            )
        raw = frame.evaluate(_CONTROL_SCRIPT, self._payload(request, action="observe"))
        return _observation(raw)

    def _require_descriptor(self, request: SemanticControlRequest) -> ControlDescriptor:
        current = self._inspection.require(request.descriptor.descriptor_id)
        if current != request.descriptor:
            raise StalePageBinding("control descriptor changed after inspection")
        return current

    def _frame(self, path: tuple[int, ...]) -> object:
        frame = getattr(self._page, "main_frame", self._page)
        for index in path:
            children = list(getattr(frame, "child_frames", ()) or ())
            if index >= len(children):
                raise StalePageBinding("control frame detached after inspection")
            frame = children[index]
        return frame

    @staticmethod
    def _payload(
        request: SemanticControlRequest,
        *,
        action: str,
    ) -> dict[str, object]:
        descriptor = request.descriptor
        return {
            "action": action,
            "descriptor_id": descriptor.descriptor_id,
            "shadow_path": list(descriptor.shadow_path),
            "locator": descriptor.locator,
            "operation": request.operation,
            "value": request.value,
        }

    @staticmethod
    def _frame_signature(frame: object) -> str:
        raw = frame.evaluate(
            """() => JSON.stringify({url: location.href, title: document.title,
            text: (document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').trim(),
            controls: document.querySelectorAll('input,select,textarea,button,[role]').length})"""
        )
        return hashlib.sha256(str(raw).encode("utf-8")).hexdigest()

    def _navigation_observation(
        self,
        frame: object,
        request: SemanticControlRequest,
        observed: ControlObservation,
    ) -> ControlObservation:
        signature = self._frame_signature(frame)
        before = self._navigation_before.get(request.descriptor.descriptor_id)
        if not before or signature == before:
            return replace(observed, page_signature=None)
        return replace(observed, page_signature=signature)


def _observation(raw: object) -> ControlObservation:
    if not isinstance(raw, Mapping):
        raise SemanticControlUncertain("control observation payload is invalid")
    checked = raw.get("checked")
    if checked is not None and not isinstance(checked, bool):
        raise SemanticControlUncertain("control checked postcondition is invalid")
    value = raw.get("value")
    signature = raw.get("page_signature")
    return ControlObservation(
        descriptor_id=str(raw.get("descriptor_id") or ""),
        value=str(value) if value is not None else None,
        checked=checked,
        page_signature=str(signature) if signature is not None else None,
    )


def _validate_context_binding(context: ContextBundle, binding: PageBinding) -> None:
    if context.actor_id != binding.owner_id or context.attempt_id != binding.attempt_id:
        raise SemanticControlDenied("P1 context does not own the page binding")
    if dict(context.page_binding) != binding.as_dict():
        raise SemanticControlDenied("P1 context page binding is not canonical")
    if context.endpoint.generation != context.browser_generation:
        raise SemanticControlDenied("P1 browser generation is stale")


def _validate_request_binding(
    context: ContextBundle,
    bundle: BrowserLeaseBundle,
    request: SemanticControlRequest,
) -> None:
    _validate_context_binding(context, bundle.page_binding)
    descriptor = request.descriptor
    if descriptor.page_binding != bundle.page_binding:
        raise SemanticControlDenied("control descriptor page epoch is stale")
    if (
        descriptor.actor_id != context.actor_id
        or descriptor.attempt_id != context.attempt_id
        or descriptor.application_session_id != context.application_session_id
        or descriptor.browser_generation != context.browser_generation
    ):
        raise SemanticControlDenied("control descriptor P1 context binding changed")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=lambda item: item.as_dict() if hasattr(item, "as_dict") else str(item),
    ).encode("utf-8")


__all__ = [
    "ControlDescriptor",
    "ControlInspectionDenied",
    "ControlObservation",
    "FormInspection",
    "FormSurface",
    "PlaywrightSemanticControlDriver",
    "SemanticControlAuthority",
    "SemanticControlAuthorityIssuer",
    "SemanticControlDenied",
    "SemanticControlRequest",
    "SemanticControlResult",
    "SemanticControlUncertain",
    "execute_semantic_control",
    "inspect_form_surfaces",
    "provider_for_url",
]
