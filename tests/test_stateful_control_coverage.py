from __future__ import annotations

import pytest

from applypilot.apply import page_observation, worker_orchestration


def _coverage_report(coverage: dict[str, object]) -> dict[str, object]:
    return {"stateful_control_coverage": coverage}


def test_stateful_control_coverage_contract_requires_complete_classification() -> None:
    complete = {
        "schema_version": 1,
        "discovered_count": 0,
        "classified_visible_native_count": 0,
        "unclassified_count": 0,
        "selected_or_filled_count": 0,
        "overflow": False,
        "proof_complete": True,
    }
    unclassified = {
        **complete,
        "discovered_count": 1,
        "unclassified_count": 1,
        "selected_or_filled_count": 1,
        "proof_complete": False,
    }

    assert worker_orchestration._stateful_control_coverage_error(
        _coverage_report(complete)
    ) is None
    assert worker_orchestration._stateful_control_coverage_error({}) == (
        "stateful_control_coverage_unproven"
    )
    assert worker_orchestration._stateful_control_coverage_error(
        _coverage_report(unclassified)
    ) == "stateful_control_unclassified"


@pytest.mark.parametrize(
    ("body", "expected_unclassified", "expected_selected"),
    [
        (
            '<input id="ordinary" value="filled">',
            0,
            0,
        ),
        (
            (
                '<input id="ordinary" value="filled">'
                '<div role="checkbox" aria-checked="true" tabindex="0">'
                "Accept Terms</div>"
            ),
            1,
            1,
        ),
        (
            (
                '<div role="checkbox" aria-disabled="true" '
                'aria-checked="true">Accept Terms</div>'
            ),
            1,
            1,
        ),
        (
            (
                '<div role="radio" aria-disabled="true" '
                'aria-checked="true">Selected option</div>'
            ),
            1,
            1,
        ),
        (
            (
                '<div role="unknown CHECKBOX" aria-disabled="true">'
                "Fallback role</div>"
            ),
            1,
            0,
        ),
        (
            (
                '<button type="button" aria-disabled="true" '
                'aria-pressed="mixed" style="display:none">'
                "Hidden mixed toggle</button>"
            ),
            1,
            1,
        ),
        (
            (
                '<div role="checkbox" aria-disabled="true" '
                'aria-checked="false" aria-required="true" '
                'style="display:none">Required option</div>'
            ),
            1,
            0,
        ),
        (
            (
                '<div role="switch" aria-disabled="TRUE" '
                'aria-checked="mixed">Optional setting</div>'
            ),
            1,
            1,
        ),
        (
            (
                '<button type="button" aria-disabled="true" '
                'aria-pressed="true">Selected toggle</button>'
            ),
            1,
            1,
        ),
        (
            (
                '<input id="ordinary" value="filled">'
                '<input id="terms" type="checkbox" checked style="display:none">'
                '<label for="terms"><span class="proxy">'
                "Accept Terms</span></label>"
            ),
            1,
            1,
        ),
    ],
)
def test_real_local_chromium_classifies_custom_and_hidden_stateful_controls(
    body: str,
    expected_unclassified: int,
    expected_selected: int,
) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.route("**/*", lambda route: route.abort())
            page.set_content(f"<!doctype html><html><body>{body}</body></html>")
            coverage = page.evaluate(
                page_observation._STATEFUL_CONTROL_COVERAGE_SCRIPT
            )
        finally:
            browser.close()

    assert coverage["schema_version"] == 1
    assert coverage["unclassified_count"] == expected_unclassified
    assert coverage["selected_or_filled_count"] == expected_selected
    assert coverage["proof_complete"] is (expected_unclassified == 0)


def test_custom_aria_disabled_state_cannot_escape_through_hidden_payload() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.route("**/*", lambda route: route.abort())
            page.set_content(
                "<!doctype html><form id=form>"
                '<button type="button" disabled role="checkbox" '
                'aria-checked="true" aria-disabled="true">Accept Terms</button>'
                '<input type="hidden" name="terms" value="true">'
                "</form>"
            )
            coverage = page.evaluate(
                page_observation._STATEFUL_CONTROL_COVERAGE_SCRIPT
            )
            submitted = page.evaluate(
                "Object.fromEntries(new FormData(document.querySelector('#form')))"
            )
        finally:
            browser.close()

    assert submitted == {"terms": "true"}
    assert coverage["discovered_count"] == 1
    assert coverage["unclassified_count"] == 1
    assert coverage["selected_or_filled_count"] == 1
    assert coverage["proof_complete"] is False


def test_html_disabled_native_controls_remain_excluded_from_coverage() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.route("**/*", lambda route: route.abort())
            page.set_content(
                "<!doctype html><form id=form>"
                '<input name="terms" type="checkbox" checked disabled>'
                "</form>"
            )
            coverage = page.evaluate(
                page_observation._STATEFUL_CONTROL_COVERAGE_SCRIPT
            )
            submitted = page.evaluate(
                "Object.fromEntries(new FormData(document.querySelector('#form')))"
            )
        finally:
            browser.close()

    assert submitted == {}
    assert coverage["discovered_count"] == 0
    assert coverage["unclassified_count"] == 0
    assert coverage["selected_or_filled_count"] == 0
    assert coverage["proof_complete"] is True


def test_native_aria_disabled_cannot_escape_successful_control_coverage() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.route("**/*", lambda route: route.abort())
            page.set_content(
                "<!doctype html><form id=form>"
                '<input name="terms" type="checkbox" checked '
                'aria-disabled="true" style="display:none">'
                "</form>"
            )
            coverage = page.evaluate(
                page_observation._STATEFUL_CONTROL_COVERAGE_SCRIPT
            )
            submitted = page.evaluate(
                "Object.fromEntries(new FormData(document.querySelector('#form')))"
            )
        finally:
            browser.close()

    assert submitted == {"terms": "on"}
    assert coverage["unclassified_count"] == 1
    assert coverage["selected_or_filled_count"] == 1
    assert coverage["proof_complete"] is False
