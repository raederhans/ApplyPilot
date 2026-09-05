from __future__ import annotations

import pytest

from applypilot.apply.provider_registry import (
    host_supports_credential_relay,
    provider_for_url,
    provider_names,
    provider_supports,
)


def test_capabilities_are_scoped_instead_of_using_a_supported_provider_union() -> None:
    assert provider_for_url("https://jobs.lever.co/example/1", "detection") == "lever"
    assert provider_for_url("https://jobs.lever.co/example/1", "semantic_upload") is None
    assert provider_for_url("https://jobs.lever.co/example/1", "control_write") is None
    assert provider_supports("lever", "application_episode") is False

    assert provider_for_url(
        "https://tenant.myworkdayjobs.com/apply", "semantic_upload"
    ) == "workday"
    assert provider_for_url(
        "https://jobs.smartrecruiters.com/example/1", "control_write"
    ) == "smartrecruiters"
    assert provider_names("application_episode") == {
        "workday",
        "smartrecruiters",
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://tenant.myworkdayjobs.com/apply",
        "https://smartrecruiters.com.evil.test/apply",
        "https://unknown.example/apply",
        "not a url",
    ],
)
def test_unknown_or_malformed_provider_targets_fail_closed(url: str) -> None:
    assert provider_for_url(url, "semantic_upload") is None
    assert provider_for_url(url, "control_write") is None


def test_credential_relay_hosts_do_not_gain_detection_or_write_capabilities() -> None:
    assert host_supports_credential_relay("tenant.icims.com") is True
    assert provider_for_url("https://tenant.icims.com/apply", "detection") is None
    assert provider_for_url("https://tenant.icims.com/apply", "semantic_upload") is None


@pytest.mark.parametrize(
    "url",
    [
        "https://career2.successfactors.eu/careers?company=temasekcapP2",
        "https://careersd2.successfactors.eu/career?company=ACEEU95",
    ],
)
def test_successfactors_eu_is_credential_relay_only(url: str) -> None:
    assert provider_for_url(url, "credential_relay") == "successfactors"
    assert provider_for_url(url, "detection") is None
    assert provider_for_url(url, "semantic_upload") is None
    assert provider_for_url(url, "control_write") is None
    assert provider_for_url(url, "linkedin_external_handoff") is None
    assert provider_supports("successfactors", "application_episode") is False


def test_successfactors_eu_credential_relay_requires_https() -> None:
    assert provider_for_url(
        "http://career2.successfactors.eu/careers?company=temasekcapP2",
        "credential_relay",
    ) is None


@pytest.mark.parametrize(
    "hostname",
    [
        "successfactors.eu.evil.test",
        "notsuccessfactors.eu",
        "successfactors-eu.example",
    ],
)
def test_successfactors_eu_spoof_suffixes_are_rejected(hostname: str) -> None:
    assert host_supports_credential_relay(hostname) is False
