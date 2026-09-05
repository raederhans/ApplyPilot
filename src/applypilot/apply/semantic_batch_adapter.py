"""Playwright adapter for the routine-only semantic batch production path."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping

from applypilot.apply.application_sessions import ContextBundle
from applypilot.apply.control_descriptors import (
    ControlDescriptor,
    ControlObservation,
    FormInspection,
    PlaywrightSemanticControlDriver,
    SemanticControlDenied,
    SemanticControlRequest,
    inspect_form_surfaces,
)
from applypilot.apply.semantic_batch import (
    BatchControlDescriptor,
    BrowserPageObservation,
    SemanticBatchDenied,
)

ADAPTER_VERSION = "playwright-semantic-batch/v1"
_ROUTINE_KINDS = frozenset({"text", "native_select", "custom_combobox"})
_ROUTINE_LABEL_PATTERNS = {
    "country": re.compile(r"^country(?:\s*/\s*region|\s+or\s+region)?$", re.IGNORECASE),
}
_ROUTINE_ALIAS_SOURCE_SEMANTICS = {
    "country": frozenset({"location"}),
}


def inspection_signature(inspection: FormInspection) -> str:
    """Hash only structural, non-PII descriptor claims."""

    payload = {
        "provider": inspection.provider,
        "page_binding": inspection.page_binding.as_dict(),
        "surfaces": [
            {
                "surface_id": surface.surface_id,
                "frame_path": surface.frame_path,
                "control_count": surface.control_count,
            }
            for surface in inspection.surfaces
        ],
        "controls": [
            {
                "descriptor_id": control.descriptor_id,
                "frame_path": control.frame_path,
                "shadow_path": control.shadow_path,
                "locator_digest": control.locator_digest,
                "kind": control.kind,
                "semantic": control.semantic,
                "required": control.required,
                "writable": control.writable,
                "stateful": control.stateful,
                "option_count": len(control.options),
                "options_truncated": control.options_truncated,
            }
            for control in inspection.controls
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class PlaywrightProductionSemanticBatchAdapter:
    """Use one exact P1 inspection and no navigation/file/Submit capability."""

    adapter_version = ADAPTER_VERSION

    def __init__(
        self,
        page: object,
        context: ContextBundle,
        *,
        provider: str,
        values: Mapping[str, str],
        validate_authority: Callable[[], None],
    ) -> None:
        if provider not in {"workday", "smartrecruiters"}:
            raise ValueError("semantic batch provider is unsupported")
        if not values or any(not key or not str(value) for key, value in values.items()):
            raise ValueError("semantic batch values must be non-empty")
        self.provider = provider
        self._page = page
        self._context = context
        self._values = dict(values)
        self._validate_authority = validate_authority
        self._inspection = self._inspect()
        self._page_signature = inspection_signature(self._inspection)
        self._initial: dict[str, ControlObservation] = {}
        self._requests: dict[str, SemanticControlRequest] = {}
        self._effect_count = 0
        self._effect_sink: Callable[[], None] = lambda: None

    @property
    def page_signature(self) -> str:
        return self._page_signature

    @property
    def effect_count(self) -> int:
        return self._effect_count

    def bind_effect_sink(self, sink: Callable[[], None]) -> None:
        if self._effect_count:
            raise RuntimeError("effect sink cannot change after a browser effect")
        self._effect_sink = sink

    def observe_page(self) -> BrowserPageObservation:
        inspection = self._inspect()
        return BrowserPageObservation(
            page_url=str(getattr(self._page, "url", "") or ""),
            frame_path=(),
            page_signature=inspection_signature(inspection),
            page_epoch=inspection.page_binding.page_epoch,
        )

    def control_for(self, field_semantic: str) -> BatchControlDescriptor:
        inspection = self._inspect()
        matches = [
            descriptor
            for descriptor in inspection.controls
            if self._matches_semantic(descriptor, field_semantic)
        ]
        if len(matches) != 1:
            raise SemanticBatchDenied("routine semantic control is absent or ambiguous")
        descriptor = matches[0]
        classification = self._classification(descriptor)
        page = BrowserPageObservation(
            page_url=str(getattr(self._page, "url", "") or ""),
            frame_path=(),
            page_signature=inspection_signature(inspection),
            page_epoch=inspection.page_binding.page_epoch,
        )
        control = BatchControlDescriptor(
            control_id=descriptor.descriptor_id,
            field_semantic=field_semantic,
            classification=classification,
            page=page,
        )
        if classification == "routine":
            request = self._request(descriptor, self._values[field_semantic])
            driver = PlaywrightSemanticControlDriver(self._page, inspection)
            observed = driver.observe(request)
            self._requests[field_semantic] = request
            self._initial.setdefault(field_semantic, observed)
        return control

    def apply_routine_control(
        self,
        control: BatchControlDescriptor,
        value: str,
    ) -> None:
        if control.classification != "routine":
            raise SemanticBatchDenied("semantic batch control is not routine")
        inspection = self._inspect()
        matches = [
            descriptor
            for descriptor in inspection.controls
            if descriptor.descriptor_id == control.control_id
            and self._matches_semantic(descriptor, control.field_semantic)
        ]
        if len(matches) != 1:
            raise SemanticBatchDenied("semantic batch descriptor drifted before write")
        descriptor = matches[0]
        if self._classification(descriptor) != "routine":
            raise SemanticBatchDenied("semantic batch descriptor lost routine capability")
        request = self._request(descriptor, value)
        driver = PlaywrightSemanticControlDriver(self._page, inspection)
        before = driver.observe(request)
        self._initial.setdefault(control.field_semantic, before)
        if self._matches(request, before):
            if driver.observe(request) != before:
                raise SemanticBatchDenied("routine control changed during no-op verification")
            return
        first = driver.perform(request)
        self._effect_count += 1
        self._effect_sink()
        second = driver.observe(request)
        if not self._matches(request, first) or first != second:
            raise SemanticBatchDenied("routine control postcondition is unproven")
        self._requests[control.field_semantic] = request

    def pristine(self) -> bool:
        inspection = self._inspect()
        driver = PlaywrightSemanticControlDriver(self._page, inspection)
        for semantic, initial in self._initial.items():
            request = self._requests.get(semantic)
            if request is None:
                matches = [
                    descriptor
                    for descriptor in inspection.controls
                    if self._matches_semantic(descriptor, semantic)
                ]
                if len(matches) != 1 or self._classification(matches[0]) != "routine":
                    return False
                request = self._request(matches[0], self._values[semantic])
            try:
                current = driver.observe(request)
            except Exception:  # noqa: BLE001 - missing proof is not pristine
                return False
            if current != initial:
                return False
        return True

    def _inspect(self) -> FormInspection:
        self._validate_authority()
        inspection = inspect_form_surfaces(
            self._page,
            self._context,
            provider=self.provider,  # type: ignore[arg-type]
        )
        if inspection.page_binding.as_dict() != dict(self._context.page_binding):
            raise SemanticBatchDenied("P1 page binding changed during semantic batch")
        self._inspection = inspection
        return inspection

    @staticmethod
    def _classification(descriptor: ControlDescriptor) -> str:
        if descriptor.kind == "final_submit":
            return "final_submit"
        if descriptor.kind == "navigation":
            return "navigation"
        if descriptor.frame_path:
            return "frame"
        if descriptor.kind not in _ROUTINE_KINDS or descriptor.stateful or not descriptor.writable:
            return "sensitive"
        return "routine"

    @staticmethod
    def _matches_semantic(descriptor: ControlDescriptor, field_semantic: str) -> bool:
        if descriptor.semantic == field_semantic:
            return True
        pattern = _ROUTINE_LABEL_PATTERNS.get(field_semantic)
        admitted_sources = _ROUTINE_ALIAS_SOURCE_SEMANTICS.get(field_semantic)
        if pattern is None or admitted_sources is None or descriptor.semantic not in admitted_sources:
            return False
        label = " ".join(descriptor.label.replace("*", " ").replace("✱", " ").split())
        if descriptor.kind == "native_select" and descriptor.options:
            option_suffix = " ".join(" ".join(descriptor.options).split())
            if label.endswith(f" {option_suffix}"):
                label = label[: -(len(option_suffix) + 1)].rstrip()
        return pattern.fullmatch(label) is not None

    @staticmethod
    def _request(descriptor: ControlDescriptor, value: str) -> SemanticControlRequest:
        if descriptor.kind == "text":
            operation = "set_text"
        elif descriptor.kind in {"native_select", "custom_combobox"}:
            operation = "select_option"
        else:
            raise SemanticControlDenied("control kind is outside routine batch authority")
        return SemanticControlRequest(
            descriptor=descriptor,
            operation=operation,  # type: ignore[arg-type]
            value=value,
        )

    @staticmethod
    def _matches(
        request: SemanticControlRequest,
        observation: ControlObservation,
    ) -> bool:
        return observation.descriptor_id == request.descriptor.descriptor_id and observation.value == request.value


__all__ = [
    "ADAPTER_VERSION",
    "PlaywrightProductionSemanticBatchAdapter",
    "inspection_signature",
]
