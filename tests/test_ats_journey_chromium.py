from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_control_descriptors import _bind_page_target, _bundle_and_context

from applypilot.apply.ats import build_form_ir
from applypilot.apply.control_descriptors import inspect_form_surfaces

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "apply"
FIXTURE_HTML = (FIXTURE_DIR / "ats_journey.html").read_text(encoding="utf-8")
CASES = json.loads((FIXTURE_DIR / "ats_journey_cases.json").read_text(encoding="utf-8"))


def _assertions(page, assertions: list[dict[str, object]]) -> None:
    for assertion in assertions:
        locator = page.locator(str(assertion["selector"]))
        if "property" in assertion:
            assert locator.input_value() == assertion["equals"]
            continue
        attribute = str(assertion["attribute"])
        actual = locator.get_attribute(attribute)
        if attribute == "required" and assertion["equals"] == "true":
            assert actual is not None
        else:
            assert actual == assertion["equals"]


def _run_action(page, action: dict[str, object], tmp_path: Path) -> None:
    kind = str(action["kind"])
    locator = page.locator(str(action["selector"]))
    if kind == "fill":
        locator.fill(str(action["value"]))
    elif kind == "blur":
        locator.blur()
    elif kind == "click":
        locator.click()
    elif kind == "select":
        locator.select_option(str(action["value"]))
    elif kind == "upload":
        filename = str(action["filename"])
        suffix = b"%PDF-1.7 fixture\n" if filename.endswith(".pdf") else b"plain fixture\n"
        locator.set_input_files(
            {
                "name": filename,
                "mimeType": "application/pdf" if filename.endswith(".pdf") else "text/plain",
                "buffer": suffix,
            }
        )
    elif kind == "wait_for":
        locator.wait_for(state=str(action.get("state") or "visible"), timeout=2_000)
    else:  # pragma: no cover - fixture cases are intentionally declarative
        raise AssertionError(f"unknown fixture action: {kind}")


@pytest.mark.browser
def test_local_ats_journey_matrix_uses_production_observation_and_bounded_fixture(
    tmp_path: Path,
) -> None:
    """Exercise representative local journeys without claiming recovery or submission.

    The DOM assertions establish deterministic fixture observations. The production
    inspection and IR build below only prove that the same observed controls can be
    described by the real ApplyPilot path; they do not prove browser recovery,
    external ATS compatibility, receipt authority, or a successful submission.
    """
    sync_api = pytest.importorskip("playwright.sync_api")
    assert CASES["schema_version"] == "applypilot-ats-journey/v1"
    cases = CASES["cases"]
    assert 12 <= len(cases) <= 18

    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except sync_api.Error as exc:
            pytest.skip(f"local Playwright browser unavailable: {exc}")
        try:
            for case in cases:
                case_id = str(case["id"])
                page = browser.new_context().new_page()
                page.route(
                    "https://tenant.myworkdayjobs.com/**",
                    lambda route: route.fulfill(
                        status=200,
                        content_type="text/html",
                        body=FIXTURE_HTML,
                    ),
                )
                url = f"https://tenant.myworkdayjobs.com/ats-journey?case={case_id}"
                page.goto(url, wait_until="load")

                _broker, _bundle, context = _bundle_and_context(tmp_path / case_id)
                context = _bind_page_target(context, page)
                inspection = inspect_form_surfaces(page, context, provider="workday")
                assert inspection.proof_complete is True
                assert inspection.controls

                observed_fields = [
                    {
                        "id": descriptor.locator.removeprefix("#"),
                        "label": descriptor.label,
                        "control": descriptor.kind,
                        "required": descriptor.required,
                        "options": list(descriptor.options),
                    }
                    for descriptor in inspection.controls
                ]
                form = build_form_ir(page.url, observed_fields)
                assert len(form.fields) == len(inspection.controls)
                assert all(field.field_key for field in form.fields)
                if case_id == "late_required_field_coverage":
                    assert len(form.fields) == 100
                    assert form.fields[94].required is True

                for action in case["actions"]:
                    _run_action(page, action, tmp_path / case_id)
                page.wait_for_timeout(140)
                _assertions(page, case["assertions"])

                # Re-observation catches post-action conditional/rerender surfaces;
                # it remains observation-only and never activates Submit authority.
                refreshed = inspect_form_surfaces(page, context, provider="workday")
                assert refreshed.proof_complete is True
                if case_id == "conditional_question_after_selection":
                    assert any(item.locator == "#sponsorship" for item in refreshed.controls)
                if case_id == "stale_rerender":
                    assert any(item.locator == "#rerender-phone" for item in refreshed.controls)
                page.context.close()
        finally:
            browser.close()
