from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from applypilot.apply.application_sessions import ContextBundle, EndpointDescriptor
from applypilot.apply.browser_broker import BrowserBroker, StalePageBinding
from applypilot.apply.contracts import application_actor_id
from applypilot.apply.control_descriptors import (
    ControlDescriptor,
    ControlInspectionDenied,
    ControlObservation,
    PlaywrightSemanticControlDriver,
    SemanticControlAuthorityIssuer,
    SemanticControlDenied,
    SemanticControlRequest,
    SemanticControlUncertain,
    execute_semantic_control,
    inspect_form_surfaces,
)
from applypilot.apply.runtime_namespace import RuntimeNamespace


def _bundle_and_context(tmp_path: Path):
    attempt_id = "attempt-p2"
    actor_id = application_actor_id(attempt_id)
    broker = BrowserBroker()
    bundle = broker.acquire_bundle(
        profile_id="profile-p2",
        page_id="page-p2",
        owner_id=actor_id,
        scope_id="scope-p2",
        attempt_id=attempt_id,
        runtime_id="runtime-p2",
    )
    context = ContextBundle(
        namespace=RuntimeNamespace(
            root=tmp_path,
            run_id="run-p2",
            session_id="session-p2",
            profile_id="profile-p2",
        ),
        worker_id=0,
        application_session_id="application-p2",
        actor_id=actor_id,
        attempt_id=attempt_id,
        phase="prepare",
        runtime_backend="test",
        browser_runtime="chromium",
        browser_profile_id="profile-p2",
        browser_generation=1,
        endpoint=EndpointDescriptor(
            endpoint_id="endpoint-p2",
            generation=1,
            transport="http",
            address="http://127.0.0.1:8931/mcp",
            reusable=True,
        ),
        root_target_ids=("target-p2",),
        page_binding=bundle.page_binding.as_dict(),
    )
    return broker, bundle, context


def _bind_page_target(context: ContextBundle, page) -> ContextBundle:
    session = page.context.new_cdp_session(page)
    try:
        payload = session.send("Target.getTargetInfo")
    finally:
        session.detach()
    return replace(
        context,
        root_target_ids=(payload["targetInfo"]["targetId"],),
    )


class _FakeDriver:
    def __init__(self, *, mismatch: bool = False) -> None:
        self.calls = 0
        self.mismatch = mismatch

    def perform(self, request: SemanticControlRequest) -> ControlObservation:
        self.calls += 1
        return ControlObservation(
            descriptor_id=request.descriptor.descriptor_id,
            value=str(request.value),
            page_signature="stable",
        )

    def observe(self, request: SemanticControlRequest) -> ControlObservation:
        self.calls += 1
        return ControlObservation(
            descriptor_id=request.descriptor.descriptor_id,
            value="wrong" if self.mismatch else str(request.value),
            page_signature="stable",
        )


def _text_descriptor(context: ContextBundle) -> ControlDescriptor:
    binding = context.page_binding
    from applypilot.apply.page_binding import PageBinding

    return ControlDescriptor(
        descriptor_id="d" * 64,
        actor_id=context.actor_id,
        attempt_id=context.attempt_id,
        application_session_id=context.application_session_id,
        browser_generation=context.browser_generation,
        provider="workday",
        page_binding=PageBinding.from_mapping(binding),
        surface_id="s" * 64,
        frame_path=(),
        frame_url="https://tenant.myworkdayjobs.com/apply",
        shadow_path=(),
        locator="#name",
        kind="text",
        semantic="full_name",
        label="Full name",
        required=True,
        writable=True,
        stateful=False,
    )


