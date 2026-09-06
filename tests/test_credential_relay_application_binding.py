from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from applypilot.apply import credential_relay

KEPPEL_POSTING = (
    "https://keppel.wd3.myworkdayjobs.com/en-US/KeppelCareers/job/"
    "XMLNAME--Keppel-Internship-Programme-2027--Intern--Data-Analytics--"
    "Jan---May-2027-_10016300"
)
KEPPEL_AUTOFILL = (
    "https://keppel.wd3.myworkdayjobs.com/en-US/KeppelCareers/job/Singapore/"
    "XMLNAME--Keppel-Internship-Programme-2027--Intern--Data-Analytics--"
    "Jan---May-2027-_10016300/apply/autofillWithResume"
)


class _Page:
    def __init__(self, url: str) -> None:
        self.url = url
        self.context = SimpleNamespace(
            new_cdp_session=lambda _page: SimpleNamespace(
                send=lambda _method: {"targetInfo": {"targetId": "root"}}
            )
        )
        self.frames_read = False

    @property
    def frames(self):
        self.frames_read = True
        raise AssertionError("an unbound job page must be rejected before field inspection")


class _PlaywrightContext:
    def __init__(self, browser: object) -> None:
        self._playwright = SimpleNamespace(
            chromium=SimpleNamespace(connect_over_cdp=lambda _url: browser)
        )

    def __enter__(self):
        return self._playwright

    def __exit__(self, _exc_type, _exc, _traceback) -> bool:
        return False


