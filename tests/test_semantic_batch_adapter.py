from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import ClassVar

import pytest

from applypilot.apply import semantic_batch_adapter as adapter_mod
from applypilot.apply.application_sessions import ContextBundle, EndpointDescriptor
from applypilot.apply.browser_broker import BrowserBroker, StalePageBinding
from applypilot.apply.contracts import application_actor_id
from applypilot.apply.control_descriptors import (
    ControlDescriptor,
    ControlObservation,
    FormInspection,
    FormSurface,
)
from applypilot.apply.runtime_namespace import RuntimeNamespace
from applypilot.apply.semantic_batch import (
    BatchControlDescriptor,
    BrowserPageObservation,
    SemanticBatchDenied,
)

URL = "https://tenant.wd5.myworkdayjobs.com/apply/REQ-1"


def _context_and_inspection(tmp_path: Path, *, kind: str = "text"):
    attempt_id = "attempt-adapter"
    actor_id = application_actor_id(attempt_id)
    bundle = BrowserBroker().acquire_bundle(
        profile_id="profile-adapter",
        page_id="page-adapter",
        owner_id=actor_id,
        scope_id="scope-adapter",
        attempt_id=attempt_id,
        runtime_id="runtime-adapter",
    )
    context = ContextBundle(
        namespace=RuntimeNamespace(tmp_path, "run", "session", "profile"),
        worker_id=0,
        application_session_id="application-adapter",
        actor_id=actor_id,
        attempt_id=attempt_id,
        phase="prepare",
        runtime_backend="test",
        browser_runtime="edge",
        browser_profile_id="profile-adapter",
        browser_generation=1,
        endpoint=EndpointDescriptor("endpoint", 1, "stdio", "test", False),
        root_target_ids=("target-adapter",),
        page_binding=bundle.page_binding.as_dict(),
    )
    descriptor = ControlDescriptor(
        descriptor_id="d" * 64,
        actor_id=actor_id,
        attempt_id=attempt_id,
        application_session_id=context.application_session_id,
        browser_generation=1,
        provider="workday",
        page_binding=bundle.page_binding,
        surface_id="s" * 64,
        frame_path=(),
        frame_url=URL,
        shadow_path=(),
        locator="#email",
        kind=kind,  # type: ignore[arg-type]
        semantic="email" if kind in {"text", "textarea"} else "page_progress",
        label="Email" if kind in {"text", "textarea"} else "Next",
        required=True,
        writable=True,
        stateful=False,
    )
    surface = FormSurface("s" * 64, (), URL, "https://tenant.wd5.myworkdayjobs.com:443", 1)
    inspection = FormInspection(
        provider="workday",
        context=context,
        page_binding=bundle.page_binding,
        surfaces=(surface,),
        controls=(descriptor,),
        proof_complete=True,
    )
    return context, inspection


class _Page:
    url = URL


class _Driver:
    values: ClassVar[dict[str, str]] = {}
    perform_calls: ClassVar[int] = 0

    def __init__(self, _page, inspection) -> None:
        self.inspection = inspection

    def observe(self, request) -> ControlObservation:
        return ControlObservation(
            descriptor_id=request.descriptor.descriptor_id,
            value=self.values.get(request.descriptor.descriptor_id, ""),
        )

    def perform(self, request) -> ControlObservation:
        type(self).perform_calls += 1
        self.values[request.descriptor.descriptor_id] = str(request.value)
        return self.observe(request)


def test_playwright_adapter_executes_verified_routine_control_once(
    monkeypatch,
    tmp_path: Path,
) -> None:
    context, inspection = _context_and_inspection(tmp_path)
    _Driver.values = {}
    _Driver.perform_calls = 0
    monkeypatch.setattr(adapter_mod, "inspect_form_surfaces", lambda *_a, **_k: inspection)
    monkeypatch.setattr(adapter_mod, "PlaywrightSemanticControlDriver", _Driver)
    effects: list[bool] = []
    adapter = adapter_mod.PlaywrightProductionSemanticBatchAdapter(
        _Page(),
        context,
        provider="workday",
        values={"email": "private@example.test"},
        validate_authority=lambda: None,
    )
    control = adapter.control_for("email")
    adapter.bind_effect_sink(lambda: effects.append(True))

    adapter.apply_routine_control(control, "private@example.test")

    assert control.classification == "routine"
    assert adapter.effect_count == 1
    assert effects == [True]
    assert adapter.pristine() is False
    assert _Driver.perform_calls == 1


def test_playwright_adapter_denies_email_textarea_without_driver_write(
    monkeypatch,
    tmp_path: Path,
) -> None:
    context, inspection = _context_and_inspection(tmp_path, kind="textarea")
    _Driver.values = {}
    _Driver.perform_calls = 0
    monkeypatch.setattr(adapter_mod, "inspect_form_surfaces", lambda *_a, **_k: inspection)
    monkeypatch.setattr(adapter_mod, "PlaywrightSemanticControlDriver", _Driver)
    adapter = adapter_mod.PlaywrightProductionSemanticBatchAdapter(
        _Page(),
        context,
        provider="workday",
        values={"email": "private@example.test"},
        validate_authority=lambda: None,
    )

    control = adapter.control_for("email")

    assert control.classification == "sensitive"
    with pytest.raises(SemanticBatchDenied, match="not routine"):
        adapter.apply_routine_control(control, "private@example.test")
    assert adapter.effect_count == 0
    assert _Driver.perform_calls == 0


