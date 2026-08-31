from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from applypilot.apply import credential_relay, launcher


def _binding(
    target_url: str = "https://jobs.lever.co/acme/job-123",
) -> dict[str, object]:
    return {
        "schema_version": "1",
        "attempt_id": "attempt-1",
        "application_id": "application-1",
        "target_urls": [target_url],
        "provider_binding": {},
    }


def _install_context(monkeypatch, tmp_path, binding: dict[str, object]) -> None:
    raw = json.dumps(
        {"credential_binding": binding},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    path = tmp_path / "ats-context.json"
    path.write_bytes(raw)
    monkeypatch.setenv("APPLYPILOT_ATS_CONTEXT_PATH", str(path))
    monkeypatch.setenv(
        "APPLYPILOT_CREDENTIAL_APPLICATION_CONTEXT_SHA256",
        hashlib.sha256(raw).hexdigest(),
    )
    monkeypatch.setenv("APPLYPILOT_CREDENTIAL_ATTEMPT_ID", "attempt-1")
    monkeypatch.setenv("APPLYPILOT_CREDENTIAL_APPLICATION_ID", "application-1")


def _install_browser_preflight(monkeypatch, descriptors: list[dict[str, object]]) -> None:
    target_url = "https://jobs.lever.co/acme/job-123"
    frame = SimpleNamespace(url=target_url)
    page = SimpleNamespace(url=target_url, frames=[frame], main_frame=frame)

    class BrowserContext:
        def __init__(self) -> None:
            self.pages = [page]

        @staticmethod
        def new_cdp_session(_page):
            return SimpleNamespace(
                send=lambda _method: {"targetInfo": {"targetId": "root-target"}}
            )

    page.context = BrowserContext()
    browser = SimpleNamespace(
        contexts=[page.context],
        new_browser_cdp_session=lambda: SimpleNamespace(
            send=lambda _method: {
                "targetInfos": [{"targetId": "root-target", "openerId": ""}]
            }
        ),
    )
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(connect_over_cdp=lambda _url: browser)
    )

    class PlaywrightContextManager:
        def __enter__(self):
            return playwright

        def __exit__(self, _exc_type, _exc, _traceback):
            return False

    locators = [SimpleNamespace(index=index) for index in range(len(descriptors))]
    monkeypatch.setattr(
        credential_relay, "sync_playwright", lambda: PlaywrightContextManager()
    )
    monkeypatch.setattr(credential_relay, "_candidate_pages", lambda pages: pages)
    monkeypatch.setattr(
        credential_relay,
        "_visible_locators",
        lambda _frame, _selectors: locators,
    )
    monkeypatch.setattr(
        credential_relay,
        "_protected_identifier_descriptor",
        lambda locator: descriptors[locator.index],
    )
    monkeypatch.setenv("APPLYPILOT_CREDENTIAL_ROOT_TARGET_IDS", "root-target")
    monkeypatch.setenv("APPLYPILOT_CREDENTIAL_ALLOWED_HOSTS", "jobs.lever.co")
    monkeypatch.setenv("APPLYPILOT_CDP_PORT", "9222")


def test_launcher_context_binds_exact_attempt_application_and_routes() -> None:
    job = {
        "url": "https://jobs.lever.co/acme/job-123?source=linkedin",
        "application_url": "https://jobs.lever.co/acme/job-123/apply",
        "company_name": "Acme",
        "title": "Data Scientist",
        "platform_job_id": "job-123",
    }

    context = launcher._build_ats_application_context(
        job, {}, attempt_id="attempt-1"
    )

    binding = context["credential_binding"]
    assert binding["attempt_id"] == "attempt-1"
    assert len(binding["application_id"]) == 64
    assert binding["target_urls"] == [
        "https://jobs.lever.co/acme/job-123/apply",
    ]


def test_launcher_context_keeps_only_allowlisted_query_identity_evidence() -> None:
    context = launcher._build_ats_application_context(
        {
            "url": "https://career.successfactors.com/career",
            "application_url": (
                "https://career.successfactors.com/career?career_job_req_id=123"
                "&session_token=must-not-escape&utm_source=linkedin"
            ),
            "company_name": "Acme",
            "title": "Role",
        },
        {},
        attempt_id="attempt-1",
    )
    target = context["credential_binding"]["target_urls"][0]
    assert target == (
        "https://career.successfactors.com/career?career_job_req_id=123"
    )
    assert "session_token" not in json.dumps(context["credential_binding"])


def test_exact_job_matches_but_same_ats_host_other_job_is_rejected() -> None:
    binding = _binding()
    assert credential_relay._application_url_is_bound(
        "https://jobs.lever.co/acme/job-123/apply", binding
    )
    assert not credential_relay._application_url_is_bound(
        "https://jobs.lever.co/acme/job-999/apply", binding
    )


def test_opener_descendant_navigation_to_other_job_invalidates_authorization() -> None:
    binding = _binding("https://boards.greenhouse.io/acme/jobs/123")
    assert not credential_relay._application_url_is_bound(
        "https://boards.greenhouse.io/acme/jobs/999", binding
    )


def test_known_ats_cross_tenant_redirect_is_not_generic_authority() -> None:
    binding = _binding("https://careers.acme.example/jobs/123")
    assert not credential_relay._application_url_is_bound(
        "https://jobs.smartrecruiters.com/Other/999-role", binding
    )