def test_ordinary_credential_relay_rejects_same_host_other_job_before_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    page = _Page("https://jobs.lever.co/acme/job-999/apply")
    browser = SimpleNamespace(
        new_browser_cdp_session=lambda: SimpleNamespace(
            send=lambda _method: {"targetInfos": [{"targetId": "root"}]}
        ),
        contexts=[SimpleNamespace(pages=[page])],
    )
    monkeypatch.setattr(credential_relay, "sync_playwright", lambda: _PlaywrightContext(browser))
    monkeypatch.setattr(credential_relay, "_relay_is_authorized", lambda: True)
    monkeypatch.setattr(credential_relay, "_allowed_hosts", lambda: {"jobs.lever.co"})
    monkeypatch.setattr(credential_relay, "_root_target_ids", lambda: {"root"})
    monkeypatch.setattr(
        credential_relay,
        "_application_context_binding",
        lambda: {
            "schema_version": "1",
            "attempt_id": "attempt-1",
            "application_id": "application-1",
            "target_urls": ["https://jobs.lever.co/acme/job-123/apply"],
            "provider_binding": {},
        },
    )
    monkeypatch.setenv("APPLYPILOT_ATS_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.setenv("APPLYPILOT_CREDENTIAL_ATTEMPT_ID", "attempt-1")
    monkeypatch.setenv("APPLYPILOT_CREDENTIAL_APPLICATION_ID", "application-1")

    with pytest.raises(
        credential_relay.CredentialRelayError,
        match="No visible editable credential field",
    ):
        credential_relay._fill_fields(9432, "email", "candidate@example.com", "unused")

    assert page.frames_read is False
    records = [
        json.loads(line)
        for line in (
            tmp_path / credential_relay.RELAY_DIAGNOSTIC_FILENAME
        ).read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        record["stage"] == "page_binding"
        and record["lineage_bound"] is True
        and record["url_bound"] is False
        for record in records
    )
    assert records[-1]["stage"] == "candidate_selection"
    assert records[-1]["status"] == "rejected"
    assert records[-1]["candidate_count"] == 0


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://jobs.lever.co/acme/job-123/apply", True),
        ("https://jobs.lever.co/login", True),
        ("https://jobs.lever.co/account/password-reset", False),
        ("https://jobs.lever.co/login-job-999", False),
        ("https://jobs.lever.co/acme/job-999/apply", False),
        ("https://jobs.lever.co/candidate?utm_source=login", False),
        ("http://jobs.lever.co/login", False),
        (
            (
                "https://career5.successfactors.eu/career"
                "?company=Temasek&login_ns=register&lang=en_GB"
            ),
            False,
        ),
        (
            (
                "https://career5.successfactors.eu/career"
                "?career_job_req_id=999&company=Temasek&login_ns=register&lang=en_GB"
            ),
            False,
        ),
    ],
)
def test_credential_surface_binding_allows_only_exact_job_or_narrow_auth_route(
    url: str, expected: bool
) -> None:
    binding = {
        "target_urls": [
            "https://jobs.lever.co/acme/job-123/apply",
            (
                "https://career5.successfactors.eu/career"
                "?career_job_req_id=123&company=Temasek&lang=en_GB"
            ),
        ],
        "provider_binding": {},
    }

    assert credential_relay._credential_surface_url_is_bound(url, binding) is expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            KEPPEL_AUTOFILL,
            True,
        ),
        (
            KEPPEL_POSTING + "/apply/autofillWithResume",
            True,
        ),
        (
            KEPPEL_AUTOFILL.replace("10016300", "10016301"),
            False,
        ),
        (
            KEPPEL_AUTOFILL.replace("/Singapore/", "/Singapore/Extra/"),
            True,
        ),
        (
            KEPPEL_AUTOFILL.replace("/Singapore/", "/Singapore/West/"),
            True,
        ),
        (
            KEPPEL_AUTOFILL.replace("/Singapore/", "/Singapore%2FWest/"),
            False,
        ),
        (
            KEPPEL_AUTOFILL.replace("/Singapore/", "/%2E%2E/"),
            False,
        ),
        (
            KEPPEL_AUTOFILL.replace("XMLNAME--", "XMLNAME--%2F"),
            False,
        ),
        (
            KEPPEL_AUTOFILL.replace("/Singapore/", "/Singapore\\West/"),
            False,
        ),
        (
            KEPPEL_AUTOFILL.replace("/en-US/", "/en-GB/"),
            True,
        ),
        (
            KEPPEL_AUTOFILL.replace("/KeppelCareers/", "/OtherCareers/"),
            False,
        ),
        (
            KEPPEL_AUTOFILL.replace("/apply/autofillWithResume", "/apply/accountSettings"),
            True,
        ),
        (
            KEPPEL_AUTOFILL.replace("keppel.wd3", "other.wd3"),
            False,
        ),
        (
            KEPPEL_AUTOFILL.replace("https://", "http://"),
            False,
        ),
    ],
    ids=(
        "real-location-route",
        "no-location-route",
        "other-job",
        "extra-segment",
        "two-location-segments",
        "encoded-location-slash",
        "encoded-dot-segment",
        "encoded-posting-slash",
        "backslash-confusion",
        "other-locale",
        "other-site",
        "unknown-step",
        "other-host",
        "http",
    ),
)
def test_workday_autofill_credential_surface_stays_bound_to_exact_job(
    url: str, expected: bool
) -> None:
    binding = {
        "target_urls": [KEPPEL_POSTING],
        "provider_binding": {},
    }

    assert credential_relay._credential_surface_url_is_bound(url, binding) is expected


def test_successfactors_company_only_careers_route_is_not_generic_auth_authority() -> None:
    binding = {
        "target_urls": [
            (
                "https://jobs.temasek.com.sg/job/Data-Engineer-Intern%2C-Technology-"
                "%28Jan-Jun-2027%29-238891/1369169257/"
            )
        ],
        "provider_binding": {},
    }
    actual = "https://career2.successfactors.eu/careers?company=temasekcapP2"

    assert credential_relay.host_is_known_ats("career2.successfactors.eu")
    assert not credential_relay._credential_surface_url_is_bound(actual, binding)


