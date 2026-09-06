"""Normal navigation must agree across login and the final application audit."""

from types import SimpleNamespace

import pytest

from applypilot.apply import credential_relay, page_observation

POSTING = "https://tenant.wd5.myworkdayjobs.com/en-US/Careers/job/Analyst_UNI4036-1"
APPLICATION = (
    "https://tenant.wd5.myworkdayjobs.com/en-GB/Careers/job/"
    "Singapore%2C-South-West/Updated-Title_UNI4036-1/apply"
)


@pytest.mark.parametrize(
    "actual",
    [POSTING, APPLICATION, APPLICATION + "/autofillWithResume",
     APPLICATION + "/review?source=LinkedIn&utm_campaign=internship"],
)
def test_login_and_final_audit_accept_same_job_navigation(actual):
    assert credential_relay._credential_surface_url_is_bound(
        actual, {"target_urls": [POSTING]}
    )
    assert page_observation._same_bound_application_flow(POSTING, actual, {})


@pytest.mark.parametrize(
    "actual",
    [APPLICATION.replace("UNI4036-1", "UNI4036-2"),
     APPLICATION.replace("/Careers/", "/OtherCareers/"),
     APPLICATION.replace("tenant.wd5", "other.wd5"),
     APPLICATION.replace("https://", "http://"),
     APPLICATION.replace("https://", "https://untrusted@"),
     APPLICATION.replace("Updated-Title", "Updated%2FTitle"),
     APPLICATION + "/password/reset"],
)
def test_login_and_final_audit_still_reject_different_or_ambiguous_jobs(actual):
    assert not credential_relay._credential_surface_url_is_bound(
        actual, {"target_urls": [POSTING]}
    )
    assert not page_observation._same_bound_application_flow(POSTING, actual, {})


@pytest.mark.browser
def test_clicking_apply_then_email_first_login_reaches_the_same_job(monkeypatch, tmp_path):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()

        def route_page(route):
            if route.request.url == POSTING:
                body = f'<a href="{APPLICATION}/autofillWithResume">Apply</a>'
            elif route.request.url.endswith("autofillWithResume"):
                body = ('<label>Email<input type="email" id="email"></label>'
                        f'<a href="{APPLICATION}">Continue</a>')
            else:
                body = '<h1>Analyst UNI4036-1</h1><button>Submit application</button>'
            route.fulfill(status=200, content_type="text/html", body=body)

        page.route("**/*", route_page)
        page.goto(POSTING)
        page.get_by_role("link", name="Apply", exact=True).click()
        target_id = page.context.new_cdp_session(page).send("Target.getTargetInfo")["targetInfo"]["targetId"]

        class ExistingBrowser:
            def __enter__(self):
                return SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda _url: browser))

            def __exit__(self, *_args):
                return False

        monkeypatch.setattr(credential_relay, "sync_playwright", ExistingBrowser)
        monkeypatch.setattr(credential_relay, "_relay_is_authorized", lambda: True)
        monkeypatch.setattr(credential_relay, "_allowed_hosts", lambda: {"tenant.wd5.myworkdayjobs.com"})
        monkeypatch.setattr(credential_relay, "_root_target_ids", lambda: {target_id})
        monkeypatch.setattr(credential_relay, "_application_context_binding",
                            lambda: {"target_urls": [POSTING]})
        # A broken diagnostic location must not become a login requirement.
        monkeypatch.setenv("APPLYPILOT_ATS_CONTEXT_PATH", str(tmp_path / "missing" / "context.json"))
        try:
            result = credential_relay._fill_fields(0, "email", "candidate@example.test", "unused")
            assert result["status"] == "filled"
            assert result["submitted"] is False
            assert page.locator("#email").input_value() == "candidate@example.test"
            page.get_by_role("link", name="Continue", exact=True).click()
            issues = page_observation._validate_pre_submit_snapshot(
                {"url": page.url, "submit_control_count": 1}, {}, {"url": POSTING}
            )
            assert "unexpected_application_url" not in issues
        finally:
            browser.close()