def test_same_host_and_path_with_different_query_job_id_is_rejected() -> None:
    binding = _binding(
        "https://career.successfactors.com/career?career_job_req_id=123&company=acme"
    )
    assert credential_relay._application_url_is_bound(
        "https://career.successfactors.com/career?company=acme&career_job_req_id=123",
        binding,
    )
    assert not credential_relay._application_url_is_bound(
        "https://career.successfactors.com/career?career_job_req_id=999&company=acme",
        binding,
    )


def test_actual_identity_query_cannot_extend_queryless_authority() -> None:
    binding = _binding("https://career.successfactors.com/career")
    assert not credential_relay._application_url_is_bound(
        "https://career.successfactors.com/career?career_job_req_id=999", binding
    )


def test_smartrecruiters_redirect_requires_resolved_exact_tenant_and_publication() -> None:
    binding = _binding(
        "https://jobs.smartrecruiters.com/Grab/744000145885499-data-science-intern"
    )
    binding["provider_binding"] = {
        "provider": "smartrecruiters",
        "tenant": "Grab",
        "posting_id": "744000145885499",
        "publication_id": "11111111-2222-3333-4444-555555555555",
        "resolved": True,
    }
    exact = (
        "https://jobs.smartrecruiters.com/oneclick-ui/company/Grab/publication/"
        "11111111-2222-3333-4444-555555555555?dcr_ci=Grab"
    )
    other_tenant = exact.replace("/Grab/", "/Other/").replace(
        "dcr_ci=Grab", "dcr_ci=Other"
    )
    assert credential_relay._application_url_is_bound(exact, binding)
    assert not credential_relay._application_url_is_bound(other_tenant, binding)


def test_context_missing_or_wrong_attempt_fails_closed(monkeypatch, tmp_path) -> None:
    with pytest.raises(credential_relay.CredentialRelayError, match="missing"):
        credential_relay._application_context_binding()

    _install_context(monkeypatch, tmp_path, _binding())
    monkeypatch.setenv("APPLYPILOT_CREDENTIAL_ATTEMPT_ID", "attempt-2")
    with pytest.raises(credential_relay.CredentialRelayError, match="exact attempt"):
        credential_relay._application_context_binding()


def test_context_digest_rejects_post_authorization_mutation(monkeypatch, tmp_path) -> None:
    binding = _binding()
    _install_context(monkeypatch, tmp_path, binding)
    path = tmp_path / "ats-context.json"
    path.write_text(
        json.dumps({"credential_binding": _binding("https://jobs.lever.co/acme/job-999")}),
        encoding="utf-8",
    )

    with pytest.raises(credential_relay.CredentialRelayError, match="launcher digest"):
        credential_relay._application_context_binding()


def test_fin_decryption_is_one_shot_and_context_checked_first(
    monkeypatch, tmp_path
) -> None:
    _install_context(monkeypatch, tmp_path, _binding())
    credential_relay._IDENTITY_DECRYPTED_ATTEMPTS.discard("attempt-1")
    calls: list[tuple[object, str]] = []

    def fake_decrypt(path, property_name):
        calls.append((path, property_name))
        return "protected-test-value"

    monkeypatch.setattr(credential_relay, "_decrypt_dpapi_value", fake_decrypt)
    monkeypatch.setattr(
        credential_relay, "_assert_identity_page_preflight", lambda _port, _binding: None
    )
    monkeypatch.setenv("APPLYPILOT_CDP_PORT", "9222")
    assert credential_relay._decrypt_fin(tmp_path / "never-read.json") == "protected-test-value"
    with pytest.raises(credential_relay.CredentialRelayError, match="already consumed"):
        credential_relay._decrypt_fin(tmp_path / "never-read.json")
    assert len(calls) == 1
    credential_relay._IDENTITY_DECRYPTED_ATTEMPTS.discard("attempt-1")


@pytest.mark.parametrize(
    "descriptors",
    [
        [],
        [{"text": "FIN Number", "required": False}],
        [
            {"text": "FIN Number", "required": True},
            {"text": "NRIC / FIN", "required": True},
        ],
    ],
    ids=("missing", "optional", "ambiguous"),
)
def test_fin_is_not_decrypted_without_one_exact_required_candidate(
    monkeypatch,
    tmp_path,
    descriptors: list[dict[str, object]],
) -> None:
    _install_context(monkeypatch, tmp_path, _binding())
    _install_browser_preflight(monkeypatch, descriptors)
    credential_relay._IDENTITY_DECRYPTED_ATTEMPTS.discard("attempt-1")
    decrypt_calls: list[object] = []
    monkeypatch.setattr(
        credential_relay,
        "_decrypt_dpapi_value",
        lambda *_args: decrypt_calls.append(object()),
    )

    with pytest.raises(credential_relay.CredentialRelayError, match="exact required FIN"):
        credential_relay._decrypt_fin(tmp_path / "never-read.json")

    assert decrypt_calls == []
    credential_relay._IDENTITY_DECRYPTED_ATTEMPTS.discard("attempt-1")


def test_display_mask_instability_clears_field_and_fails_closed() -> None:
    class ReactiveLocator:
        def __init__(self) -> None:
            self.value = "protected-test-value"

        def evaluate(self, _script, _kind):
            return {"type": "text", "marker": None}

        def input_value(self):
            return self.value

        def fill(self, value):
            self.value = value

    locator = ReactiveLocator()
    with pytest.raises(credential_relay.CredentialRelayError, match="display mask"):
        credential_relay._apply_protected_display_mask(
            locator, "fin", "protected-test-value"
        )
    assert locator.value == ""