def test_relay_diagnostic_strips_query_fragment_and_secret_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    context_path = tmp_path / "ats-application-context.json"
    monkeypatch.setenv("APPLYPILOT_ATS_CONTEXT_PATH", str(context_path))
    monkeypatch.setenv("APPLYPILOT_CREDENTIAL_ATTEMPT_ID", "attempt-secret-safe")
    monkeypatch.setenv("APPLYPILOT_CREDENTIAL_APPLICATION_ID", "application-secret-safe")

    credential_relay._record_credential_diagnostic(
        "page_binding",
        "evaluated",
        surface="page",
        url=(
            "https://career.example/careers?requestParams=secret-token"
            "&otp=123456#password-secret"
        ),
        lineage_bound=True,
        url_bound=False,
        candidate_count=0,
    )

    diagnostic_path = tmp_path / credential_relay.RELAY_DIAGNOSTIC_FILENAME
    rendered = diagnostic_path.read_text(encoding="utf-8")
    assert "https://career.example/careers" in rendered
    assert "requestParams" not in rendered
    assert "secret-token" not in rendered
    assert "123456" not in rendered
    assert "password-secret" not in rendered
    diagnostic = json.loads(rendered)
    assert diagnostic["module_source"] == str(
        credential_relay.Path(credential_relay.__file__).resolve()
    )


@pytest.mark.browser
def test_real_workday_location_route_fills_visible_fields_in_isolated_browser(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    body = """
        <!doctype html><html><body>
        <label for="email">Email Address</label>
        <input id="email" type="email">
        <label for="password">Password</label>
        <input id="password" type="password">
        </body></html>
    """
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.route(
            "**/*",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body=body,
            ),
        )
        page.goto(KEPPEL_AUTOFILL)
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
            credential_relay,
            "sync_playwright",
            lambda: _ExistingBrowserContext(),
        )
        monkeypatch.setattr(credential_relay, "_relay_is_authorized", lambda: True)
        monkeypatch.setattr(
            credential_relay,
            "_allowed_hosts",
            lambda: {"keppel.wd3.myworkdayjobs.com"},
        )
        monkeypatch.setattr(
            credential_relay,
            "_password_host_is_allowed",
            lambda _host: True,
        )
        monkeypatch.setattr(
            credential_relay, "_known_ats_redirect_enabled", lambda: False
        )
        monkeypatch.setattr(
            credential_relay, "_root_target_ids", lambda: {target_id}
        )
        monkeypatch.setattr(
            credential_relay,
            "_application_context_binding",
            lambda: {
                "schema_version": "1",
                "attempt_id": "attempt-1",
                "application_id": "application-1",
                "target_urls": [KEPPEL_POSTING],
                "provider_binding": {},
            },
        )
        monkeypatch.setenv(
            "APPLYPILOT_ATS_CONTEXT_PATH", str(tmp_path / "context.json")
        )
        monkeypatch.setenv("APPLYPILOT_CREDENTIAL_ATTEMPT_ID", "attempt-1")
        monkeypatch.setenv(
            "APPLYPILOT_CREDENTIAL_APPLICATION_ID", "application-1"
        )

        try:
            outcome = credential_relay._fill_fields(
                0,
                "both",
                "candidate@example.com",
                "test-only-password",
            )
            assert outcome["status"] == "filled"
            assert outcome["submitted"] is False
            assert page.locator("#email").input_value() == "candidate@example.com"
            assert page.locator("#password").input_value() == "test-only-password"
            rendered = (
                tmp_path / credential_relay.RELAY_DIAGNOSTIC_FILENAME
            ).read_text(encoding="utf-8")
            assert '"lineage_bound": true' in rendered
            assert '"url_bound": true' in rendered
            assert '"visible_email_fields": 1' in rendered
            assert '"visible_password_fields": 1' in rendered
            assert "candidate@example.com" not in rendered
            assert "test-only-password" not in rendered
        finally:
            browser.close()
