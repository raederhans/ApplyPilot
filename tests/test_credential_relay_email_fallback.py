from __future__ import annotations

import pytest

from applypilot.apply import credential_relay

pytestmark = pytest.mark.browser


def _isolated_page(playwright, body: str):
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    unexpected_requests: list[str] = []
    page.route(
        "**/*",
        lambda route: (unexpected_requests.append(route.request.url), route.abort()),
    )
    page.set_content(f"<!doctype html><html><body>{body}</body></html>")
    assert unexpected_requests == []
    return browser, page


def test_accessible_email_address_label_selects_text_username_input_only() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        browser, page = _isolated_page(
            playwright,
            """
            <label for="wrong-username">Username</label>
            <input id="wrong-username" name="login" type="text">
            <label for="username">Email Address:*</label>
            <input id="username" name="username" type="text">
            """,
        )
        try:
            assert credential_relay._visible_locator(
                page.main_frame,
                credential_relay.EMAIL_SELECTORS,
            ) is None
            locator = credential_relay._visible_accessible_email_locator(page.main_frame)
            assert locator is not None
            assert locator.get_attribute("id") == "username"
        finally:
            browser.close()


@pytest.mark.parametrize(
    "body",
    [
        '<label for="username">Username</label><input id="username" type="text">',
        '<input id="username" name="username" type="text">',
        (
            '<label for="secondary">Secondary email</label>'
            '<input id="secondary" name="secondaryemail" type="text">'
        ),
        '<label for="identity">Email Address:*</label><input id="identity" type="number">',
        (
            '<label for="primary">Email</label><input id="primary" type="text">'
            '<label for="backup">Email Address</label><input id="backup" type="text">'
        ),
    ],
    ids=("username-label", "name-only", "secondary-email", "non-text-input", "ambiguous"),
)
def test_accessible_email_fallback_rejects_nonexact_or_ambiguous_candidates(body: str) -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        browser, page = _isolated_page(playwright, body)
        try:
            assert credential_relay._visible_accessible_email_locator(page.main_frame) is None
        finally:
            browser.close()
