from __future__ import annotations

from types import SimpleNamespace

import pytest

from applypilot.apply import credential_relay
from applypilot.apply.successfactors_binding import (
    successfactors_auth_url_is_bound,
    successfactors_credential_host,
)

PUBLIC_URL = (
    "https://jobs.temasek.com.sg/job/Data-Engineer-Intern%2C-Technology-"
    "%28Jan-Jun-2027%29-238891/1369169257/"
)
AUTH_URL = "https://career2.successfactors.eu/careers?company=temasekcapP2"
REGISTER_URL = (
    "https://career2.successfactors.eu/career?company=temasekcapP2"
    "&login_ns=register&career_ns=job_application&career_job_req_id=12189"
)
REGISTER_LINK = (
    "/career?company=temasekcapP2&login_ns=register"
    "&career_ns=job_application&career_job_req_id=12189"
)
FORGOT_LINK = (
    "/career?company=temasekcapP2&login_ns=forgot_pwd"
    "&career_ns=job_application&career_job_req_id=12189"
)
OPAQUE_REGISTER_LINK = (
    f"{REGISTER_LINK}&requestParams=opaque%2Froute&_s.crb=value%2Fsegment"
)


def _provider_binding() -> dict[str, object]:
    return {
        "provider": "successfactors",
        "resolved": True,
        "source_url": PUBLIC_URL,
        "source_host": "jobs.temasek.com.sg",
        "source_posting_id": "1369169257",
        "public_apply_url": (
            "https://jobs.temasek.com.sg/talentcommunity/apply/1369169257/"
        ),
        "ats_host": "career2.successfactors.eu",
        "company": "temasekcapP2",
        "job_req_id": "12189",
        "job_application_url": (
            "https://career2.successfactors.eu/career?company=temasekcapP2"
            "&career_ns=job_application&career_job_req_id=12189"
        ),
        "evidence_kind": "official_jobs2web_dom",
    }


@pytest.mark.parametrize(
    "url",
    [
        "https://career9.successfactors.eu/careers?company=temasekcapP2",
        "https://career2.successfactors.eu/careers?company=other",
        (
            "https://career2.successfactors.eu/career?company=temasekcapP2"
            "&career_ns=job_application&career_job_req_id=999"
        ),
        (
            "https://career2.successfactors.eu/career?company=temasekcapP2"
            "&company=other&career_job_req_id=12189"
        ),
        (
            "https://career2.successfactors.eu/career?company=temasekcapP2"
            "&login_ns=forgot_pwd&career_job_req_id=12189"
        ),
        (
            "https://career2.successfactors.eu/careers?company=temasekcapP2"
            "&reset_password=true"
        ),
        "https://career2.successfactors.eu/password/reset?company=temasekcapP2",
    ],
)
def test_auth_surface_rejects_wrong_host_identity_duplicates_and_recovery(
    url: str,
) -> None:
    assert not successfactors_auth_url_is_bound(url, _provider_binding())


def test_company_only_auth_uses_employer_binding_without_requiring_job_links() -> None:
    binding = _provider_binding()
    assert successfactors_auth_url_is_bound(AUTH_URL, binding)
    assert successfactors_auth_url_is_bound(REGISTER_URL, binding)
    assert not successfactors_auth_url_is_bound(
        REGISTER_URL.replace("12189", "999"), binding
    )


def test_ntuc_sapsf_company_only_handoff_uses_exact_959_tuple() -> None:
    binding = {
        "provider": "successfactors",
        "resolved": True,
        "source_url": (
            "https://careers.ntuchealth.sg/job/Intern%2C-Data-Analyst/959-en_GB"
        ),
        "source_posting_id": "959",
        "ats_host": "career44.sapsf.com",
        "company": "ntuchealth",
        "job_req_id": "959",
    }
    auth_url = "https://career44.sapsf.com/careers?company=ntuchealth"
    assert successfactors_credential_host("career44.sapsf.com")
    assert credential_relay._credential_surface_url_is_bound(
        auth_url,
        {"target_urls": [binding["source_url"]], "provider_binding": binding},
    )


