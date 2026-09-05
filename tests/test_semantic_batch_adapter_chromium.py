from __future__ import annotations

import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

import pytest

from applypilot.apply.application_sessions import ContextBundle, EndpointDescriptor
from applypilot.apply.browser_broker import BrowserBroker
from applypilot.apply.contracts import application_actor_id
from applypilot.apply.control_descriptors import inspect_form_surfaces
from applypilot.apply.prepare_fast_path import run_prepare_fast_path
from applypilot.apply.runtime_namespace import RuntimeNamespace
from applypilot.apply.semantic_batch import BatchPageBinding, BrowserResourceIdentity, SemanticPatch
from applypilot.apply.semantic_batch_adapter import (
    ADAPTER_VERSION,
    PlaywrightProductionSemanticBatchAdapter,
)
from applypilot.apply.semantic_batch_runtime import (
    SemanticBatchRuntimeRequest,
    run_production_semantic_batch,
)

pytestmark = pytest.mark.browser

FIXTURE = Path(__file__).parent / "fixtures" / "apply" / "semantic_batch_browser.html"
CDP_ENDPOINT = os.environ.get("APPLYPILOT_TEST_CDP_ENDPOINT", "").strip()


def _connect_browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    playwright = sync_api.sync_playwright().start()
    try:
        if CDP_ENDPOINT:
            browser = playwright.chromium.connect_over_cdp(CDP_ENDPOINT)
        else:
            try:
                browser = playwright.chromium.launch(headless=True)
            except sync_api.Error:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
    except Exception:
        playwright.stop()
        raise
    return playwright, browser


def _routed_page(browser, provider: str):
    host = "tenant.myworkdayjobs.com" if provider == "workday" else "jobs.smartrecruiters.com"
    url = f"https://{host}/apply/local-fixture"
    html = FIXTURE.read_text(encoding="utf-8")
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()
    unexpected: list[str] = []

    def handle(route) -> None:
        if urlparse(route.request.url).hostname == host:
            route.fulfill(status=200, content_type="text/html", body=html)
        else:
            unexpected.append(route.request.url)
            route.abort()

    page.route("**/*", handle)
    page.goto(url, wait_until="load")
    assert unexpected == []
    return page, url, unexpected


def _bundle_context(tmp_path: Path, page, *, provider: str, suffix: str):
    attempt_id = f"attempt-{provider}-{suffix}"
    actor_id = application_actor_id(attempt_id)
    broker = BrowserBroker()
    bundle = broker.acquire_bundle(
        profile_id=f"profile-{suffix}",
        page_id=f"page-{suffix}",
        owner_id=actor_id,
        scope_id=f"scope-{suffix}",
        attempt_id=attempt_id,
        runtime_id=f"runtime-{suffix}",
    )
    session = page.context.new_cdp_session(page)
    try:
        target_id = session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
    finally:
        session.detach()
    context = ContextBundle(
        namespace=RuntimeNamespace(tmp_path, f"run-{suffix}", f"session-{suffix}", f"profile-{suffix}"),
        worker_id=0,
        application_session_id=f"application-{suffix}",
        actor_id=actor_id,
        attempt_id=attempt_id,
        phase="prepare",
        runtime_backend="test",
        browser_runtime="edge",
        browser_profile_id=f"profile-{suffix}",
        browser_generation=1,
        endpoint=EndpointDescriptor(
            "endpoint",
            1,
            "http" if CDP_ENDPOINT else "in-process",
            CDP_ENDPOINT or "playwright:isolated-browser",
            False,
        ),
        root_target_ids=(target_id,),
        page_binding=bundle.page_binding.as_dict(),
    )
    return broker, bundle, context


