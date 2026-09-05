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
    ],
    ids=("username-label", "name-only", "secondary-email", "non-text-input"),
)
def test_accessible_email_fallback_rejects_nonexact_or_ambiguous_candidates(body: str) -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        browser, page = _isolated_page(playwright, body)
        try:
            assert credential_relay._visible_accessible_email_locator(page.main_frame) is None
        finally:
            browser.close()


def test_accessible_email_fallback_rejects_multiple_exact_primary_labels() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        browser, page = _isolated_page(
            playwright,
            (
                '<label for="primary">Email</label><input id="primary" type="text">'
                '<label for="backup">Email Address</label><input id="backup" type="text">'
            ),
        )
        try:
            with pytest.raises(
                credential_relay.CredentialRelayError,
                match="multiple primary email fields",
            ):
                credential_relay._visible_accessible_email_locator(page.main_frame)
        finally:
            browser.close()


def test_temasek_registration_prefers_primary_email_and_fills_in_safe_order() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        browser, page = _isolated_page(
            playwright,
            """
            <label for="fbclc_emailConf">Confirm Email Address:*</label>
            <input id="fbclc_emailConf" name="emailConfirmation" type="text">
            <label for="fbclc_userName">Email Address:*</label>
            <input id="fbclc_userName" type="text" autocomplete="off">
            <label for="fbclc_pwd">Password:*</label>
            <input id="fbclc_pwd" type="password">
            <label for="fbclc_pwdConf">Retype Password:*</label>
            <input id="fbclc_pwdConf" type="password">
            <script>
              globalThis.fillOrder = [];
              for (const element of document.querySelectorAll('input')) {
                element.addEventListener('input', () => globalThis.fillOrder.push(element.id));
              }
              for (const element of document.querySelectorAll('input[type=password]')) {
                element.addEventListener('focus', () => {
                  const primary = document.getElementById('fbclc_userName');
                  if (!primary.value) primary.focus();
                });
              }
            </script>
            """,
        )
        try:
            primary = credential_relay._visible_accessible_email_locator(page.main_frame)
            confirmation = credential_relay._visible_email_confirmation_locator(
                page.main_frame
            )
            passwords = credential_relay._visible_locators(
                page.main_frame, credential_relay.PASSWORD_SELECTORS
            )
            assert primary is not None
            assert confirmation is not None
            assert primary.get_attribute("id") == "fbclc_userName"
            assert confirmation.get_attribute("id") == "fbclc_emailConf"

            result = credential_relay._fill_credential_fields(
                page.main_frame,
                primary,
                confirmation,
                passwords,
                "candidate@example.com",
                "test-only-secret",
            )

            assert result == (True, True, 2)
            assert page.locator("#fbclc_userName").input_value() == "candidate@example.com"
            assert page.locator("#fbclc_emailConf").input_value() == "candidate@example.com"
            assert page.locator("#fbclc_pwd").input_value() == "test-only-secret"
            assert page.locator("#fbclc_pwdConf").input_value() == "test-only-secret"
            assert page.evaluate("globalThis.fillOrder") == [
                "fbclc_userName",
                "fbclc_emailConf",
                "fbclc_pwd",
                "fbclc_pwdConf",
            ]
        finally:
            browser.close()


def test_password_readback_failure_clears_exact_misplaced_secret_in_current_frame() -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    secret = "test-only-misplaced-secret"
    with sync_api.sync_playwright() as playwright:
        browser, page = _isolated_page(
            playwright,
            """
            <label for="primary">Email Address:*</label>
            <input id="primary" type="text">
            <label for="password">Password:*</label>
            <input id="password" type="password">
            <script>
              const primary = document.getElementById('primary');
              const password = document.getElementById('password');
              password.addEventListener('input', () => {
                primary.value = password.value;
                password.value = '';
              });
            </script>
            """,
        )
        try:
            primary = credential_relay._visible_accessible_email_locator(page.main_frame)
            confirmation = credential_relay._visible_email_confirmation_locator(
                page.main_frame
            )
            passwords = credential_relay._visible_locators(
                page.main_frame, credential_relay.PASSWORD_SELECTORS
            )
            with pytest.raises(credential_relay.CredentialRelayError) as raised:
                credential_relay._fill_credential_fields(
                    page.main_frame,
                    primary,
                    confirmation,
                    passwords,
                    "candidate@example.com",
                    secret,
                )

            assert str(raised.value) == (
                "Credential relay could not verify that every password field was filled."
            )
            assert secret not in str(raised.value)
            assert page.locator("#primary").input_value() == ""
            assert page.locator("#password").input_value() == ""
        finally:
            browser.close()