def test_playwright_adapter_never_exposes_navigation_as_routine(
    monkeypatch,
    tmp_path: Path,
) -> None:
    context, inspection = _context_and_inspection(tmp_path, kind="navigation")
    monkeypatch.setattr(adapter_mod, "inspect_form_surfaces", lambda *_a, **_k: inspection)
    adapter = adapter_mod.PlaywrightProductionSemanticBatchAdapter(
        _Page(),
        context,
        provider="workday",
        values={"email": "private@example.test"},
        validate_authority=lambda: None,
    )

    control = adapter.control_for("page_progress")

    assert control.classification == "navigation"


def test_playwright_adapter_binds_country_plan_to_inspector_location_descriptor(
    monkeypatch,
    tmp_path: Path,
) -> None:
    context, inspection = _context_and_inspection(tmp_path, kind="native_select")
    country = replace(
        inspection.controls[0],
        semantic="location",
        label="Country Choose Singapore United States",
        options=("Choose", "Singapore", "United States"),
    )
    inspection = replace(inspection, controls=(country,))
    _Driver.values = {}
    _Driver.perform_calls = 0
    monkeypatch.setattr(adapter_mod, "inspect_form_surfaces", lambda *_a, **_k: inspection)
    monkeypatch.setattr(adapter_mod, "PlaywrightSemanticControlDriver", _Driver)
    adapter = adapter_mod.PlaywrightProductionSemanticBatchAdapter(
        _Page(),
        context,
        provider="workday",
        values={"country": "Singapore"},
        validate_authority=lambda: None,
    )

    control = adapter.control_for("country")

    assert control.classification == "routine"
    assert control.control_id == country.descriptor_id


def test_playwright_adapter_does_not_alias_country_code_ordinary_text(
    monkeypatch,
    tmp_path: Path,
) -> None:
    context, inspection = _context_and_inspection(tmp_path)
    unrelated = replace(
        inspection.controls[0],
        semantic="ordinary_text",
        label="Country code for phone",
    )
    inspection = replace(inspection, controls=(unrelated,))
    monkeypatch.setattr(adapter_mod, "inspect_form_surfaces", lambda *_a, **_k: inspection)
    adapter = adapter_mod.PlaywrightProductionSemanticBatchAdapter(
        _Page(),
        context,
        provider="workday",
        values={"country": "Singapore"},
        validate_authority=lambda: None,
    )

    with pytest.raises(SemanticBatchDenied, match="absent or ambiguous"):
        adapter.control_for("country")


@pytest.mark.parametrize(
    ("field_semantic", "label"),
    [
        ("country", "Country of birth Choose Singapore United States"),
        ("country", "Country of citizenship Choose Singapore United States"),
        ("state", "State of tax residence Choose Singapore United States"),
    ],
)
def test_playwright_adapter_rejects_sensitive_location_aliases(
    monkeypatch,
    tmp_path: Path,
    field_semantic: str,
    label: str,
) -> None:
    context, inspection = _context_and_inspection(tmp_path, kind="native_select")
    sensitive_location = replace(
        inspection.controls[0],
        semantic="location",
        label=label,
        options=("Choose", "Singapore", "United States"),
    )
    inspection = replace(inspection, controls=(sensitive_location,))
    monkeypatch.setattr(adapter_mod, "inspect_form_surfaces", lambda *_a, **_k: inspection)
    adapter = adapter_mod.PlaywrightProductionSemanticBatchAdapter(
        _Page(),
        context,
        provider="workday",
        values={field_semantic: "Singapore"},
        validate_authority=lambda: None,
    )

    with pytest.raises(SemanticBatchDenied, match="absent or ambiguous"):
        adapter.control_for(field_semantic)


def test_playwright_adapter_rechecks_browser_authority_before_observation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    context, inspection = _context_and_inspection(tmp_path)
    calls = 0

    def validate() -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise StalePageBinding("stale")

    monkeypatch.setattr(adapter_mod, "inspect_form_surfaces", lambda *_a, **_k: inspection)
    adapter = adapter_mod.PlaywrightProductionSemanticBatchAdapter(
        _Page(),
        context,
        provider="workday",
        values={"email": "private@example.test"},
        validate_authority=validate,
    )

    with pytest.raises(StalePageBinding, match="stale"):
        adapter.observe_page()


def test_batch_descriptor_shape_carries_no_browser_action_capability() -> None:
    descriptor = BatchControlDescriptor(
        control_id="control-email",
        field_semantic="email",
        classification="routine",
        page=BrowserPageObservation(URL, (), "a" * 64, 1),
    )

    assert descriptor.classification == "routine"
    assert not hasattr(descriptor, "click")
    assert not hasattr(descriptor, "submit")