def _run_runtime(
    tmp_path: Path,
    page,
    *,
    provider: str,
    mode: str,
    values: dict[str, str],
    suffix: str,
    before_run=None,
):
    broker, bundle, context = _bundle_context(tmp_path, page, provider=provider, suffix=suffix)

    def validate_authority() -> None:
        broker.validate_page(bundle.page_binding)

    adapter = PlaywrightProductionSemanticBatchAdapter(
        page,
        context,
        provider=provider,
        values=values,
        validate_authority=validate_authority,
    )
    request = SemanticBatchRuntimeRequest(
        mode=mode,
        attempt_id=bundle.page.attempt_id,
        actor_id=bundle.page.owner_id,
        provider=provider,
        adapter_version=ADAPTER_VERSION,
        page_binding=BatchPageBinding(
            str(page.url),
            (),
            adapter.page_signature,
            bundle.page_binding.page_epoch,
        ),
        page_id=bundle.page.resource_id,
        page_lease_id=bundle.page.lease_id,
        page_lease_epoch=bundle.page.epoch,
        resources=BrowserResourceIdentity(
            str(tmp_path / f"{suffix}.db"),
            str(tmp_path / f"profile-{suffix}"),
            9556,
        ),
        patches=tuple(SemanticPatch(semantic, value) for semantic, value in values.items()),
    )
    if before_run is not None:
        before_run()
    state = {"bundle": bundle}

    def advance_page(expected_page_epoch: int) -> int:
        state["bundle"] = broker.advance_page(
            state["bundle"],
            expected_page_epoch=expected_page_epoch,
        )
        return state["bundle"].page_binding.page_epoch

    result = run_production_semantic_batch(
        request,
        adapter=adapter,
        connection=sqlite3.connect(":memory:"),
        close_resources=lambda: None,
        advance_page=advance_page,
    )
    return result, context, state["bundle"]


def _fast_path_contract(*, semantic: str, control: str):
    action = "select" if control in {"select", "select-one", "native_select"} else "fill"
    audit = {
        "disposition": "retry_prepare",
        "repairable_issues": [f"required_field_empty:{semantic}"],
        "ats_fill_plan_snapshot": {
            "form_fields": [
                {"field_key": f"field-{semantic}", "label": semantic, "control": control}
            ],
        },
    }
    plan = {
        "context": {
            "snapshot_ref": f"ats-form:{semantic}",
            "snapshot_sha256": "a" * 64,
            "plan_sha256": "b" * 64,
            "submit_authority": False,
            "plan": {
                "fields": [
                    {
                        "field_key": f"field-{semantic}",
                        "semantic": semantic,
                        "control": control,
                        "writable": True,
                    }
                ],
                "actions": [
                    {
                        "field_key": f"field-{semantic}",
                        "semantic": semantic,
                        "source_key": semantic,
                        "action": action,
                        "requires_review": False,
                    }
                ],
            },
        }
    }
    return audit, plan


@pytest.mark.parametrize("provider", ["workday", "smartrecruiters"])
@pytest.mark.parametrize(
    ("mode", "expected_status", "expected_disposition", "expected_value", "expected_audits"),
    [
        ("off", "off", "continue_agent", "", 0),
        ("shadow", "shadow_match", "continue_agent", "", 1),
        ("canary", "verified", "ready_to_submit", "fixture@example.test", 1),
    ],
)
def test_real_cdp_fast_path_modes_reach_production_writer_without_submit(
    tmp_path: Path,
    provider: str,
    mode: str,
    expected_status: str,
    expected_disposition: str,
    expected_value: str,
    expected_audits: int,
) -> None:
    playwright, browser = _connect_browser()
    page, _url, unexpected = _routed_page(browser, provider)
    try:
        audit, plan = _fast_path_contract(semantic="email", control="email")
        audit_calls = 0
        batch_calls = 0
        runtime_state = {}

        def host_audit():
            nonlocal audit_calls
            audit_calls += 1
            return "required_field_empty:email", audit

        def execute_batch(_report, _plan):
            nonlocal batch_calls
            batch_calls += 1
            runtime, runtime_context, runtime_bundle = _run_runtime(
                tmp_path,
                page,
                provider=provider,
                mode=mode,
                values={"email": "fixture@example.test"},
                suffix=f"{provider}-{mode}",
            )
            runtime_state.update(context=runtime_context, bundle=runtime_bundle)
            return runtime.as_dict()

        job = {"_attempt_id": f"attempt-fast-{provider}-{mode}"}
        result = run_prepare_fast_path(
            job,
            {"personal": {"email": "fixture@example.test"}},
            mode=mode,
            phase="prepare",
            resume_existing_page=False,
            dry_run=False,
            route="browser",
            provider=provider,
            host_audit=host_audit,
            prepare_plan=lambda _report: plan,
            execute_batch=execute_batch,
        )

        assert result.status == expected_status
        assert result.disposition == expected_disposition
        assert audit_calls == expected_audits
        assert batch_calls == (0 if mode == "off" else 1)
        assert page.locator("#email").input_value() == expected_value
        assert page.locator("#submit").evaluate("element => element.form.dataset.submitted || ''") == ""
        assert page.evaluate(
            "window.fixtureEvents.filter(event => event.eventName === 'submit').length"
        ) == 0
        assert unexpected == []
        if mode == "canary":
            # Mirror the outer worker's mandatory second pre-submit audit on the advanced page epoch.
            audit_context = replace(
                runtime_state["context"],
                page_binding=runtime_state["bundle"].page_binding.as_dict(),
            )
            inspection = inspect_form_surfaces(page, audit_context, provider=provider)  # type: ignore[arg-type]
            email_controls = [control for control in inspection.controls if control.semantic == "email"]
            assert len(email_controls) == 1
            assert page.locator(email_controls[0].locator).input_value() == "fixture@example.test"
    finally:
        page.close()
        playwright.stop()