def test_control_authority_binds_p1_context_and_advances_page_epoch(tmp_path: Path) -> None:
    broker, bundle, context = _bundle_and_context(tmp_path)
    request = SemanticControlRequest(
        descriptor=_text_descriptor(context),
        operation="set_text",
        value="Synthetic Candidate",
    )
    issuer = SemanticControlAuthorityIssuer()
    authority = issuer.issue(
        context=context,
        bundle=bundle,
        request=request,
        submit_started=False,
    )
    with pytest.raises(SemanticControlDenied, match="already issued"):
        issuer.issue(
            context=context,
            bundle=bundle,
            request=request,
            submit_started=False,
        )
    driver = _FakeDriver()

    result = execute_semantic_control(
        broker,
        driver,
        issuer,
        bundle=bundle,
        context=context,
        authority=authority,
        request=request,
    )

    assert result.bundle.page_binding.page_epoch == 1
    assert result.observation.value == "Synthetic Candidate"
    assert driver.calls == 2
    with pytest.raises(SemanticControlDenied, match="already consumed"):
        execute_semantic_control(
            broker,
            driver,
            issuer,
            bundle=bundle,
            context=context,
            authority=authority,
            request=request,
        )


def test_stale_page_epoch_is_zero_write(tmp_path: Path) -> None:
    broker, bundle, context = _bundle_and_context(tmp_path)
    request = SemanticControlRequest(
        descriptor=_text_descriptor(context),
        operation="set_text",
        value="Synthetic Candidate",
    )
    issuer = SemanticControlAuthorityIssuer()
    authority = issuer.issue(
        context=context,
        bundle=bundle,
        request=request,
        submit_started=False,
    )
    broker.advance_page(bundle, expected_page_epoch=0)
    driver = _FakeDriver()

    with pytest.raises(StalePageBinding):
        execute_semantic_control(
            broker,
            driver,
            issuer,
            bundle=bundle,
            context=context,
            authority=authority,
            request=request,
        )

    assert driver.calls == 0


def test_postcondition_mismatch_is_uncertain_and_never_advances(tmp_path: Path) -> None:
    broker, bundle, context = _bundle_and_context(tmp_path)
    request = SemanticControlRequest(
        descriptor=_text_descriptor(context),
        operation="set_text",
        value="Synthetic Candidate",
    )
    issuer = SemanticControlAuthorityIssuer()
    authority = issuer.issue(
        context=context,
        bundle=bundle,
        request=request,
        submit_started=False,
    )
    driver = _FakeDriver(mismatch=True)

    with pytest.raises(SemanticControlUncertain, match="postcondition"):
        execute_semantic_control(
            broker,
            driver,
            issuer,
            bundle=bundle,
            context=context,
            authority=authority,
            request=request,
        )

    assert broker.validate_page(bundle.page_binding).page_epoch == 0
    assert driver.calls == 2


def test_authority_rejects_submit_phase_and_stale_p1_generation(tmp_path: Path) -> None:
    _broker, bundle, context = _bundle_and_context(tmp_path)
    request = SemanticControlRequest(
        descriptor=_text_descriptor(context),
        operation="set_text",
        value="Synthetic Candidate",
    )
    issuer = SemanticControlAuthorityIssuer()
    with pytest.raises(SemanticControlDenied, match="pre-submit"):
        issuer.issue(
            context=context,
            bundle=bundle,
            request=request,
            submit_started=True,
        )

    stale_generation = replace(
        context,
        browser_generation=2,
        endpoint=replace(context.endpoint, generation=2),
    )
    with pytest.raises(SemanticControlDenied, match="P1 context binding changed"):
        issuer.issue(
            context=stale_generation,
            bundle=bundle,
            request=request,
            submit_started=False,
        )