@pytest.mark.browser
@pytest.mark.parametrize(
    ("body", "field", "should_fill"),
    [
        (
            f"""
            <!doctype html><html><body>
            <a href="{OPAQUE_REGISTER_LINK}">Create an account</a>
            <a href="{FORGOT_LINK}">Forgot password</a>
            <label for="email">Email Address</label>
            <input id="email" type="email">
            <label for="password">Password</label>
            <input id="password" type="password">
            </body></html>
            """,
            "both",
            True,
        ),
        (
            f"""
            <!doctype html><html><body>
            <a href="{REGISTER_LINK}">Create an account</a>
            <label for="email">Email Address</label>
            <input id="email" type="email">
            </body></html>
            """,
            "email",
            True,
        ),
        (
            """
            <!doctype html><html><body>
            <a href="/career?company=temasekcapP2&login_ns=register"
               >Create an account</a>
            <label for="email">Email Address</label>
            <input id="email" type="email">
            <label for="password">Password</label>
            <input id="password" type="password">
            </body></html>
            """,
            "both",
            True,
        ),
        (
            '<label for="password">Password</label><input id="password" type="password">',
            "password",
            True,
        ),
        (
            '<label for="email">Email Address</label><input id="email" type="email">',
            "both",
            False,
        ),
    ],
)
def test_isolated_company_login_supports_normal_and_email_first_forms(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    body: str,
    field: str,
    should_fill: bool,
) -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.route(
            "**/*",
            lambda route: route.fulfill(status=200, content_type="text/html", body=body),
        )
        page.goto(AUTH_URL)
        target_id = page.context.new_cdp_session(page).send("Target.getTargetInfo")[
            "targetInfo"
        ]["targetId"]

        class _ExistingBrowserContext:
            def __enter__(self):
                return SimpleNamespace(
                    chromium=SimpleNamespace(connect_over_cdp=lambda _url: browser)
                )

            def __exit__(self, _exc_type, _exc, _traceback) -> bool:
                return False

        monkeypatch.setattr(
            credential_relay, "sync_playwright", lambda: _ExistingBrowserContext()
        )
        monkeypatch.setattr(credential_relay, "_relay_is_authorized", lambda: True)
        monkeypatch.setattr(
            credential_relay, "_allowed_hosts", lambda: {"jobs.temasek.com.sg"}
        )
        monkeypatch.setattr(
            credential_relay, "_password_host_is_allowed", lambda _host: True
        )
        monkeypatch.setattr(
            credential_relay, "_known_ats_redirect_enabled", lambda: True
        )
        monkeypatch.setattr(credential_relay, "_root_target_ids", lambda: {target_id})
        monkeypatch.setattr(
            credential_relay,
            "_application_context_binding",
            lambda: {
                "schema_version": "1",
                "attempt_id": "attempt-sf",
                "application_id": "application-sf",
                "target_urls": [PUBLIC_URL],
                "provider_binding": _provider_binding(),
            },
        )
        monkeypatch.setenv(
            "APPLYPILOT_ATS_CONTEXT_PATH", str(tmp_path / "context.json")
        )
        monkeypatch.setenv("APPLYPILOT_CREDENTIAL_ATTEMPT_ID", "attempt-sf")
        monkeypatch.setenv(
            "APPLYPILOT_CREDENTIAL_APPLICATION_ID", "application-sf"
        )
        try:
            if should_fill:
                outcome = credential_relay._fill_fields(
                    0,
                    field,
                    "candidate@example.com",
                    "fake-password",
                )
                assert outcome["status"] == "filled"
                assert outcome["submitted"] is False
                if field in {"email", "both"}:
                    assert page.locator("#email").input_value() == "candidate@example.com"
                if field in {"password", "both"}:
                    assert page.locator("#password").input_value() == "fake-password"
            else:
                with pytest.raises(
                    credential_relay.CredentialRelayError,
                    match="No visible editable credential field",
                ):
                    credential_relay._fill_fields(
                        0,
                        "both",
                        "candidate@example.com",
                        "fake-password",
                    )
        finally:
            browser.close()
