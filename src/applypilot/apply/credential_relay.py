"""Fill ATS credentials without exposing the password to the browser agent.

The password is encrypted for the current Windows user by PowerShell DPAPI.
This helper decrypts it only inside a short-lived child process, connects to the
already-running Edge instance over CDP, fills visible fields, and never submits.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, unquote, urlparse

from playwright.sync_api import Frame, Locator, Page, sync_playwright

from applypilot import config
from applypilot.apply.provider_registry import host_supports_credential_relay

BLOCKED_IDENTITY_HOSTS = {
    "accounts.google.com",
    "appleid.apple.com",
    "login.live.com",
    "login.microsoftonline.com",
    "okta.com",
}

EMAIL_SELECTORS = (
    'input[type="email"]',
    'input[autocomplete="email"]',
    'input[autocomplete="username"]',
    'input[name*="email" i]',
    'input[id*="email" i]',
)
EMAIL_LABEL_FALLBACK_SELECTOR = (
    'input:not([type]), input[type="text"], input[type="email"]'
)
EMAIL_LABEL_RE = re.compile(
    r"^email(?:\s+address)?(?:\s*[:：*✱])*$",
    re.IGNORECASE,
)
EMAIL_CONFIRMATION_LABEL_RE = re.compile(
    r"^(?:confirm|retype|re-enter)\s+email(?:\s+address)?(?:\s*[:：*✱])*$",
    re.IGNORECASE,
)
PASSWORD_SELECTORS = (
    'input[type="password"]',
)
PROTECTED_IDENTIFIER_INPUT_SELECTOR = (
    'input:not([type]), input[type="text"], input[type="tel"], input[type="number"]'
)
FIN_FIELD_RE = re.compile(
    r"(?:^|\b)(?:nric\s*(?:/|or)\s*fin|nric|fin)"
    r"(?:\s+(?:identification\s+)?(?:no\.?|number))?(?:\b|$)",
    re.IGNORECASE,
)
_NAME_FIELD_RE = re.compile(
    r"\b(?:first|last|full|legal|preferred)\s+name\b", re.IGNORECASE
)
VOLATILE_QUERY_KEYS = {
    "gh_src",
    "li_fat_id",
    "referrer",
    "source",
    "trackingid",
    "trk",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
}
_IDENTITY_DECRYPTED_ATTEMPTS: set[str] = set()


def _is_fin_field_descriptor(value: object) -> bool:
    """Exclude name fields whose placeholder merely references an ID document."""
    text = " ".join(str(value or "").split())
    if _NAME_FIELD_RE.search(text) and re.search(
        r"\bas\s+(?:shown\s+)?in\s+(?:your\s+)?(?:nric|passport)\b",
        text,
        re.IGNORECASE,
    ):
        return False
    return FIN_FIELD_RE.search(text) is not None


class CredentialRelayError(RuntimeError):
    """Expected, user-safe credential relay failure."""


def _host_matches(host: str, candidate: str) -> bool:
    host = host.lower().strip(".")
    candidate = candidate.lower().strip(".")
    return host == candidate or host.endswith(f".{candidate}")


def host_is_allowed(host: str, configured_hosts: set[str]) -> bool:
    """Return whether a host is an exact authorized employer credential target.

    Known ATS domains are deliberately *not* globally authorized here.  They
    are considered only by the unique-redirect fallback in ``_fill_fields``.
    This keeps a second, unrelated ATS tab from receiving credentials.
    """
    normalized = host.lower().strip(".")
    if not normalized:
        return False
    if any(_host_matches(normalized, blocked) for blocked in BLOCKED_IDENTITY_HOSTS):
        return False
    return any(
        _host_matches(normalized, candidate)
        for candidate in configured_hosts
    )


def host_is_known_ats(host: str) -> bool:
    """Return whether a non-identity host belongs to a supported ATS family."""
    normalized = host.lower().strip(".")
    if not normalized:
        return False
    if any(_host_matches(normalized, blocked) for blocked in BLOCKED_IDENTITY_HOSTS):
        return False
    return host_supports_credential_relay(normalized)


def _select_candidate(candidates: list[dict[str, object]]) -> dict[str, object]:
    """Choose one unambiguous page/frame candidate.

    Exact configured-host candidates win.  A known-ATS redirect is accepted
    only when the caller explicitly enabled it and exactly one browser page
    contains eligible credential fields.  Multiple matching tabs fail closed
    instead of selecting whichever Playwright happened to enumerate first.
    """
    exact = [candidate for candidate in candidates if candidate["match"] == "exact"]
    eligible = exact or [
        candidate for candidate in candidates if candidate["match"] == "known_ats_redirect"
    ]
    if not eligible:
        raise CredentialRelayError(
            "No visible editable credential field was found on the authorized job page."
        )

    page_indexes = {int(candidate["page_index"]) for candidate in eligible}
    if len(page_indexes) != 1:
        raise CredentialRelayError(
            "Credential relay found matching fields on multiple browser tabs; close or "
            "disambiguate the unrelated ATS tab and retry."
        )

    ranked = sorted(
        eligible,
        key=lambda candidate: (
            int(candidate.get("field_count", 0)),
            candidate.get("frame_url") == candidate.get("page_url"),
        ),
        reverse=True,
    )
    best = ranked[0]
    tied = [
        candidate
        for candidate in ranked
        if candidate.get("field_count") == best.get("field_count")
        and (candidate.get("frame_url") == candidate.get("page_url"))
        == (best.get("frame_url") == best.get("page_url"))
    ]
    if len(tied) > 1:
        raise CredentialRelayError(
            "Credential relay found equally plausible credential forms in one tab; retry "
            "after the page finishes navigating."
        )
    return best


def _credential_path() -> Path:
    configured = os.environ.get("APPLYPILOT_ATS_CREDENTIAL_FILE")
    if configured:
        return Path(configured)
    return config.APP_DIR / "credentials" / "ats-signup.json"


def _identity_credential_path() -> Path:
    configured = os.environ.get("APPLYPILOT_IDENTITY_CREDENTIAL_FILE")
    if configured:
        return Path(configured)
    return config.APP_DIR / "credentials" / "identity-protected.json"


def _read_record(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise CredentialRelayError(
            "ATS credential relay is not configured. Run set-ats-credentials.ps1 locally."
        )
    try:
        record = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CredentialRelayError("ATS credential record is unreadable.") from exc

    email = str(record.get("email", "")).strip().lower()
    encrypted = str(record.get("password_dpapi", "")).strip()
    if not email or not encrypted:
        raise CredentialRelayError("ATS credential record is incomplete.")
    return {"email": email, "password_dpapi": encrypted}


def _read_identity_record(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise CredentialRelayError(
            "Protected-identity relay is not configured. Run "
            "set-identity-credentials.ps1 locally."
        )
    try:
        record = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CredentialRelayError("Protected-identity credential record is unreadable.") from exc

    encrypted = str(record.get("fin_dpapi", "")).strip()
    if not encrypted:
        raise CredentialRelayError("Protected-identity credential record is incomplete.")
    return {"fin_dpapi": encrypted}


def _decrypt_dpapi_value(path: Path, property_name: str) -> str:
    """Decrypt one fixed record property without putting plaintext in argv or logs."""
    if property_name not in {"password_dpapi", "fin_dpapi"}:
        raise CredentialRelayError("Unsupported protected credential property.")
    powershell = os.environ.get("COMSPEC_POWERSHELL", "powershell.exe")
    script = r"""