def _route_fixture(page, provider: str) -> str:
    host = (
        "tenant.myworkdayjobs.com"
        if provider == "workday"
        else "jobs.smartrecruiters.com"
    )
    root_url = f"https://{host}/apply"
    child = """
      <fieldset><legend>Availability</legend>
        <label><input id="available" type="radio" name="available" value="yes">Yes</label>
      </fieldset>
    """
    main = """
      <!doctype html><html><body><form id="application">
        <label>Full name <input id="name" required></label>
        <label>Summary <textarea id="summary"></textarea></label>
        <label>Country <select id="country"><option>Choose</option><option value="sg">Singapore</option></select></label>
        <div id="department" role="combobox" aria-label="Department" aria-controls="department-options"></div>
        <div id="department-options" role="listbox"><div id="engineering" role="option">Engineering</div></div>
        <label><input id="consent" type="checkbox">Routine consent</label>
        <button id="updates" type="button" role="switch" aria-checked="false">Updates</button>
        <label>Date available <input id="available-date" type="date"></label>
        <label>Resume <input id="resume" type="file" required accept="application/pdf"></label>
        <div id="shadow-host"></div>
        <iframe id="child" src="/child"></iframe>
        <button id="next" type="button">Next</button>
        <button id="submit" type="submit">Submit</button>
        <output id="progress">step-one</output>
      </form><script>
        const host = document.querySelector('#shadow-host');
        const root = host.attachShadow({mode: 'open'});
        root.innerHTML = '<label>Shadow email <input id="shadow-email" type="email"></label>';
        document.querySelector('#engineering').addEventListener('click', () => {
          document.querySelector('#department').setAttribute('aria-valuetext', 'Engineering');
        });
        document.querySelector('#updates').addEventListener('click', event => {
          event.currentTarget.setAttribute('aria-checked', 'true');
        });
        document.querySelector('#next').addEventListener('click', () => {
          document.querySelector('#progress').textContent = 'step-two';
        });
      </script></body></html>
    """

    def handle(route) -> None:
        body = child if route.request.url.endswith("/child") else main
        route.fulfill(status=200, content_type="text/html", body=body)

    page.route(f"https://{host}/**", handle)
    page.goto(root_url, wait_until="load")
    return root_url


@pytest.mark.parametrize("provider", ["workday", "smartrecruiters"])
def test_real_chromium_supported_provider_controls_use_p1_epoch_bound_gateway(
    tmp_path: Path,
    provider: str,
) -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except sync_api.Error as exc:
            pytest.skip(f"local Playwright browser unavailable: {exc}")
        page = browser.new_page()
        _route_fixture(page, provider)
        broker, bundle, context = _bundle_and_context(tmp_path)
        context = _bind_page_target(context, page)

        operations = (
            ("text", "set_text", "Synthetic Candidate"),
            ("textarea", "set_text", "Synthetic summary"),
            ("native_select", "select_option", "Singapore"),
            ("custom_combobox", "select_option", "Engineering"),
            ("checkbox", "set_checked", True),
            ("switch", "set_checked", True),
            ("date", "set_date", "2026-11-10"),
            ("radio", "set_checked", True),
            ("navigation", "activate_navigation", "Next"),
        )
        for kind, operation, value in operations:
            inspection = inspect_form_surfaces(page, context, provider=provider)  # type: ignore[arg-type]
            candidates = [item for item in inspection.controls if item.kind == kind]
            assert candidates, kind
            descriptor = candidates[0]
            request = SemanticControlRequest(
                descriptor=descriptor,
                operation=operation,  # type: ignore[arg-type]
                value=value,
            )
            issuer = SemanticControlAuthorityIssuer()
            authority = issuer.issue(
                context=context,
                bundle=bundle,
                request=request,
                submit_started=False,
            )
            result = execute_semantic_control(
                broker,
                PlaywrightSemanticControlDriver(page, inspection),
                issuer,
                bundle=bundle,
                context=context,
                authority=authority,
                request=request,
            )
            bundle = result.bundle
            context = replace(context, page_binding=bundle.page_binding.as_dict())

        final_inspection = inspect_form_surfaces(page, context, provider=provider)  # type: ignore[arg-type]
        assert any(item.frame_path for item in final_inspection.controls)
        assert any(item.shadow_path for item in final_inspection.controls)
        resume = next(item for item in final_inspection.controls if item.kind == "resume_file")
        with pytest.raises(SemanticControlDenied, match="bound resume"):
            SemanticControlRequest(
                descriptor=resume,
                operation="set_text",
                value="C:/unsafe/resume.pdf",
            )
        submit = next(item for item in final_inspection.controls if item.kind == "final_submit")
        with pytest.raises(SemanticControlDenied, match="final Submit"):
            SemanticControlRequest(
                descriptor=submit,
                operation="activate_navigation",
                value="Submit",
            )
        assert page.locator("#submit").evaluate("element => element.form.dataset.submitted || ''") == ""
        browser.close()


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (
            '<iframe src="data:text/html,<input id=foreign>"></iframe>',
            "cross_origin_frame_inaccessible",
        ),
        (
            '<input name="duplicate"><input name="duplicate">',
            "unstable_or_ambiguous_locator",
        ),
        (
            '<button id="mystery" type="button" aria-pressed="true">Mystery</button>',
            "stateful_control_unclassified",
        ),
    ],
)
def test_real_chromium_surface_fail_closed_boundaries(
    tmp_path: Path,
    body: str,
    reason: str,
) -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except sync_api.Error as exc:
            pytest.skip(f"local Playwright browser unavailable: {exc}")
        page = browser.new_page()
        page.route(
            "https://tenant.myworkdayjobs.com/**",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body=f"<!doctype html><html><body>{body}</body></html>",
            ),
        )
        page.goto("https://tenant.myworkdayjobs.com/apply", wait_until="load")
        _broker, _bundle, context = _bundle_and_context(tmp_path)
        context = _bind_page_target(context, page)
        with pytest.raises(ControlInspectionDenied, match=reason):
            inspect_form_surfaces(page, context, provider="workday")
        browser.close()


