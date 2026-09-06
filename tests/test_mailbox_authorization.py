from __future__ import annotations

import pytest

from applypilot.apply import launcher, prompt
from applypilot.apply.authentication_policy import authentication_capability


def _profile(value: object) -> dict:
    return {
        "personal": {"email": "candidate@example.test"},
        "authentication": {
            "ordinary_ats_sign_in_authorized": True,
            "credential_relay_authorized": True,
            "gmail_verification_authorized": value,
            "gmail_verification_mailbox": "candidate@example.test",
        },
    }


@pytest.mark.parametrize("value", [False, "false", "true", 1, "1", None, [], {"enabled": True}])
def test_non_boolean_mailbox_authorization_cannot_enable_prompt_or_receipt_observer(value: object) -> None:
    profile = _profile(value)
    steps = prompt._build_login_steps(profile, available_tools=("mailbox_search", "mailbox_get_message"))

    assert "Do not open email or enter verification codes." in steps
    assert authentication_capability(profile, "mailbox_read_authorized") is False
    assert launcher._configured_receipt_observers(profile) == []


def test_literal_true_preserves_legacy_mailbox_read_and_otp_prompt() -> None:
    profile = _profile(True)
    steps = prompt._build_login_steps(profile, available_tools=("mailbox_search", "mailbox_get_message"))

    assert "read-only mailbox tools" in steps
    assert authentication_capability(profile, "mailbox_read_authorized") is True
    assert [provider for provider, _spec in launcher._configured_receipt_observers(profile)] == ["gmail"]


@pytest.mark.parametrize("value", [False, "false", "true", 1, None])
def test_explicit_mailbox_read_value_overrides_legacy_true_without_coercion(value: object) -> None:
    profile = _profile(True)
    profile["authentication"]["mailbox_read_authorized"] = value

    assert authentication_capability(profile, "mailbox_read_authorized") is False
    assert launcher._configured_receipt_observers(profile) == []


def test_explicit_mailbox_read_true_keeps_receipts_separate_from_verification() -> None:
    profile = _profile(False)
    profile["authentication"]["mailbox_read_authorized"] = True
    steps = prompt._build_login_steps(profile, available_tools=("mailbox_search", "mailbox_get_message"))

    assert "Do not open email or enter verification codes." in steps
    assert authentication_capability(profile, "mailbox_read_authorized") is True
    assert [provider for provider, _spec in launcher._configured_receipt_observers(profile)] == ["gmail"]
