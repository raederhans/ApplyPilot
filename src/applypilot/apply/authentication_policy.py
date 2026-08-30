"""Versioned, capability-specific authentication policy helpers."""

from collections.abc import Mapping

_LEGACY_ACCOUNT_CREATION_KEY = "ats_account_creation_authorized"
_LEGACY_COMPATIBLE_KEYS = frozenset(
    {"ordinary_ats_sign_in_authorized", "credential_relay_authorized"}
)


def authentication_capability(profile: Mapping[str, object], key: str) -> bool:
    """Return one authentication capability without widening adjacent authority.

    Older trusted profiles used account-creation authorization for both ordinary
    ATS sign-in and credential relay. Preserve that behavior only while the new
    capability key is absent; an explicit new value always wins.
    """
    raw_authentication = profile.get("authentication")
    if not isinstance(raw_authentication, Mapping):
        return False
    if key in raw_authentication:
        return bool(raw_authentication[key])
    if key in _LEGACY_COMPATIBLE_KEYS:
        return bool(raw_authentication.get(_LEGACY_ACCOUNT_CREATION_KEY, False))
    return False