def test_real_chromium_closed_shadow_root_fails_closed(tmp_path: Path) -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except sync_api.Error as exc:
            pytest.skip(f"local Playwright browser unavailable: {exc}")
        page = browser.new_page()
        page.route(
            "https://tenant.myworkdayjobs.com/**",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body="""<!doctype html><div id="host"></div><script>
                  const root = document.querySelector('#host').attachShadow({mode: 'closed'});
                  root.innerHTML = '<input id="hidden-control">';
                </script>""",
            ),
        )
        page.goto("https://tenant.myworkdayjobs.com/apply", wait_until="load")
        _broker, _bundle, context = _bundle_and_context(tmp_path)
        context = _bind_page_target(context, page)
        with pytest.raises(ControlInspectionDenied, match="closed_shadow_root"):
            inspect_form_surfaces(page, context, provider="workday")
        browser.close()


def test_inspection_without_cdp_shadow_evidence_fails_closed(tmp_path: Path) -> None:
    _broker, _bundle, context = _bundle_and_context(tmp_path)
    page = SimpleNamespace(
        url="https://tenant.myworkdayjobs.com/apply",
        main_frame=SimpleNamespace(url="https://tenant.myworkdayjobs.com/apply"),
        context=SimpleNamespace(),
    )
    with pytest.raises(
        ControlInspectionDenied,
        match="closed_shadow_observability_unproven",
    ):
        inspect_form_surfaces(page, context, provider="workday")


def test_inspection_rejects_page_outside_p1_root_targets(tmp_path: Path) -> None:
    class Session:
        def send(self, method: str, _params=None):
            if method == "Target.getTargetInfo":
                return {"targetInfo": {"targetId": "other-target"}}
            return {"root": {"children": []}}

        def detach(self) -> None:
            return None

    _broker, _bundle, context = _bundle_and_context(tmp_path)
    main = SimpleNamespace(url="https://tenant.myworkdayjobs.com/apply")
    page = SimpleNamespace(
        url=main.url,
        main_frame=main,
        frames=[main],
        context=SimpleNamespace(new_cdp_session=lambda _page: Session()),
    )
    with pytest.raises(ControlInspectionDenied, match="outside the P1 ContextBundle"):
        inspect_form_surfaces(page, context, provider="workday")