$ErrorActionPreference = 'Stop'
$record = Get-Content -LiteralPath $env:APPLYPILOT_DPAPI_CREDENTIAL_FILE -Raw | ConvertFrom-Json
$propertyName = $env:APPLYPILOT_DPAPI_PROPERTY
$encrypted = $record.$propertyName
if (-not $encrypted) { throw 'Protected credential property is missing.' }
$secure = ConvertTo-SecureString -String $encrypted
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
  [Console]::Out.Write([Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr))
} finally {
  [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
}
"""
    child_env = os.environ.copy()
    child_env["APPLYPILOT_DPAPI_CREDENTIAL_FILE"] = str(path)
    child_env["APPLYPILOT_DPAPI_PROPERTY"] = property_name
    # A Python process launched from PowerShell 7 may inherit a PSModulePath
    # that contains only pwsh module roots. Windows PowerShell then cannot
    # autoload Microsoft.PowerShell.Security, which makes DPAPI decryption fail
    # even for the same Windows user. Point the child at the built-in Windows
    # PowerShell modules explicitly.
    system_root = child_env.get("SystemRoot", r"C:\Windows")
    child_env["PSModulePath"] = str(
        Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "Modules"
    )
    try:
        completed = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            env=child_env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CredentialRelayError("Windows DPAPI decryption could not be started.") from exc
    if completed.returncode != 0 or not completed.stdout:
        raise CredentialRelayError(
            "Windows DPAPI could not decrypt the ATS credential for this user."
        )
    return completed.stdout


def _decrypt_password(path: Path) -> str:
    """Decrypt the ATS password without placing plaintext in argv or output logs."""
    return _decrypt_dpapi_value(path, "password_dpapi")


def _decrypt_fin(path: Path) -> str:
    """Decrypt once only after the launcher context passes exact binding checks."""
    binding = _application_context_binding()
    attempt_id = str(binding["attempt_id"])
    if attempt_id in _IDENTITY_DECRYPTED_ATTEMPTS:
        raise CredentialRelayError(
            "Protected-identity authorization was already consumed for this attempt."
        )
    try:
        cdp_port = int(os.environ.get("APPLYPILOT_CDP_PORT", ""))
    except ValueError as exc:
        raise CredentialRelayError(
            "Protected-identity relay has no valid worker browser port."
        ) from exc
    _assert_identity_page_preflight(cdp_port, binding)
    _IDENTITY_DECRYPTED_ATTEMPTS.add(attempt_id)
    return _decrypt_dpapi_value(path, "fin_dpapi")


def _visible_locators(frame: Frame, selectors: tuple[str, ...]) -> list[Locator]:
    locator = frame.locator(", ".join(selectors))
    visible: list[Locator] = []
    for index in range(min(locator.count(), 10)):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible() and candidate.is_editable():
                visible.append(candidate)
        except Exception:  # noqa: BLE001, S112 - detached frames are expected during navigation
            continue
    return visible


def _visible_locator(frame: Frame, selectors: tuple[str, ...]) -> Locator | None:
    visible = _visible_locators(frame, selectors)
    return visible[0] if visible else None


def _accessible_label(locator: Locator) -> str:
    """Read one input's non-secret accessible label."""
    label = locator.evaluate(
        r"""element => {
          const labelledBy = String(element.getAttribute('aria-labelledby') || '')
            .trim().split(/\s+/).filter(Boolean)
            .map(id => document.getElementById(id)?.innerText || '')
            .filter(Boolean).join(' ');
          if (labelledBy) return labelledBy;
          const ariaLabel = String(element.getAttribute('aria-label') || '').trim();
          if (ariaLabel) return ariaLabel;
          return [...(element.labels || [])]
            .map(label => label.innerText || label.textContent || '')
            .filter(Boolean).join(' ');
        }"""
    )
    return " ".join(str(label or "").split())


def _visible_accessible_email_locator(frame: Frame) -> Locator | None:
    """Return one text-like input whose accessible label is exactly email."""

    matches: list[Locator] = []
    for candidate in _visible_locators(frame, (EMAIL_LABEL_FALLBACK_SELECTOR,)):
        try:
            normalized = _accessible_label(candidate)
        except Exception:  # noqa: BLE001, S112 - detached fields are expected during navigation
            continue
        if EMAIL_LABEL_RE.fullmatch(normalized):
            matches.append(candidate)
    if len(matches) > 1:
        raise CredentialRelayError(
            "Credential relay found multiple primary email fields and refused an ambiguous form."
        )
    return matches[0] if matches else None


def _visible_email_confirmation_locator(frame: Frame) -> Locator | None:
    """Return at most one explicit confirm/retype email input."""
    matches: list[Locator] = []
    for candidate in _visible_locators(frame, (EMAIL_LABEL_FALLBACK_SELECTOR,)):
        try:
            if EMAIL_CONFIRMATION_LABEL_RE.fullmatch(_accessible_label(candidate)):
                matches.append(candidate)
        except Exception:  # noqa: BLE001, S112 - detached fields are expected during navigation
            continue
    if len(matches) > 1:
        raise CredentialRelayError(
            "Credential relay found multiple email confirmation fields and refused an ambiguous form."
        )
    return matches[0] if matches else None


def _fill_password_fields(password_fields: list[Locator], password: str) -> int:
    """Fill an ordinary ATS password plus its optional confirmation field."""
    if len(password_fields) > 2:
        raise CredentialRelayError(
            "Credential relay found more than two password fields and refused a "
            "possible password-change or recovery form."
        )
    if not password_fields:
        return 0
    for field in password_fields:
        try:
            is_password = bool(
                field.evaluate(
                    "element => String(element.type || '').toLowerCase() === 'password'"
                )
            )
        except Exception as exc:
            raise CredentialRelayError(
                "Credential relay could not verify the password field type."
            ) from exc
        if not is_password:
            raise CredentialRelayError(
                "Credential relay refused a non-password credential field."
            )
    for field in password_fields:
        try:
            field.fill(password)
        except Exception:  # noqa: BLE001 - never expose a driver error containing the secret
            raise CredentialRelayError(
                "Credential relay could not fill every password field."
            ) from None
    try:
        password_fields[-1].blur()
    except Exception:  # noqa: BLE001 - do not accept credentials before validation blur
        raise CredentialRelayError(
            "Credential relay could not finalize password-field validation."
        ) from None
    for field in password_fields:
        try:
            matched = bool(
                field.evaluate(
                    "(element, expected) => "
                    "String(element.type || '').toLowerCase() === 'password' "
                    "&& element.value === expected",
                    password,
                )
            )
        except Exception:  # noqa: BLE001 - detached fields fail closed
            matched = False
        if not matched:
            raise CredentialRelayError(
                "Credential relay could not verify that every password field was filled."
            )
    return len(password_fields)


def _clear_exact_secret_from_text_inputs(frame: Frame, password: str) -> None:
    """Clear only current-frame text inputs proven equal to this attempt's secret."""
    for locator in _visible_locators(frame, (EMAIL_LABEL_FALLBACK_SELECTOR,)):
        try:
            misplaced = bool(
                locator.evaluate(
                    "(element, expected) => element.value === expected",
                    password,
                )
            )
            if misplaced:
                locator.fill("")
        except Exception:  # noqa: BLE001, S112 - best-effort cleanup stays in this frame
            continue


def _fill_credential_fields(
    frame: Frame,
    email_locator: Locator | None,
    email_confirmation_locator: Locator | None,
    password_locators: list[Locator],
    email: str,
    password: str,
) -> tuple[bool, bool, int]:
    """Fill credentials in primary-email, confirmation, then password order."""
    email_filled = False
    email_confirmation_filled = False
    if email_locator is not None:
        email_locator.fill(email)
        email_filled = True
    if email_confirmation_locator is not None:
        email_confirmation_locator.fill(email)
        email_confirmation_filled = True
    try:
        password_fields_filled = _fill_password_fields(password_locators, password)
    except CredentialRelayError:
        _clear_exact_secret_from_text_inputs(frame, password)
        raise
    return email_filled, email_confirmation_filled, password_fields_filled


def _requested_fields_present(field: str, email_present: bool, password_present: bool) -> bool:
    if field == "both":
        return email_present and password_present
    if field == "email":
        return email_present
    return password_present


def _candidate_pages(pages: list[Page]) -> list[Page]:
    return sorted(
        (page for page in pages if page.url and page.url != "about:blank"),
        key=lambda page: page.url.startswith("https://"),
        reverse=True,
    )


def _allowed_hosts() -> set[str]:
    raw = os.environ.get("APPLYPILOT_CREDENTIAL_ALLOWED_HOSTS", "")
    return {host.strip().lower() for host in raw.split(",") if host.strip()}


def _password_allowed_hosts() -> set[str]:
    raw = os.environ.get("APPLYPILOT_CREDENTIAL_PASSWORD_ALLOWED_HOSTS", "")
    return {host.strip().lower() for host in raw.split(",") if host.strip()}


def _relay_is_authorized() -> bool:
    return os.environ.get("APPLYPILOT_CREDENTIAL_RELAY_AUTHORIZED", "").strip() == "1"


def _identity_relay_is_authorized() -> bool:
    return os.environ.get("APPLYPILOT_IDENTITY_RELAY_AUTHORIZED", "").strip() == "1"


def _password_host_is_allowed(host: str) -> bool:
    return host_is_known_ats(host) or host_is_allowed(host, _password_allowed_hosts())


def _known_ats_redirect_enabled() -> bool:
    return os.environ.get("APPLYPILOT_CREDENTIAL_ALLOW_KNOWN_ATS_REDIRECT", "").strip() == "1"


def _root_target_ids() -> set[str]:
    raw = os.environ.get("APPLYPILOT_CREDENTIAL_ROOT_TARGET_IDS", "")
    return {value.strip() for value in raw.split(",") if value.strip()}


def _application_context_binding() -> dict[str, object]:
    """Load the launcher-authored, digest-bound application identity."""
    path_value = os.environ.get("APPLYPILOT_ATS_CONTEXT_PATH", "").strip()
    expected_digest = os.environ.get(
        "APPLYPILOT_CREDENTIAL_APPLICATION_CONTEXT_SHA256", ""
    ).strip().casefold()
    expected_attempt = os.environ.get(
        "APPLYPILOT_CREDENTIAL_ATTEMPT_ID", ""
    ).strip()
    expected_application = os.environ.get(
        "APPLYPILOT_CREDENTIAL_APPLICATION_ID", ""
    ).strip()
    if not path_value or not expected_digest or not expected_attempt or not expected_application:
        raise CredentialRelayError(
            "Protected-identity relay is missing its exact application context."
        )
    try:
        raw = Path(path_value).read_bytes()
    except OSError as exc:
        raise CredentialRelayError(
            "Protected-identity application context is unavailable."
        ) from exc
    if hashlib.sha256(raw).hexdigest() != expected_digest:
        raise CredentialRelayError(
            "Protected-identity application context did not match the launcher digest."
        )
    try:
        context = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CredentialRelayError(
            "Protected-identity application context is unreadable."
        ) from exc
    binding = context.get("credential_binding") if isinstance(context, Mapping) else None
    if not isinstance(binding, Mapping):
        raise CredentialRelayError(
            "Protected-identity application context has no credential binding."
        )
    if (
        binding.get("schema_version") != "1"
        or str(binding.get("attempt_id") or "") != expected_attempt
        or str(binding.get("application_id") or "") != expected_application
    ):
        raise CredentialRelayError(
            "Protected-identity application context does not identify this exact attempt."
        )
    target_urls = binding.get("target_urls")
    if not isinstance(target_urls, list) or not all(
        isinstance(url, str) and url.strip() for url in target_urls
    ):
        raise CredentialRelayError(
            "Protected-identity application context has no exact target route."
        )
    return dict(binding)


def _same_exact_application_path(expected_url: str, actual_url: str) -> bool:
    expected = urlparse(expected_url)
    actual = urlparse(actual_url)
    if (
        expected.scheme.casefold() != "https"
        or actual.scheme.casefold() != "https"
        or not expected.hostname
        or expected.hostname.casefold() != (actual.hostname or "").casefold()
    ):
        return False
    expected_path = unquote(expected.path).rstrip("/").removesuffix("/apply")
    actual_path = unquote(actual.path).rstrip("/").removesuffix("/apply")
    expected_tokens = tuple(re.findall(r"[a-z0-9]+", expected_path.casefold()))
    actual_tokens = tuple(re.findall(r"[a-z0-9]+", actual_path.casefold()))
    if not expected_tokens or expected_tokens != actual_tokens:
        return False
    expected_identity_query = tuple(
        sorted(
            (key.casefold(), value)
            for key, value in parse_qsl(expected.query, keep_blank_values=True)
            if key.casefold() not in VOLATILE_QUERY_KEYS
        )
    )
    actual_identity_query = tuple(
        sorted(
            (key.casefold(), value)
            for key, value in parse_qsl(actual.query, keep_blank_values=True)
            if key.casefold() not in VOLATILE_QUERY_KEYS
        )
    )
    return expected_identity_query == actual_identity_query


def _smartrecruiters_application_is_bound(
    expected_url: str,
    actual_url: str,
    provider_binding: Mapping[str, object],
) -> bool:
    """Bind the public posting to one resolved one-click tenant/publication."""
    expected = urlparse(expected_url)
    actual = urlparse(actual_url)
    expected_parts = [part for part in expected.path.split("/") if part]
    actual_parts = [part for part in actual.path.split("/") if part]
    if (
        expected.scheme.casefold() != "https"
        or actual.scheme.casefold() != "https"
        or (expected.hostname or "").casefold() != "jobs.smartrecruiters.com"
        or (actual.hostname or "").casefold() != "jobs.smartrecruiters.com"
        or len(expected_parts) < 2
        or len(actual_parts) < 5
        or actual_parts[:2] != ["oneclick-ui", "company"]
        or actual_parts[3] != "publication"
    ):
        return False
    tenant = expected_parts[0]
    posting_id = expected_parts[1].split("-", 1)[0]
    actual_tenant = actual_parts[2]
    publication_id = actual_parts[4]
    query_tenant = (parse_qs(actual.query).get("dcr_ci") or [""])[0]
    if any(
        key.casefold() not in VOLATILE_QUERY_KEYS | {"dcr_ci"}
        for key, _value in parse_qsl(actual.query, keep_blank_values=True)
    ):
        return False
    return bool(
        provider_binding.get("resolved") is True
        and str(provider_binding.get("provider") or "").casefold()
        == "smartrecruiters"
        and tenant.casefold() == actual_tenant.casefold() == query_tenant.casefold()
        and str(provider_binding.get("tenant") or "").casefold() == tenant.casefold()
        and str(provider_binding.get("posting_id") or "") == posting_id
        and str(provider_binding.get("publication_id") or "").casefold()
        == publication_id.casefold()
    )


def _application_url_is_bound(actual_url: str, binding: Mapping[str, object]) -> bool:
    """Require the page to prove the exact launcher-selected job route."""
    target_urls = binding.get("target_urls")
    if not isinstance(target_urls, list):
        return False
    provider_binding = binding.get("provider_binding")
    provider_binding = provider_binding if isinstance(provider_binding, Mapping) else {}
    for expected_url in target_urls:
        if not isinstance(expected_url, str):
            continue
        if _same_exact_application_path(expected_url, actual_url):
            return True
        if _smartrecruiters_application_is_bound(
            expected_url, actual_url, provider_binding
        ):
            return True
    return False


def _target_descends_from(
    target_id: str,
    root_target_ids: set[str],
    target_infos: dict[str, dict[str, object]],
) -> bool:
    """Accept the original application CDP target or an opener descendant."""
    current = target_id
    visited: set[str] = set()
    while current and current not in visited:
        if current in root_target_ids:
            return True
        visited.add(current)
        current = str(target_infos.get(current, {}).get("openerId") or "")
    return False


def _assert_identity_page_preflight(
    cdp_port: int, binding: Mapping[str, object]
) -> None:
    """Verify one exact required FIN field before any identity decryption."""
    root_target_ids = _root_target_ids()
    if not root_target_ids:
        raise CredentialRelayError(
            "Protected-identity relay could not bind this request to the worker's application tab."
        )
    configured_hosts = _allowed_hosts()
    if not configured_hosts:
        raise CredentialRelayError(
            "No authorized employer/ATS host was configured for this job."
        )
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        browser_session = browser.new_browser_cdp_session()
        target_infos = {
            str(info.get("targetId") or ""): info
            for info in browser_session.send("Target.getTargets").get("targetInfos", [])
            if info.get("targetId")
        }
        matching_target_ids: set[str] = set()
        matching_fields: list[dict[str, object]] = []
        pages = [page for context in browser.contexts for page in context.pages]
        for page in _candidate_pages(pages):
            try:
                target_info = page.context.new_cdp_session(page).send(
                    "Target.getTargetInfo"
                )["targetInfo"]
            except Exception:  # noqa: BLE001, S112 - navigation can detach here
                continue
            target_id = str(target_info.get("targetId") or "")
            target_infos[target_id] = target_info
            if (
                _target_descends_from(target_id, root_target_ids, target_infos)
                and _application_url_is_bound(page.url, binding)
            ):
                matching_target_ids.add(target_id)
            else:
                continue
            page_host = (urlparse(page.url).hostname or "").lower()
            for frame in page.frames:
                if frame is not page.main_frame and not _application_url_is_bound(
                    frame.url, binding
                ):
                    continue
                frame_host = (urlparse(frame.url).hostname or page_host).lower()
                target_host = frame_host or page_host
                if not host_is_allowed(target_host, configured_hosts):
                    continue
                for locator in _visible_locators(
                    frame,
                    (PROTECTED_IDENTIFIER_INPUT_SELECTOR,),
                ):
                    descriptor = _protected_identifier_descriptor(locator)
                    if _is_fin_field_descriptor(descriptor["text"]):
                        matching_fields.append(descriptor)
        if (
            len(matching_target_ids) != 1
            or len(matching_fields) != 1
            or matching_fields[0].get("required") is not True
        ):
            raise CredentialRelayError(
                "Protected-identity relay did not find one exact required FIN field on the "
                "attempt-bound application page."
            )


def _fill_fields(cdp_port: int, field: str, email: str, password: str) -> dict[str, object]:
    if not _relay_is_authorized():
        raise CredentialRelayError("Credential relay is not authorized by the trusted profile.")
    configured_hosts = _allowed_hosts()
    if not configured_hosts:
        raise CredentialRelayError("No authorized employer/ATS host was configured for this job.")
    root_target_ids = _root_target_ids()
    if not root_target_ids:
        raise CredentialRelayError(
            "Credential relay could not bind this request to the worker's application tab."
        )

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        browser_session = browser.new_browser_cdp_session()
        target_infos = {
            str(info.get("targetId") or ""): info
            for info in browser_session.send("Target.getTargets").get("targetInfos", [])
            if info.get("targetId")
        }
        pages = [page for context in browser.contexts for page in context.pages]
        candidates: list[dict[str, object]] = []
        for page_index, page in enumerate(_candidate_pages(pages)):
            try:
                target_info = page.context.new_cdp_session(page).send("Target.getTargetInfo")[
                    "targetInfo"
                ]
            except Exception:  # noqa: BLE001, S112 - navigation may detach between snapshots
                continue
            target_id = str(target_info.get("targetId") or "")
            target_infos[target_id] = target_info
            if not _target_descends_from(target_id, root_target_ids, target_infos):
                continue
            page_host = (urlparse(page.url).hostname or "").lower()
            for frame_index, frame in enumerate(page.frames):
                frame_host = (urlparse(frame.url).hostname or page_host).lower()
                target_host = frame_host or page_host
                if host_is_allowed(target_host, configured_hosts):
                    match = "exact"
                elif _known_ats_redirect_enabled() and host_is_known_ats(target_host):
                    match = "known_ats_redirect"
                else:
                    continue
                if field in {"password", "both"} and not (
                    _password_host_is_allowed(target_host)
                ):
                    continue

                email_locator = None
                email_confirmation_locator = None
                if field in {"email", "both"}:
                    email_locator = _visible_accessible_email_locator(frame)
                    email_confirmation_locator = _visible_email_confirmation_locator(frame)
                    if email_locator is None and email_confirmation_locator is None:
                        email_locator = _visible_locator(frame, EMAIL_SELECTORS)
                password_locators = (
                    _visible_locators(frame, PASSWORD_SELECTORS)
                    if field in {"password", "both"}
                    else []
                )
                if len(password_locators) > 2:
                    continue
                if not _requested_fields_present(
                    field,
                    email_locator is not None,
                    bool(password_locators),
                ):
                    continue
                candidates.append(
                    {
                        "page_index": page_index,
                        "target_id": target_id,
                        "frame_index": frame_index,
                        "page_url": page.url,
                        "frame_url": frame.url,
                        "host": target_host,
                        "match": match,
                        "frame": frame,
                        "field_count": (
                            int(email_locator is not None)
                            + int(email_confirmation_locator is not None)
                            + len(password_locators)
                        ),
                        "email_locator": email_locator,
                        "email_confirmation_locator": email_confirmation_locator,
                        "password_locators": password_locators,
                    }
                )

        selected = _select_candidate(candidates)
        email_locator = selected["email_locator"]
        email_confirmation_locator = selected["email_confirmation_locator"]
        password_locators = list(selected["password_locators"])
        (
            email_filled,
            _email_confirmation_filled,
            password_fields_filled,
        ) = _fill_credential_fields(
            selected["frame"],
            email_locator,
            email_confirmation_locator,
            password_locators,
            email,
            password,
        )
        return {
            "status": "filled",
            "host": selected["host"],
            "selection": selected["match"],
            "email_filled": email_filled,
            "password_filled": password_fields_filled > 0,
            "password_fields_filled": password_fields_filled,
            "submitted": False,
        }


def _protected_identifier_descriptor(locator: Locator) -> dict[str, object]:
    """Read only non-secret label and required-state metadata for one input."""
    value = locator.evaluate(
        """element => {
          const id = element.id || '';
          const escaped = globalThis.CSS?.escape ? CSS.escape(id) : id.replace(/"/g, '\\"');
          const explicit = id ? document.querySelector(`label[for="${escaped}"]`) : null;
          const wrapping = element.closest('label');
          const container = element.closest(
            '[role="group"], [role="radiogroup"], .form-group, .field, [class*="form-item"], [data-automation-id]'
          );
          const text = [
            explicit?.innerText,
            wrapping?.innerText,
            element.getAttribute('aria-label'),
            element.getAttribute('name'),
            element.getAttribute('id'),
            element.getAttribute('placeholder'),
            container?.querySelector('label')?.innerText,
          ].filter(Boolean).join(' ');
          const required = Boolean(
            element.required ||
            element.getAttribute('aria-required') === 'true' ||
            /(?:^|\\s)required(?:\\s|$)/i.test(text) ||
            /\\*/.test(text)
          );
          return { text, required };
        }"""
    )
    if not isinstance(value, dict):
        return {"text": "", "required": False}
    return {
        "text": str(value.get("text") or ""),
        "required": value.get("required") is True,
    }


def _apply_protected_display_mask(locator: Locator, kind: str, value: str) -> None:
    """Apply and verify the mask after one microtask; clear on any instability."""
    try:
        state = locator.evaluate(
            """async (element, kind) => {
              element.setAttribute('data-applypilot-protected', kind);
              element.setAttribute('autocomplete', 'off');
              if (element.tagName === 'INPUT') element.type = 'password';
              await Promise.resolve();
              return {
                type: element.getAttribute('type'),
                marker: element.getAttribute('data-applypilot-protected'),
              };
            }""",
            kind,
        )
        stable = bool(
            isinstance(state, Mapping)
            and str(state.get("type") or "").casefold() == "password"
            and state.get("marker") == kind
            and locator.input_value() == value
        )
    except Exception:  # noqa: BLE001 - detached/reactive fields must fail closed
        stable = False
    if stable:
        return
    try:
        locator.fill("")
    except Exception:  # noqa: BLE001, S110 - best-effort secret removal
        pass
    raise CredentialRelayError(
        "Protected-identity field did not retain its verified display mask."
    )


def _fill_protected_identifier(cdp_port: int, kind: str, value: str) -> dict[str, object]:
    """Fill one required protected identifier in the bound ATS tab without submitting."""
    if kind != "fin":
        raise CredentialRelayError("Unsupported protected identifier kind.")
    if not _identity_relay_is_authorized():
        raise CredentialRelayError(
            "Protected-identity relay is not authorized by the trusted profile."
        )
    configured_hosts = _allowed_hosts()
    if not configured_hosts:
        raise CredentialRelayError("No authorized employer/ATS host was configured for this job.")
    root_target_ids = _root_target_ids()
    if not root_target_ids:
        raise CredentialRelayError(
            "Protected-identity relay could not bind this request to the worker's application tab."
        )
    application_binding = _application_context_binding()

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        browser_session = browser.new_browser_cdp_session()
        target_infos = {
            str(info.get("targetId") or ""): info
            for info in browser_session.send("Target.getTargets").get("targetInfos", [])
            if info.get("targetId")
        }
        pages = [page for context in browser.contexts for page in context.pages]
        candidates: list[dict[str, object]] = []
        for page_index, page in enumerate(_candidate_pages(pages)):
            try:
                target_info = page.context.new_cdp_session(page).send("Target.getTargetInfo")[
                    "targetInfo"
                ]
            except Exception:  # noqa: BLE001, S112 - navigation may detach between snapshots
                continue
            target_id = str(target_info.get("targetId") or "")
            target_infos[target_id] = target_info
            if not _target_descends_from(target_id, root_target_ids, target_infos):
                continue
            if not _application_url_is_bound(page.url, application_binding):
                continue
            page_host = (urlparse(page.url).hostname or "").lower()
            for frame_index, frame in enumerate(page.frames):
                frame_host = (urlparse(frame.url).hostname or page_host).lower()
                target_host = frame_host or page_host
                if frame is not page.main_frame and not _application_url_is_bound(
                    frame.url, application_binding
                ):
                    continue
                if host_is_allowed(target_host, configured_hosts):
                    match = "exact"
                else:
                    continue
                matching: list[tuple[Locator, dict[str, object]]] = []
                for locator in _visible_locators(
                    frame,
                    (PROTECTED_IDENTIFIER_INPUT_SELECTOR,),
                ):
                    descriptor = _protected_identifier_descriptor(locator)
                    if _is_fin_field_descriptor(descriptor["text"]):
                        matching.append((locator, descriptor))
                if len(matching) != 1:
                    continue
                locator, descriptor = matching[0]
                if descriptor["required"] is not True:
                    continue
                candidates.append(
                    {
                        "page_index": page_index,
                        "target_id": target_id,
                        "frame_index": frame_index,
                        "page_url": page.url,
                        "frame_url": frame.url,
                        "host": target_host,
                        "match": match,
                        "field_count": 1,
                        "locator": locator,
                    }
                )

        selected = _select_candidate(candidates)
        locator = selected["locator"]
        locator.fill(value)
        if locator.input_value() != value:
            raise CredentialRelayError(
                "Protected-identity field did not retain the exact secure value."
            )
        _apply_protected_display_mask(locator, kind, value)
        return {
            "status": "filled",
            "kind": kind,
            "host": selected["host"],
            "selection": selected["match"],
            "required_field_verified": True,
            "value_persistence_verified": True,
            "display_masked": True,
            "submitted": False,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Securely fill employer ATS credentials.")
    parser.add_argument("--field", choices=("email", "password", "both"), default="both")
    parser.add_argument(
        "--cdp-port",
        type=int,
        default=int(os.environ.get("APPLYPILOT_CDP_PORT", "9222")),
    )
    args = parser.parse_args(argv)

    path = _credential_path().resolve()
    try:
        record = _read_record(path)
        password = _decrypt_password(path)
        result = _fill_fields(args.cdp_port, args.field, record["email"], password)
    except CredentialRelayError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False))
        return 2
    finally:
        password = ""  # best-effort release; plaintext is never printed or persisted

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