@pytest.mark.parametrize("provider", ["workday", "smartrecruiters"])
def test_real_cdp_native_country_select_is_a_verified_host_write(
    tmp_path: Path,
    provider: str,
) -> None:
    playwright, browser = _connect_browser()
    page, _url, unexpected = _routed_page(browser, provider)
    try:
        result, _context, _bundle = _run_runtime(
            tmp_path,
            page,
            provider=provider,
            mode="canary",
            values={"country": "Singapore"},
            suffix=f"{provider}-country",
        )
        assert result.status == "verified", result
        assert result.effect_count == 1
        assert page.locator("#country").input_value() == "sg"
        assert page.evaluate(
            "window.fixtureEvents.filter(event => event.eventName === 'submit').length"
        ) == 0
        assert unexpected == []
    finally:
        page.close()
        playwright.stop()


@pytest.mark.parametrize("control", ["checkbox", "file", "combobox"])
def test_nonroutine_fast_path_fields_fall_back_before_real_browser_write(
    tmp_path: Path,
    control: str,
) -> None:
    playwright, browser = _connect_browser()
    page, _url, unexpected = _routed_page(browser, "workday")
    try:
        audit, plan = _fast_path_contract(semantic="email", control=control)
        batch_calls = 0

        def execute_batch(_report, _plan):
            nonlocal batch_calls
            batch_calls += 1
            raise AssertionError("nonroutine control reached writer")

        result = run_prepare_fast_path(
            {"_attempt_id": f"attempt-nonroutine-{control}"},
            {"personal": {"email": "fixture@example.test"}},
            mode="canary",
            phase="prepare",
            resume_existing_page=False,
            dry_run=False,
            route="browser",
            provider="workday",
            host_audit=lambda: ("required_field_empty:email", audit),
            prepare_plan=lambda _report: plan,
            execute_batch=execute_batch,
        )
        assert result.status == "fallback"
        assert result.disposition == "continue_agent"
        assert batch_calls == 0
        assert page.locator("#email").input_value() == ""
        assert page.locator("#legal").is_checked() is False
        assert page.locator("#resume").input_value() == ""
        assert page.locator("#department").get_attribute("aria-valuetext") is None
        assert unexpected == []
    finally:
        page.close()
        playwright.stop()


def test_dynamic_native_options_drift_is_zero_effect_fallback(tmp_path: Path) -> None:
    playwright, browser = _connect_browser()
    page, _url, unexpected = _routed_page(browser, "workday")
    try:
        result, _context, _bundle = _run_runtime(
            tmp_path,
            page,
            provider="workday",
            mode="canary",
            values={"country": "Singapore"},
            suffix="dynamic-country",
            before_run=lambda: page.evaluate("window.removeSingaporeOption()"),
        )
        assert result.status == "fallback"
        assert result.effect_count == 0
        assert result.legacy_fallback_safe is True
        assert page.locator("#country").input_value() == ""
        assert unexpected == []
    finally:
        page.close()
        playwright.stop()


def test_postcondition_drift_after_real_write_parks_without_agent_fallback(tmp_path: Path) -> None:
    playwright, browser = _connect_browser()
    page, _url, unexpected = _routed_page(browser, "smartrecruiters")
    try:
        page.evaluate("window.armEmailPostconditionDrift()")
        result, _context, _bundle = _run_runtime(
            tmp_path,
            page,
            provider="smartrecruiters",
            mode="canary",
            values={"email": "fixture@example.test"},
            suffix="postcondition-drift",
        )
        assert result.status == "parked"
        assert result.effect_count == 1
        assert result.legacy_fallback_safe is False
        assert page.locator("#email").input_value() == ""
        assert unexpected == []
    finally:
        page.close()
        playwright.stop()
