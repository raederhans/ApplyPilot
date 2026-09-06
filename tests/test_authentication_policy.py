from __future__ import annotations

import pytest

from applypilot.apply.authentication_policy import authentication_capability


@pytest.mark.parametrize("value", [False, "false", "true", 0, 1, None, [], {}])
def test_authentication_capability_requires_literal_true(value: object) -> None:
    profile = {"authentication": {"credential_relay_authorized": value}}

    assert authentication_capability(profile, "credential_relay_authorized") is False


def test_explicit_false_still_overrides_legacy_account_creation_authority() -> None:
    profile = {
        "authentication": {
            "ats_account_creation_authorized": True,
            "credential_relay_authorized": False,
        }
    }

    assert authentication_capability(profile, "credential_relay_authorized") is False


def test_legacy_compatibility_also_requires_literal_true() -> None:
    profile = {"authentication": {"ats_account_creation_authorized": "true"}}

    assert authentication_capability(profile, "ordinary_ats_sign_in_authorized") is False
    assert authentication_capability(profile, "credential_relay_authorized") is False
